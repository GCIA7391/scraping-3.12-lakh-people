"""Candidate scoring and the final match decision.

Pipeline for one person:

    SERP results
      -> structural screen (is it even a personal LinkedIn profile?)
      -> feature extraction (name, company, slug, city, locale, industry, role)
      -> raw score  = core conjunction + corroboration
      -> confidence = calibration(raw score)
      -> mandatory evidence gates + ambiguity margin
      -> Decision

Nothing here ever invents a value. A candidate that fails any gate is either
dropped or routed to the review queue; it is never written to the output's
LinkedIn column.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from ..ingest.normalize import (
    NormalizedCompany,
    NormalizedName,
    company_token_coverage,
    location_match,
    name_similarity,
    name_slug_agreement,
    normalize_company,
    normalize_name,
)
from ..providers.base import SerpResult, canonical_profile_url
from .reject import (
    CandidateRejection,
    RejectReason,
    check_candidate,
    check_margin,
    is_company_account,
    screen_result,
)


class Decision(str, Enum):
    MATCHED = "matched"
    BLANK_LOW_CONFIDENCE = "blank_low_confidence"
    BLANK_AMBIGUOUS = "blank_ambiguous"
    BLANK_NO_CANDIDATE = "blank_no_candidate"
    BLANK_SKIPPED = "blank_skipped"
    BLANK_ERROR = "blank_error"


@dataclass(frozen=True)
class Subject:
    """The person we are trying to find, in normalised form."""

    row_uid: str
    name: NormalizedName
    company: NormalizedCompany
    location: str = ""
    industry: str = ""
    designation: str = ""

    @classmethod
    def from_fields(
        cls, row_uid: str, name: str, company: str,
        location: str = "", industry: str = "", designation: str = "",
    ) -> "Subject":
        return cls(
            row_uid=row_uid,
            name=normalize_name(name),
            company=normalize_company(company),
            location=location, industry=industry, designation=designation,
        )

    @property
    def name_tokens(self) -> frozenset[str]:
        return frozenset(self.name.tokens)

    @property
    def company_tokens(self) -> frozenset[str]:
        return frozenset(self.company.tokens)


@dataclass
class ScoredCandidate:
    """One search result, scored against a subject."""

    result: SerpResult
    url: str = ""
    features: dict[str, float] = field(default_factory=dict)
    raw_score: float = 0.0
    confidence: float = 0.0
    rejection: CandidateRejection = field(default_factory=lambda: CandidateRejection(False))

    @property
    def rejected(self) -> bool:
        return self.rejection.rejected

    @property
    def reason(self) -> str:
        return self.rejection.reason.value

    def evidence(self) -> str:
        """Human-readable feature summary for Verification Notes / review queue."""
        parts = [f"{k}={v:.2f}" for k, v in sorted(self.features.items()) if v]
        return ", ".join(parts)


@dataclass
class MatchResult:
    """The outcome for one person."""

    decision: Decision
    linkedin_url: str = ""
    confidence: float = 0.0
    notes: str = ""
    source_urls: list[str] = field(default_factory=list)
    top_score: float = 0.0
    runner_up_score: float = 0.0
    margin: float = 0.0
    review_candidates: list[ScoredCandidate] = field(default_factory=list)

    @property
    def matched(self) -> bool:
        return self.decision is Decision.MATCHED


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate(raw_score: float, calibration) -> float:
    """Map a raw score to a probability.

    Uses a fitted isotonic curve when ``main.py calibrate`` has produced one,
    otherwise the shipped logistic. Both are monotone, so the ranking of
    candidates is identical either way — calibration only moves the threshold.
    """
    points = getattr(calibration, "isotonic_points", None)
    if points:
        knots = sorted((float(x), float(y)) for x, y in points)
        if raw_score <= knots[0][0]:
            return knots[0][1]
        if raw_score >= knots[-1][0]:
            return knots[-1][1]
        for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
            if x0 <= raw_score <= x1:
                if x1 == x0:
                    return y1
                ratio = (raw_score - x0) / (x1 - x0)
                return y0 + ratio * (y1 - y0)
    z = calibration.slope * (raw_score - calibration.midpoint)
    # Guard against overflow at extreme scores.
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-min(z, 60.0)))
    return math.exp(max(z, -60.0)) / (1.0 + math.exp(max(z, -60.0)))


# ---------------------------------------------------------------------------
# Feature extraction and scoring
# ---------------------------------------------------------------------------

def extract_features(subject: Subject, result: SerpResult) -> dict[str, float]:
    """Compute every scoring feature for one candidate."""
    text = result.text
    candidate_name = _candidate_name_from_title(result.title)

    # The company gate excludes tokens shared with the person's own name: an
    # eponymous company ("Zainab Fatima Fast Foods") would otherwise appear to
    # corroborate itself from a result that only matched the person's name.
    coverage = company_token_coverage(
        subject.company, text, exclude_tokens=subject.name_tokens
    )

    features = {
        "name_similarity": name_similarity(subject.name, candidate_name),
        "company_coverage": coverage,
        "slug_agreement": name_slug_agreement(subject.name, result.url),
        "location_match": location_match(subject.location, text),
        "country_subdomain": 1.0 if "//in.linkedin.com" in (result.url or "").lower() else 0.0,
        "industry_match": _industry_match(subject.industry, text),
        "designation_match": _designation_match(subject.designation, text),
    }
    return features


def score_candidate(subject: Subject, result: SerpResult, settings) -> ScoredCandidate:
    """Screen, extract features and score a single search result."""
    candidate = ScoredCandidate(result=result, url=canonical_profile_url(result.url))

    structural = screen_result(result, settings.reject)
    if structural.rejected:
        candidate.rejection = structural
        return candidate

    if is_company_account(result, subject.company_tokens):
        candidate.rejection = CandidateRejection(True, RejectReason.COMPANY_PROFILE)
        return candidate

    features = extract_features(subject, result)
    candidate.features = features

    weights = settings.weights
    # The conjunction: a product, so either factor collapsing collapses the score.
    core = features["name_similarity"] * features["company_coverage"]
    raw = weights.core_conjunction * core
    raw += weights.slug_agreement * features["slug_agreement"]
    raw += weights.location_match * features["location_match"]
    raw += weights.country_subdomain * features["country_subdomain"]
    raw += weights.industry_match * features["industry_match"]
    # Designation only ever *rewards*. In this dataset "Director" is an MCA board
    # appointment, not a job title, so its absence from a LinkedIn headline is
    # expected and must never count against a candidate.
    raw += weights.designation_match * features["designation_match"]

    candidate.raw_score = min(1.0, raw)
    candidate.confidence = calibrate(candidate.raw_score, settings.calibration)

    candidate.rejection = check_candidate(
        name_similarity=features["name_similarity"],
        company_coverage=features["company_coverage"],
        rules=settings.reject,
    )
    return candidate


def resolve(subject: Subject, results: list[SerpResult], settings) -> MatchResult:
    """Score every result and produce the final decision for one person."""
    scored = [score_candidate(subject, r, settings) for r in results]
    survivors = sorted(
        (c for c in scored if not c.rejected), key=lambda c: -c.raw_score
    )

    # Candidates that failed a gate but still look interesting are worth a human
    # glance. They are never promoted to a match.
    near_misses = [
        c for c in scored
        if c.rejected and c.confidence >= settings.review_queue_floor
        and c.rejection.reason in {RejectReason.NAME_MISMATCH, RejectReason.COMPANY_ABSENT}
    ]

    if not survivors:
        return MatchResult(
            decision=Decision.BLANK_NO_CANDIDATE,
            notes="no search result passed the name and company evidence gates",
            review_candidates=near_misses[:3],
            source_urls=[r.url for r in results[:3]],
        )

    top = survivors[0]
    runner_up = survivors[1] if len(survivors) > 1 else None
    runner_up_score = runner_up.raw_score if runner_up else 0.0
    margin = top.raw_score - runner_up_score

    base = {
        "top_score": top.raw_score,
        "runner_up_score": runner_up_score,
        "margin": margin if runner_up else 1.0,
        "source_urls": [c.url for c in survivors[:3]],
    }

    # Two distinct profiles that both survive the gates and score alike: there is
    # no principled way to choose, so choose neither.
    if runner_up and top.url != runner_up.url:
        ambiguity = check_margin(top.raw_score, runner_up_score, settings.reject)
        if ambiguity.rejected:
            return MatchResult(
                decision=Decision.BLANK_AMBIGUOUS,
                confidence=top.confidence,
                notes=(
                    f"ambiguous: top two candidates within {margin:.3f} "
                    f"(minimum separation {settings.reject.min_margin}); "
                    f"{top.url} vs {runner_up.url}"
                ),
                review_candidates=survivors[:2],
                **base,
            )

    if top.confidence < settings.confidence_threshold:
        return MatchResult(
            decision=Decision.BLANK_LOW_CONFIDENCE,
            confidence=top.confidence,
            notes=(
                f"best candidate {top.confidence:.3f} is below the "
                f"{settings.confidence_threshold:.2f} threshold ({top.evidence()})"
            ),
            review_candidates=survivors[:3],
            **base,
        )

    return MatchResult(
        decision=Decision.MATCHED,
        linkedin_url=top.url,
        confidence=top.confidence,
        notes=f"matched on {top.evidence()}",
        **base,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _candidate_name_from_title(title: str) -> NormalizedName:
    """Extract the person's name from a LinkedIn SERP title.

    LinkedIn titles follow "Name - Headline - Company | LinkedIn" (or "Name -
    Company | LinkedIn"), so the person's name is the first dash-delimited
    segment. Falls back to the whole title when the pattern does not hold.
    """
    head = (title or "").split("|")[0]
    lead = head.split(" - ")[0].strip() or head.strip()
    return normalize_name(lead)


# Industry labels in the source file are coarse buckets; these are the words that
# would plausibly appear in a LinkedIn headline for each.
_INDUSTRY_HINTS: dict[str, tuple[str, ...]] = {
    "technology": ("software", "technology", "tech", "it services", "engineer", "developer"),
    "internet": ("internet", "e-commerce", "ecommerce", "saas", "product"),
    "healthcare": ("healthcare", "hospital", "medical", "clinic", "pharma", "diagnostics"),
    "education": ("education", "school", "college", "university", "training", "academy"),
    "real estate": ("real estate", "realty", "property", "construction", "builder"),
    "retail": ("retail", "store", "merchandis", "fmcg"),
    "food & beverage": ("food", "beverage", "restaurant", "catering", "hospitality"),
    "logistics & transportation": ("logistics", "transport", "supply chain", "freight", "shipping"),
    "capital goods": ("manufacturing", "machinery", "industrial", "equipment"),
    "materials": ("materials", "chemical", "steel", "cement", "polymer"),
    "corporate services": ("consulting", "advisory", "services", "outsourcing"),
    "telecom & media": ("telecom", "media", "broadcast", "advertising", "publishing"),
    "automobiles & components": ("automotive", "automobile", "vehicle", "motors"),
    "agriculture": ("agriculture", "agri", "farming", "seeds", "crop"),
    "energy, environment & utilities": ("energy", "solar", "power", "utilities", "renewable"),
    "travel, hospitality & leisure": ("travel", "tourism", "hotel", "hospitality", "resort"),
    "consumer durables & apparel": ("apparel", "textile", "garment", "fashion", "consumer"),
    "personal services": ("salon", "wellness", "fitness", "personal"),
}


def _industry_match(industry: str, text: str) -> float:
    hints = _INDUSTRY_HINTS.get((industry or "").strip().lower())
    if not hints or not text:
        return 0.0
    lowered = text.lower()
    return 1.0 if any(hint in lowered for hint in hints) else 0.0


# Registry roles mapped to the words a LinkedIn headline would actually use.
_DESIGNATION_HINTS: dict[str, tuple[str, ...]] = {
    "director": ("director",),
    "managing director": ("managing director", "md", "director"),
    "wholetime director": ("director", "whole time director"),
    "executive director": ("executive director", "director"),
    "ceo": ("ceo", "chief executive"),
    "founder": ("founder",),
    "co-founder": ("co-founder", "cofounder", "founder"),
    "owner": ("owner", "proprietor"),
    "proprietor": ("proprietor", "owner"),
    "partner": ("partner",),
    "managing partner": ("managing partner", "partner"),
    "chairman": ("chairman", "chairperson"),
    "president": ("president",),
    "promoter": ("promoter", "founder"),
}


def _designation_match(designation: str, text: str) -> float:
    hints = _DESIGNATION_HINTS.get((designation or "").strip().lower())
    if not hints or not text:
        return 0.0
    lowered = text.lower()
    return 1.0 if any(hint in lowered for hint in hints) else 0.0
