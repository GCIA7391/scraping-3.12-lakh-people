"""Contact routes: what is emitted, what is refused, and why.

The refusals matter more than the extractions here. The brief drew a line —
published professional contact routes, not personal emails or phone numbers —
and these tests are where that line is enforced rather than merely documented.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.ingest.normalize import normalize_company, normalize_name
from linkedin_enrichment.output import contacts as c
from linkedin_enrichment.providers.base import SerpResult

GREYTIP = normalize_company("Greytip Software Private Limited")
GIRISH = normalize_name("Girish Rowjee")


def result(url: str, title: str = "", snippet: str = "") -> SerpResult:
    return SerpResult(title=title or "Greytip Software", url=url, snippet=snippet)


class TestHierarchy:
    def test_directness_order_is_the_documented_one(self) -> None:
        assert [t.value for t in c.DIRECTNESS] == [
            "direct_corporate_email", "executive_office", "investor_relations",
            "board_office", "assistant", "reception", "linkedin_profile",
            "linkedin_company", "official_social", "contact_form",
            "conference_page",
        ]

    def test_every_route_type_has_a_rank(self) -> None:
        for route_type in c.RouteType:
            assert route_type in c.DIRECTNESS, f"{route_type} is unranked"

    def test_best_route_is_the_most_direct(self) -> None:
        routes = [
            c.ContactRoute(c.RouteType.CONFERENCE_PAGE, "u1", "u1"),
            c.ContactRoute(c.RouteType.INVESTOR_RELATIONS, "ir@x.com", "u2"),
            c.ContactRoute(c.RouteType.LINKEDIN_COMPANY, "u3", "u3"),
        ]
        assert c.best_route(routes).type is c.RouteType.INVESTOR_RELATIONS

    def test_no_routes_means_no_best_route(self) -> None:
        assert c.best_route([]) is None


class TestScopeGuardrail:
    """The line the brief drew, enforced."""

    def test_personal_gmail_matching_the_person_is_refused(self) -> None:
        routes = c.routes_from_result(
            result("https://example.com/news/story",
                   snippet="Reach Girish at girish.rowjee@gmail.com"),
            GREYTIP, GIRISH,
        )
        assert not any("gmail.com" in r.value for r in routes)

    def test_named_address_on_an_unrelated_domain_is_refused(self) -> None:
        """A person's address on someone else's domain is not a published
        corporate route for them."""
        routes = c.routes_from_result(
            result("https://random-blog.example/post",
                   snippet="contact girish.rowjee@someothercompany.com"),
            GREYTIP, GIRISH,
        )
        assert not any("@" in r.value for r in routes)

    def test_role_mailbox_on_a_free_provider_is_allowed_as_a_company_route(self) -> None:
        """Indian micro-companies genuinely publish info@gmail-style addresses."""
        routes = c.routes_from_result(
            result("https://greytip.com/contact", snippet="Write to info@gmail.com"),
            GREYTIP,
        )
        emails = [r for r in routes if "@" in r.value]
        assert emails and emails[0].scope == "company"

    @pytest.mark.parametrize("number", [
        "+91 98765 43210",     # 10-digit mobile, 9-series
        "+91-7012345678",      # 7-series
        "09876543210",         # trunk-prefixed mobile
    ])
    def test_mobile_numbers_are_refused_as_a_class(self, number: str) -> None:
        assert c.is_mobile_number(number)
        assert not c.is_publishable_phone(number)
        routes = c.routes_from_result(
            result("https://greytip.com/contact", snippet=f"Call us on {number}"),
            GREYTIP,
        )
        assert not any(r.value == number for r in routes)

    @pytest.mark.parametrize("number", ["080-4123 4567", "+91 80 4123 4567",
                                        "040 2345 6789", "1800 200 3344"])
    def test_switchboards_written_with_an_area_code_are_allowed(self, number: str) -> None:
        """The split between area code and local part is the only reliable tell
        a snippet gives, and it is the one a human reads too."""
        assert c.is_publishable_phone(number)

    def test_a_bare_ten_digit_number_is_refused_even_on_the_company_site(self) -> None:
        """Undecidable between a switchboard and a handset, so refused."""
        assert not c.is_publishable_phone("+91 9845012345")
        routes = c.routes_from_result(
            result("https://greytip.com/contact", snippet="Call +91 9845012345"),
            GREYTIP,
        )
        assert not any("9845" in r.value for r in routes)

    def test_landline_switchboard_is_allowed(self) -> None:
        routes = c.routes_from_result(
            result("https://greytip.com/contact", snippet="Tel: 080-4123 4567"),
            GREYTIP,
        )
        assert any("080" in r.value for r in routes), routes

    def test_phone_numbers_are_only_taken_from_the_company_site(self) -> None:
        """A number in a news article is as likely to be the reporter's desk."""
        routes = c.routes_from_result(
            result("https://economictimes.indiatimes.com/story",
                   snippet="Tel: 080-4123 4567"),
            GREYTIP,
        )
        assert not any("4123" in r.value for r in routes)

    def test_nothing_is_ever_synthesised(self) -> None:
        """No address-pattern inference. Every route must be observed text."""
        page = result("https://greytip.com/leadership",
                      title="Leadership | Greytip Software",
                      snippet="Girish Rowjee, Co-founder and CEO")
        for route in c.routes_from_result(page, GREYTIP, GIRISH):
            assert "@" not in route.value or route.value in page.text, (
                f"{route.value!r} does not appear in the source text"
            )

    def test_a_stranger_s_profile_never_becomes_a_route(self) -> None:
        """Caught in a live pilot: route collection was emitting every ``/in/``
        URL in the result set, so a row picked up 26 'contact routes' that were
        other people's profiles. A candidate identity is not an established one —
        only the runner adds a profile route, and only for a confirmed match."""
        routes = c.routes_from_result(
            result("https://www.linkedin.com/in/someone-else",
                   title="Someone Else - Greytip Software | LinkedIn"),
            GREYTIP, GIRISH,
        )
        assert not any(r.type is c.RouteType.LINKEDIN_PROFILE for r in routes)

    def test_a_speaker_page_for_a_different_person_is_not_a_route(self) -> None:
        routes = c.routes_from_result(
            result("https://techsummit.example/speakers/anita-rao",
                   title="Anita Rao — Speaker", snippet="CTO, Greytip Software"),
            GREYTIP, GIRISH,
        )
        assert routes == []

    def test_a_speaker_page_naming_the_person_is_a_route(self) -> None:
        routes = c.routes_from_result(
            result("https://techsummit.example/speakers/girish-rowjee",
                   title="Girish Rowjee — Speaker", snippet="CEO, Greytip Software"),
            GREYTIP, GIRISH,
        )
        assert [r.type for r in routes] == [c.RouteType.CONFERENCE_PAGE]
        assert routes[0].scope == "person"

    def test_another_company_s_linkedin_page_is_not_a_route(self) -> None:
        routes = c.routes_from_result(
            result("https://www.linkedin.com/company/unrelated-holdings",
                   title="Unrelated Holdings | LinkedIn", snippet="Investments"),
            GREYTIP, GIRISH,
        )
        assert routes == []

    def test_the_company_s_own_linkedin_page_is_a_route(self) -> None:
        routes = c.routes_from_result(
            result("https://www.linkedin.com/company/greytip-software",
                   title="Greytip Software | LinkedIn", snippet="HR and payroll"),
            GREYTIP, GIRISH,
        )
        assert [r.type for r in routes] == [c.RouteType.LINKEDIN_COMPANY]

    def test_a_leadership_page_is_a_company_route_not_a_personal_claim(self) -> None:
        """It reaches the office, and survives the person moving on."""
        routes = c.routes_from_result(
            result("https://greytip.com/leadership", title="Leadership"),
            GREYTIP, GIRISH,
        )
        assert routes and routes[0].type is c.RouteType.EXECUTIVE_OFFICE
        assert routes[0].scope == "company"

    def test_a_colleague_s_mailbox_is_not_this_person_s_route(self) -> None:
        routes = c.routes_from_result(
            result("https://greytip.com/team",
                   title="Team", snippet="Anita Rao — anita@greytip.com"),
            GREYTIP, GIRISH,
        )
        assert not any("anita@" in r.value for r in routes)

    def test_scraper_domains_produce_no_routes(self) -> None:
        assert c.routes_from_result(
            result("https://rocketreach.co/girish-rowjee",
                   snippet="girish@greytip.com"),
            GREYTIP, GIRISH,
        ) == []


