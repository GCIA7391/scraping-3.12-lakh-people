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
        "https://www.mca.gov.in/filing/x",
        "https://www.nseindia.com/companies/x",
    ])
    def test_registry_filings_are_accepted(self, url: str) -> None:
        """Registry and statutory filings count as corroboration.

        For an HNI prospect list, a filing naming the person against the company
        is the confirmation a sales team needs. They are classified separately so
        the weaker provenance stays visible — the input was itself MCA-derived,
        so a registry hit confirms the directorship rather than the identity.
        """
        from linkedin_enrichment.identity.corroborate import is_registry
        assert is_registry(url)
        assert not is_aggregator(url), "registry filings are no longer excluded"

    @pytest.mark.parametrize("url", [
        "https://rocketreach.co/girish-rowjee-email_1786181",
        "https://www.zoominfo.com/p/Chandrima-Mitra/3525821859",
        "https://www.signalhire.com/profiles/x",
    ])
    def test_contact_scrapers_remain_excluded(self, url: str) -> None:
        """The one class that adds nothing: they restate LinkedIn, often stale."""
        assert is_aggregator(url)

    @pytest.mark.parametrize("url", [
        "https://www.legal500.com/firms/32792-dsk-legal/lawyers/552761",
        "https://www.crunchbase.com/person/girish-rowjee",
        "https://pitchbook.com/profiles/person/x",
        "https://www.bloomberg.com/profile/person/x",
        "https://economictimes.indiatimes.com/x",
        "https://www.financialexpress.com/x",
        "https://www.business-standard.com/x",
        "https://www.forbes.com/profile/x",
    ])
    def test_press_and_financial_data_are_authoritative(self, url: str) -> None:
        assert is_authoritative(url)
        assert not is_aggregator(url)

    @pytest.mark.parametrize("url", [
        "https://someco.com/press/series-b-announcement",
        "https://conference.example/speakers/jane-doe",
        "https://startup.example/team",
    ])
    def test_press_and_speaker_pages_count_on_any_domain(self, url: str) -> None:
        from linkedin_enrichment.identity.corroborate import looks_like_editorial_page
        assert looks_like_editorial_page(url)


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

    def test_registry_filing_corroborates_but_is_labelled_as_such(self) -> None:
        """Accepted for yield, flagged for provenance.

        A registry filing naming the person against the company is usable
        confirmation for a sales team. It is recorded as `registry_filing` rather
        than dressed up as independent, because the input was MCA-derived: it
        confirms the directorship, not that the LinkedIn profile is the same
        human.
        """
        evidence = assess(
            [result(
                "GIRISH ROWJEE - Director - GREYTIP SOFTWARE PRIVATE LIMITED | ZaubaCorp",
                "https://www.zaubacorp.com/company/GREYTIP-SOFTWARE-PRIVATE-LIMITED",
                "Girish Rowjee is a director of Greytip Software Private Limited",
            )],
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
        )
        assert evidence.found
        assert evidence.kind == "registry_filing"

    def test_a_stronger_source_outranks_a_registry_filing(self) -> None:
        """When both are present the better provenance is the one recorded."""
        evidence = assess(
            [
                result("X - ZaubaCorp", "https://www.zaubacorp.com/company/GREYTIP-SOFTWARE",
                       "Girish Rowjee director of Greytip Software"),
                result("Girish Rowjee - Crunchbase", "https://www.crunchbase.com/person/girish-rowjee",
                       "Co-founder and CEO of Greytip Software"),
            ],
            normalize_name("Girish Rowjee"),
            normalize_company("Greytip Software Private Limited"),
        )
        assert evidence.found
        assert evidence.kind == "press_or_financial_data"

    def test_contact_scraper_alone_still_does_not_corroborate(self) -> None:
        evidence = assess(
            [result(
                "Girish Rowjee Email & Phone | Greytip Software CEO",
                "https://rocketreach.co/girish-rowjee-email_1786181",
                "Girish Rowjee, CEO at Greytip Software",
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
