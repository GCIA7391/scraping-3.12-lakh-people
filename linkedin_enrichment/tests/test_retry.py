"""Row-level retry of transient failures.

Regression suite for a real defect: rows whose search failed were written with
the note "this row will be retried" and then marked ``done``. Nothing reclaimed
them, so the retry never happened and the note was false in customer-facing
output. Rows that threw were stranded in ``error``, which nothing reclaimed
either.

The distinction these tests protect: a *terminal* decision means "we searched and
this is the answer"; a *transient* failure means "we did not get an answer". Only
the first may be written as a result.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from linkedin_enrichment.database.store import Store
from linkedin_enrichment.identity.scorer import Decision, MatchResult
from linkedin_enrichment.workers.pool import RunProgress, WorkerPool
from linkedin_enrichment.workers.runner import RowOutcome


def seed(store: Store, count: int = 1) -> None:
    store.insert_records([
        {
            "row_uid": f"r{i}", "source_file": "f.csv", "row_index": i,
            "name": "Girish Rowjee", "company": "Greytip Software Private Limited",
            "company_key": "greytip-software", "location": "Bangalore",
        }
        for i in range(count)
    ])


def make_pool(store: Store, settings) -> WorkerPool:
    runner = SimpleNamespace(client=SimpleNamespace(provider=SimpleNamespace(name="stub")))
    return WorkerPool(store, runner, settings, RunProgress())


class TestTransientFailureIsDeferred:
    def test_search_error_is_deferred_not_done(self, store: Store, settings) -> None:
        """THE regression test. This fails against the buggy implementation."""
        seed(store)
        store.claim_batch(1)
        pool = make_pool(store, settings)

        pool._persist(RowOutcome("r0", MatchResult(
            Decision.BLANK_ERROR, notes="search failed"), error="backend 503"))

        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "deferred"
        # No result is written while attempts remain — the row is unanswered.
        assert store.get_result("r0") is None
        assert pool.progress.deferred == 1

    def test_deferred_row_is_requeued_on_the_next_pass(self, store: Store, settings) -> None:
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(
            RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="boom")
        )

        assert store.requeue_deferred(settings.max_row_attempts) == 1
        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "pending"
        assert len(store.claim_batch(10)) == 1, "requeued row must be claimable"

    def test_attempt_budget_is_enforced_and_terminates(self, store: Store, settings) -> None:
        """The loop must close: no infinite retrying."""
        settings.max_row_attempts = 3
        seed(store)
        pool = make_pool(store, settings)

        for _ in range(10):  # far more passes than the budget
            store.requeue_deferred(settings.max_row_attempts)
            if not store.claim_batch(1):
                break
            pool._persist(RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="boom"))

        assert store.scalar("SELECT attempts FROM records WHERE row_uid='r0'") == 3
        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "done"
        assert store.requeue_deferred(settings.max_row_attempts) == 0

    def test_exhausted_row_gets_an_honest_note(self, store: Store, settings) -> None:
        """No promise of a retry that will not come."""
        settings.max_row_attempts = 1
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(
            RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="HTTP 403 blocked")
        )

        result = store.get_result("r0")
        assert result["decision"] == "blank_error"
        assert "after 1 attempt" in result["notes"]
        assert "HTTP 403 blocked" in result["notes"]
        assert "will be retried" not in result["notes"]

    def test_worker_exception_is_deferred_not_stranded(self, store: Store, settings) -> None:
        """Previously set status='error', which nothing ever reclaimed."""
        seed(store)
        store.claim_batch(1)
        store.defer_row("r0", error="ValueError: boom",
                        max_attempts=settings.max_row_attempts)

        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "deferred"
        assert store.requeue_deferred(settings.max_row_attempts) == 1


class TestTerminalDecisionsAreNeverDeferred:
    @pytest.mark.parametrize("decision", [
        Decision.MATCHED,
        Decision.BLANK_NO_CANDIDATE,
        Decision.BLANK_LOW_CONFIDENCE,
        Decision.BLANK_AMBIGUOUS,
        Decision.BLANK_SKIPPED,
    ])
    def test_terminal_decision_is_written_immediately(
        self, store: Store, settings, decision: Decision
    ) -> None:
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(RowOutcome("r0", MatchResult(decision)))

        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "done"
        assert store.get_result("r0") is not None


class TestRetryCommand:
    def test_requeue_failed_picks_up_deferred(self, store: Store, settings) -> None:
        seed(store, 2)
        store.claim_batch(2)
        pool = make_pool(store, settings)
        for uid in ("r0", "r1"):
            pool._persist(RowOutcome(uid, MatchResult(Decision.BLANK_ERROR), error="boom"))

        assert store.requeue_failed() == 2
        assert store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'") == 2

    def test_reset_requeues_exhausted_rows(self, store: Store, settings) -> None:
        """After fixing the real cause, rows that used up their budget against a
        problem that no longer exists must be reprocessable."""
        settings.max_row_attempts = 1
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(
            RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="egress blocked")
        )
        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "done"

        assert store.requeue_failed(reset_attempts=True) == 1
        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "pending"
        assert store.scalar("SELECT attempts FROM records WHERE row_uid='r0'") == 0

    def test_requeue_is_a_noop_when_nothing_failed(self, store: Store, settings) -> None:
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(RowOutcome("r0", MatchResult(Decision.MATCHED)))
        assert store.requeue_failed() == 0


class TestUnresolvedOutputLines:
    def test_deferred_rows_explain_themselves_in_the_output(
        self, store: Store, settings
    ) -> None:
        seed(store)
        store.claim_batch(1)
        make_pool(store, settings)._persist(
            RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="boom")
        )

        assert store.record_unresolved_results() == 1
        notes = store.get_result("r0")["notes"]
        assert "queued for retry" in notes
        assert "main.py retry" in notes


class TestEventualSuccess:
    """The loop must actually close: a row that fails then succeeds ends matched."""

    def test_row_recovers_across_passes(self, store: Store, settings) -> None:
        seed(store)
        pool = make_pool(store, settings)

        # Pass 1 and 2 fail transiently.
        for _ in range(2):
            store.requeue_deferred(settings.max_row_attempts)
            claimed = store.claim_batch(1)
            assert claimed, "row must still be available"
            pool._persist(RowOutcome("r0", MatchResult(Decision.BLANK_ERROR), error="throttled"))

        # Pass 3 succeeds.
        store.requeue_deferred(settings.max_row_attempts)
        assert store.claim_batch(1), "row must be claimable for the final attempt"
        pool._persist(RowOutcome("r0", MatchResult(
            Decision.MATCHED, linkedin_url="https://www.linkedin.com/in/girishrowjee",
            confidence=0.9959,
        )))

        assert store.scalar("SELECT status FROM records WHERE row_uid='r0'") == "done"
        result = store.get_result("r0")
        assert result["decision"] == "matched"
        assert result["linkedin_url"] == "https://www.linkedin.com/in/girishrowjee"


class TestTrustworthiness:
    def _report(self, **kwargs):
        from linkedin_enrichment.output.report import QCReport
        return QCReport(**kwargs)

    def test_clean_run_is_trustworthy(self) -> None:
        report = self._report(queries_issued=1000, inconclusive_searches=10, deferred_rows=0)
        assert report.trustworthy

    def test_high_inconclusive_rate_is_not_trustworthy(self) -> None:
        report = self._report(queries_issued=1000, inconclusive_searches=850, deferred_rows=0)
        assert not report.trustworthy
        assert report.inconclusive_rate == pytest.approx(0.85)

    def test_any_deferred_rows_make_a_run_untrustworthy(self) -> None:
        assert not self._report(queries_issued=1000, inconclusive_searches=0,
                                deferred_rows=5).trustworthy

    def test_warning_text_tells_the_operator_what_to_do(self) -> None:
        from linkedin_enrichment.output.report import render_text
        text = render_text(self._report(
            queries_issued=1000, inconclusive_searches=850, deferred_rows=12
        ))
        assert "RUN TRUSTWORTHINESS" in text
        assert "NOT evidence" in text
        assert "main.py preflight" in text
        assert "main.py retry" in text

    def test_clean_run_says_so_without_alarm(self) -> None:
        from linkedin_enrichment.output.report import render_text
        text = render_text(self._report(queries_issued=1000, inconclusive_searches=0))
        assert "OK —" in text
        assert "WARNING" not in text
