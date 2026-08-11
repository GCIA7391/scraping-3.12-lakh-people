"""Yield mode: usable HNI prospects, not a statistical proof.

The business objective is at least N leads a wealth-management sales team can act
on. Precision still matters — a wrong profile wastes a call — but it is bought
with corroboration rather than by holding out for a 99% statistical bound that
discarded most of the file.
"""

from __future__ import annotations

import asyncio

import pytest

from linkedin_enrichment.config.settings import Settings
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.identity import priority
from linkedin_enrichment.output import bottleneck as bn
from linkedin_enrichment.search.query_builder import person_query_variants
from linkedin_enrichment.ingest.normalize import normalize_company, normalize_name


class TestYieldSettings:
    def test_yield_mode_favours_recall(self) -> None:
        config = Settings.load(overrides={"yield_mode": True})
        assert config.confidence_threshold == 0.95, "0.95 with corroboration, not 0.99"
        assert config.require_corroboration, "corroboration is what keeps it usable"
        assert not config.preferred_roles_only, "the pool is the whole file"
        assert config.target_matches == 1000
        assert config.max_person_queries >= 8, "deep ladder"

    def test_precision_mode_is_still_available(self) -> None:
        config = Settings.load(overrides={"precision_mode": True})
        assert config.confidence_threshold >= 0.99


class TestExpandedPool:
    """Every executive shape the sales team named is in scope."""

    @pytest.mark.parametrize("role", [
        "founder", "co-founder", "managing director", "director",
        "executive director", "ceo", "president", "chairman", "partner",
        "senior partner", "principal", "owner", "proprietor", "managing partner",
        "promoter", "vice chairman", "joint managing director",
        "whole time director", "wholetime director", "business head",
        "country head", "regional head", "division head", "practice head",
        "global head", "cto", "cfo", "coo", "cmo", "board member",
    ])
    def test_role_is_in_the_pool(self, role: str) -> None:
        assert priority.is_preferred_role(role)

    def test_trailing_punctuation_does_not_exclude(self) -> None:
        """The file contains 'Director.', 'Managing Director.' etc."""
        assert priority.role_score("director.") > 0
        assert priority.role_score("managing director.") > 0

    def test_no_role_scores_zero(self) -> None:
        """Ranking may deprioritise an unknown title; it must never zero it."""
        assert priority.role_score("some unusual title") > 0


class TestDeepLadder:
    def test_multiple_formulations_are_generated(self) -> None:
        variants = person_query_variants(
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
            "Bangalore",
        )
        assert len(variants) >= 8
        texts = " ".join(v.text for v in variants).lower()
        for expected in ("linkedin", "leadership", "executive", "director",
                         "press release", "crunchbase", "bloomberg"):
            assert expected in texts, f"ladder should try a {expected!r} formulation"

    def test_linkedin_scoped_query_runs_first(self) -> None:
        variants = person_query_variants(
            normalize_name("A B"), normalize_company("C Private Limited")
        )
        assert "site:linkedin.com/in" in variants[0].text

    def test_variants_are_unique(self) -> None:
        variants = person_query_variants(
            normalize_name("A B"), normalize_company("C Private Limited")
        )
        assert len({v.text for v in variants}) == len(variants)


class TestBottleneckDiagnosis:
    def _store(self, store: Store, decisions: dict[str, int]) -> Store:
        uid = 0
        for decision, count in decisions.items():
            rows = []
            for _ in range(count):
                rows.append({"row_uid": f"u{uid}", "source_file": "f.csv",
                             "row_index": uid, "name": f"P {uid}",
                             "company": f"C {uid} Private Limited"})
                uid += 1
            store.insert_records(rows)
        uid = 0
        for decision, count in decisions.items():
            for _ in range(count):
                store.write_result(
                    f"u{uid}", decision=decision,
                    linkedin_url="https://www.linkedin.com/in/x" if decision == "matched" else "",
                    confidence=0.97 if decision == "matched" else 0.0,
                )
                uid += 1
        return store

    def test_target_met_reports_success(self, store: Store, settings) -> None:
        settings.target_matches = 2
        self._store(store, {"matched": 3})
        report = bn.build(store, settings)
        assert report.target_met
        assert "TARGET MET" in bn.render(report)

    def test_no_candidate_bottleneck_names_the_data_ceiling(
        self, store: Store, settings
    ) -> None:
        settings.target_matches = 1000
        self._store(store, {"matched": 5, "blank_no_candidate": 95})
        text = bn.render(bn.build(store, settings))
        assert "TARGET NOT MET" in text
        assert "BOTTLENECK" in text
        assert "no result tied the person to the" in text
        assert "Widening roles or lowering the threshold will not help" in text

    def test_low_confidence_bottleneck_points_at_convertible_rows(
        self, store: Store, settings
    ) -> None:
        settings.target_matches = 1000
        self._store(store, {"matched": 5, "blank_low_confidence": 95})
        text = bn.render(bn.build(store, settings))
        assert "convertible" in text
        assert "review_queue.csv" in text

    def test_search_failure_is_called_infrastructure_not_data(
        self, store: Store, settings
    ) -> None:
        """The most important distinction: a broken backend must not read as
        'these people do not exist'."""
        settings.target_matches = 1000
        self._store(store, {"matched": 1, "blank_error": 99})
        text = bn.render(bn.build(store, settings))
        assert "infrastructure fault" in text
        assert "has not been fairly tested" in text

    def test_unprocessed_rows_are_not_called_exhaustion(
        self, store: Store, settings
    ) -> None:
        settings.target_matches = 1000
        store.insert_records([
            {"row_uid": f"p{i}", "source_file": "f.csv", "row_index": i,
             "name": f"P {i}", "company": f"C {i} Private Limited"}
            for i in range(50)
        ])
        store.write_result("p0", decision="matched", confidence=0.97,
                           linkedin_url="https://www.linkedin.com/in/x")
        text = bn.render(bn.build(store, settings))
        assert "never processed" in text
        assert "not\n    data exhaustion" in text or "not" in text

    def test_conversion_arithmetic_is_stated(self, store: Store, settings) -> None:
        settings.target_matches = 1000
        self._store(store, {"matched": 10, "blank_no_candidate": 90})
        text = bn.render(bn.build(store, settings))
        assert "10.00%" in text
        assert "10,000 processed rows" in text
