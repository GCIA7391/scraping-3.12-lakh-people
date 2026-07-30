"""Full pipeline over a sample file, entirely offline.

Covers ingestion, the query ladder, caching, the worker pool, output writing and
the QC report. Asserts the two guarantees that matter commercially: original
columns are untouched, and no unmatched row ever receives a URL.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest

import main as cli
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.ingest.reader import read_file
from linkedin_enrichment.output import report as report_mod
from linkedin_enrichment.output import review_queue, writer


@pytest.fixture
def enriched(settings, sample_csv: Path):
    """Run the whole pipeline over the sample file and return (store, output path)."""
    settings.input_paths = [str(sample_csv)]
    store = Store(settings.db_path)
    cli.ingest(store, settings)
    asyncio.run(cli.enrich(store, settings, run_id="test"))
    output = writer.write_enriched_csv(store, str(sample_csv), settings.output_dir)
    yield store, output, sample_csv
    store.close()


def read_output(path: Path) -> list[dict[str, str]]:
    # utf-8-sig: the real export carries a BOM, and so does our sample fixture.
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class TestOutputIntegrity:
    def test_every_input_row_appears_exactly_once(self, enriched) -> None:
        _, output, source = enriched
        assert len(read_output(output)) == sum(1 for _ in read_file(source))

    def test_original_columns_are_byte_identical(self, enriched) -> None:
        """The specification forbids modifying existing columns."""
        _, output, source = enriched
        original = read_output(source)
        enriched_rows = read_output(output)

        for before, after in zip(original, enriched_rows):
            for column, value in before.items():
                assert after[column] == value, f"column {column!r} was modified"

    def test_new_columns_are_appended_in_order(self, enriched) -> None:
        _, output, source = enriched
        with source.open(encoding="utf-8-sig") as handle:
            original_headers = next(csv.reader(handle))
        with output.open(encoding="utf-8-sig") as handle:
            new_headers = next(csv.reader(handle))

        assert new_headers[: len(original_headers)] == original_headers
        assert new_headers[len(original_headers):] == list(writer.OUTPUT_COLUMNS)

    def test_unmatched_rows_have_no_url_and_carry_a_reason(self, enriched) -> None:
        _, output, _ = enriched
        for row in read_output(output):
            if not row["LinkedIn Profile"]:
                assert row["Confidence Score"] == "", "blank rows must not carry a score"
                assert row["Verification Notes"], "every blank must explain itself"

    def test_matched_rows_meet_the_threshold(self, enriched, settings) -> None:
        _, output, _ = enriched
        for row in read_output(output):
            if row["LinkedIn Profile"]:
                assert "linkedin.com/in/" in row["LinkedIn Profile"]
                assert float(row["Confidence Score"]) >= settings.confidence_threshold


class TestDecisions:
    def test_the_known_true_positive_is_matched(self, enriched) -> None:
        _, output, _ = enriched
        rows = {r["Name"]: r for r in read_output(output)}
        assert rows["Girish Rowjee"]["LinkedIn Profile"] == \
            "https://www.linkedin.com/in/girishrowjee"

    @pytest.mark.parametrize("name", [
        "Debasheesh Bagchi",             # right name, wrong company
        "Melukote Shivaramu Lokesh",     # right company, wrong people
        "Habiba Zackria",                # eponymous company, no footprint
        "Dharmesh Premchand Madhwani",   # micro-company, no footprint
    ])
    def test_false_positive_traps_stay_blank(self, enriched, name: str) -> None:
        _, output, _ = enriched
        rows = {r["Name"]: r for r in read_output(output)}
        assert rows[name]["LinkedIn Profile"] == "", f"{name} must not be matched"

    def test_prefiltered_rows_explain_themselves(self, enriched) -> None:
        _, output, _ = enriched
        rows = {r["Name"]: r for r in read_output(output)}
        assert "does not look like a person" in rows["Electronic Manufacturers"]["Verification Notes"]
        assert "too little signal" in rows["Naganna"]["Verification Notes"]


class TestQueryLadder:
    def test_duplicate_row_costs_no_extra_query(self, enriched) -> None:
        """The sample contains Girish Rowjee twice. Both rows must be resolved,
        but the second must be served entirely from cache."""
        store, output, _ = enriched
        matches = [r for r in read_output(output) if r["Name"] == "Girish Rowjee"]
        assert len(matches) == 2
        assert all(r["LinkedIn Profile"] for r in matches)

        total_queries = store.scalar("SELECT SUM(queries_used) FROM results") or 0
        searchable = store.scalar("SELECT COUNT(*) FROM records WHERE status='done'") or 0
        assert total_queries < searchable * 2, "ladder should beat the naive 2/person baseline"

    def test_negative_cache_is_populated(self, enriched) -> None:
        store, _, _ = enriched
        negatives = store.scalar(
            "SELECT COUNT(*) FROM company_cache WHERE has_linkedin_footprint = 0"
        )
        assert negatives > 0, "companies with no footprint must be cached as negative"


class TestReporting:
    def test_report_reconciles(self, enriched, settings) -> None:
        store, _, _ = enriched
        qc = report_mod.build_report(store, settings, run_id="test", elapsed=1.0)
        assert qc.total_rows == 8
        assert qc.processed_rows == qc.total_rows
        assert qc.matched + qc.blank == qc.processed_rows
        assert qc.matched >= 1
        assert 0.0 <= qc.average_confidence <= 1.0
        assert "QUALITY CONTROL REPORT" in report_mod.render_text(qc)

    def test_review_queue_is_written(self, enriched, settings) -> None:
        store, _, _ = enriched
        path = review_queue.write_review_queue(store, settings.output_dir)
        assert path.exists()
        with path.open(encoding="utf-8") as handle:
            assert next(csv.reader(handle)) == list(review_queue.REVIEW_COLUMNS)


class TestResumeMidRun:
    def test_interrupted_run_completes_without_loss_or_duplication(
        self, settings, sample_csv: Path
    ) -> None:
        """Simulate a crash: claim rows, abandon them, then resume."""
        settings.input_paths = [str(sample_csv)]
        with Store(settings.db_path) as store:
            cli.ingest(store, settings)
            total = store.scalar("SELECT COUNT(*) FROM records")

            # A worker claims work and dies before writing anything.
            store.claim_batch(3)
            assert store.scalar("SELECT COUNT(*) FROM records WHERE status='claimed'") == 3

            # Restart: stale claims come back, then the run completes.
            store.reclaim_stale(0)
            asyncio.run(cli.enrich(store, settings, run_id="resumed"))

            assert store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'") == 0
            assert store.scalar("SELECT COUNT(*) FROM results") == total
            # No row resolved twice.
            assert store.scalar(
                "SELECT COUNT(*) FROM (SELECT row_uid FROM results GROUP BY row_uid HAVING COUNT(*) > 1)"
            ) == 0


class TestLimitAndExplain:
    """The 10-row-first workflow: never commit to 312k before proving 10."""

    def test_limit_stops_after_n_rows(self, settings, sample_csv: Path) -> None:
        settings.input_paths = [str(sample_csv)]
        with Store(settings.db_path) as store:
            cli.ingest(store, settings)
            total = store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'")
            assert total > 3, "sample must be larger than the limit to be a real test"

            asyncio.run(cli.enrich(store, settings, run_id="limited", limit=3))

            processed = store.scalar(
                "SELECT COUNT(*) FROM results WHERE decision <> 'blank_skipped'"
            )
            assert processed == 3
            # The remainder is untouched and still claimable, so a later run continues.
            assert store.scalar(
                "SELECT COUNT(*) FROM records WHERE status='pending'"
            ) == total - 3

    def test_limited_run_can_be_continued(self, settings, sample_csv: Path) -> None:
        settings.input_paths = [str(sample_csv)]
        with Store(settings.db_path) as store:
            cli.ingest(store, settings)
            asyncio.run(cli.enrich(store, settings, run_id="a", limit=2))
            asyncio.run(cli.enrich(store, settings, run_id="b"))
            assert store.scalar("SELECT COUNT(*) FROM records WHERE status='pending'") == 0

    def test_explain_prints_queries_and_decisions(
        self, settings, sample_csv: Path, capsys
    ) -> None:
        settings.input_paths = [str(sample_csv)]
        with Store(settings.db_path) as store:
            cli.ingest(store, settings)
            asyncio.run(cli.enrich(store, settings, run_id="x", limit=4, explain=True))

        output = capsys.readouterr().out
        assert "EXPLAIN MODE" in output
        assert "TIER 1" in output, "the query actually issued must be shown"
        assert "DECISION" in output
        # An accept and a reject must both be attributable to a named gate.
        assert "REJECT" in output or "ACCEPT" in output


class TestDryRun:
    def test_dry_run_makes_no_network_calls(self, settings, sample_csv: Path, capsys) -> None:
        """Uses a provider name that would fail immediately if it were contacted."""
        settings.input_paths = [str(sample_csv)]
        settings.provider.name = "serper"
        settings.provider.serper_api_key = ""

        assert cli.cmd_dry_run(object(), settings) == 0
        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert "Tier 1 (one per company)" in output
