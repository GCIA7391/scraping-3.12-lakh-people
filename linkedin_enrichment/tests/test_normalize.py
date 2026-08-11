"""Name, company and location normalisation.

The pairs below are drawn from the real 312,160-row export and from live search
results captured during design, not invented. Each false-positive case names the
failure mode it guards against.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.ingest.normalize import (
    company_token_coverage,
    location_match,
    name_similarity,
    name_slug_agreement,
    normalize_company,
    normalize_name,
)

THRESHOLD = 0.82  # RejectRules.min_name_similarity


def sim(a: str, b: str) -> float:
    return name_similarity(normalize_name(a), normalize_name(b))


@pytest.mark.parametrize("registry,linkedin", [
    ("Girish Rowjee", "Girish Rowjee"),
    # MCA long-form vs the contracted form people actually use on LinkedIn.
    ("Melukote Shivaramu Lokesh", "Lokesh M S"),
    ("Melukote Shivaramu Lokesh", "M S Lokesh"),
    ("Galligekere Ramaswamy Gurumurthy", "Gurumurthy G R"),
    ("Vaddagere Chowdappa Ramesh", "Ramesh V C"),
    # South Indian names frequently place the given name last.
    ("Pollachi Venugopal Ravindran", "Ravindran Venugopal"),
    # A dropped trailing surname is normal, not disqualifying.
    ("Jagtar Singh Chaudhry", "Jagtar Singh"),
    ("Seetharam Rajeevalochanam Munikote", "Seetharam Munikote"),
    # 2.8% of the file is ALL-CAPS.
    ("SHOBHA SAIGAL", "Shobha Saigal"),
    # Romanisation variants of the same name: vowel and aspirate differences only.
    ("Lakshmi Prasad Yerneni", "Laxmi Prasad Yerneni"),
    ("Krishnamurthy Rao", "Krishnamoorthi Rao"),
    ("Gurumurthy Rao", "Gurumurti Rao"),
    ("Shivaramu Gowda", "Shivram Gowda"),
    ("Venugopal Reddy", "Venugopala Reddi"),
])
def test_same_person_matches(registry: str, linkedin: str) -> None:
    assert sim(registry, linkedin) >= THRESHOLD


@pytest.mark.parametrize("registry,other,guards_against", [
    # Both of these appeared in ONE live search result set. Jaro-Winkler alone
    # rates them 0.87 because of the shared prefix; the indel blend and the
    # consonant-skeleton rule are what separate them.
    ("Debasheesh Bagchi", "Debarshi Bagchi", "shared-prefix collision"),
    # An identical, very common surname must not carry a mismatched given name.
    ("Rajesh Kumar", "Ramesh Kumar", "common-surname masking"),
    ("Manoj Kumar", "Manish Kumar", "common-surname masking"),
    ("Suresh Babu", "Naresh Babu", "common-surname masking"),
    ("Anil Sharma", "Sunil Sharma", "common-surname masking"),
    ("Deepak Jain", "Dinesh Jain", "common-surname masking"),
    # Shared given name, different surname.
    ("Girish Rowjee", "Girish Kumar", "shared given name"),
    ("Sunil Naik", "Sunil Kumar", "shared given name"),
    ("Amit Kumar", "Amit Agarwal", "shared given name"),
    # Entirely unrelated colleague returned by a company-roster query.
    ("Melukote Shivaramu Lokesh", "Guru sajjan", "roster colleague"),
    ("Melukote Shivaramu Lokesh", "Jagadish B M", "roster colleague"),
])
def test_different_people_rejected(registry: str, other: str, guards_against: str) -> None:
    assert sim(registry, other) < THRESHOLD, guards_against


def test_initials_alone_never_establish_a_match() -> None:
    """A bare-initials name must not align with an arbitrary person."""
    assert sim("M S", "Melukote Shivaramu") < THRESHOLD


class TestCompanyNormalisation:
    @pytest.mark.parametrize("raw,expected_key", [
        ("Greytip Software Private Limited", "greytip-software"),
        ("Ubiqtech Software Private Limited", "ubiqtech-software"),
        # OCR damage present verbatim in the real export.
        ("Fractal Information Systems Private Limi Ted", "fractal-information-systems"),
        ("Flipkart Digital Services Private Limite D", "flipkart-digital-services"),
        ("Datasensor India Private Limited Cn", "datasensor-india"),
        ("South India Prime Tannery Pvt Ltd", "south-india-prime-tannery"),
    ])
    def test_brand_extraction(self, raw: str, expected_key: str) -> None:
        assert normalize_company(raw).key == expected_key

    def test_all_generic_company_is_flagged(self) -> None:
        """A company of only generic words cannot anchor a search result."""
        assert not normalize_company("Technology Resources Private Limited").has_distinctive_token
        assert normalize_company("Greytip Software Private Limited").has_distinctive_token


class TestCompanyCoverage:
    def test_company_present_scores_high(self) -> None:
        coverage = company_token_coverage(
            normalize_company("Greytip Software Private Limited"),
            "Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
        )
        assert coverage == pytest.approx(1.0)

    def test_company_absent_scores_zero(self) -> None:
        """Guards the 'right name, wrong company' failure mode."""
        coverage = company_token_coverage(
            normalize_company("Ubiqtech Software Private Limited"),
            "Debasheesh Bagchi - wep solutions india ltd | LinkedIn",
        )
        assert coverage == 0.0

    def test_generic_token_alone_is_insufficient(self) -> None:
        """Sharing only the word 'Technology' must not clear the 0.60 gate."""
        coverage = company_token_coverage(
            normalize_company("Technology Resources Private Limited"),
            "Ramesh Gupta - Technology Solutions Pvt Ltd",
        )
        assert coverage < 0.60

    def test_eponymous_company_cannot_corroborate_itself(self) -> None:
        """The company is named after the person, so a name-only hit must not
        also count as company evidence — otherwise one fact is counted twice."""
        subject_name = normalize_name("Zainab Fatima")
        coverage = company_token_coverage(
            normalize_company("Zainab Fatima Fast Foods Private Limited"),
            "zainab fatima - Berkeley, Illinois, United States | LinkedIn",
            exclude_tokens=set(subject_name.tokens),
        )
        assert coverage == 0.0


class TestSlugAndLocation:
    def test_slug_agreement(self) -> None:
        name = normalize_name("Girish Rowjee")
        assert name_slug_agreement(name, "https://www.linkedin.com/in/girishrowjee/") == 1.0

    def test_slug_ignores_hex_suffix(self) -> None:
        name = normalize_name("Debasheesh Bagchi")
        url = "https://www.linkedin.com/in/debasheesh-bagchi-683b3b1/"
        assert name_slug_agreement(name, url) == 1.0

    def test_slug_disagrees_for_other_person(self) -> None:
        name = normalize_name("Melukote Shivaramu Lokesh")
        assert name_slug_agreement(name, "https://in.linkedin.com/in/guru-sajjan-ba156213b") == 0.0

    @pytest.mark.parametrize("city,text,expected", [
        ("Bangalore", "Bengaluru, Karnataka, India", 1.0),
        ("Bangalore", "Bangalore Urban", 1.0),
        ("Hyderabad", "Secunderabad, Telangana", 1.0),
        ("Hyderabad", "Hyderabad, Telangana, India", 1.0),
        ("Bangalore", "Mumbai, Maharashtra", 0.0),
    ])
    def test_city_aliases(self, city: str, text: str, expected: float) -> None:
        assert location_match(city, text) == expected
