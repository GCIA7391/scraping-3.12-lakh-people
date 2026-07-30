"""Replay-only provider backed by recorded SERP JSON.

This exists so the entire pipeline — ladder, caches, scorer, reject rules,
workers, resume, output — is testable with **zero network access**. That is not
merely convenient: the environment this pipeline was developed in blocks every
search engine, and CI should never depend on a live third-party API either.

Cassettes live in ``tests/fixtures/serp/`` as one JSON file per query. Six of
them are verbatim recordings of real searches used to validate the matching
rules; the rest are synthetic edge cases.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .base import ProviderError, SearchProvider, SearchResponse, SerpResult, query_hash

logger = logging.getLogger(__name__)


class CassetteProvider(SearchProvider):
    """Serves recorded responses; optionally strict about unknown queries."""

    name = "cassette"
    cost_per_1k = 0.0

    def __init__(self, config: Any, *, strict: bool = False) -> None:
        super().__init__(config)
        directory = getattr(config, "cassette_dir", "") or ""
        self.directory = Path(directory) if directory else _default_dir()
        self.strict = strict
        self._by_query: dict[str, SearchResponse] = {}
        self._load()

    def _load(self) -> None:
        if not self.directory.exists():
            logger.warning("cassette directory %s does not exist", self.directory)
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("unreadable cassette %s: %s", path, exc)
                continue
            for entry in data if isinstance(data, list) else [data]:
                response = SearchResponse.from_dict(entry)
                response.provider = self.name
                if response.query:
                    self._by_query[_key(response.query)] = response
        logger.info("loaded %d cassette(s) from %s", len(self._by_query), self.directory)

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        recorded = self._by_query.get(_key(query))
        if recorded is None:
            if self.strict:
                raise ProviderError(f"no cassette recorded for query: {query!r}")
            # A query with no recording is a legitimate "nothing found" — which is
            # exactly what most micro-company queries return in production.
            return SearchResponse(query=query, provider=self.name, results=[])
        return SearchResponse(
            query=query,
            provider=self.name,
            results=list(recorded.results[:limit]),
            error=recorded.error,
        )

    # -- recording helper, used to build fixtures from real responses ----------
    def record(self, response: SearchResponse) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{query_hash(response.query, 'fixture')}.json"
        path.write_text(json.dumps(response.to_dict(), indent=2), encoding="utf-8")
        self._by_query[_key(response.query)] = response
        return path


def _key(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _default_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "serp"


def make_response(query: str, results: list[dict[str, str]]) -> SearchResponse:
    """Convenience constructor for tests."""
    return SearchResponse(
        query=query,
        provider="cassette",
        results=[
            SerpResult(
                title=r.get("title", ""), url=r.get("url", ""),
                snippet=r.get("snippet", ""), rank=i,
            )
            for i, r in enumerate(results)
        ],
    )
