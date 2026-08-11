"""Company-level cache, including the negative cache.

This is the largest single saving in the pipeline. The 312,160 people belong to
only 140,351 companies, and the company distribution is extreme: 24,947 companies
contribute one person and 81,524 contribute exactly two (Indian private limited
companies must have at least two directors). Most are micro-entities with no web
presence whatsoever.

Two consequences:

* A roster fetched once serves every person at that company.
* A company proven to have *no* LinkedIn footprint disqualifies all of its people
  immediately, because the reject rules require company evidence for any match.
  Those rows cost zero queries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..providers.base import SerpResult

logger = logging.getLogger(__name__)


@dataclass
class CompanyRecord:
    """What is known about one company's LinkedIn presence."""

    company_key: str
    brand_name: str = ""
    has_footprint: bool | None = None   # None = not yet searched
    roster: list[SerpResult] = None     # type: ignore[assignment]
    company_page_url: str = ""

    def __post_init__(self) -> None:
        if self.roster is None:
            self.roster = []

    @property
    def searched(self) -> bool:
        return self.has_footprint is not None

    @property
    def is_negative(self) -> bool:
        """True when the company was searched and shown to have no presence."""
        return self.has_footprint is False


class CompanyCache:
    """Thin, typed layer over the ``company_cache`` table."""

    def __init__(self, store) -> None:
        self._store = store
        self.hits = 0
        self.misses = 0
        self.negative_hits = 0

    def get(self, company_key: str) -> CompanyRecord | None:
        row = self._store.get_company(company_key)
        if row is None:
            self.misses += 1
            return None

        self.hits += 1
        footprint = row["has_linkedin_footprint"]
        record = CompanyRecord(
            company_key=company_key,
            brand_name=row["brand_name"] or "",
            has_footprint=None if footprint is None else bool(footprint),
            roster=_decode_roster(row["roster_json"]),
            company_page_url=row["company_page_url"] or "",
        )
        if record.is_negative:
            self.negative_hits += 1
        return record

    def put(
        self, company_key: str, *, brand_name: str, has_footprint: bool | None,
        roster: list[SerpResult] | None = None, company_page_url: str = "",
        queries: int = 1,
    ) -> None:
        self._store.put_company(
            company_key,
            brand_name=brand_name,
            has_footprint=has_footprint,
            roster=[r.to_dict() for r in (roster or [])],
            company_page_url=company_page_url,
            queries=queries,
        )

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "company_cache_hits": self.hits,
            "company_cache_misses": self.misses,
            "company_cache_hit_rate": (self.hits / total) if total else 0.0,
            "negative_cache_hits": self.negative_hits,
        }


def _decode_roster(raw: str | None) -> list[SerpResult]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [SerpResult.from_dict(item) for item in data if isinstance(item, dict)]
