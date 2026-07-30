"""Search provider interface.

Every adapter returns the same shape — title, url, snippet, rank — because that
is all the identity scorer needs and all that every SERP API reliably supplies.

Design constraint, deliberate: **no adapter ever fetches linkedin.com directly.**
Matching is done entirely from search-result metadata. Fetching profile pages
would violate LinkedIn's terms on automated access, and is unnecessary — a SERP
title such as "Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn" already
carries the person, the company and (in the snippet) the location.
"""

from __future__ import annotations

import abc
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Sequence


@dataclass(frozen=True)
class SerpResult:
    """One organic search result."""

    title: str
    url: str
    snippet: str = ""
    rank: int = 0

    @property
    def text(self) -> str:
        """Title and snippet combined — the haystack for token matching."""
        return f"{self.title} {self.snippet}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet, "rank": self.rank}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SerpResult":
        return cls(
            title=data.get("title", "") or "",
            url=data.get("url", data.get("link", "")) or "",
            snippet=data.get("snippet", data.get("description", "")) or "",
            rank=int(data.get("rank", 0) or 0),
        )


class Outcome(str, Enum):
    """Three-state search outcome.

    ``INCONCLUSIVE`` is the important one. A backend that answers HTTP 200 with
    zero parseable results is *not* the same as "this person has no profile" —
    it usually means the instance was throttled, served a consent/CAPTCHA page,
    or the page markup changed and the parser no longer matches. Collapsing that
    into "no results" is how a run silently produces 312,160 blanks and looks
    like it worked.
    """

    SUCCESS = "success"
    FAILED = "failed"
    INCONCLUSIVE = "inconclusive"


class FailureKind(str, Enum):
    """Why a search failed, in the operator's vocabulary."""

    NONE = ""
    DNS = "dns"
    CONNECTION_REFUSED = "connection_refused"
    PROXY_POLICY = "proxy_policy"
    TLS = "tls"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    ANTI_BOT = "anti_bot"
    EMPTY_RESULTS = "empty_results"
    PARSE_ERROR = "parse_error"
    CONFIG = "config"


@dataclass
class SearchResponse:
    """A provider's answer to one query, with enough detail to diagnose it."""

    query: str
    results: list[SerpResult] = field(default_factory=list)
    provider: str = ""
    from_cache: bool = False
    error: str = ""

    # Diagnostics — populated by live backends, surfaced by preflight and --explain.
    backend: str = ""
    http_status: int | None = None
    elapsed_ms: float = 0.0
    failure_kind: FailureKind = FailureKind.NONE
    response_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def outcome(self) -> Outcome:
        if self.error:
            return Outcome.FAILED
        if not self.results:
            # Reached the backend and got a clean response, but nothing parsed.
            return Outcome.INCONCLUSIVE
        return Outcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "error": self.error,
            "backend": self.backend,
            "http_status": self.http_status,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchResponse":
        return cls(
            query=data.get("query", ""),
            provider=data.get("provider", ""),
            error=data.get("error", ""),
            backend=data.get("backend", "") or "",
            http_status=data.get("http_status"),
            results=[SerpResult.from_dict(r) for r in data.get("results", [])],
        )


