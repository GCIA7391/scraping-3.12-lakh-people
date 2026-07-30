"""Search client: the single path through which every query is issued.

Composes cache -> rate limit -> retry -> provider, so no caller can accidentally
bypass one of them. Also the one place that counts queries, which is what the
cost estimate and the QC report are built from.
"""

from __future__ import annotations

import logging

from ..cache.serp_cache import SerpCache
from ..providers.base import ProviderError, SearchProvider, SearchResponse
from .ratelimit import AdaptiveRateLimiter
from .retry import with_retry

logger = logging.getLogger(__name__)


class SearchClient:
    """Cached, rate-limited, retrying wrapper around a ``SearchProvider``."""

    def __init__(self, provider: SearchProvider, cache: SerpCache, settings) -> None:
        self.provider = provider
        self.cache = cache
        self.settings = settings
        self.limiter = AdaptiveRateLimiter(settings.rate_limit)

        self.queries_issued = 0
        self.queries_served_from_cache = 0
        self.errors = 0

    async def search(self, query: str) -> SearchResponse:
        """Return results for ``query``, from cache when possible.

        Never raises: a failed search is returned as an empty response carrying
        the error text. A single unreachable query must not abort a run that may
        already have processed hundreds of thousands of rows — the row is instead
        recorded with an explicit error and can be retried on the next pass.
        """
        cached = self.cache.get(query, self.provider.name)
        if cached is not None:
            self.queries_served_from_cache += 1
            return cached

        await self.limiter.acquire()

        async def _once() -> SearchResponse:
            return await self.provider.search(
                query, limit=self.settings.provider.results_per_query
            )

        try:
            response = await with_retry(
                _once, self.settings.retry,
                on_throttle=self.limiter.on_throttled,
                description=f"search({query[:60]!r})",
            )
        except ProviderError as exc:
            self.errors += 1
            logger.error("search failed permanently for %r: %s", query[:80], exc)
            return SearchResponse(query=query, provider=self.provider.name, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            logger.exception("unexpected search failure for %r", query[:80])
            return SearchResponse(query=query, provider=self.provider.name, error=str(exc))

        self.queries_issued += 1
        self.limiter.on_success()
        self.cache.put(response)
        return response

    def stats(self) -> dict[str, float | int]:
        return {
            "queries_issued": self.queries_issued,
            "queries_from_cache": self.queries_served_from_cache,
            "search_errors": self.errors,
            "throttle_events": self.limiter.throttle_events,
            "current_rate_per_second": round(self.limiter.rate, 4),
            **self.cache.stats(),
        }

    async def close(self) -> None:
        await self.provider.close()
