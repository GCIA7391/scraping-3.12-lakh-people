"""``public_search`` — the default provider.

Uses only publicly accessible search endpoints: no API key, no account, no paid
tier. It is a *composite* — it tries several independent backends in order and
returns the first that yields usable results, so one backend throttling or
changing its markup does not stop the run.

Backends are deliberately small and self-contained. Each supplies a request and
a parser; everything else (rate limiting, retry, caching, scoring) is shared with
every other provider through ``SearchProvider``. Adding or removing a backend
touches only ``BACKENDS`` and one class. Swapping the whole thing for SerpApi or
Google CSE remains a one-line config change, because they all implement the same
interface.

**Why the parsers are hand-rolled.** These endpoints return HTML, not JSON, and
their markup drifts. Rather than depend on a heavyweight scraper, each parser
uses the standard library's ``HTMLParser`` and looks for the smallest stable
signal — the anchor that carries the result URL. When the markup does change, the
parser returns nothing and the provider reports **INCONCLUSIVE**, not "no
results", so the failure is visible instead of silently producing blanks.

**Politeness.** These are public services being used without a contract. The
adaptive rate limiter defaults to one request every two seconds, backs off on any
throttle signal, and identifies itself honestly in the User-Agent. Do not raise
the rate to the point where you are a burden; if you need volume, use a paid
adapter that sells it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from html import unescape
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urlparse

from .base import (
    FailureKind,
    ProviderError,
    SearchProvider,
    SearchResponse,
    SerpResult,
    classify_exception,
    dedupe_results,
)

logger = logging.getLogger(__name__)

# Presented honestly: a real contact-style identifier rather than a browser
# impersonation string. Public endpoints are entitled to know who is calling.
DEFAULT_UA = (
    "Mozilla/5.0 (compatible; gcia-linkedin-enrichment/1.0; "
    "+https://github.com/GCIA7391/scraping-3.12-lakh-people)"
)

# Markers that mean "we reached the service but it refused to answer properly".
_ANTI_BOT_MARKERS = (
    "unusual traffic", "are you a robot", "captcha", "recaptcha",
    "verify you are human", "too many requests",
    "detected unusual", "please enable javascript and cookies",
)

# Markers that mean an egress firewall answered, not the search engine. These are
# checked FIRST, because both produce HTTP 403 and the remedies are completely
# different: one needs a different backend, the other needs a network allowlist
# change. Misreporting a policy block as anti-bot sends the operator to spend a
# day rotating user-agents against a firewall that will never let them through.
_POLICY_DENIAL_MARKERS = (
    "host not in allowlist",
    "host not permitted",
    "not in allowlist",
    "egress",
    "blocked by policy",
    "request rejected",
    "forbidden by proxy",
)

#: Response headers some gateways set to explain a denial.
_POLICY_DENIAL_HEADERS = ("x-deny-reason", "x-block-reason", "x-proxy-deny")


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

class _AnchorCollector(HTMLParser):
    """Collect anchors, their text, and the text that follows them.

    Deliberately structure-agnostic: it gathers every ``<a href>`` with its
    visible text rather than relying on a specific class name or nesting, so
    cosmetic markup changes do not break extraction.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._depth = 0
        self._tail_target: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = {k: (v or "") for k, v in attrs}
        href = attributes.get("href", "")
        if not href:
            return
        self._current = {
            "href": href,
            "class": attributes.get("class", ""),
            "text": [],
            "tail": [],
        }
        self._depth = 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._current is not None:
            self._current["text"] = "".join(self._current["text"]).strip()
            self.anchors.append(self._current)
            self._tail_target = self._current
            self._current = None
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["text"].append(data)
        elif self._tail_target is not None:
            # Snippet text usually follows the title anchor as a sibling.
            self._tail_target["tail"].append(data)


def _extract_anchors(html: str) -> list[dict[str, Any]]:
    parser = _AnchorCollector()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed HTML must not kill a run
        logger.debug("HTML parse aborted early; using anchors found so far")
    for anchor in parser.anchors:
        if isinstance(anchor.get("tail"), list):
            anchor["tail"] = "".join(anchor["tail"]).strip()
    return parser.anchors


