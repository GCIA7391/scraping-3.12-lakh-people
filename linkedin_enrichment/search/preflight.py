"""Preflight: prove the search layer works before spending a long run on it.

This exists because of a specific, expensive failure mode. If searches silently
return nothing, the pipeline does exactly what it is designed to do — write a
blank and a reason — and a 312,160-row run completes "successfully" with zero
matches. The output looks like a finding ("these people aren't on LinkedIn") when
it is actually an infrastructure fault.

So the run is gated. Preflight answers, with evidence:

* which provider and backends are configured, and the exact URL each will request
* for each backend: DNS -> TCP -> TLS -> HTTP -> parse, and where it stopped
* a canary query whose correct answer is known, to prove results are real
* a verdict of SUCCESS / FAILED / INCONCLUSIVE per backend, and for failures a
  classification an operator can act on

Exit codes: 0 = at least one backend SUCCESS, 1 = all FAILED, 2 = INCONCLUSIVE.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..providers import FailureKind, Outcome, build_provider
from ..providers.base import ProviderError, SearchResponse
from ..providers.public_search import PublicSearchProvider

logger = logging.getLogger(__name__)

#: A query with a known-good answer, taken from live validation during the build.
#: If a backend answers HTTP 200 but cannot find this, it is not returning real
#: web results and the run must not proceed.
CANARY_QUERY = '"Girish Rowjee" "Greytip Software" LinkedIn'
CANARY_EXPECT_SUBSTRING = "linkedin.com/in/"

#: Plain-language remedies, keyed by classification.
REMEDIES: dict[FailureKind, str] = {
    FailureKind.DNS:
        "The hostname does not resolve. Check DNS and any split-horizon resolver.",
    FailureKind.CONNECTION_REFUSED:
        "Nothing is listening at that address. If this is SearXNG, start it: "
        "`docker run -d -p 8080:8080 searxng/searxng`, then set SEARXNG_URL.",
    FailureKind.PROXY_POLICY:
        "A proxy or egress firewall refused this host — the search engine was "
        "never reached. This is a network allowlist, NOT anti-bot protection and "
        "NOT a code problem: no provider change, user-agent, or rate limit will "
        "route around it. Either add the host to your network egress allowlist, "
        "or run the pipeline from a network that permits search engines.",
    FailureKind.TLS:
        "TLS handshake failed. Check the CA bundle and any intercepting proxy.",
    FailureKind.TIMEOUT:
        "The backend accepted the connection but did not answer in time. Raise "
        "provider.timeout_seconds, or the host is overloaded.",
    FailureKind.HTTP_ERROR:
        "The backend answered with an error status. See the body excerpt above.",
    FailureKind.RATE_LIMITED:
        "Throttled. Lower rate_limit.requests_per_second; the limiter also backs "
        "off on its own. Free endpoints tolerate roughly one request every 2s.",
    FailureKind.ANTI_BOT:
        "A challenge or consent page was served instead of results. This endpoint "
        "is refusing automation from this IP; try another backend or a paid API.",
    FailureKind.EMPTY_RESULTS:
        "HTTP 200 but nothing parsed. Either the markup changed (parser needs "
        "updating) or the endpoint returned an empty page. NOT proof that no "
        "profiles exist — do not run in bulk on this.",
    FailureKind.PARSE_ERROR:
        "The response could not be parsed. Likely a markup change in the endpoint.",
    FailureKind.CONFIG:
        "Configuration problem — see the message above.",
}


@dataclass
class StageResult:
    name: str
    ok: bool
    detail: str = ""
    elapsed_ms: float = 0.0


@dataclass
class BackendReport:
    """Everything learned about one backend."""

    backend: str
    url: str = ""
    method: str = "GET"
    outcome: Outcome = Outcome.FAILED
    failure_kind: FailureKind = FailureKind.NONE
    stages: list[StageResult] = field(default_factory=list)
    http_status: int | None = None
    response_bytes: int = 0
    elapsed_ms: float = 0.0
    result_count: int = 0
    canary_ok: bool = False
    sample_urls: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def verdict(self) -> str:
        return self.outcome.value.upper()


@dataclass
class PreflightReport:
    provider: str
    backends: list[BackendReport] = field(default_factory=list)
    config_snapshot: dict = field(default_factory=dict)

    @property
    def any_success(self) -> bool:
        return any(b.outcome is Outcome.SUCCESS for b in self.backends)

    @property
    def any_inconclusive(self) -> bool:
        return any(b.outcome is Outcome.INCONCLUSIVE for b in self.backends)

    @property
    def exit_code(self) -> int:
        if self.any_success:
            return 0
        if self.any_inconclusive:
            return 2
        return 1

    @property
    def summary(self) -> str:
        if self.any_success:
            return "SUCCESS — searches work; safe to run"
        if self.any_inconclusive:
            return "INCONCLUSIVE — reachable but returned no parseable results; DO NOT run in bulk"
        return "FAILED — no backend could search; DO NOT run"


# ---------------------------------------------------------------------------
# Transport-level staged checks
# ---------------------------------------------------------------------------

def _check_dns(host: str) -> StageResult:
    started = time.monotonic()
    try:
        info = socket.getaddrinfo(host, None)
        addresses = sorted({i[4][0] for i in info})[:3]
        return StageResult("dns", True, ", ".join(addresses),
                           (time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return StageResult("dns", False, f"{type(exc).__name__}: {exc}",
                           (time.monotonic() - started) * 1000)


def _check_tcp(host: str, port: int, timeout: float = 8.0) -> StageResult:
    """Direct TCP connect, deliberately bypassing any proxy.

    Compared against the proxied HTTP attempt below, this separates "the host is
    unreachable" from "the proxy refuses the host".
    """
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return StageResult("tcp", True, f"connected to {host}:{port}",
                               (time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return StageResult("tcp", False, f"{type(exc).__name__}: {exc}",
                           (time.monotonic() - started) * 1000)


def _check_tls(host: str, port: int, timeout: float = 8.0) -> StageResult:
    started = time.monotonic()
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                version = tls.version() or "?"
                return StageResult("tls", True, f"{version}",
                                   (time.monotonic() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        return StageResult("tls", False, f"{type(exc).__name__}: {exc}",
                           (time.monotonic() - started) * 1000)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_preflight(settings, *, deep: bool = True) -> PreflightReport:
    """Probe every backend of the configured provider."""
    report = PreflightReport(
        provider=settings.provider.name,
        config_snapshot=settings.to_dict().get("provider", {}),
    )

    provider = build_provider(settings.provider.name, settings.provider)
    try:
        provider.validate()
    except ProviderError as exc:
        report.backends.append(BackendReport(
            backend=settings.provider.name, outcome=Outcome.FAILED,
            failure_kind=exc.kind or FailureKind.CONFIG, error=str(exc),
        ))
        return report

    try:
        if isinstance(provider, PublicSearchProvider):
            for backend in provider.backends:
                report.backends.append(
                    await _probe_public_backend(provider, backend, settings, deep=deep)
                )
        else:
            report.backends.append(
                await _probe_generic_provider(provider, settings, deep=deep)
            )
    finally:
        await provider.close()

    return report


async def _probe_public_backend(provider, backend, settings, *, deep: bool) -> BackendReport:
    url = backend.url
    if backend.name == "searxng":
        url = (settings.provider.searxng_url or "").rstrip("/") + "/search"

    entry = BackendReport(backend=backend.name, url=url, method=backend.method)
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    if deep and host:
        entry.stages.append(_check_dns(host))
        entry.stages.append(_check_tcp(host, port))
        if parsed.scheme == "https":
            entry.stages.append(_check_tls(host, port))

    response = await provider.probe(backend, CANARY_QUERY, settings.provider.results_per_query)
    _absorb(entry, response)
    return entry


async def _probe_generic_provider(provider, settings, *, deep: bool) -> BackendReport:
    """Probe a single-endpoint provider (searxng, serper, google_cse, cassette...)."""
    entry = BackendReport(backend=provider.name, url=_provider_url(provider, settings))

    parsed = urlparse(entry.url) if entry.url else None
    if deep and parsed and parsed.hostname:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        entry.stages.append(_check_dns(parsed.hostname))
        entry.stages.append(_check_tcp(parsed.hostname, port))
        if parsed.scheme == "https":
            entry.stages.append(_check_tls(parsed.hostname, port))

    try:
        response = await provider.search(
            CANARY_QUERY, limit=settings.provider.results_per_query
        )
    except ProviderError as exc:
        entry.outcome = Outcome.FAILED
        entry.failure_kind = exc.kind or FailureKind.HTTP_ERROR
        entry.error = str(exc)
        return entry
    except Exception as exc:  # noqa: BLE001
        from ..providers.base import classify_exception
        kind, detail = classify_exception(exc)
        entry.outcome = Outcome.FAILED
        entry.failure_kind = kind
        entry.error = detail
        return entry

    _absorb(entry, response)
    return entry


def _absorb(entry: BackendReport, response: SearchResponse) -> None:
    """Fold a probe response into the backend report."""
    entry.http_status = response.http_status
    entry.response_bytes = response.response_bytes
    entry.elapsed_ms = response.elapsed_ms
    entry.result_count = len(response.results)
    entry.error = response.error
    entry.outcome = response.outcome
    entry.failure_kind = response.failure_kind
    entry.sample_urls = [r.url for r in response.results[:5]]
    entry.canary_ok = any(
        CANARY_EXPECT_SUBSTRING in (r.url or "") for r in response.results
    )

    # Reachable and parsing, but the canary is missing: results are not real web
    # results (or the index is unusable for our purpose). Not a pass.
    if entry.outcome is Outcome.SUCCESS and not entry.canary_ok:
        entry.outcome = Outcome.INCONCLUSIVE
        entry.failure_kind = FailureKind.EMPTY_RESULTS
        entry.error = (
            f"returned {entry.result_count} result(s) but none was a "
            f"{CANARY_EXPECT_SUBSTRING} URL for a query with a known answer"
        )


def _provider_url(provider, settings) -> str:
    name = provider.name
    if name == "searxng":
        return (settings.provider.searxng_url or "").rstrip("/") + "/search"
    return {
        "serper": "https://google.serper.dev/search",
        "serpapi": "https://serpapi.com/search",
        "google_cse": "https://www.googleapis.com/customsearch/v1",
        "duckduckgo": "https://duckduckgo.com/ (via the ddgs package)",
        "cassette": "(local fixture replay — no network)",
    }.get(name, "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(report: PreflightReport) -> str:
    lines: list[str] = []
    add = lines.append
    rule = "=" * 78

    add(rule)
    add("  PREFLIGHT — can this machine actually search?")
    add(rule)
    add(f"  Provider configured : {report.provider}")
    for key in ("public_backends", "searxng_url", "timeout_seconds", "results_per_query", "user_agent"):
        if key in report.config_snapshot:
            value = report.config_snapshot[key]
            if value not in ("", None, (), []):
                add(f"  {key:20s}: {value}")
    add(f"  Canary query        : {CANARY_QUERY}")
    add("")

    for entry in report.backends:
        marker = {"SUCCESS": "PASS", "INCONCLUSIVE": "WARN", "FAILED": "FAIL"}[entry.verdict]
        add("-" * 78)
        add(f"  [{marker}] {entry.backend}   ({entry.verdict})")
        add("-" * 78)
        if entry.url:
            add(f"    request        : {entry.method} {entry.url}")

        for stage in entry.stages:
            status = "ok  " if stage.ok else "FAIL"
            add(f"    {stage.name:<14}: {status} {stage.detail}  [{stage.elapsed_ms:.0f}ms]")

        if entry.http_status is not None:
            add(f"    http status    : {entry.http_status}")
        if entry.response_bytes:
            add(f"    response bytes : {entry.response_bytes:,}")
        if entry.elapsed_ms:
            add(f"    request time   : {entry.elapsed_ms:.0f}ms")
        add(f"    results parsed : {entry.result_count}")
        add(f"    canary found   : {'yes' if entry.canary_ok else 'no'}")

        for url in entry.sample_urls:
            add(f"      - {url}")

        if entry.error:
            add(f"    error          : {entry.error}")
        if entry.failure_kind and entry.failure_kind is not FailureKind.NONE:
            add(f"    classification : {entry.failure_kind.value}")
            remedy = REMEDIES.get(entry.failure_kind)
            if remedy:
                add(f"    what to do     : {remedy}")
        add("")

    add(rule)
    add(f"  VERDICT: {report.summary}")
    add(f"  exit code {report.exit_code}   (0=success, 1=all failed, 2=inconclusive)")
    add(rule)
    return "\n".join(lines)


def preflight_blocking(settings, *, deep: bool = True) -> PreflightReport:
    """Synchronous wrapper for CLI use."""
    return asyncio.run(run_preflight(settings, deep=deep))
