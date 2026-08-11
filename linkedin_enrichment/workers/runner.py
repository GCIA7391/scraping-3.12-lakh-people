"""Per-row execution: a production line, not a search engine.

One method, ``resolve_row``, takes a claimed database record and drives it to a
final outcome. It is deliberately the only place that decides when a query is
issued, so the cost model is auditable in one screenful.

The flow is flat and unconditional — no stage may end the row early on the
grounds that the *next* stage is unlikely to succeed:

    company stage   roster (shared) + contact routes (shared, cached once)
    person stage    the source ladder, exhausted
    collect         every professional route found, person-level and company-level
    decide          a profile if one cleared the identity gate; otherwise the
                    routes, which are delivered on their own

Two outputs, scored independently, because they are different claims:

* **LinkedIn Profile** — an assertion about which human a profile belongs to.
  Still gated on confidence and corroboration: writing the wrong person's
  profile is worse than writing nothing.
* **Contact Routes** — published, checkable facts about how to reach an
  executive at that company. Not confidence-gated, because "Acme publishes
  ir@acme.com on acme.com/investors" is not a guess about identity.

Cost per person, in the common cases:

* company already searched -> **0 company queries** (roster and routes cached)
* company never seen -> up to 1 roster + ``max_company_contact_queries`` contact
  queries, shared with everyone else at that company (2.2 people on average)
* person -> up to ``max_person_queries`` rungs of the ladder, stopping the
  moment a profile is confirmed
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..cache.company_cache import CompanyCache
from ..identity import corroborate
from ..identity.company_contact import (
    CompanyContactFinder,
    CompanyContactProfile,
    routes_for_person,
)
from ..identity.scorer import Decision, MatchResult, Subject, resolve
from ..output import contacts as contacts_mod
from ..output.contacts import ContactRoute, RouteType
from ..providers.base import SerpResult, dedupe_results, is_profile_url
from ..search.client import SearchClient
from ..search.query_builder import (
    Tier,
    company_roster_query,
    person_query_variants,
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
    #: Professional contact routes, most direct first. Independent of ``match``.
    routes: list[ContactRoute] = field(default_factory=list)

    @property
    def delivered(self) -> bool:
        """Is this row usable by a sales team?"""
        return self.match.delivered

    @property
    def best_route_type(self) -> str:
        return self.routes[0].type.value if self.routes else ""


class LadderRunner:
    """Executes the company stage, the person ladder, and route collection."""

    def __init__(
        self, client: SearchClient, company_cache: CompanyCache, settings,
        contact_finder: CompanyContactFinder | None = None,
    ) -> None:
        self.client = client
        self.companies = company_cache
        self.settings = settings
        self.contacts = contact_finder

        self.tier1_queries = 0
        self.tier2_queries = 0
        self.negative_cache_skips = 0
        self.roster_resolutions = 0
        #: Rows deferred because the company lookup could not be trusted.
        self.unverified_company_skips = 0
        self.corroboration_queries = 0
        self.corroborated = 0
        self.corroboration_failed = 0
        #: Rows delivered on contact routes alone, with no profile.
        self.contact_only_deliveries = 0
        self.rows_with_routes = 0
        #: Which ladder rung produced each hit — shows whether the deeper ladder
        #: is earning its cost, by name rather than by index.
        self.variant_hits: dict[str, int] = {}

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
        """Drive one record to an outcome, recording every query it issues."""
        subject = Subject.from_fields(
            record["row_uid"],
            record["name"], record["company"],
            record["location"], record["industry"], record["designation"],
        )
        traces: list[QueryTrace] = []
        outcome = await self._resolve_row(record, subject, traces)
        outcome.traces = traces
        outcome.subject = subject
        # Count only queries that actually hit the network. A cache-served step
        # costs nothing, and reporting it as a query would overstate the run's
        # real cost — which is exactly what the estimate and the QC report are
        # read for. Duplicated people therefore correctly show zero.
        outcome.queries_used = sum(1 for t in traces if not t.from_cache)
        if outcome.routes:
            self.rows_with_routes += 1
        return outcome

    # ------------------------------------------------------------------
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

        # ---------- Company stage: roster ----------
        roster, has_footprint, used, trustworthy = await self._company_roster(
            subject, company_key, traces
        )
        queries += used

        # ---------- Company stage: contact routes ----------
        # Runs regardless of the roster outcome. A company with no LinkedIn
        # presence at all may still publish an investor-relations address, and
        # refusing to look would throw away the row for no reason.
        company_profile, used = await self._company_contacts(subject, company_key, traces)
        queries += used
        company_routes = company_profile.routes if company_profile else []

        if not trustworthy and not company_routes:
            # The roster query failed *and* nothing was found on the contact
            # side. We know nothing about this row; recording that as an answer
            # would bake an infrastructure fault into the deliverable.
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

        # ---------- Person stage ----------
        match, evidence, used, last_error = await self._find_person(
            record, subject, roster, has_footprint, traces
        )
        queries += used

        # ---------- Routes ----------
        person_routes = contacts_mod.collect_routes(
            evidence, subject.company, subject.name, limit=self.settings.max_row_routes
        ) if self.settings.enable_contact_routes else []

        if match.matched and match.linkedin_url:
            person_routes.insert(0, ContactRoute(
                type=RouteType.LINKEDIN_PROFILE, value=match.linkedin_url,
                source_url=match.linkedin_url,
                label=f"Verified profile ({match.confidence:.2%} confidence)",
                scope="person",
            ))

        routes = routes_for_person(
            company_profile, person_routes, limit=self.settings.max_row_routes
        ) if self.settings.enable_contact_routes else []

        # ---------- Decide ----------
        match = self._decide(match, routes, last_error)
        if match.decision is Decision.CONTACT_ROUTE_ONLY:
            self.contact_only_deliveries += 1

        return RowOutcome(
            row_uid, match, queries_used=queries,
            tier=int(Tier.PERSON), error=last_error, routes=routes,
        )

    # ------------------------------------------------------------------
    def _decide(
        self, match: MatchResult, routes: list[ContactRoute], last_error: str
    ) -> MatchResult:
        """Fold the two independent outputs into one row-level outcome.

        A confirmed profile always wins — it is the stronger deliverable and it
        carries its routes with it. Failing that, published routes stand on their
        own: they are what makes a row actionable when the individual could not
        be identified, and they are facts about the company rather than claims
        about which human a profile belongs to.
        """
        if match.matched:
            return match

        if not (routes and self.settings.accept_on_contact_route):
            return match

        if match.decision is Decision.BLANK_ERROR:
            # An infrastructure fault is not converted into a delivery. The row
            # still has to be retried; the routes are recorded either way.
            return match

        best = routes[0]
        match.decision = Decision.CONTACT_ROUTE_ONLY
        match.linkedin_url = ""
        match.notes = (
            f"no LinkedIn profile met the {self.settings.confidence_threshold:.0%} bar; "
            f"delivered on {len(routes)} published professional contact route"
            f"{'s' if len(routes) != 1 else ''} "
            f"(most direct: {best.type.value})"
            + (f" | prior note: {match.notes}" if match.notes else "")
        )
        match.source_urls = list(dict.fromkeys(
            list(match.source_urls) + [r.source_url for r in routes if r.source_url]
        ))
        return match

    # ------------------------------------------------------------------
    async def _company_roster(
        self, subject: Subject, company_key: str, traces: list[QueryTrace]
    ) -> tuple[list[SerpResult], bool | None, int, bool]:
        """Roster and footprint for this company.

        Returns ``(roster, has_footprint, queries, trustworthy)``. ``has_footprint``
        is tri-state: True, False, or **None for "we do not know"**. That third
        state matters — an inconclusive lookup must not be able to act as a
        proven absence anywhere downstream.
        """
        cached = self.companies.get(company_key) if self.settings.enable_negative_cache else None
        if cached is not None and cached.searched:
            return cached.roster, bool(cached.has_footprint), 0, True

        if not self.settings.enable_company_tier:
            return [], True, 0, True

        roster, used, trustworthy = await self._fetch_company_roster(
            subject, company_key, traces
        )
        self.tier1_queries += used

        if not trustworthy:
            # Leave the cache untouched: writing has_footprint=0 here would
            # permanently blank every person at this company on the strength of
            # one bad response.
            return roster, None, used, False

        has_footprint = roster_covers_company(roster, subject.company, subject.name_tokens)
        self.companies.put(
            company_key,
            brand_name=subject.company.brand,
            has_footprint=has_footprint,
            roster=roster,
        )
        return roster, has_footprint, used, True

    async def _company_contacts(
        self, subject: Subject, company_key: str, traces: list[QueryTrace]
    ) -> tuple[CompanyContactProfile | None, int]:
        """Published contact routes for this company — discovered once, reused."""
        if not (self.settings.enable_contact_routes and self.contacts is not None):
            return None, 0

        async def search(text: str, tier: int):
            return await self._traced_search(text, tier, traces)

        profile = await self.contacts.discover(subject.company, company_key, search)
        return profile, profile.queries_used

    async def _find_person(
        self, record, subject: Subject, roster: list[SerpResult],
        has_footprint: bool | None, traces: list[QueryTrace],
    ) -> tuple[MatchResult, list[SerpResult], int, str]:
        """Work the person ladder. Returns (match, evidence gathered, queries, last error).

        The evidence list is returned whatever the outcome, because route
        extraction runs over it: a search that failed to identify the person may
        still have surfaced the company's contact page.
        """
        queries = 0

        # No LinkedIn presence for the company means no LinkedIn profile can
        # carry its brand tokens, and a match requires exactly that (see
        # identity/reject.py). This is a logical consequence of the reject rule,
        # not a heuristic gate — and it no longer ends the row, only this stage.
        #
        # ``has_footprint is False`` and not ``not has_footprint``: an
        # inconclusive lookup (None) must run the ladder rather than inherit the
        # behaviour of a proven absence.
        if self.settings.enable_negative_cache and has_footprint is False:
            self.negative_cache_skips += 1
            return (
                MatchResult(
                    Decision.BLANK_NO_CANDIDATE,
                    notes=(
                        "no LinkedIn presence found for this company, so no profile "
                        "can be attributed to this person with the required confidence"
                    ),
                ),
                list(roster), queries, "",
            )

        # Answer from the shared roster first, for free.
        if roster:
            result = resolve(subject, roster, self.settings)
            if result.matched:
                # Corroboration applies here too — a match found for free must
                # clear the same bar as one found by a person query, or the
                # precision rule would have a hole straight through it.
                if self.settings.require_corroboration:
                    result, used = await self._corroborate(subject, result, traces)
                    queries += used
                if result.matched:
                    self.roster_resolutions += 1
                    return result, list(roster), queries, ""

        if not self.settings.enable_person_tier:
            return (
                MatchResult(
                    Decision.BLANK_NO_CANDIDATE,
                    notes="not found in the company roster; per-person tier disabled",
                ),
                list(roster), queries, "",
            )

        variants = person_query_variants(
            subject.name, subject.company, record["location"]
        )[: self.settings.max_person_queries]

        accumulated: list[SerpResult] = list(roster)
        last_error = ""
        match: MatchResult | None = None

        for variant in variants:
            response = await self._traced_search(variant.text, int(Tier.PERSON), traces)
            queries += 1
            self.tier2_queries += 1

            if response.error:
                # Record it and keep going — a throttle on one formulation says
                # nothing about the others.
                last_error = response.error
                continue

            last_error = ""
            accumulated = dedupe_results(accumulated + list(response.results))

            # Score against everything gathered so far, so evidence from earlier
            # rungs still counts toward the decision.
            match = resolve(subject, accumulated, self.settings)
            if match.matched:
                self.variant_hits[variant.step] = self.variant_hits.get(variant.step, 0) + 1
                break

        if match is None:
            # Every rung errored; nothing was ever scored.
            return (
                MatchResult(
                    Decision.BLANK_ERROR,
                    notes=f"all {len(variants)} search variants failed; last: {last_error}",
                ),
                accumulated, queries, last_error,
            )

        if match.matched and self.settings.require_corroboration:
            match, used = await self._corroborate(subject, match, traces)
            queries += used

        return match, accumulated, queries, last_error

    # ------------------------------------------------------------------
    async def _corroborate(self, subject: Subject, match, traces: list[QueryTrace]):
        """Demand a second, independent source before accepting a match.

        Required because name+company alone scores 0.9644 — enough for 95%, not
        for more. Validated against a real failure: the registry lists a
        "co-founder" of eMudhra who is not one, and a differently-employed person
        of that exact name does exist on LinkedIn.

        The query deliberately omits the LinkedIn restriction, because what we
        want is a source that is *not* LinkedIn.
        """
        query = f'"{subject.name.display}" "{subject.company.brand}"'
        response = await self._traced_search(query, int(Tier.PERSON), traces)
        self.corroboration_queries += 1

        if response.error:
            # Could not check. Do not upgrade an unverified match to a delivered
            # identity claim — but the row can still be delivered on its routes.
            self.corroboration_failed += 1
            match.decision = Decision.BLANK_LOW_CONFIDENCE
            match.notes = (
                f"met the score threshold but corroboration could not be checked "
                f"({response.error}); profile not written"
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
                "person and this company (contact-scraper sites do not count, being "
                "republications of LinkedIn itself)"
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
        """One query whose answer is shared by everyone at the company.

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

    def stats(self) -> dict[str, object]:
        data: dict[str, object] = {
            "tier1_company_queries": self.tier1_queries,
            "tier2_person_queries": self.tier2_queries,
            "negative_cache_skips": self.negative_cache_skips,
            "resolved_from_roster": self.roster_resolutions,
            "unverified_company_skips": self.unverified_company_skips,
            "corroboration_queries": self.corroboration_queries,
            "corroborated": self.corroborated,
            "corroboration_failed": self.corroboration_failed,
            "rows_with_contact_routes": self.rows_with_routes,
            "delivered_on_routes_alone": self.contact_only_deliveries,
            "variant_hits": dict(sorted(self.variant_hits.items())),
        }
        if self.contacts is not None:
            data.update(self.contacts.stats())
        return data
