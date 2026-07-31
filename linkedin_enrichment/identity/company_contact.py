"""Company contact discovery — paid once, reused by every executive there.

The arithmetic that makes this the highest-leverage stage in the pipeline:

* 312,160 people belong to **140,351 companies** — 2.2 people per company on
  average, and far more at the large employers, which are precisely the rows a
  wealth-management team most wants.
* In the last measured run, **37.5% of companies** had a discoverable web
  presence while only **12.5% of people** converted. The company is findable
  roughly three times as often as the individual.

So the company is searched once, its published contact routes are cached, and
every executive on its board inherits them. A director of a company with a
leadership page and an ir@ address is a usable lead even when no personal search
ever resolves — and that is where the yield the brief asks for comes from.

Trust rule, inherited from the negative cache: an *inconclusive* lookup is never
recorded as "this company publishes nothing". One throttled response would
otherwise blank every executive at that company permanently.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..ingest.normalize import NormalizedCompany
from ..output.contacts import (
    ContactRoute,
    RouteType,
    collect_routes,
    deserialise,
    serialise,
)
from ..providers.base import dedupe_results
from ..search.query_builder import Tier, company_contact_queries

logger = logging.getLogger(__name__)


@dataclass
class CompanyContactProfile:
    """Everything published about how to reach one company."""

    company_key: str
    brand_name: str = ""
    website: str = ""
    routes: list[ContactRoute] = field(default_factory=list)
    #: True = searched and routes found, False = searched and nothing published,
    #: None = the lookup was inconclusive and must be retried.
    discovered: bool | None = None
    queries_used: int = 0

    @property
    def searched(self) -> bool:
        return self.discovered is not None

    @property
    def has_routes(self) -> bool:
        return bool(self.routes)


class CompanyContactFinder:
    """Discovers and caches company-level contact routes.

    Kept separate from ``CompanyCache`` because the two answer different
    questions — that one asks "does this company exist on LinkedIn", this one
    asks "how would a person reach it" — and because they fail independently: a
    company with no LinkedIn footprint at all may still publish an IR address.
    """

    def __init__(self, store, settings) -> None:
        self.store = store
        self.settings = settings
        self.hits = 0
        self.misses = 0
        self.queries = 0
        self.companies_with_routes = 0
        self.inconclusive = 0

    # ------------------------------------------------------------------
    def cached(self, company_key: str) -> CompanyContactProfile | None:
        row = self.store.get_company_contacts(company_key)
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        discovered = row["discovered"]
        try:
            payload = json.loads(row["routes_json"] or "[]")
        except json.JSONDecodeError:
            payload = []
        return CompanyContactProfile(
            company_key=company_key,
            brand_name=row["brand_name"] or "",
            website=row["website"] or "",
            routes=deserialise(payload),
            discovered=None if discovered is None else bool(discovered),
        )

    def save(self, profile: CompanyContactProfile) -> None:
        self.store.put_company_contacts(
            profile.company_key,
            brand_name=profile.brand_name,
            website=profile.website,
            routes=serialise(profile.routes),
            discovered=profile.discovered,
            queries=profile.queries_used,
        )

    # ------------------------------------------------------------------
    async def discover(
        self, company: NormalizedCompany, company_key: str, search,
    ) -> CompanyContactProfile:
        """Find this company's published routes, or return the cached answer.

        ``search`` is an ``async (query_text, tier) -> SearchResponse`` callable —
        the runner passes its traced wrapper so --explain and the audit log see
        these queries exactly as they see every other one.

        A cached *inconclusive* entry is re-attempted; a cached conclusive one
        (routes found, or genuinely nothing published) is returned without
        spending a query.
        """
        cached = self.cached(company_key)
        if cached is not None and cached.searched:
            return cached

        profile = CompanyContactProfile(
            company_key=company_key, brand_name=company.brand,
        )
        queries = company_contact_queries(
            company, limit=self.settings.max_company_contact_queries
        )
        if not queries:
            return profile

        gathered = []
        errors = 0
        for query in queries:
            response = await search(query.text, int(Tier.COMPANY_CONTACT))
            profile.queries_used += 1
            self.queries += 1
            if response.error:
                errors += 1
                continue
            gathered = dedupe_results(gathered + list(response.results))

        if errors == len(queries):
            # Every formulation failed. We know nothing; say so, and let the
            # next pass try again rather than caching a fabricated absence.
            self.inconclusive += 1
            profile.discovered = None
            logger.debug("company contact lookup inconclusive for %s", company_key)
            return profile

        profile.routes = collect_routes(
            gathered, company, limit=self.settings.max_company_routes
        )
        profile.website = _pick_website(profile.routes)
        profile.discovered = bool(profile.routes)
        if profile.routes:
            self.companies_with_routes += 1
        self.save(profile)
        return profile

    # ------------------------------------------------------------------
    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "company_contact_queries": self.queries,
            "company_contact_cache_hits": self.hits,
            "company_contact_cache_misses": self.misses,
            "company_contact_hit_rate": (self.hits / total) if total else 0.0,
            "companies_with_routes": self.companies_with_routes,
            "company_contact_inconclusive": self.inconclusive,
        }


#: Routes whose value is a URL on the company's own site, best first.
_WEBSITE_ROUTES = (
    RouteType.CONTACT_FORM, RouteType.EXECUTIVE_OFFICE,
    RouteType.INVESTOR_RELATIONS, RouteType.BOARD_OFFICE,
)


def _pick_website(routes: list[ContactRoute]) -> str:
    """The company's own domain, taken from whichever route revealed it."""
    for wanted in _WEBSITE_ROUTES:
        for route in routes:
            if route.type is wanted and route.value.startswith("http"):
                return route.value
    return ""


def routes_for_person(
    profile: CompanyContactProfile | None,
    person_routes: list[ContactRoute],
    *,
    limit: int = 12,
) -> list[ContactRoute]:
    """Combine what was found for the person with what the company publishes.

    Person-level routes come first at equal directness because they are the more
    specific answer; the company's routes fill in behind them and are what make a
    row deliverable when nothing personal was ever found.
    """
    from ..output.contacts import merge_routes

    company_routes = profile.routes if profile else []
    return merge_routes(person_routes, company_routes, limit=limit)
