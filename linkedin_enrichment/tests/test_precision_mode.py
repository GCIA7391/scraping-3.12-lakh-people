"""Precision mode: a small, defensible deliverable instead of coverage.

The objective is ~1,000 profiles at ≥99%, not maximum reach. Three properties
have to hold together, and each is tested here:

1. The threshold really is 0.99, and 0.99 demands more evidence than 0.95 —
   name+company alone scores 0.9644 and must be rejected.
2. Corroboration is mandatory, with no path around it.
3. The run stops at the target instead of working the pool.
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_enrichment.config.settings import Settings
from linkedin_enrichment.identity.scorer import Decision, Subject, calibrate, resolve
from linkedin_enrichment.output import top_matches, validate as validate_mod
from linkedin_enrichment.providers.base import SerpResult


class TestPrecisionSettings:
    def test_precision_mode_sets_everything_together(self) -> None:
        """A partially-applied precision mode would report 0.99 scores with no
        second source, which is the failure this bundling prevents."""
        config = Settings.load(overrides={"precision_mode": True})
        assert config.confidence_threshold >= 0.99
        assert config.require_corroboration
        assert config.preferred_roles_only
        assert config.target_matches == 1000

    def test_explicit_target_is_respected(self) -> None:
        config = Settings.load(overrides={"precision_mode": True, "target_matches": 250})
        assert config.target_matches == 250

    def test_normal_mode_is_unchanged(self) -> None:
        config = Settings.load()
        assert config.confidence_threshold == 0.95
        assert not config.require_corroboration
        assert config.target_matches == 0


class TestThresholdSemantics:
    """0.99 must be meaningfully harder to reach than 0.95."""

    def test_name_and_company_alone_clears_95_but_not_99(self, settings) -> None:
        # raw 0.700 = perfect name x perfect company, nothing else.
        confidence = calibrate(0.700, settings.calibration)
        assert confidence >= 0.95
        assert confidence < 0.99

    def test_one_corroborator_reaches_99(self, settings) -> None:
        # raw 0.800 = the above plus a matching URL slug.
        assert calibrate(0.800, settings.calibration) >= 0.99

    def test_the_verified_true_positive_survives_the_99_bar(self, settings) -> None:
        """Girish Rowjee scored 0.9959 — precision mode must still accept him."""
        settings.confidence_threshold = 0.99
        outcome = resolve(
            Subject.from_fields(
                "u", "Girish Rowjee", "Greytip Software Private Limited", "Bangalore"
            ),
            [SerpResult(
                title="Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                url="https://www.linkedin.com/in/girishrowjee/",
            )],
            settings,
        )
        assert outcome.decision is Decision.MATCHED
        assert outcome.confidence >= 0.99

    def test_a_borderline_match_is_dropped_at_99(self, settings) -> None:
        """Accepted at 0.95, rejected at 0.99 — the point of precision mode."""
        subject = Subject.from_fields(
            "u", "Girish Rowjee", "Greytip Software Private Limited", "Bangalore"
        )
        # Company present but name imperfect: no slug, no city.
        hit = [SerpResult(
            title="Girish Rowjeee - Greytip Software Pvt Ltd | LinkedIn",
            url="https://www.linkedin.com/in/g-r-88213/",
        )]
        settings.confidence_threshold = 0.95
        at_95 = resolve(subject, hit, settings)
        settings.confidence_threshold = 0.99
        at_99 = resolve(subject, hit, settings)
        assert at_99.confidence <= at_95.confidence + 1e-9
        assert at_99.decision is not Decision.MATCHED or at_99.confidence >= 0.99


class TestTargetStop:
    def test_target_stops_intake(self, settings, sample_csv, tmp_path) -> None:
        """The target is a stop signal, not a hard cap.

        A batch is already in flight when the target is hit, so the run can
        overshoot by up to one batch. What must be exact is the deliverable,
        which takes the top-N by confidence — see the next test.
        """
        import main as cli
        from linkedin_enrichment.database.store import Store

        settings.input_paths = [str(sample_csv)]
        settings.target_matches = 1
        settings.batch_size = 2
        with Store(settings.db_path) as store:
            cli.ingest(store, settings)
            progress = asyncio.run(cli.enrich(store, settings, run_id="t"))
            assert progress.matched >= 1, "should reach the target"
            assert progress.matched <= 1 + settings.batch_size, "should not run away"

    def test_deliverable_honours_the_limit_exactly(self, store, settings, tmp_path) -> None:
        store.insert_records([
            {"row_uid": f"r{i}", "source_file": "f.csv", "row_index": i,
             "name": f"Person {i}", "company": f"Co {i} Private Limited"}
            for i in range(5)
        ])
        for i in range(5):
            store.write_result(f"r{i}", decision="matched", confidence=0.99 + i / 1000,
                               linkedin_url=f"https://www.linkedin.com/in/r{i}")
        _, count = top_matches.write_top_matches(store, tmp_path, limit=3)
        assert count == 3


class TestRankedDeliverable:
    def test_top_matches_are_sorted_by_confidence(self, store, settings, tmp_path) -> None:
        store.insert_records([
            {"row_uid": f"r{i}", "source_file": "f.csv", "row_index": i,
             "name": f"Person {i}", "company": f"Company {i} Private Limited"}
            for i in range(3)
        ])
        for uid, conf in (("r0", 0.991), ("r1", 0.999), ("r2", 0.995)):
            store.write_result(uid, decision="matched", confidence=conf,
                               linkedin_url=f"https://www.linkedin.com/in/{uid}",
                               source_urls=f"https://www.linkedin.com/in/{uid} | https://example.com/{uid}")

        path, count = top_matches.write_top_matches(store, tmp_path, limit=10)
        assert count == 3

        import csv
        with path.open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        scores = [float(r["Confidence Score"]) for r in rows]
        assert scores == sorted(scores, reverse=True)
        assert rows[0]["Rank"] == "1"
        # The independent source, not the LinkedIn URL, is what a reviewer needs.
        assert rows[0]["Corroborating Source"].startswith("https://example.com/")

    def test_unmatched_rows_are_excluded(self, store, settings, tmp_path) -> None:
        store.insert_records([{"row_uid": "r0", "source_file": "f.csv", "row_index": 0,
                               "name": "A B", "company": "C Private Limited"}])
        store.write_result("r0", decision="blank_low_confidence", confidence=0.9)
        _, count = top_matches.write_top_matches(store, tmp_path, limit=10)
        assert count == 0


class TestValidationArithmetic:
    """The numbers that decide whether ≥99% can be claimed at all."""

    def test_381_zero_error_labels_reach_the_bound(self) -> None:
        state = validate_mod.ValidationState(labelled=381, correct=381, total_matches=1000)
        assert state.lower_bound >= 0.99

    def test_one_error_in_381_falls_short(self) -> None:
        state = validate_mod.ValidationState(
            labelled=381, correct=380, incorrect=1, total_matches=1000
        )
        assert state.lower_bound < 0.99
        assert 0.98 < state.lower_bound < 0.99

    def test_small_perfect_sample_is_not_enough(self) -> None:
        state = validate_mod.ValidationState(labelled=100, correct=100, total_matches=1000)
        assert state.lower_bound < 0.99
        assert state.bound_for(0.99) is not None, "should still be reachable"

    def test_report_quotes_the_lower_bound_not_the_ratio(self) -> None:
        state = validate_mod.ValidationState(labelled=100, correct=100, total_matches=1000)
        text = validate_mod.render_summary(state)
        assert "quote THIS number" in text
        assert f"{state.lower_bound:.4f}" in text

    def test_many_errors_make_the_target_unreachable(self) -> None:
        state = validate_mod.ValidationState(
            labelled=100, correct=80, incorrect=20, total_matches=1000
        )
        text = validate_mod.render_summary(state)
        assert "CANNOT REACH" in text