def _unwrap_redirect(href: str) -> str:
    """Resolve the interstitial redirect URLs these engines wrap results in.

    DuckDuckGo returns ``//duckduckgo.com/l/?uddg=<urlencoded target>``; Mojeek
    and others use similar ``url=``/``q=`` parameters. Without unwrapping, every
    result URL would look like the search engine's own domain and every candidate
    would be discarded as "not a LinkedIn URL".
    """
    if not href:
        return ""
    candidate = href
    if candidate.startswith("//"):
        candidate = "https:" + candidate

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""

    if parsed.query:
        params = parse_qs(parsed.query)
        for key in ("uddg", "url", "u", "q", "r"):
            values = params.get(key)
            if values and values[0].startswith(("http://", "https://", "//")):
                target = values[0]
                return "https:" + target if target.startswith("//") else target

    if candidate.startswith(("http://", "https://")):
        return candidate
    return ""


def _looks_like_anti_bot(body: str) -> bool:
    lowered = body[:4000].lower()
    return any(marker in lowered for marker in _ANTI_BOT_MARKERS)


def looks_like_policy_denial(body: str, headers: Any = None) -> bool:
    """True when an egress firewall answered instead of the search engine.

    Detected by an explicit deny header where one is present, otherwise by the
    body text. A policy denial is typically a tiny plaintext response, which is
    itself a strong hint — a real search engine's own 403 is a full HTML page.
    """
    if headers:
        try:
            for header in _POLICY_DENIAL_HEADERS:
                if header in headers:
                    return True
        except TypeError:
            pass
    lowered = (body or "")[:2000].lower()
    return any(marker in lowered for marker in _POLICY_DENIAL_MARKERS)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class Backend:
    """One public search endpoint."""

    name: str = ""
    method: str = "GET"
    url: str = ""

    def build(self, query: str, limit: int) -> dict[str, Any]:
        """Return kwargs for the HTTP call (params/data/headers)."""
        raise NotImplementedError

    def parse(self, body: str, limit: int) -> list[SerpResult]:
        """Extract results from the response body."""
        raise NotImplementedError

    # Shared extraction: keep anchors that resolve to a real off-engine URL.
    def _results_from_anchors(
        self, body: str, limit: int, *, skip_hosts: Iterable[str] = ()
    ) -> list[SerpResult]:
        skip = tuple(skip_hosts)
        results: list[SerpResult] = []
        seen: set[str] = set()

        for anchor in _extract_anchors(body):
            url = _unwrap_redirect(anchor["href"])
            if not url or url in seen:
                continue
            host = (urlparse(url).netloc or "").lower()
            if not host or any(host.endswith(s) for s in skip):
                continue
            title = unescape(anchor.get("text") or "").strip()
            if not title:
                continue
            seen.add(url)
            results.append(
                SerpResult(
                    title=title,
                    url=url,
                    snippet=unescape(anchor.get("tail") or "").strip()[:400],
                    rank=len(results),
                )
            )
            if len(results) >= limit:
                break
        return results


class DdgHtmlBackend(Backend):
    """DuckDuckGo's no-JavaScript HTML endpoint. The most dependable key-free source."""

    name = "ddg_html"
    method = "POST"
    url = "https://html.duckduckgo.com/html/"

    def build(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "data": {"q": query, "kl": "in-en"},
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        }

    def parse(self, body: str, limit: int) -> list[SerpResult]:
        return self._results_from_anchors(
            body, limit, skip_hosts=("duckduckgo.com",)
        )


class DdgLiteBackend(Backend):
    """DuckDuckGo's 'lite' endpoint — different markup and rate-limit pool."""

    name = "ddg_lite"
    method = "POST"
    url = "https://lite.duckduckgo.com/lite/"

    def build(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "data": {"q": query, "kl": "in-en"},
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        }

    def parse(self, body: str, limit: int) -> list[SerpResult]:
        return self._results_from_anchors(body, limit, skip_hosts=("duckduckgo.com",))


