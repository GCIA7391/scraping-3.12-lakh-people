"""Preflight verdicts and failure classification.

Preflight exists to stop a specific expensive mistake: a run where searches
silently return nothing produces 312,160 blanks that look like a finding. So the
three verdicts must be distinct and the classification must point at the real
remedy — telling an operator "the search engine is refusing automation" when a
firewall blocked the host costs a day of rotating user-agents against a wall.
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_enrichment.providers.base import (
    FailureKind,
    Outcome,
    ProviderError,
    SearchProvider,
    SearchResponse,
    SerpResult,
    classify_exception,
)
from linkedin_enrichment.search import preflight as pf


class _Stub(SearchProvider):
    name = "stub"

    def __init__(self, config, response=None, raises=None):
        super().__init__(config)
        self._response = response
        self._raises = raises

    async def search(self, query: str, *, limit: int = 10) -> SearchResponse:
        if self._raises is not None:
            raise self._raises
        return self._response


def _report(settings, provider) -> pf.BackendReport:
    return asyncio.run(pf._probe_generic_provider(provider, settings, deep=False))


class TestVerdicts:
    def test_success_requires_the_canary(self, settings) -> None:
        provider = _Stub(settings.provider, SearchResponse(
            query=pf.CANARY_QUERY, provider="stub",
            results=[SerpResult(
                title="Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                url="https://www.linkedin.com/in/girishrowjee/",
            )],
        ))
        entry = _report(settings, provider)
        assert entry.outcome is Outcome.SUCCESS
        assert entry.canary_ok

    def test_results_without_the_canary_are_inconclusive(self, settings) -> None:
        """Results came back, but not the one with a known answer. Something is
        wrong with the index or the query — not a pass."""
        provider = _Stub(settings.provider, SearchResponse(
            query=pf.CANARY_QUERY, provider="stub",
            results=[SerpResult(title="Unrelated", url="https://example.com/page")],
        ))
        entry = _report(settings, provider)
        assert entry.outcome is Outcome.INCONCLUSIVE
        assert not entry.canary_ok

    def test_empty_is_inconclusive_not_success(self, settings) -> None:
        provider = _Stub(settings.provider, SearchResponse(
            query=pf.CANARY_QUERY, provider="stub", results=[]
        ))
        assert _report(settings, provider).outcome is Outcome.INCONCLUSIVE

    def test_error_is_failed(self, settings) -> None:
        provider = _Stub(settings.provider, raises=ProviderError(
            "blocked", kind=FailureKind.PROXY_POLICY
        ))
        entry = _report(settings, provider)
        assert entry.outcome is Outcome.FAILED
        assert entry.failure_kind is FailureKind.PROXY_POLICY


class TestExitCodes:
    """0 = go, 1 = stop, 2 = do not trust the results."""

    def _make(self, *outcomes: Outcome) -> pf.PreflightReport:
        report = pf.PreflightReport(provider="stub")
        for index, outcome in enumerate(outcomes):
            report.backends.append(pf.BackendReport(backend=f"b{index}", outcome=outcome))
        return report

    def test_any_success_exits_zero(self) -> None:
        assert self._make(Outcome.FAILED, Outcome.SUCCESS).exit_code == 0

    def test_all_failed_exits_one(self) -> None:
        assert self._make(Outcome.FAILED, Outcome.FAILED).exit_code == 1

    def test_inconclusive_exits_two(self) -> None:
        assert self._make(Outcome.FAILED, Outcome.INCONCLUSIVE).exit_code == 2

    def test_summaries_are_actionable(self) -> None:
        assert "safe to run" in self._make(Outcome.SUCCESS).summary
        assert "DO NOT run" in self._make(Outcome.FAILED).summary
        assert "DO NOT run in bulk" in self._make(Outcome.INCONCLUSIVE).summary


class TestExceptionClassification:
    @pytest.mark.parametrize("exc,expected", [
        (ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 8080)"),
         FailureKind.CONNECTION_REFUSED),
        (OSError("CONNECT tunnel failed, response 403"), FailureKind.PROXY_POLICY),
        (OSError("request rejected: host not permitted"), FailureKind.PROXY_POLICY),
        (OSError("certificate verify failed"), FailureKind.TLS),
        (OSError("Temporary failure in name resolution"), FailureKind.DNS),
        (TimeoutError("timed out"), FailureKind.TIMEOUT),
    ])
    def test_classification(self, exc: BaseException, expected: FailureKind) -> None:
        kind, detail = classify_exception(exc)
        assert kind is expected
        assert detail


class TestRemedies:
    def test_every_failure_kind_has_advice(self) -> None:
        """An unclassified failure leaves the operator with nothing to do."""
        for kind in FailureKind:
            if kind is FailureKind.NONE:
                continue
            assert pf.REMEDIES.get(kind), f"no remedy text for {kind.value}"

    def test_proxy_policy_advice_does_not_suggest_a_provider_change(self) -> None:
        """The wrong advice here is the expensive one — no user-agent or provider
        switch gets through a network allowlist."""
        text = pf.REMEDIES[FailureKind.PROXY_POLICY].lower()
        assert "allowlist" in text
        assert "not a code problem" in text

    def test_empty_results_advice_warns_against_bulk(self) -> None:
        assert "not" in pf.REMEDIES[FailureKind.EMPTY_RESULTS].lower()


class TestRendering:
    def test_render_includes_verdict_and_classification(self, settings) -> None:
        report = pf.PreflightReport(provider="stub")
        report.backends.append(pf.BackendReport(
            backend="ddg_html", url="https://html.duckduckgo.com/html/",
            outcome=Outcome.FAILED, failure_kind=FailureKind.PROXY_POLICY,
            http_status=403, error="blocked by network egress policy",
        ))
        text = pf.render(report)
        assert "PREFLIGHT" in text
        assert "proxy_policy" in text
        assert "403" in text
        assert "exit code 1" in text
