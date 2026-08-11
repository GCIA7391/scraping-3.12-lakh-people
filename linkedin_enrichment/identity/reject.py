"""Hard reject rules — the precision engine.

Scoring decides which candidate is *best*; these rules decide whether the best
candidate is good enough to write down at all. They exist because the stated
requirement is absolute: never fabricate, never guess, blank when unsure.

Every rule here was derived from a real search executed against this dataset
during design, not invented defensively. The comment on each names the case it
came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..providers.base import SerpResult, is_linkedin_url, is_profile_url


class RejectReason(str, Enum):
    """Why a candidate was discarded. Surfaced verbatim in the review queue."""

    NONE = ""
    NOT_LINKEDIN = "not a linkedin.com URL"
    NOT_A_PROFILE = "not a personal profile URL"
    COMPANY_ABSENT = "company name absent from the search result"
    NAME_MISMATCH = "name does not match the search result"
    COMPANY_PROFILE = "profile appears to be a company account, not a person"
    BELOW_THRESHOLD = "confidence below threshold"
    AMBIGUOUS = "top two candidates too close to separate"
    SHARED_URL = "profile is also the best match for another person"


@dataclass
class CandidateRejection:
    rejected: bool
    reason: RejectReason = RejectReason.NONE

    def __bool__(self) -> bool:
        return self.rejected


def screen_result(result: SerpResult, rules) -> CandidateRejection:
    """Structural screening — applied before any scoring, since these are cheap
    and unambiguous.
    """
    url = result.url or ""

    if not is_linkedin_url(url):
        return CandidateRejection(True, RejectReason.NOT_LINKEDIN)

    # A company-roster query returns the company page, directory listings and
    # job posts alongside real people. Observed in the live "Greytip Software"
    # search: a /company/ page and a /pub/dir/ listing both ranked in the top 10.
    lowered = url.lower()
    if any(marker in lowered for marker in rules.non_profile_markers):
        return CandidateRejection(True, RejectReason.NOT_A_PROFILE)

    if not is_profile_url(url):
        return CandidateRejection(True, RejectReason.NOT_A_PROFILE)

    return CandidateRejection(False)


def is_company_account(result: SerpResult, company_tokens: frozenset[str]) -> bool:
    """Detect a company-branded personal profile.

    Small Indian firms routinely register a ``/in/`` profile in the *company's*
    name — the live "Greytip Software" search returned
    ``/in/greytip-software-802362140`` and the "Synthesis Winding" search returned
    ``/in/synthesis-winding-technologies-pvt-ltd-bangalore``. These are real
    ``/in/`` URLs, so the path check above passes them, but they are not people
    and must never be attached to a named individual.
    """
    if not company_tokens:
        return False
    lead = (result.title or "").split("-")[0].split("|")[0].strip().lower()
    if not lead:
        return False
    lead_tokens = {t for t in lead.replace(".", " ").split() if len(t) > 2}
    if not lead_tokens:
        return False
    # The "person" name consists only of company tokens => it is the company.
    return lead_tokens.issubset(company_tokens)


def check_candidate(
    *, name_similarity: float, company_coverage: float, rules,
) -> CandidateRejection:
    """The two mandatory evidence gates.

    Both must hold. Neither alone is sufficient — that is the central finding
    from live validation:

    * Company absent: searching "Debasheesh Bagchi" + "Ubiqtech Software" surfaced
      that exact person's real profile at a *different* employer. Name-only
      matching would have written it as a confident match.
    * Name mismatch: searching the roster of "Synthesis Winding Technologies"
      returned ten genuine employees of the right company, none of whom was the
      subject. Company-only matching would have written the top one.
    """
    if rules.require_company_token and company_coverage < rules.min_company_token_coverage:
        return CandidateRejection(True, RejectReason.COMPANY_ABSENT)
    if name_similarity < rules.min_name_similarity:
        return CandidateRejection(True, RejectReason.NAME_MISMATCH)
    return CandidateRejection(False)


def check_margin(top: float, runner_up: float, rules) -> CandidateRejection:
    """Reject when the field is too close to call.

    The live "Praveen Kumar / Director / Hyderabad" search returned eight
    different, equally plausible people. The input file itself contains 54 people
    named "Praveen Kumar", so homonyms are the norm rather than an edge case.
    When two candidates are within ``min_margin`` there is no principled way to
    choose, and the correct output is a blank.
    """
    if runner_up > 0.0 and (top - runner_up) < rules.min_margin:
        return CandidateRejection(True, RejectReason.AMBIGUOUS)
    return CandidateRejection(False)
