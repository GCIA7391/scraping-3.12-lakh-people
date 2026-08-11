"""The pilot report: measure a slice, project the file, state the caveat.

A pilot exists to replace an estimate with a measurement before committing
312,160 rows. It is only useful if it cannot flatter itself — so the properties
pinned here are the ones that keep it honest: a broken backend is called out as a
backend problem, a ranked pilot is labelled an upper bound, and a shortfall is
stated as a shortfall.
"""

from __future__ import annotations

from linkedin_enrichment.output import pilot as p
from linkedin_enrichment.search.query_builder import (
    COMPANY_CONTACT_LADDER,
    PERSON_LADDER,
    company_contact_queries,
    person_query_variants,
)
from linkedin_enrichment.ingest.normalize import normalize_company, normalize_name


def make(**kwargs) -> p.Pilot:
    base = dict(rows=1000, profiles=20, contact_only=180, with_routes=240,
                routes_found=600, elapsed=500.0, queries=4000,
                file_rows=312_160, target=3000)
    base.update(kwargs)
    return p.Pilot(**base)


class TestArithmetic:
    def test_conversion_counts_both_kinds_of_deliverable(self) -> None:
        assert make().delivered == 200
        assert make().conversion == 0.20

    def test_projection_is_conversion_times_the_file(self) -> None:
        assert make().projected_total == 62_432

    def test_rows_to_target_inverts_the_conversion(self) -> None:
        assert make().rows_to_target == 15_000

    def test_routes_per_row_divides_by_rows_that_have_routes(self) -> None:
        assert make().routes_per_row == 2.5

    def test_zero_conversion_does_not_divide_by_zero(self) -> None:
        pilot = make(profiles=0, contact_only=0)
        assert pilot.conversion == 0.0
        assert pilot.rows_to_target == 0
        assert "no rate to project from" in p.render(pilot)


class TestHonesty:
    def test_a_broken_backend_is_named_as_such(self) -> None:
        text = p.render(make(errors=800))
        assert "WARNING" in text
        assert "SEARCH layer" in text
        assert "not the data" in text

    def test_a_ranked_pilot_is_labelled_an_upper_bound(self) -> None:
        assert "upper bound" in p.render(make(ranked=True))

    def test_an_unranked_pilot_is_labelled_a_fair_sample(self) -> None:
        assert "fair sample" in p.render(make(ranked=False))

    def test_a_shortfall_is_stated_plainly(self) -> None:
        text = p.render(make(profiles=1, contact_only=1, target=3000))
        assert "SHORTFALL" in text

    def test_the_two_kinds_of_deliverable_are_reported_separately(self) -> None:
        text = p.render(make())
        assert "LinkedIn profiles confirmed" in text
        assert "Delivered on contact routes" in text


class TestLadderDepth:
    def test_the_ladder_covers_every_named_source(self) -> None:
        variants = person_query_variants(
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
            "Bangalore",
        )
        text = " ".join(v.text for v in variants).lower()
        for expected in (
            "linkedin.com/in", "leadership", "board of directors", "annual report",
            "investor relations", "press release", "speaker",
            "economictimes", "business-standard", "moneycontrol",
            "bloomberg", "crunchbase", "din", "bseindia", "nseindia",
            "email", "twitter",
        ):
            assert expected in text, f"the ladder should try a {expected!r} formulation"

    def test_the_ladder_is_deep(self) -> None:
        assert len(PERSON_LADDER) >= 20

    def test_every_rung_is_named(self) -> None:
        """The step name is what the variant-hit stats are keyed on, so a blank
        one would silently merge two rungs in the report."""
        steps = [step for step, _ in PERSON_LADDER]
        assert all(steps) and len(set(steps)) == len(steps)

    def test_variants_are_unique_and_carry_their_step(self) -> None:
        variants = person_query_variants(
            normalize_name("A B"), normalize_company("C Private Limited")
        )
        assert len({v.text for v in variants}) == len(variants)
        assert all(v.step for v in variants)

    def test_location_rungs_are_dropped_when_there_is_no_location(self) -> None:
        with_location = person_query_variants(
            normalize_name("A B"), normalize_company("C Private Limited"), "Bangalore"
        )
        without = person_query_variants(
            normalize_name("A B"), normalize_company("C Private Limited")
        )
        assert len(with_location) == len(without) + 1
        assert not any("{location}" in v.text for v in without)

    def test_company_contact_ladder_is_budgeted(self) -> None:
        company = normalize_company("Greytip Software Private Limited")
        assert len(company_contact_queries(company, limit=3)) == 3
        assert len(company_contact_queries(company)) == len(COMPANY_CONTACT_LADDER)

    def test_company_contact_queries_do_not_name_a_person(self) -> None:
        """They are about the organisation, and are shared by everyone in it."""
        company = normalize_company("Greytip Software Private Limited")
        for query in company_contact_queries(company):
            assert query.company_key == company.key
            assert "{person}" not in query.text
