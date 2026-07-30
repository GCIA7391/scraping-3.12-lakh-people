"""Provider registry and factory.

Adding a provider means implementing ``SearchProvider`` and registering it here;
nothing else in the pipeline changes.
"""

from __future__ import annotations

from typing import Any, Type

from .base import (
    ProviderError,
    SearchProvider,
    SearchResponse,
    SerpResult,
    canonical_profile_url,
    dedupe_results,
    is_linkedin_url,
    is_profile_url,
    query_hash,
)
from .cassette import CassetteProvider, make_response
from .http_providers import (
    DuckDuckGoProvider,
    GoogleCseProvider,
    SearxngProvider,
    SerpApiProvider,
    SerperProvider,
)

#: Free providers first — the pipeline is designed to run at zero cost by default.
REGISTRY: dict[str, Type[SearchProvider]] = {
    "searxng": SearxngProvider,          # free, self-hosted (default)
    "duckduckgo": DuckDuckGoProvider,    # free, no key, heavily rate-limited
    "google_cse": GoogleCseProvider,     # 100/day free, then $5/1k
    "serper": SerperProvider,            # paid
    "serpapi": SerpApiProvider,          # paid
    "cassette": CassetteProvider,        # offline replay, tests only
}

FREE_PROVIDERS = frozenset({"searxng", "duckduckgo", "cassette"})


def build_provider(name: str, provider_config: Any) -> SearchProvider:
    """Instantiate a provider by name, raising a helpful error for typos."""
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise ProviderError(
            f"unknown search provider {name!r}. Available: {', '.join(sorted(REGISTRY))}",
            retryable=False,
        )
    return REGISTRY[key](provider_config)


__all__ = [
    "REGISTRY", "FREE_PROVIDERS", "build_provider",
    "SearchProvider", "SearchResponse", "SerpResult", "ProviderError",
    "CassetteProvider", "make_response",
    "SearxngProvider", "DuckDuckGoProvider", "GoogleCseProvider",
    "SerperProvider", "SerpApiProvider",
    "query_hash", "is_linkedin_url", "is_profile_url",
    "canonical_profile_url", "dedupe_results",
]
