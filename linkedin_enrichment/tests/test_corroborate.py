"""Independent corroboration — the second source a 99% claim requires.

Anchored on two real cases from live validation:

* **Chandrima Mitra / DSK Legal** resolved cleanly, corroborated by the firm's
  own team page. This must be accepted.
* **Bidisha Nayak / eMudhra** is a registry row calling her a "co-founder" of a
  company she did not found, while a differently-employed person of that exact
  name does exist on LinkedIn. This must be rejected.

The load-bearing rule is what counts as *independent*. The input came from the
MCA registry, so ZaubaCorp, Tofler and the rest are republications of the input
itself — agreeing with them is the data agreeing with itself.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.identity.corroborate import (
    assess,
    is_aggregator,
    is_authoritative,
    looks_like_company_site,
)
from linkedin_enrichment.ingest.normalize import normalize_company, normalize_name
from linkedin_enrichment.providers.base import SerpResult


def result(title: str, url: str, snippet: str = "") -> SerpResult:
    return SerpResult(title=title, url=url, snippet=snippet)


class TestSourceClassification:
    @pytest.mark.parametrize("url", [
        "https://www.zaubacorp.com/company/X-PRIVATE-LIMITED/U72200KA",
        "https://tofler.in/x-private-limited/company/U72200KA",
        "https://www.thecompanycheck.com/company/x",
        "https://m.indiamart.com/nayak-impex",
        "https://www.instafinancials.com/company/x",
    ])
    def test_registry_republications_are_not_independent(self, url: str) -> None:
        """These restate the MCA registry the input came from."""
        assert is_aggregator(url)

    @pytest.mark.parametrize("url", [
        "https://rocketreach.co/girish-rowjee-email_1786181",
        "https://www.zoominfo.com/p/Chandrima-Mitra/3525821859",
        "https://www.signalhire.com/profiles/x",
    ])
    def test_contact_scrapers_are_not_independent(self, url: str) -> None:
        """These restate LinkedIn, so they cannot corroborate LinkedIn."""
        assert is_aggregator(url)

    @pytest.mark.parametrize("url", [
        "https://www.legal500.com/firms/32792-dsk-legal/lawyers/552761",
        "https://www.crunchbase.com/person/girish-rowjee",
        "https://economictimes.indiatimes.com/x",
    ])
    def test_editorial_sources_are_authoritative(self, url: str) -> None:
        assert is_authoritative(url)
        assert not is_aggregator(url)


class TestCompanySiteDetection:
    def test_own_domain_recognised(self) -> None:
        company = normalize_company("Greytip Software Private Limited")
        assert looks_like_company_site("https://www.greytip.com/about/", company)

    def test_unrelated_domain_rejected(self) -> None:
        company = normalize_company("Greytip Software Private Limited")
        assert not looks_like_company_site("https://random-blog.example/greytip", company)

    def test_aggregator_never_counts_as_company_site(self) -> None:
        company = normalize_company("Greytip Software Private Limited")
        assert not looks_like_company_site(
            "https://www.zaubacorp.com/company/GREYTIP-SOFTWARE", company
        )


class TestRealCases:
    def test_dsk_legal_team_page_corroborates(self) -> None:
        """Verified live: this person, this firm, on the firm's own site."""
        evidence = assess(
            [result(
                "Chandrima Mitra – DSK Legal : True Value, True Values",
                "https://dsklegal.com/team/chandrima-mitra/",
                "Chandrima Mitra is a Partner at DSK Legal specialising in media law.",
            )],
            normalize_name("Chandrima Mitra"),
            normalize_company("Dsk Legal Services LLP"),
        )
        assert evidence.found
        assert evidence.kind == "official_site_leadership"

    def test_emudhra_impostor_is_not_corroborated(self) -> None:
        """Verified live: the registry's 'co-founder' is not one, and the
        similarly-named LinkedIn person works in an unrelated field."""
        evidence = assess(
            [
                result("Bidisha Nayak - Corporate Real Estate | LinkedIn",
                       "https://www.linkedin.com/in/bidisha-nayak-5344a8134/",
                       "Workplace operations specialist"),
                result("EMudhra - Founders and Board of Directors - Tracxn",
                       "https://tracxn.com/d/companies/emudhra/founders",
                       "eMudhra was founded by Venkatraman Srinivasan and Kaushik Srinivasan."),
            ],
            normalize_name("Bidisha Nayak"),
            normalize_company("Emudhra Consumer Services Limited"),
        )
        assert not evidence.found

    def test_registry_aggregator_alone_does_not_corroborate(self) -> None:
        """The strongest-looking evidence that is worth nothing: a page that
        names both, sourced from the same registry as the input."""
        evidence = assess(
            [result(
                "GIRISH ROWJEE - Director - GREYTIP SOFTWARE PRIVATE LIMITED | ZaubaCorp",
                "https://www.zaubacorp.com/company/GREYTIP-SOFTWARE-PRIVATE-LIMITED",
                "Girish Rowjee is a director of Greytip Software Private Limited",
            )],
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
        )
        assert not evidence.found

    def test_page_naming_a_different_person_does_not_corroborate(self) -> None:
        evidence = assess(
            [result("Our Leadership - Greytip Software",
                    "https://www.greytip.com/about/leadership/",
                    "Meet our CEO Someone Else and the leadership team.")],
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
        )
        assert not evidence.found


class TestBrandAliasing:
    """Registry entity names differ from the employer string LinkedIn shows."""

    @pytest.mark.parametrize("entity,employer_text", [
        ("HP PPS Services India Private Limited", "Ajay Gupta - HP | LinkedIn"),
        ("Wells Fargo International Solutions Private Limited",
         "Prasanth T - Wells Fargo | LinkedIn"),
        ("Ibm India Private Limited", "Suraj K - IBM | LinkedIn"),
    ])
    def test_parent_brand_accepted(self, entity: str, employer_text: str) -> None:
        from linkedin_enrichment.ingest.normalize import brand_alias_match
        assert brand_alias_match(normalize_company(entity), employer_text)

    def test_rename_is_not_guessed(self) -> None:
        """'Sorting Hat Technologies' trades as Unacademy. They share no tokens,
        so this stays unmatched rather than being invented."""
        from linkedin_enrichment.ingest.normalize import brand_alias_match
        assert not brand_alias_match(
            normalize_company("Sorting Hat Technologies Private Limited"),
            "Sunil Jangra - Unacademy | LinkedIn",
        )

    def test_different_company_rejected(self) -> None:
        from linkedin_enrichment.ingest.normalize import brand_alias_match
        assert not brand_alias_match(
            normalize_company("Alpha Technologies Private Limited"),
            "Someone - Beta Technologies Pvt Ltd",
        )
