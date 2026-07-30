"""End-to-end decisions over the seven recorded real search result sets.

This is the pipeline's precision regression. Six of the seven cases must produce
a blank; producing a URL for any of them means the matcher has started guessing.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.identity.scorer import Decision, Subject, calibrate, resolve

# (label, name, company, city, designation, industry, cassette query, expected)
CASES = [
    ("case1 true positive",
     "Girish Rowjee", "Greytip Software Private Limited", "Bangalore", "CEO", "",
     '"Girish Rowjee" "Greytip Software" LinkedIn Bangalore',
     Decision.MATCHED),

    ("case2 right name, wrong company",
     "Debasheesh Bagchi", "Ubiqtech Software Private Limited", "Bangalore", "Director", "Technology",
     '"Debasheesh Bagchi" "Ubiqtech Software" LinkedIn',
     Decision.BLANK_NO_CANDIDATE),

    ("case3 right company, wrong people",
     "Melukote Shivaramu Lokesh", "Synthesis Winding Technologies Private Limited",
     "Bangalore", "Wholetime Director", "",
     'site:linkedin.com/in "Melukote Shivaramu Lokesh" Synthesis Winding Technologies',
     Decision.BLANK_NO_CANDIDATE),

    ("case4 homonyms with no company anchor",
     "Praveen Kumar", "Sri Sai Infra Developers Private Limited", "Hyderabad", "Director", "Real Estate",
     '"Praveen Kumar" "Director" LinkedIn Hyderabad',
     Decision.BLANK_NO_CANDIDATE),

    ("case5 roster misses the founder",
     "Girish Rowjee", "Greytip Software Private Limited", "Bangalore", "CEO", "",
     'site:linkedin.com/in "Greytip Software"',
     Decision.BLANK_NO_CANDIDATE),

    ("case6a eponymous company, no footprint",
     "Habiba Zackria", "Zainab Fatima Fast Foods Private Limited", "Bangalore", "Wholetime Director", "",
     'site:linkedin.com/in "Zainab Fatima Fast Foods"',
     Decision.BLANK_NO_CANDIDATE),

    ("case6b micro-company, no footprint",
     "Dharmesh Premchand Madhwani", "Natesh Impex Private Limited", "Bangalore", "Wholetime Director", "",
     'site:linkedin.com/in "Natesh Impex"',
     Decision.BLANK_NO_CANDIDATE),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,name,company,city,designation,industry,query,expected",
    CASES, ids=[c[0] for c in CASES],
)
async def test_recorded_cases(
    settings, cassette, label, name, company, city, designation, industry, query, expected,
) -> None:
    response = await cassette.search(query, limit=10)
    assert response.results, f"cassette missing for {query!r}"

    subject = Subject.from_fields("uid", name, company, city, industry, designation)
    outcome = resolve(subject, response.results, settings)

    assert outcome.decision is expected, (
        f"{label}: expected {expected.value}, got {outcome.decision.value} "
        f"(url={outcome.linkedin_url!r}, confidence={outcome.confidence:.4f})"
    )
    if expected is not Decision.MATCHED:
        assert outcome.linkedin_url == "", f"{label}: must not emit a URL"


@pytest.mark.asyncio
async def test_true_positive_details(settings, cassette) -> None:
    """The one genuine match must clear the threshold and cite its evidence."""
    response = await cassette.search(
        '"Girish Rowjee" "Greytip Software" LinkedIn Bangalore', limit=10
    )
    subject = Subject.from_fields(
        "uid", "Girish Rowjee", "Greytip Software Private Limited", "Bangalore", "", "CEO"
    )
    outcome = resolve(subject, response.results, settings)

    assert outcome.linkedin_url == "https://www.linkedin.com/in/girishrowjee"
    assert outcome.confidence >= settings.confidence_threshold
    assert "name_similarity" in outcome.notes
    assert outcome.source_urls


class TestCalibration:
    def test_is_monotone(self, settings) -> None:
        scores = [i / 20 for i in range(21)]
        values = [calibrate(s, settings.calibration) for s in scores]
        assert values == sorted(values)

    def test_perfect_name_and_company_alone_clears_the_bar(self, settings) -> None:
        """core=1.0 -> raw 0.70. An exact name at an exactly-matching company is
        a match even with no corroborating signals."""
        assert calibrate(0.70, settings.calibration) >= settings.confidence_threshold

    def test_partial_evidence_does_not_clear_the_bar(self, settings) -> None:
        """An imperfect name against a matching company must not be written."""
        assert calibrate(0.63, settings.calibration) < settings.confidence_threshold

    def test_isotonic_points_take_precedence(self, settings) -> None:
        settings.calibration.isotonic_points = [(0.0, 0.0), (0.5, 0.5), (1.0, 1.0)]
        assert calibrate(0.25, settings.calibration) == pytest.approx(0.25)
        assert calibrate(0.75, settings.calibration) == pytest.approx(0.75)


class TestDesignationNeverPenalises:
    """Registry 'Director' is a board appointment, not a LinkedIn headline.
    A mismatch is the norm and must not reduce a score."""

    def test_missing_designation_does_not_reduce_score(self, settings) -> None:
        from linkedin_enrichment.providers.base import SerpResult

        hit = SerpResult(
            title="Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
            url="https://www.linkedin.com/in/girishrowjee/",
        )
        with_role = resolve(
            Subject.from_fields("u", "Girish Rowjee", "Greytip Software Private Limited",
                                "Bangalore", "", "Director"),
            [hit], settings,
        )
        without_role = resolve(
            Subject.from_fields("u", "Girish Rowjee", "Greytip Software Private Limited",
                                "Bangalore", "", ""),
            [hit], settings,
        )
        assert with_role.top_score >= without_role.top_score
        assert with_role.decision is Decision.MATCHED
        assert without_role.decision is Decision.MATCHED
