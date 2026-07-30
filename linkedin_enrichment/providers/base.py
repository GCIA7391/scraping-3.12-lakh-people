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


@dataclass
class SearchResponse:
    """A provider's answer to one query."""

    query: str
    results: list[SerpResult] = field(default_factory=list)
    provider: str = ""
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "provider": self.provider,
            "error": self.error,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchResponse":
        return cls(
            query=data.get("query", ""),
            provider=data.get("provider", ""),
            error=data.get("error", ""),
            results=[SerpResult.from_dict(r) for r in data.get("results", [])],
        )


class ProviderError(RuntimeError):
    """Provider failure. ``retryable`` drives the backoff policy."""

    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class SearchProvider(abc.ABC):
    """Adapter contract. Implementations must be safe to call concurrently."""

    name: str = "base"
    #: Approximate cost in USD per 1,000 queries — powers ``main.py estimate``.
    cost_per_1k: float = 0.0
    #: Provider-imposed hard cap on queries per day (None = unlimited).
    daily_query_cap: int | None = None

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
