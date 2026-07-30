"""Per-row execution of the query ladder.

One method, ``resolve_row``, takes a claimed database record and drives it to a
final decision. It is deliberately the only place that decides when a query is
issued, so the cost model is auditable in one screenful.

Cost per person, in the common cases:

* company already known to have no footprint  -> **0 queries** (negative cache)
* company roster already fetched, subject present in it -> **0 queries**
* company roster already fetched, subject absent -> 1 query (Tier 2)
* company never seen -> 1 query (Tier 1) shared with everyone else there,
  plus at most 1 more for this person
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..cache.company_cache import CompanyCache
from ..identity.scorer import Decision, MatchResult, Subject, resolve
from ..ingest.normalize import normalize_company, normalize_name
from ..providers.base import SerpResult, dedupe_results, is_profile_url
from ..search.client import SearchClient
from ..search.query_builder import (
    Tier,
    company_roster_query,
    person_fallback_query,
    person_query,
    roster_covers_company,
)

logger = logging.getLogger(__name__)


@dataclass
class RowOutcome:
    """What happened to one row, ready to be written to the store."""

    row_uid: str
    match: MatchResult
    queries_used: int = 0
    tier: int = 0
    error: str = ""


class LadderRunner:
    """Executes Tier 1 / 1b / 2 for individual rows."""

    def __init__(self, client: SearchClient, company_cache: CompanyCache, settings) -> None:
        self.client = client
        self.companies = company_cache
        self.settings = settings

        self.tier1_queries = 0
        self.tier2_queries = 0
        self.negative_cache_skips = 0
        self.roster_resolutions = 0

    async def resolve_row(self, record) -> RowOutcome:
        """Drive one record to a decision."""
        row_uid = record["row_uid"]
        subject = Subject.from_fields(
            row_uid,
            record["name"], record["company"],
            record["location"], record["industry"], record["designation"],
        )
        queries = 0

        if subject.name.is_empty or subject.company.is_empty:
            return RowOutcome(
                row_uid,
                MatchResult(Decision.BLANK_SKIPPED, notes="row lacks a usable name or company"),
            )

        company_key = record["company_key"] or subject.company.key

        # ---------- Tier 1 / 1b : the company ----------
        cached = self.companies.get(company_key) if self.settings.enable_negative_cache else None

        if cached is None or not cached.searched:
            if self.settings.enable_company_tier:
                roster, used = await self._fetch_company_roster(subject, company_key)
                queries += used
                self.tier1_queries += used
            else:
                roster = []
            has_footprint = roster_covers_company(
                roster, subject.company, subject.name_tokens
            )
            self.companies.put(
                company_key,
                brand_name=subject.company.brand,
                has_footprint=has_footprint,
                roster=roster,
            )
        else:
            roster = cached.roster
            has_footprint = bool(cached.has_footprint)

        # Tier 1b: no company footprint means no person at this company can ever
        # clear the company evidence gate. Resolve to a blank without querying.
        if self.settings.enable_negative_cache and not has_footprint:
            self.negative_cache_skips += 1
            return RowOutcome(
                row_uid,
                MatchResult(
                    Decision.BLANK_NO_CANDIDATE,
                    notes=(
                        "no LinkedIn presence found for this company, so no profile "
                        "can be attributed to this person with the required confidence"
                    ),
                ),
                queries_used=queries, tier=int(Tier.COMPANY_ROSTER),
            )

        # ---------- Try to answer from the shared roster, for free ----------
        if roster:
            result = resolve(subject, roster, self.settings)
            if result.matched:
                self.roster_resolutions += 1
                return RowOutcome(
                    row_uid, result, queries_used=queries, tier=int(Tier.COMPANY_ROSTER)
                )

        # ---------- Tier 2 : the person ----------
        # Required because a roster query has limited recall — validated live: a
        # roster for Greytip Software returned nine employees but not the founder.
        if not self.settings.enable_person_tier:
            return RowOutcome(
                row_uid,
                MatchResult(
                    Decision.BLANK_NO_CANDIDATE,
                    notes="not found in the company roster; per-person tier disabled",
                ),
                queries_used=queries, tier=int(Tier.COMPANY_ROSTER),
            )

        query = person_query(subject.name, subject.company, record["location"])
        response = await self.client.search(query.text)
        queries += 1
        self.tier2_queries += 1

        results = list(response.results)
        if not results and not response.error:
            # Some free providers return nothing for `site:` queries; retry once
            # without the operator. Precision is unaffected — the reject rules
            # still discard every non-LinkedIn URL.
            fallback = person_fallback_query(subject.name, subject.company)
            response = await self.client.search(fallback.text)
            queries += 1
            self.tier2_queries += 1
            results = list(response.results)

        if response.error:
            return RowOutcome(
                row_uid,
                MatchResult(Decision.BLANK_ERROR, notes=f"search failed: {response.error}"),
                queries_used=queries, tier=int(Tier.PERSON), error=response.error,
            )

        # Score against the person results plus the roster — the subject may appear
        # in either, and merging gives the ambiguity check a complete field.
        combined = dedupe_results(list(results) + list(roster))
        match = resolve(subject, combined, self.settings)
        return RowOutcome(row_uid, match, queries_used=queries, tier=int(Tier.PERSON))

    async def _fetch_company_roster(
        self, subject: Subject, company_key: str
    ) -> tuple[list[SerpResult], int]:
        """Tier 1: one query whose answer is shared by everyone at the company."""
        query = company_roster_query(subject.company)
        response = await self.client.search(query.text)
        if response.error:
            logger.warning("roster query failed for %s: %s", company_key, response.error)
            return [], 1
        roster = [r for r in dedupe_results(response.results) if is_profile_url(r.url)]
        return roster, 1

    def stats(self) -> dict[str, int]:
        return {
            "tier1_company_queries": self.tier1_queries,
            "tier2_person_queries": self.tier2_queries,
            "negative_cache_skips": self.negative_cache_skips,
            "resolved_from_roster": self.roster_resolutions,
        }