class MojeekBackend(Backend):
    """Mojeek — an independent crawler, notably tolerant of automated access."""

    name = "mojeek"
    method = "GET"
    url = "https://www.mojeek.com/search"

    def build(self, query: str, limit: int) -> dict[str, Any]:
        return {"params": {"q": query, "t": str(min(limit, 20))}}

    def parse(self, body: str, limit: int) -> list[SerpResult]:
        return self._results_from_anchors(body, limit, skip_hosts=("mojeek.com",))


class SearxngJsonBackend(Backend):
    """A SearXNG instance with the JSON API enabled — self-hosted or public."""

    name = "searxng"
    method = "GET"
    url = ""  # filled from config at request time

    def build(self, query: str, limit: int) -> dict[str, Any]:
        return {
            "params": {"q": query, "format": "json", "language": "en", "safesearch": "0"}
        }

    def parse(self, body: str, limit: int) -> list[SerpResult]:
        import json

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            # Almost always the instance serving HTML because `formats: [json]`
            # is missing from settings.yml — a specific, fixable misconfiguration.
            raise ProviderError(
                "SearXNG returned non-JSON. Add `json` to `search.formats` in "
                "its settings.yml and restart the instance.",
                retryable=False, kind=FailureKind.CONFIG,
            ) from exc

        return [
            SerpResult(
                title=item.get("title", "") or "",
                url=item.get("url", "") or "",
                snippet=item.get("content", "") or "",
                rank=index,
            )
            for index, item in enumerate(payload.get("results", [])[:limit])
        ]


#: Order matters — first backend to return results wins.
BACKENDS: dict[str, type[Backend]] = {
    "ddg_html": DdgHtmlBackend,
    "ddg_lite": DdgLiteBackend,
    "mojeek": MojeekBackend,
    "searxng": SearxngJsonBackend,
}

DEFAULT_BACKEND_ORDER = ("ddg_html", "ddg_lite", "mojeek", "searxng")


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------