class TestProvenance:
    def test_every_route_carries_a_source_url(self) -> None:
        routes = c.collect_routes([
            result("https://greytip.com/contact", snippet="info@greytip.com | 080-4123 4567"),
            result("https://www.linkedin.com/company/greytip-software"),
            result("https://greytip.com/leadership"),
        ], GREYTIP, GIRISH)
        assert routes
        for route in routes:
            assert route.source_url, f"{route} has no source"

    def test_render_shows_type_value_and_source(self) -> None:
        route = c.ContactRoute(
            c.RouteType.INVESTOR_RELATIONS, "ir@acme.com", "https://acme.com/investors",
        )
        rendered = route.render()
        assert "investor_relations" in rendered
        assert "ir@acme.com" in rendered
        assert "https://acme.com/investors" in rendered

    def test_round_trip_through_the_database_shape(self) -> None:
        routes = [
            c.ContactRoute(c.RouteType.RECEPTION, "info@x.com", "https://x.com"),
            c.ContactRoute(c.RouteType.CONTACT_FORM, "https://x.com/contact",
                           "https://x.com/contact"),
        ]
        assert c.deserialise(c.serialise(routes)) == sorted(routes, key=lambda r: r.rank)


class TestPageClassification:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.linkedin.com/in/girishrowjee", c.RouteType.LINKEDIN_PROFILE),
        ("https://www.linkedin.com/company/greytip", c.RouteType.LINKEDIN_COMPANY),
        ("https://twitter.com/greytip", c.RouteType.OFFICIAL_SOCIAL),
        ("https://greytip.com/leadership", c.RouteType.EXECUTIVE_OFFICE),
        ("https://greytip.com/investors", c.RouteType.INVESTOR_RELATIONS),
        ("https://greytip.com/corporate-governance", c.RouteType.BOARD_OFFICE),
        ("https://greytip.com/contact-us", c.RouteType.CONTACT_FORM),
        ("https://techsummit.example/speakers/girish", c.RouteType.CONFERENCE_PAGE),
    ])
    def test_urls_classify_to_the_right_route(self, url: str, expected) -> None:
        classified = c.classify_page(url, GREYTIP)
        assert classified is not None, f"{url} produced no route"
        assert classified[0] is expected

    def test_a_leadership_path_on_a_foreign_domain_is_not_a_route(self) -> None:
        """An article about the company is not a way to reach it."""
        assert c.classify_page(
            "https://economictimes.indiatimes.com/company/leadership-changes", GREYTIP
        ) is None

    def test_aggregators_classify_to_nothing(self) -> None:
        assert c.classify_page("https://zoominfo.com/c/greytip", GREYTIP) is None


