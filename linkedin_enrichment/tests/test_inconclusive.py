"""An unverifiable search must never be recorded as an absence.

This is the most consequential failure mode in the pipeline. The negative cache
turns one company-level answer into a decision for every person at that company.
If a throttled or unparseable response is mistaken for "this company has no
LinkedIn presence", a single bad HTTP response permanently blanks everyone there
— and the output looks like a finding rather than a fault.

The rule under test: only a backend that can distinguish "no results" from "we
failed to read the page" is allowed to establish an absence.
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_enrichment.cache.company_cache import CompanyCache
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.identity.scorer import Decision
from linkedin_enrichment.providers.base import (
    FailureKind,
    Outcome,
    SearchProvider,
    SearchResponse,
)
from linkedin_enrichment.search.client import SearchClient
from linkedin_enrichment.cache.serp_cache import SerpCache
from linkedin_enrichment.workers.runner import LadderRunner


class _StubProvider(SearchProvider):
    """Returns a fixed response; ``empty_means_absent`` is the variable under test."""

    name = "stub"

    def __init__(self, config, *, authoritative: bool, response: SearchResponse):
        super().__init__(config)
        self.empty_means_absent = authoritative
        self._response = response
        self.calls = 0

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        self.calls += 1
        return SearchResponse(
            query=query, provider=self.name,
            results=list(self._response.results),
            error=self._response.error,
            failure_kind=self._response.failure_kind,
            http_status=self._response.http_status,
        )


def _record(store: Store) -> dict:
    store.insert_records([{
        "row_uid": "r1", "source_file": "f.csv", "row_index": 1,
        "name": "Girish Rowjee", "company": "Greytip Software Private Limited",
        "company_key": "greytip-software", "location": "Bangalore",
    }])
    return store.claim_batch(1)[0]


def _runner(store, settings, provider):
    client = SearchClient(provider, SerpCache(store, 30), settings)
    return LadderRunner(client, CompanyCache(store), settings), client


class TestNegativeCacheTrust:
    def test_html_backend_empty_does_not_establish_absence(self, store, settings) -> None:
        """HTML scraping cannot tell 'no results' from 'parse failed'."""
        provider = _StubProvider(
            settings.provider, authoritative=False,
            response=SearchResponse(query="", results=[]),
        )
        runner, _ = _runner(store, settings, provider)
        record = _record(store)

        outcome = asyncio.run(runner.resolve_row(record))

        assert outcome.match.decision is Decision.BLANK_ERROR
        assert "inconclusive" in outcome.match.notes.lower()
        # Crucially: nothing was written to the company cache.
        assert store.get_company("greytip-software") is None
        assert runner.unverified_company_skips == 1

    def test_json_backend_empty_does_establish_absence(self, store, settings) -> None:
        """A JSON API returns an explicit empty array — that IS authoritative."""
        provider = _StubProvider(
            settings.provider, authoritative=True,
            response=SearchResponse(query="", results=[]),
        )
        runner, _ = _runner(store, settings, provider)
        record = _record(store)

        outcome = asyncio.run(runner.resolve_row(record))

        assert outcome.match.decision is Decision.BLANK_NO_CANDIDATE
        cached = store.get_company("greytip-software")
        assert cached is not None
        assert cached["has_linkedin_footprint"] == 0

    def test_errored_lookup_never_establishes_absence(self, store, settings) -> None:
        provider = _StubProvider(
            settings.provider, authoritative=True,
            response=SearchResponse(
                query="", results=[], error="blocked by network egress policy",
                failure_kind=FailureKind.PROXY_POLICY, http_status=403,
            ),
        )
        runner, _ = _runner(store, settings, provider)
        record = _record(store)

        outcome = asyncio.run(runner.resolve_row(record))

        assert outcome.match.decision is Decision.BLANK_ERROR
        assert store.get_company("greytip-software") is None


class TestOutcomeSemantics:
    def test_empty_response_is_inconclusive_not_success(self) -> None:
        assert SearchResponse(query="q").outcome is Outcome.INCONCLUSIVE

    def test_error_response_is_failed(self) -> None:
        assert SearchResponse(query="q", error="boom").outcome is Outcome.FAILED

    def test_populated_response_is_success(self) -> None:
        from linkedin_enrichment.providers.base import SerpResult
        response = SearchResponse(
            query="q", results=[SerpResult(title="t", url="https://x/in/y")]
        )
        assert response.outcome is Outcome.SUCCESS


class TestPolicyDenialDetection:
    """A firewall 403 and an anti-bot 403 need opposite responses, so they must
    never be conflated. Both strings below are verbatim from real gateways."""

    @pytest.mark.parametrize("body,headers", [
        ("Host not in allowlist: html.duckduckgo.com. Add this host to your "
         "network egress settings to allow access.", None),
        ("request rejected: host not permitted", None),
        ("anything at all", {"x-deny-reason": "host_not_allowed"}),
    ])
    def test_policy_denial_detected(self, body, headers) -> None:
        from linkedin_enrichment.providers.public_search import looks_like_policy_denial
        assert looks_like_policy_denial(body, headers)

    @pytest.mark.parametrize("body", [
        "Our systems have detected unusual traffic from your computer network.",
        "Please complete the CAPTCHA to continue.",
    ])
    def test_real_anti_bot_not_misread_as_policy(self, body) -> None:
        from linkedin_enrichment.providers.public_search import looks_like_policy_denial
        assert not looks_like_policy_denial(body, {})
