"""Cache of raw provider responses.

Stored so that changing a weight, a threshold or a reject rule never costs
another search. On a 312k run against a free provider the searching is the
expensive part by orders of magnitude — re-scoring a cached corpus takes minutes,
re-searching it takes days. This is what makes ``main.py calibrate`` and any
subsequent tuning practical.
"""

from __future__ import annotations

import logging
from typing import Any

from ..providers.base import SearchResponse, query_hash

logger = logging.getLogger(__name__)


class SerpCache:
    """Thin, typed layer over the ``serp_cache`` table."""

    def __init__(self, store, ttl_days: int = 30) -> None:
        self._store = store
        self._ttl_seconds = ttl_days * 86_400 if ttl_days and ttl_days > 0 else None
        self.hits = 0
        self.misses = 0

    def get(self, query: str, provider: str) -> SearchResponse | None:
        payload = self._store.get_serp(query_hash(query, provider), self._ttl_seconds)
        if payload is None:
            self.misses += 1
            return None
        self.hits += 1
        response = SearchResponse.from_dict(payload)
        response.from_cache = True
        return response

    def put(self, response: SearchResponse) -> None:
        # Never cache a failure: a transient provider error would otherwise be
        # frozen in for the whole TTL and poison every retry.
        if not response.ok:
            return
        self._store.put_serp(
            query_hash(response.query, response.provider),
            response.query, response.provider, response.to_dict(),
        )

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "serp_cache_hits": self.hits,
            "serp_cache_misses": self.misses,
            "serp_cache_hit_rate": (self.hits / total) if total else 0.0,
        }