class TestExtraction:
    def test_emails_are_deduped_case_insensitively(self) -> None:
        found = c.extract_emails("Info@Greytip.com and info@greytip.com")
        assert len(found) == 1

    def test_ir_mailbox_classifies_as_investor_relations(self) -> None:
        assert c.classify_email("ir", "acme.com", on_company_domain=True) \
            is c.RouteType.INVESTOR_RELATIONS

    def test_ceo_office_mailbox_classifies_as_executive_office(self) -> None:
        assert c.classify_email("ceo.office", "acme.com", on_company_domain=True) \
            is c.RouteType.EXECUTIVE_OFFICE

    def test_named_mailbox_on_the_company_domain_is_the_most_direct_route(self) -> None:
        assert c.classify_email("girish", "greytip.com", on_company_domain=True) \
            is c.RouteType.DIRECT_CORPORATE_EMAIL

    def test_years_and_figures_are_not_read_as_phone_numbers(self) -> None:
        assert c.extract_phones("Revenue grew from 2019 to 2024 by 1234567") == []

    def test_pin_codes_are_not_read_as_phone_numbers(self) -> None:
        assert c.extract_phones("Bengaluru 560103, Karnataka") == []


class TestMerging:
    def test_person_routes_win_over_company_routes_at_equal_directness(self) -> None:
        person = [c.ContactRoute(c.RouteType.RECEPTION, "a@x.com", "u1")]
        company = [c.ContactRoute(c.RouteType.RECEPTION, "b@x.com", "u2")]
        assert c.merge_routes(person, company)[0].value == "a@x.com"

    def test_duplicates_across_groups_collapse(self) -> None:
        route = c.ContactRoute(c.RouteType.RECEPTION, "a@x.com", "u1")
        assert len(c.merge_routes([route], [route])) == 1

    def test_summary_counts_rows_and_types(self) -> None:
        summary = c.RouteSummary()
        summary.add([c.ContactRoute(c.RouteType.RECEPTION, "a@x.com", "u")])
        summary.add([])
        summary.add([
            c.ContactRoute(c.RouteType.RECEPTION, "b@x.com", "u"),
            c.ContactRoute(c.RouteType.CONTACT_FORM, "https://x.com/c", "u"),
        ])
        assert summary.rows_with_routes == 2
        assert summary.total_routes == 3
        assert summary.by_type["reception"] == 2
        assert summary.routes_per_row == 1.5
