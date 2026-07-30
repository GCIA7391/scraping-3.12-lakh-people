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
from dataclasses import dataclass, field

from ..cache.company_cache import CompanyCache
from ..identity import corroborate
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
class QueryTrace:
    """One query as issued, for --explain and the audit log."""

    tier: int
    text: str
    backend: str = ""
    http_status: int | None = None
    result_count: int = 0
    elapsed_ms: float = 0.0
    outcome: str = ""
    error: str = ""
    from_cache: bool = False


@dataclass
class RowOutcome:
    """What happened to one row, ready to be written to the store."""

    row_uid: str
    match: MatchResult
    queries_used: int = 0
    tier: int = 0
    error: str = ""
    #: Every query issued for this row, in order. Empty unless tracing is on.
    traces: list[QueryTrace] = field(default_factory=list)
    subject: Subject | None = None


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
        #: Rows deferred because the company lookup could not be trusted.
        self.unverified_company_skips = 0
        self.corroboration_queries = 0
        self.corroborated = 0
        self.corroboration_failed = 0

    async def _traced_search(self, query_text: str, tier: int, traces: list[QueryTrace]):
        """Issue a query and record exactly what came back.

        Every network call in the ladder goes through here, so --explain and the
        audit log can never disagree with what was actually requested.
        """
        response = await self.client.search(query_text)
        traces.append(QueryTrace(
            tier=tier, text=query_text, backend=response.backend,
            http_status=response.http_status, result_count=len(response.results),
            elapsed_ms=response.elapsed_ms, outcome=response.outcome.value,
            error=response.error, from_cache=response.from_cache,
        ))
        return response

    async def resolve_row(self, record) -> RowOutcome:
        """Drive one record to a decision, recording every query it issues."""
        subject = Subject.from_fields(
            record["row_uid"],
            record["name"], record["company"],
            record["location"], record["industry"], record["designation"],
        )
        traces: list[QueryTrace] = []
        outcome = await self._resolve_row(record, subject, traces)
        outcome.traces = traces
        outcome.subject = subject
        return outcome

    async def _resolve_row(
        self, record, subject: Subject, traces: list[QueryTrace]
    ) -> RowOutcome:
        row_uid = record["row_uid"]
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
            trustworthy = True
            if self.settings.enable_company_tier:
                roster, used, trustworthy = await self._fetch_company_roster(
                    subject, company_key, traces
                )
                queries += used
                self.tier1_queries += used
            else:
                roster = []

            has_footprint = roster_covers_company(
                roster, subject.company, subject.name_tokens
            )

            if not trustworthy:
                # The roster query failed or came back unparseable. We do NOT know
                # whether this company has a presence, and writing has_footprint=0
                # here would permanently blank every person at it on the strength
                # of one bad response. Leave the cache untouched and fail the row
                # so it is retried on the next pass.
                self.unverified_company_skips += 1
                return RowOutcome(
                    row_uid,
                    MatchResult(
                        Decision.BLANK_ERROR,
                        notes=(
                            "company lookup was inconclusive (the search backend did "
                            "not return usable results); deliberately not recorded as "
                            "an absence"
                        ),
                    ),
                    queries_used=queries, tier=int(Tier.COMPANY_ROSTER),
                    error="inconclusive company lookup",
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
                # Corroboration applies here too — a match found for free in the
                # roster must clear the same bar as one found by a person query,
                # or precision mode would have a hole straight through it.
                if self.settings.require_corroboration:
                    result, used = await self._corroborate(subject, result, traces)
                    queries += used
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
        response = await self._traced_search(query.text, int(Tier.PERSON), traces)
        queries += 1
        self.tier2_queries += 1

        results = list(response.results)
        if not results and not response.error:
            # Some free providers return nothing for `site:` queries; retry once
            # without the operator. Precision is unaffected — the reject rules
            # still discard every non-LinkedIn URL.
            fallback = person_fallback_query(subject.name, subject.company)
            response = await self._traced_search(fallback.text, int(Tier.PERSON), traces)
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

        if match.matched and self.settings.require_corroboration:
            match, used = await self._corroborate(subject, match, traces)
            queries += used

        return RowOutcome(row_uid, match, queries_used=queries, tier=int(Tier.PERSON))

    async def _corroborate(self, subject: Subject, match, traces: list[QueryTrace]):
        """Demand a second, independent source before accepting a match.

        Required at the 99% bar because name+company alone scores 0.9644 — enough
        for 95%, not for 99%. Validated against a real failure: the registry lists
        a "co-founder" of eMudhra who is not one, and a differently-employed
        person of that exact name does exist on LinkedIn.

        The query deliberately omits the LinkedIn restriction, because what we
        want is a source that is *not* LinkedIn.
        """
        query = f'"{subject.name.display}" "{subject.company.brand}"'
        response = await self._traced_search(query, int(Tier.PERSON), traces)
        self.corroboration_queries += 1

        if response.error:
            # Could not check. Do not upgrade an unverified match to a 99% claim.
            self.corroboration_failed += 1
            match.decision = Decision.BLANK_LOW_CONFIDENCE
            match.notes = (
                f"met the score threshold but corroboration could not be checked "
                f"({response.error}); not written at the precision bar"
            )
            match.review_candidates = match.review_candidates or []
            match.linkedin_url = ""
            return match, 1

        evidence = corroborate.assess(response.results, subject.name, subject.company)
        if not evidence.found:
            self.corroboration_failed += 1
            match.decision = Decision.BLANK_LOW_CONFIDENCE
            match.notes = (
                "met the score threshold but no independent source names both this "
                "person and this company (registry aggregators do not count, being "
                "republications of the same source as the input)"
            )
            match.linkedin_url = ""
            return match, 1

        self.corroborated += 1
        match.notes = f"{match.notes} | corroborated by {evidence.summary}"
        match.source_urls = list(match.source_urls) + [evidence.url]
        return match, 1

    async def _fetch_company_roster(
        self, subject: Subject, company_key: str, traces: list[QueryTrace]
    ) -> tuple[list[SerpResult], int, bool]:
        """Tier 1: one query whose answer is shared by everyone at the company.

        Returns ``(roster, queries_used, trustworthy)``. ``trustworthy`` is False
        when the answer cannot be relied on to mean "this company has no
        presence" — an error, or an empty response from a backend that cannot
        distinguish "no results" from "we failed to parse the page".
        """
        query = company_roster_query(subject.company)
        response = await self._traced_search(query.text, int(Tier.COMPANY_ROSTER), traces)

        if response.error:
            logger.warning("roster query failed for %s: %s", company_key, response.error)
            return [], 1, False

        roster = [r for r in dedupe_results(response.results) if is_profile_url(r.url)]

        if not response.results and not self.client.provider.empty_means_absent:
            logger.warning(
                "roster query for %s returned nothing from an HTML backend — "
                "treating as inconclusive rather than as an absence", company_key,
            )
            return roster, 1, False

        return roster, 1, True

    def stats(self) -> dict[str, int]:
        return {
            "tier1_company_queries": self.tier1_queries,
            "tier2_person_queries": self.tier2_queries,
            "negative_cache_skips": self.negative_cache_skips,
            "resolved_from_roster": self.roster_resolutions,
            "unverified_company_skips": self.unverified_company_skips,
            "corroboration_queries": self.corroboration_queries,
            "corroborated": self.corroborated,
            "corroboration_failed": self.corroboration_failed,
        }
