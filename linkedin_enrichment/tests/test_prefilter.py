"""Tier 0 — rows rejected before any query is spent."""

from __future__ import annotations

import pytest

from linkedin_enrichment.ingest import prefilter
from linkedin_enrichment.ingest.prefilter import SkipReason
from linkedin_enrichment.ingest.reader import InputRow


def row(name: str, company: str) -> InputRow:
    return InputRow(
        row_uid="uid", source_file="f.csv", sheet_name="", row_index=1,
        name=name, company=company, location="Bangalore", designation="Director",
    )


@pytest.mark.parametrize("name,company", [
    ("Girish Rowjee", "Greytip Software Private Limited"),
    ("Melukote Shivaramu Lokesh", "Synthesis Winding Technologies Private Limited"),
    ("SHOBHA SAIGAL", "Nettigritty Private Limited"),
])
def test_real_people_are_searchable(name: str, company: str) -> None:
    assert prefilter.evaluate(row(name, company)).searchable


@pytest.mark.parametrize("name,company,reason", [
    # All three of these appear verbatim in the Name column of the real export.
    ("Electronic Manufacturers", "Essae -Teraoka Private Limited", SkipReason.NAME_NOT_A_PERSON),
    ("Unni Cnc", "Thermochem Corporation Private Limited", SkipReason.NAME_NOT_A_PERSON),
    # A single token cannot be disambiguated among millions of profiles.
    ("Naganna", "Hitesh Agri Solutions Private Limited", SkipReason.NAME_TOO_SHORT),
    # Nothing to anchor a search result against.
    ("Ramesh Gupta", "Technology Resources Private Limited", SkipReason.COMPANY_NOT_DISTINCTIVE),
    ("", "Some Company Private Limited", SkipReason.MISSING_FIELDS),
    ("Ramesh Gupta", "", SkipReason.MISSING_FIELDS),
])
def test_unsearchable_rows_are_skipped(name: str, company: str, reason: SkipReason) -> None:
    verdict = prefilter.evaluate(row(name, company))
    assert not verdict.searchable
    assert verdict.reason is reason


def test_duplicate_detection() -> None:
    seen: set[str] = set()
    first = prefilter.evaluate(row("Girish Rowjee", "Greytip Software Private Limited"), seen)
    second = prefilter.evaluate(row("Girish Rowjee", "Greytip Software Private Limited"), seen)
    assert first.searchable
    assert not second.searchable
    assert second.reason is SkipReason.DUPLICATE


def test_duplicate_key_is_normalisation_insensitive() -> None:
    """Case and legal-suffix differences must not defeat duplicate detection."""
    a = prefilter.evaluate(row("GIRISH ROWJEE", "Greytip Software Private Limited"))
    b = prefilter.evaluate(row("Girish Rowjee", "Greytip Software Pvt Ltd"))
    assert a.dedup_key == b.dedup_key


def test_org_token_majority_rule() -> None:
    """The person gate is a majority rule, not a single-token veto.

    A two-token "name" that is half organisational ("Rajesh Trading") is far more
    likely a data artefact than a person, and is rejected. A longer name where the
    organisational token is outnumbered is allowed through — the scorer has much
    more context and will reject it there if it is wrong.
    """
    assert not prefilter.evaluate(row("Rajesh Trading", "Alpha Beta Private Limited")).searchable
    assert prefilter.evaluate(row("Manoj Kumar Industries", "Alpha Beta Private Limited")).searchable


def test_long_and_initialled_names_are_kept() -> None:
    """MCA long-forms and initial-heavy names must survive Tier 0."""
    assert prefilter.evaluate(
        row("Galligekere Ramaswamy Gurumurthy", "Quality Widgets Private Limited")
    ).searchable
    assert prefilter.evaluate(
        row("Lokesh M S", "Quality Widgets Private Limited")
    ).searchable
