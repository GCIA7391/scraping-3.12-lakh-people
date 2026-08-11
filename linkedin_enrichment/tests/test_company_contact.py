"""Company contact discovery, and delivering a row on routes alone.

This is the change that moves the yield number, so the properties that make it
sound are pinned here:

* the company is searched **once** and every executive there inherits the answer,
* an inconclusive lookup is retried rather than cached as "publishes nothing",
* a row with no personal presence but a company contact is **accepted**,
* the LinkedIn Profile field stays gated even when the row is accepted.
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_enrichment.cache.company_cache import CompanyCache
from linkedin_enrichment.cache.serp_cache import SerpCache
from linkedin_enrichment.identity.company_contact import CompanyContactFinder
from linkedin_enrichment.identity.scorer import Decision
from linkedin_enrichment.ingest.normalize import normalize_company
from linkedin_enrichment.output import contacts as c
from linkedin_enrichment.providers.base import SearchProvider, SearchResponse, SerpResult
from linkedin_enrichment.search.client import SearchClient
from linkedin_enrichment.workers.pool import RunProgress, WorkerPool
from linkedin_enrichment.workers.runner import LadderRunner

GREYTIP = normalize_company("Greytip Software Private Limited")

CONTACT_PAGE = SerpResult(
    title="Contact Us | Greytip Software",
    url="https://greytip.com/contact-us",
    snippet="Write to info@greytip.com or call 080-4123 4567",
)
LEADERSHIP_PAGE = SerpResult(
    title="Leadership | Greytip Software",
    url="https://greytip.com/leadership",
    snippet="Our management team",
)
LINKEDIN_COMPANY = SerpResult(
    title="Greytip Software | LinkedIn",
    url="https://www.linkedin.com/company/greytip-software",
    snippet="HR and payroll software",
)


class _ScriptedProvider(SearchProvider):
    """Answers by substring match on the query; anything unmatched returns empty."""

    name = "scripted"
    empty_means_absent = True

    def __init__(self, config, script: dict[str, list[SerpResult]], *, fail: bool = False):
        super().__init__(config)
        self.script = script
        self.fail = fail
        self.queries: list[str] = []

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        self.queries.append(query)
        if self.fail:
            return SearchResponse(query=query, provider=self.name,
                                  error="backend refused the request")
        for needle, results in self.script.items():
            if needle in query:
                return SearchResponse(query=query, provider=self.name,
                                      results=list(results))
        return SearchResponse(query=query, provider=self.name, results=[])


def _finder(store, settings, provider):
    client = SearchClient(provider, SerpCache(store, 30), settings)
    return CompanyContactFinder(store, settings), client


async def _search_via(client):
    async def search(text: str, tier: int):
        return await client.search(text)
    return search


def _discover(store, settings, provider, company=GREYTIP, key="greytip-software"):
    finder, client = _finder(store, settings, provider)

    async def run():
        search = await _search_via(client)
        try:
            return await finder.discover(company, key, search)
        finally:
            await client.close()

    return finder, asyncio.run(run())


class TestDiscovery:
    def test_published_routes_are_found(self, store, settings) -> None:
        provider = _ScriptedProvider(settings.provider, {
            "official website contact": [CONTACT_PAGE],
            "leadership team management": [LEADERSHIP_PAGE],
        })
        _, profile = _discover(store, settings, provider)

        assert profile.discovered is True
        kinds = {r.type.value for r in profile.routes}
        assert "contact_form" in kinds
        assert "reception" in kinds, "info@ and the switchboard are reception routes"

    def test_every_route_carries_its_source(self, store, settings) -> None:
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        _, profile = _discover(store, settings, provider)
        assert profile.routes
        for route in profile.routes:
            assert route.source_url.startswith("http")

    def test_nothing_published_is_recorded_as_a_conclusive_zero(
        self, store, settings
    ) -> None:
        provider = _ScriptedProvider(settings.provider, {})
        _, profile = _discover(store, settings, provider)
        assert profile.discovered is False
        assert profile.routes == []

    def test_a_failed_lookup_is_inconclusive_not_an_absence(
        self, store, settings
    ) -> None:
        """The negative-cache rule, applied to contacts: one bad response must
        not permanently blank every executive at a company."""
        provider = _ScriptedProvider(settings.provider, {}, fail=True)
        finder, profile = _discover(store, settings, provider)

        assert profile.discovered is None
        assert finder.inconclusive == 1
        assert store.get_company_contacts("greytip-software") is None, (
            "an inconclusive lookup must not be cached"
        )

    def test_query_budget_is_respected(self, store, settings) -> None:
        settings.max_company_contact_queries = 2
        provider = _ScriptedProvider(settings.provider, {})
        _discover(store, settings, provider)
        assert len(provider.queries) == 2


class TestCachingAcrossExecutives:
    def test_the_company_is_searched_once_for_all_its_executives(
        self, store, settings
    ) -> None:
        """The arithmetic the design rests on: 312,160 people, 140,351 companies."""
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        finder, client = _finder(store, settings, provider)

        async def run():
            search = await _search_via(client)
            first = await finder.discover(GREYTIP, "greytip-software", search)
            after_first = len(provider.queries)
            second = await finder.discover(GREYTIP, "greytip-software", search)
            third = await finder.discover(GREYTIP, "greytip-software", search)
            await client.close()
            return first, second, third, after_first

        first, second, third, after_first = asyncio.run(run())

        assert len(provider.queries) == after_first, (
            "the second and third executives must cost zero queries"
        )
        assert [r.key for r in second.routes] == [r.key for r in first.routes]
        assert third.routes, "the cached profile must survive repeated reads"

    def test_an_inconclusive_entry_is_retried(self, store, settings) -> None:
        provider = _ScriptedProvider(settings.provider, {}, fail=True)
        finder, client = _finder(store, settings, provider)

        async def run():
            search = await _search_via(client)
            await finder.discover(GREYTIP, "greytip-software", search)
            first = len(provider.queries)
            await finder.discover(GREYTIP, "greytip-software", search)
            await client.close()
            return first

        first = asyncio.run(run())
        assert len(provider.queries) > first, "an inconclusive lookup must be re-attempted"


class TestAcceptanceOnRoutes:
    """A row with no personal presence but a company contact is a usable lead."""

    def _record(self, store):
        store.insert_records([{
            "row_uid": "r1", "source_file": "f.csv", "row_index": 1,
            "name": "Girish Rowjee", "company": "Greytip Software Private Limited",
            "company_key": "greytip-software", "location": "Bangalore",
            "designation": "CEO",
        }])
        return store.claim_batch(1)[0]

    def _run(self, store, settings, provider):
        client = SearchClient(provider, SerpCache(store, 30), settings)
        finder = CompanyContactFinder(store, settings)
        runner = LadderRunner(client, CompanyCache(store), settings, finder)
        record = self._record(store)

        async def go():
            try:
                return await runner.resolve_row(record)
            finally:
                await client.close()

        return runner, asyncio.run(go())

    def test_no_profile_but_a_company_route_is_delivered(self, store, settings) -> None:
        settings.enable_contact_routes = True
        settings.accept_on_contact_route = True
        provider = _ScriptedProvider(settings.provider, {
            "official website contact": [CONTACT_PAGE],
            "site:linkedin.com/company": [LINKEDIN_COMPANY],
        })
        runner, outcome = self._run(store, settings, provider)

        assert outcome.match.decision is Decision.CONTACT_ROUTE_ONLY
        assert outcome.delivered
        assert outcome.routes
        assert runner.contact_only_deliveries == 1

    def test_the_linkedin_column_stays_empty_for_a_route_only_row(
        self, store, settings
    ) -> None:
        """A wrong profile is worse than a blank. Acceptance widens the
        deliverable; it never loosens the identity claim."""
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        _, outcome = self._run(store, settings, provider)
        assert outcome.match.linkedin_url == ""

    def test_the_note_says_why_it_was_delivered(self, store, settings) -> None:
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        _, outcome = self._run(store, settings, provider)
        assert "contact route" in outcome.match.notes
        assert outcome.best_route_type in {r.type.value for r in outcome.routes}

    def test_acceptance_can_be_turned_off(self, store, settings) -> None:
        settings.accept_on_contact_route = False
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        _, outcome = self._run(store, settings, provider)
        assert outcome.match.decision is not Decision.CONTACT_ROUTE_ONLY

    def test_a_search_failure_is_never_converted_into_a_delivery(
        self, store, settings
    ) -> None:
        """An infrastructure fault must stay a fault, so the row is retried."""
        provider = _ScriptedProvider(settings.provider, {}, fail=True)
        _, outcome = self._run(store, settings, provider)
        assert outcome.match.decision is Decision.BLANK_ERROR
        assert not outcome.delivered

    def test_routes_are_collected_even_when_the_company_has_no_linkedin_presence(
        self, store, settings
    ) -> None:
        """The negative cache stops the *person* ladder, not the whole row."""
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        runner, outcome = self._run(store, settings, provider)
        assert runner.negative_cache_skips == 1, "no roster hit, so the ladder is skipped"
        assert outcome.routes, "but the row still carries the company's routes"

    def test_an_inconclusive_roster_does_not_act_as_a_proven_absence(
        self, store, settings
    ) -> None:
        """Tri-state footprint. A roster that could not be read is 'unknown', and
        unknown must run the person ladder rather than inherit the behaviour of a
        company proven to have no presence."""
        provider = _ScriptedProvider(settings.provider,
                                     {"official website contact": [CONTACT_PAGE]})
        provider.empty_means_absent = False   # HTML scraping: empty is ambiguous
        runner, outcome = self._run(store, settings, provider)

        assert runner.negative_cache_skips == 0, "unknown is not an absence"
        assert runner.tier2_queries > 0, "the ladder must still be worked"
        assert store.get_company("greytip-software") is None, (
            "and nothing is cached from an unreadable roster"
        )


class TestPersistence:
    def test_routes_are_written_and_read_back(self, store) -> None:
        store.insert_records([{
            "row_uid": "r1", "source_file": "f.csv", "row_index": 1,
            "name": "A B", "company": "C Private Limited",
        }])
        store.add_contact_routes("r1", [
            {"type": "investor_relations", "value": "ir@c.com",
             "source_url": "https://c.com/investors", "label": "IR page",
             "scope": "company", "rank": 2},
        ])
        rows = store.routes_for_row("r1")
        assert len(rows) == 1
        assert rows[0]["value"] == "ir@c.com"
        assert store.rows_with_routes() == 1
        assert store.route_type_counts() == {"investor_relations": 1}

    def test_rewriting_the_same_route_does_not_duplicate_it(self, store) -> None:
        """A deferred row that is retried must not accumulate copies."""
        store.insert_records([{
            "row_uid": "r1", "source_file": "f.csv", "row_index": 1,
            "name": "A B", "company": "C Private Limited",
        }])
        payload = [{"type": "reception", "value": "info@c.com",
                    "source_url": "https://c.com", "rank": 5}]
        store.add_contact_routes("r1", payload)
        store.add_contact_routes("r1", payload)
        assert len(store.routes_for_row("r1")) == 1

    def test_delivered_prospects_include_route_only_rows(self, store) -> None:
        store.insert_records([
            {"row_uid": "r1", "source_file": "f.csv", "row_index": 1,
             "name": "A B", "company": "C Private Limited"},
            {"row_uid": "r2", "source_file": "f.csv", "row_index": 2,
             "name": "D E", "company": "F Private Limited"},
            {"row_uid": "r3", "source_file": "f.csv", "row_index": 3,
             "name": "G H", "company": "I Private Limited"},
        ])
        store.write_result("r1", decision="matched", confidence=0.97,
                           linkedin_url="https://www.linkedin.com/in/ab")
        store.write_result("r2", decision="contact_route_only", route_count=2,
                           best_route_type="investor_relations")
        store.write_result("r3", decision="blank_no_candidate")

        uids = [row["row_uid"] for row in store.delivered_prospects()]
        assert uids == ["r1", "r2"], "profiles first, then route-only rows"
        assert store.delivered_count() == 2

    def test_a_route_only_row_with_no_routes_is_not_delivered(self, store) -> None:
        store.insert_records([{
            "row_uid": "r1", "source_file": "f.csv", "row_index": 1,
            "name": "A B", "company": "C Private Limited"}])
        store.write_result("r1", decision="contact_route_only", route_count=0)
        assert store.delivered_count() == 0


class TestTargetCountsDeliverables:
    def test_progress_counts_both_kinds(self) -> None:
        progress = RunProgress()

        class _Outcome:
            def __init__(self, decision, routes):
                self.match = type("M", (), {
                    "decision": decision, "matched": decision is Decision.MATCHED,
                    "confidence": 0.97,
                })()
                self.routes = routes

        progress.record(_Outcome(Decision.MATCHED, [1]))
        progress.record(_Outcome(Decision.CONTACT_ROUTE_ONLY, [1, 2]))
        progress.record(_Outcome(Decision.BLANK_NO_CANDIDATE, []))

        assert progress.matched == 1
        assert progress.contact_only == 1
        assert progress.delivered == 2
        assert progress.with_routes == 2
        assert progress.routes_found == 3