class ProviderError(RuntimeError):
    """Provider failure. ``retryable`` drives the backoff policy, ``kind`` the
    operator-facing diagnosis printed by ``main.py preflight``."""

    def __init__(
        self, message: str, *, retryable: bool = False,
        status: int | None = None, kind: FailureKind = FailureKind.NONE,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.kind = kind


def classify_exception(exc: BaseException) -> tuple[FailureKind, str]:
    """Map a transport exception to a diagnosis an operator can act on.

    The distinctions matter: "connection refused" means *your backend is not
    running*, while a 403 on CONNECT means *the network policy forbids the host*.
    Those have completely different fixes, and reporting both as "search failed"
    is what makes this class of problem take a day to find.
    """
    import socket

    text = str(exc)
    lowered = text.lower()

    if isinstance(exc, socket.gaierror) or "name or service not known" in lowered \
            or "nodename nor servname" in lowered or "temporary failure in name resolution" in lowered:
        return FailureKind.DNS, f"DNS lookup failed: {text}"

    if isinstance(exc, ConnectionRefusedError) or "connection refused" in lowered \
            or "connect call failed" in lowered:
        return FailureKind.CONNECTION_REFUSED, (
            f"nothing is accepting connections at that address: {text}"
        )

    # The agent/egress proxy answers 403 to CONNECT for hosts outside its allowlist.
    if "tunnel" in lowered or "host not permitted" in lowered or "407" in lowered \
            or ("403" in lowered and "connect" in lowered) or "proxy" in lowered:
        return FailureKind.PROXY_POLICY, (
            f"the network policy refused this host before any request was sent: {text}"
        )

    if "certificate" in lowered or "ssl" in lowered or "tls" in lowered:
        return FailureKind.TLS, f"TLS handshake failed: {text}"

    if "timeout" in lowered or "timed out" in lowered:
        return FailureKind.TIMEOUT, f"the backend did not answer in time: {text}"

    return FailureKind.HTTP_ERROR, text


class SearchProvider(abc.ABC):
    """Adapter contract. Implementations must be safe to call concurrently."""

    name: str = "base"
    #: Approximate cost in USD per 1,000 queries — powers ``main.py estimate``.
    cost_per_1k: float = 0.0
    #: Provider-imposed hard cap on queries per day (None = unlimited).
    daily_query_cap: int | None = None
    #: Can a zero-result response be trusted as "there is genuinely nothing"?
    #:
    #: True for JSON APIs, which return an explicit empty results array. False
    #: for HTML-scraped endpoints, where zero parsed results is ambiguous between
    #: "no matches" and "the markup changed / we were served a challenge page".
    #: The negative cache is only allowed to record an absence when this is True —
    #: otherwise one bad response would permanently blank everyone at a company.
    empty_means_absent: bool = False

    def __init__(self, config: Any) -> None:
        self.config = config

    @abc.abstractmethod
    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        """Run one query. Raise ``ProviderError`` on failure; never return partial junk."""

    async def close(self) -> None:
        """Release any held resources (HTTP sessions)."""

    def validate(self) -> None:
        """Raise if the adapter is not usable (missing key, unreachable host).

        Called once at startup so a misconfigured run fails immediately rather
        than after thousands of rows.
        """


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def query_hash(query: str, provider: str) -> str:
    """Cache key for a query. Whitespace-normalised so trivially different
    spellings of the same query share a cache entry."""
    normalised = re.sub(r"\s+", " ", query.strip().lower())
    return hashlib.sha1(f"{provider}\x00{normalised}".encode("utf-8")).hexdigest()[:24]


_LINKEDIN_HOST_RE = re.compile(r"^https?://([a-z0-9-]+\.)*linkedin\.com/", re.IGNORECASE)
_PROFILE_PATH_RE = re.compile(r"/in/[^/?#]+", re.IGNORECASE)


def is_linkedin_url(url: str) -> bool:
    return bool(_LINKEDIN_HOST_RE.match(url or ""))


def is_profile_url(url: str) -> bool:
    """True for a personal profile URL (``/in/<slug>``) on any LinkedIn locale host."""
    return is_linkedin_url(url) and bool(_PROFILE_PATH_RE.search(url))


def canonical_profile_url(url: str) -> str:
    """Reduce a profile URL to a comparable canonical form.

    ``https://in.linkedin.com/in/girishrowjee/?trk=abc`` ->
    ``https://www.linkedin.com/in/girishrowjee``

    Locale subdomains and tracking parameters otherwise make the same profile
    look like several different people, which would defeat duplicate detection.
    """
    if not url:
        return ""
    match = _PROFILE_PATH_RE.search(url)
    if not match:
        return url.split("?")[0].rstrip("/")
    slug = match.group(0)[4:].strip("/")
    return f"https://www.linkedin.com/in/{slug.lower()}"


def dedupe_results(results: Sequence[SerpResult]) -> list[SerpResult]:
    """Drop repeats of the same profile, keeping the best-ranked occurrence."""
    seen: set[str] = set()
    out: list[SerpResult] = []
    for result in results:
        key = canonical_profile_url(result.url) or result.url
        if key in seen:
            continue
        seen.add(key)
        out.append(result)
    return out
