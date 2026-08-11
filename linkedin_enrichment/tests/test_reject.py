"""The reject rules, exercised against the real search results that motivated them.

Each test names the live case it encodes. If one of these starts failing, the
pipeline has begun writing profiles it cannot justify.
"""

from __future__ import annotations

import pytest

from linkedin_enrichment.identity.reject import RejectReason, is_company_account, screen_result
from linkedin_enrichment.identity.scorer import Decision, Subject, resolve, score_candidate
from linkedin_enrichment.ingest.normalize import normalize_company
from linkedin_enrichment.providers.base import SerpResult


def result(title: str, url: str, snippet: str = "", rank: int = 0) -> SerpResult:
    return SerpResult(title=title, url=url, snippet=snippet, rank=rank)


class TestStructuralScreen:
    @pytest.mark.parametrize("url", [
        "https://www.linkedin.com/company/greytip-software-pvt-ltd-",
        "https://www.linkedin.com/pub/dir/Zainab+Fatima/+",
        "https://www.linkedin.com/posts/greytip-software_activity-6958791389262331905-MCQV",
        "https://www.linkedin.com/school/some-college/",
        "https://www.linkedin.com/jobs/view/12345",
    ])
    def test_non_profile_linkedin_urls_rejected(self, settings, url: str) -> None:
        verdict = screen_result(result("anything", url), settings.reject)
        assert verdict.rejected
        assert verdict.reason is RejectReason.NOT_A_PROFILE

    @pytest.mark.parametrize("url", [
        "https://www.zaubacorp.com/company/UBIQTECH-SOFTWARE-PRIVATE-LIMITED/x",
        "https://rocketreach.co/girish-rowjee-email_1786181",
        "https://www.crunchbase.com/person/girish-rowjee",
        "https://en.wikipedia.org/wiki/Lion_Dates",
    ])
    def test_non_linkedin_urls_rejected(self, settings, url: str) -> None:
        verdict = screen_result(result("anything", url), settings.reject)
        assert verdict.rejected
        assert verdict.reason is RejectReason.NOT_LINKEDIN

    def test_genuine_profile_passes(self, settings) -> None:
        verdict = screen_result(
            result("Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/girishrowjee/"),
            settings.reject,
        )
        assert not verdict.rejected


class TestCompanyAccountDetection:
    """Small firms register /in/ profiles in the company's own name. Both of
    these URLs came from live searches and are real /in/ profiles."""

    def test_company_branded_profile_detected(self) -> None:
        tokens = frozenset(normalize_company("Greytip Software Private Limited").tokens)
        assert is_company_account(
            result("Greytip Software - Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/greytip-software-802362140/"),
            tokens,
        )

    def test_real_person_not_flagged(self) -> None:
        tokens = frozenset(normalize_company("Greytip Software Private Limited").tokens)
        assert not is_company_account(
            result("Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/girishrowjee/"),
            tokens,
        )


class TestEvidenceGates:
    def test_right_name_wrong_company_is_rejected(self, settings) -> None:
        """CASE 2. The subject's exact name, at a different employer."""
        subject = Subject.from_fields(
            "u", "Debasheesh Bagchi", "Ubiqtech Software Private Limited", "Bangalore"
        )
        candidate = score_candidate(
            subject,
            result("Debasheesh Bagchi - wep solutions india ltd | LinkedIn",
                   "https://www.linkedin.com/in/debasheesh-bagchi-683b3b1/"),
            settings,
        )
        assert candidate.rejected
        assert candidate.rejection.reason is RejectReason.COMPANY_ABSENT

    def test_right_company_wrong_person_is_rejected(self, settings) -> None:
        """CASE 3. A genuine colleague at the correct company."""
        subject = Subject.from_fields(
            "u", "Melukote Shivaramu Lokesh",
            "Synthesis Winding Technologies Private Limited", "Bangalore"
        )
        candidate = score_candidate(
            subject,
            result("Guru sajjan - Synthesis Winding Technologies Pvt Ltd",
                   "https://in.linkedin.com/in/guru-sajjan-ba156213b"),
            settings,
        )
        assert candidate.rejected
        assert candidate.rejection.reason is RejectReason.NAME_MISMATCH

    def test_both_matching_is_accepted(self, settings) -> None:
        """CASE 1. Name and company both corroborated."""
        subject = Subject.from_fields(
            "u", "Girish Rowjee", "Greytip Software Private Limited", "Bangalore", "", "CEO"
        )
        candidate = score_candidate(
            subject,
            result("Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/girishrowjee/"),
            settings,
        )
        assert not candidate.rejected
        assert candidate.confidence >= settings.confidence_threshold


class TestAmbiguity:
    def test_two_equal_candidates_produce_a_blank(self, settings) -> None:
        """Two colleagues sharing the subject's name at the same company. There
        is no principled way to choose, so neither is written."""
        subject = Subject.from_fields(
            "u", "Praveen Kumar", "Ergos Business Solutions Private Limited", "Hyderabad"
        )
        results = [
            result("Praveen Kumar - Director - Ergos Business Solutions Pvt. Ltd | LinkedIn",
                   "https://in.linkedin.com/in/praveen-kumar-4a667028", rank=0),
            result("Praveen Kumar - Ergos Business Solutions Pvt. Ltd | LinkedIn",
                   "https://in.linkedin.com/in/praveen-kumar-99887766", rank=1),
        ]
        outcome = resolve(subject, results, settings)
        assert outcome.decision is Decision.BLANK_AMBIGUOUS
        assert outcome.linkedin_url == ""

    def test_clear_winner_is_not_blocked_by_a_weak_runner_up(self, settings) -> None:
        subject = Subject.from_fields(
            "u", "Girish Rowjee", "Greytip Software Private Limited", "Bangalore"
        )
        results = [
            result("Girish Rowjee - Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/girishrowjee/", rank=0),
            result("Akshay Kumar - BDE @ Greytip Software Pvt. Ltd. | LinkedIn",
                   "https://www.linkedin.com/in/akshay-kumar-b977b08b/", rank=1),
        ]
        outcome = resolve(subject, results, settings)
        assert outcome.decision is Decision.MATCHED