class PublicSearchProvider(SearchProvider):
    """Composite over key-free public search endpoints."""

    name = "public_search"
    cost_per_1k = 0.0

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self._session: Any = None
        self._session_lock = asyncio.Lock()

        order = getattr(config, "public_backends", None) or DEFAULT_BACKEND_ORDER
        self.backends: list[Backend] = []
        for backend_name in order:
            backend_cls = BACKENDS.get(backend_name)
            if backend_cls is None:
                logger.warning("unknown public_search backend %r; ignoring", backend_name)
                continue
            if backend_name == "searxng" and not (getattr(config, "searxng_url", "") or "").strip():
                # Only include SearXNG when an instance is actually configured.
                continue
            self.backends.append(backend_cls())

        #: Backends that failed with a non-retryable policy/config error are
        #: skipped for the rest of the run rather than retried per row.
        self._disabled: dict[str, str] = {}

    # -- HTTP plumbing --------------------------------------------------------
    async def _get_session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            async with self._session_lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
                    self._session = aiohttp.ClientSession(
                        timeout=timeout,
                        headers={
                            "User-Agent": getattr(self.config, "user_agent", "") or DEFAULT_UA,
                            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9",
                            "Accept-Language": "en-IN,en;q=0.9",
                        },
                    )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def validate(self) -> None:
        if not self.backends:
            raise ProviderError(
                "public_search has no usable backends configured",
                retryable=False, kind=FailureKind.CONFIG,
            )

    # -- one backend ----------------------------------------------------------
    async def probe(self, backend: Backend, query: str, limit: int = 10) -> SearchResponse:
        """Run one query against one backend and report exactly what happened.

        Never raises for transport problems — the outcome is encoded in the
        response so ``preflight`` can classify it.
        """
        import aiohttp

        url = backend.url
        if backend.name == "searxng":
            url = (getattr(self.config, "searxng_url", "") or "").rstrip("/") + "/search"

        response = SearchResponse(query=query, provider=self.name, backend=backend.name)
        started = time.monotonic()

        try:
            session = await self._get_session()
            kwargs = backend.build(query, limit)
            async with session.request(backend.method, url, **kwargs) as http:
                body = await http.text()
                response.http_status = http.status
                response.response_bytes = len(body)
                response.elapsed_ms = (time.monotonic() - started) * 1000

                # Checked before anything else: an egress firewall and a search
                # engine both answer 403, but they need opposite responses.
                if looks_like_policy_denial(body, http.headers):
                    response.error = (
                        f"{backend.name}: blocked by network egress policy "
                        f"(HTTP {http.status}) — {body.strip()[:160]}"
                    )
                    response.failure_kind = FailureKind.PROXY_POLICY
                    return response

                if http.status in (429, 202):
                    response.error = f"{backend.name}: rate limited (HTTP {http.status})"
                    response.failure_kind = FailureKind.RATE_LIMITED
                    return response

                if http.status >= 400:
                    response.error = f"{backend.name}: HTTP {http.status} — {body.strip()[:160]}"
                    response.failure_kind = (
                        FailureKind.ANTI_BOT if http.status in (401, 403)
                        else FailureKind.HTTP_ERROR
                    )
                    return response

                if _looks_like_anti_bot(body):
                    response.error = (
                        f"{backend.name}: served a challenge/consent page instead of results"
                    )
                    response.failure_kind = FailureKind.ANTI_BOT
                    return response

                try:
                    response.results = dedupe_results(backend.parse(body, limit))
                except ProviderError as exc:
                    response.error = str(exc)
                    response.failure_kind = exc.kind
                    return response
                except Exception as exc:  # noqa: BLE001
                    response.error = f"{backend.name}: could not parse response: {exc}"
                    response.failure_kind = FailureKind.PARSE_ERROR
                    return response

                if not response.results:
                    # HTTP 200 but nothing extracted. Reported as INCONCLUSIVE by
                    # SearchResponse.outcome — never as "no matches".
                    response.failure_kind = FailureKind.EMPTY_RESULTS
                return response

        except asyncio.TimeoutError as exc:
            response.elapsed_ms = (time.monotonic() - started) * 1000
            response.error = f"{backend.name}: timed out after {self.config.timeout_seconds}s"
            response.failure_kind = FailureKind.TIMEOUT
            return response
        except aiohttp.ClientError as exc:
            response.elapsed_ms = (time.monotonic() - started) * 1000
            kind, detail = classify_exception(exc.__cause__ or exc)
            response.error = f"{backend.name}: {detail}"
            response.failure_kind = kind
            return response
        except Exception as exc:  # noqa: BLE001
            response.elapsed_ms = (time.monotonic() - started) * 1000
            kind, detail = classify_exception(exc)
            response.error = f"{backend.name}: {detail}"
            response.failure_kind = kind
            return response

    # -- the composite --------------------------------------------------------
    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        """Try each enabled backend until one returns results."""
        attempts: list[SearchResponse] = []

        for backend in self.backends:
            if backend.name in self._disabled:
                continue

            response = await self.probe(backend, query, limit)
            attempts.append(response)

            if response.results:
                return response

            # A policy or config failure will not fix itself; stop asking this
            # backend on every subsequent row.
            if response.failure_kind in (
                FailureKind.PROXY_POLICY, FailureKind.CONFIG, FailureKind.DNS
            ):
                self._disabled[backend.name] = response.error
                logger.warning(
                    "disabling backend %s for this run: %s", backend.name, response.error
                )

        if not attempts:
            raise ProviderError(
                "every public_search backend is disabled: "
                + "; ".join(f"{k}: {v}" for k, v in self._disabled.items()),
                retryable=False, kind=FailureKind.PROXY_POLICY,
            )

        # Prefer reporting a real failure over a bare empty result, so the caller
        # can tell "nothing found" apart from "nothing worked".
        failed = [a for a in attempts if a.error]
        if failed:
            worst = failed[-1]
            combined = SearchResponse(
                query=query, provider=self.name, backend=worst.backend,
                http_status=worst.http_status, elapsed_ms=worst.elapsed_ms,
                failure_kind=worst.failure_kind,
                error="; ".join(a.error for a in failed),
            )
            return combined

        # All backends answered cleanly and genuinely found nothing.
        last = attempts[-1]
        last.failure_kind = FailureKind.EMPTY_RESULTS
        return last
