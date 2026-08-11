"""The validation → calibration loop.

`validate` asks the operator for ~381 hand-labelled rows. That effort is only
worth spending if it feeds back into the model. Until this loop existed the
labels were written to the database and stranded: `calibrate` read a CSV the
operator had to assemble by hand, and printed knots for them to paste into the
config themselves.

These tests hold the loop closed end to end:

    run -> results.top_score
    validate -> validation_labels
    calibrate --from-labels -> isotonic fit
    --write -> calibration.yaml
    Settings.load -> the fitted curve is actually in force
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_enrichment.config.settings import Settings
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.identity import calibrate as cal
from linkedin_enrichment.identity.scorer import calibrate as apply_calibration
from linkedin_enrichment.output import report as report_mod


def seed_scored_matches(store: Store, n: int = 40) -> None:
    """Matches with a spread of raw scores, as a real run would leave them."""
    store.insert_records([
        {"row_uid": f"r{i}", "source_file": "f.csv", "row_index": i,
         "name": f"Person {i}", "company": f"Company {i} Private Limited"}
        for i in range(n)
    ])
    for i in range(n):
        raw = 0.50 + (i / n) * 0.45          # 0.50 .. 0.95
        store.write_result(
            f"r{i}", decision="matched", confidence=0.9, top_score=raw,
            linkedin_url=f"https://www.linkedin.com/in/person{i}",
        )


def label_by_score(store: Store, n: int = 40, cut: float = 0.75) -> None:
    """Label as a careful human would if the scorer were informative:
    high scores right, low scores wrong."""
    for i in range(n):
        raw = 0.50 + (i / n) * 0.45
        store.put_label(f"r{i}", correct=raw >= cut)


class TestTheJoinThatDidNotExist:
    def test_labels_are_paired_with_their_raw_scores(self, store: Store) -> None:
        """`validation_labels` supplies the verdict, `results.top_score` the score
        that produced it. Both were stored; nothing joined them."""
        seed_scored_matches(store, 10)
        label_by_score(store, 10)

        points = store.iter_labelled_scores()
        assert len(points) == 10
        assert all(0.0 < score <= 1.0 for score, _ in points)
        assert {label for _, label in points} == {0, 1}

    def test_unlabelled_rows_are_excluded(self, store: Store) -> None:
        seed_scored_matches(store, 10)
        store.put_label("r0", correct=True)
        assert len(store.iter_labelled_scores()) == 1

    def test_fitting_without_labels_says_what_to_do(self, store: Store) -> None:
        seed_scored_matches(store, 5)
        with pytest.raises(ValueError, match="validate"):
            cal.fit_from_labels(store)


class TestFit:
    def test_fit_is_monotone(self, store: Store) -> None:
        seed_scored_matches(store)
        label_by_score(store)
        result = cal.fit_from_labels(store, target_precision=0.90)
        ys = [y for _, y in result.knots]
        assert ys == sorted(ys), "isotonic regression must be non-decreasing"

    def test_fit_finds_a_cut_point_where_labels_support_it(self, store: Store) -> None:
        seed_scored_matches(store, 200)
        label_by_score(store, 200, cut=0.75)
        result = cal.fit_from_labels(store, target_precision=0.90)
        assert result.threshold_raw_score is not None
        assert result.threshold_raw_score >= 0.70, "cut should land near the true boundary"

    def test_all_wrong_labels_yield_no_defensible_cut(self, store: Store) -> None:
        """If nothing labelled is correct, no threshold can be justified."""
        seed_scored_matches(store, 30)
        for i in range(30):
            store.put_label(f"r{i}", correct=False)
        result = cal.fit_from_labels(store, target_precision=0.99)
        assert result.threshold_raw_score is None
        assert "NO raw-score cut point" in result.render()


class TestPersistenceClosesTheLoop:
    def test_written_yaml_is_loaded_and_changes_scoring(
        self, store: Store, settings, tmp_path: Path
    ) -> None:
        """The whole point: after fitting, the pipeline scores differently.

        Writing a file that nobody reads would look like success while changing
        nothing, so this asserts the curve is actually in force.
        """
        seed_scored_matches(store, 60)
        label_by_score(store, 60, cut=0.80)
        result = cal.fit_from_labels(store, target_precision=0.90)

        output_dir = tmp_path / "out"
        result.write(output_dir / Settings.CALIBRATION_FILENAME)

        loaded = Settings.load(overrides={"output_dir": str(output_dir)})
        assert loaded.calibration.is_fitted
        assert loaded.calibration.fitted_from_labels == 60
        assert "fitted from 60 labels" in loaded.calibration.source
        assert loaded.calibration.source.startswith("calibration.yaml")

        shipped = Settings.load()
        probe = 0.60
        assert apply_calibration(probe, loaded.calibration) != pytest.approx(
            apply_calibration(probe, shipped.calibration)
        ), "the fitted curve must actually replace the shipped prior"

    def test_absent_file_leaves_the_shipped_prior(self, tmp_path: Path) -> None:
        config = Settings.load(overrides={"output_dir": str(tmp_path / "nothing-here")})
        assert not config.calibration.is_fitted
        assert "shipped prior" in config.calibration.source

    def test_yaml_marks_the_threshold_as_not_applied(self, store: Store) -> None:
        """A refit may recommend a different cut. Applying it silently would
        change the deliverable without the operator's say-so.

        Uses a sample large enough for a cut to actually be defensible — with
        too few labels the fit correctly declines to recommend one at all, and
        then there is nothing to mark.
        """
        seed_scored_matches(store, 200)
        label_by_score(store, 200, cut=0.75)
        result = cal.fit_from_labels(store, target_precision=0.90)
        assert result.threshold_raw_score is not None, "fixture must produce a cut"

        text = result.to_yaml()
        assert "NOT APPLIED" in text
        assert "isotonic_points" in text


class TestReportProvenance:
    def test_unvalidated_report_refuses_to_claim_precision(
        self, store: Store, settings
    ) -> None:
        seed_scored_matches(store, 5)
        text = report_mod.render_text(
            report_mod.build_report(store, settings, run_id="t", elapsed=1.0)
        )
        assert "NOT YET VALIDATED" in text
        assert "MODEL SCORE, not a measured" in text or "model score" in text.lower()
        assert "main.py validate" in text

    def test_validated_report_quotes_the_lower_bound(self, store: Store, settings) -> None:
        seed_scored_matches(store, 40)
        for i in range(40):
            store.put_label(f"r{i}", correct=True)

        report = report_mod.build_report(store, settings, run_id="t", elapsed=1.0)
        assert report.labelled == 40
        assert report.measured_precision == pytest.approx(1.0)

        text = report_mod.render_text(report)
        assert "Measured precision" in text
        assert "quote THIS number" in text
        assert f"{report.precision_lower_bound:.4f}" in text

    def test_report_warns_when_the_bound_undercuts_the_threshold(
        self, store: Store, settings
    ) -> None:
        """40 perfect labels support ~0.91, not 0.99 — say so rather than imply
        the threshold is proven."""
        settings.confidence_threshold = 0.99
        seed_scored_matches(store, 40)
        for i in range(40):
            store.put_label(f"r{i}", correct=True)
        text = report_mod.render_text(
            report_mod.build_report(store, settings, run_id="t", elapsed=1.0)
        )
        assert "below the 0.99 threshold" in text

    def test_report_names_the_calibration_in_force(self, store: Store, settings) -> None:
        text = report_mod.render_text(
            report_mod.build_report(store, settings, run_id="t", elapsed=1.0)
        )
        assert "Calibration in force" in text
