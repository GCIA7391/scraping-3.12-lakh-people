"""Offline ranking: which rows to spend queries on first.

Precision mode works a ranked pool top-down and stops once the target number of
matches is reached. Ranking therefore buys **efficiency, not precision** — the
reject rules decide what is written; this only decides what gets tried first.
That distinction matters, because live measurement showed no offline signal
strongly predicts a match.

What the measurement did show (stratified samples, adversarially audited):

* Both confirmed matches had a **rare surname** that collapsed the candidate set
  to one person. Every reject failed because the company never appeared alongside
  the name in any result.
* Rarity is **necessary but not sufficient**: `Rupali Korade` is a unique name in
  this file and still had no discoverable tie to Wells Fargo.
* **Company footprint was not the constraint.** The stratum that scored 0/6 was
  entirely large, well-known employers (Unacademy, HP, ONPASSIVE, Appcito,
  MedImpact).
* The **Designation field is unreliable** — the file lists people as "owner" of
  IBM India and Apple India — so role gets a deliberately small weight.

Scores are 0–1000 integers so they can be stored and sorted in SQLite directly.
"""

from __future__ import annotations

import collections
import math
import re
from dataclasses import dataclass, field

from ..ingest.normalize import normalize_company, normalize_name
from ..ingest.prefilter import looks_like_person

# Role tiers. Ordered by seniority, weighted low because the field is noisy.
STRICT_EXEC_ROLES = frozenset({
    "founder", "co-founder", "cofounder", "ceo", "chief executive officer",
    "chairman", "chairperson", "promoter", "president",
    "executive chairman", "vice chairman",
})
SENIOR_ROLES = frozenset({
    "managing director", "joint managing director", "md & ceo",
    "whole time director & ceo",
})
OTHER_PREFERRED_ROLES = frozenset({
    "executive director", "owner", "proprietor", "partner", "managing partner",
})
PREFERRED_ROLES = STRICT_EXEC_ROLES | SENIOR_ROLES | OTHER_PREFERRED_ROLES

_TOKEN_RE = re.compile(r"[^a-z ]+")

# Component weights. Name resolvability dominates: it is the only signal that
# separated the confirmed matches, and an unresolvable name cannot reach 99%
# however impressive the company is.
W_NAME_RARITY = 0.45
W_COMPANY_SIZE = 0.25
W_ROLE = 0.12
W_MULTI_BOARD = 0.10
W_NAME_QUALITY = 0.08


@dataclass
class Corpus:
    """Frequency statistics over the whole input, built in one pass.

    Rarity is measured *within this file* because that is the population the
    homonyms actually come from: 54 "Praveen Kumar"s here matter more than the
    token's frequency in the world at large.

    Company-name tokens are counted separately, which is what lets the scorer
    tell a distinctive *person* name from a distinctive *junk* string. Both are
    rare; only the junk one ("Web Hi", "Apache Mewr", "Rockstar Productions" —
    all real entries in the Name column) is built from words that belong to
    company names.
    """

    token_counts: collections.Counter = field(default_factory=collections.Counter)
    company_token_counts: collections.Counter = field(default_factory=collections.Counter)
    company_sizes: collections.Counter = field(default_factory=collections.Counter)
    person_boards: dict[str, set[str]] = field(default_factory=lambda: collections.defaultdict(set))
    #: (company_key, role) -> how many people hold that role there.
    role_holders: collections.Counter = field(default_factory=collections.Counter)
    rows_seen: int = 0

    def observe(self, name: str, company: str, designation: str = '') -> None:
        normalized_name = normalize_name(name)
        normalized_company = normalize_company(company)

        for token in normalized_name.tokens:
            if len(token) > 2:
                self.token_counts[token] += 1
        for token in normalized_company.tokens:
            if len(token) > 2:
                self.company_token_counts[token] += 1
        role = (designation or '').strip().lower()
        if normalized_company.key:
            self.company_sizes[normalized_company.key] += 1
            if role in PREFERRED_ROLES:
                self.role_holders[(normalized_company.key, role)] += 1
            self.person_boards["-".join(normalized_name.canonical)].add(normalized_company.key)
        self.rows_seen += 1

    def role_dilution(self, company: str, designation: str) -> float:
        """DISABLED — kept for the record, always returns 0.

        The intuition was that a title shared by many people at one company is
        meaningless ("Managing Director, Apple India" a dozen times over), so it
        should be penalised. Applying it made the ranking *worse against measured
        ground truth*: both live-verified matches are exactly this shape —
        Chandrima Mitra is one of 38 partners at DSK Legal, and Prasanth
        Thallapragada one of 299 executive directors at Wells Fargo. Penalising
        dilution pushed both to the 96th percentile, i.e. the bottom of the pool.

        A large employer having many same-titled people turns out to indicate a
        substantial organisation whose staff *do* have public profiles, not a
        worthless row. Kept as a named no-op so the mistake is not re-introduced.
        """
        return 0.0

    def commercial_word_ratio(self, name: str) -> float:
        """Share of the name's tokens that behave like company words.

        A token appearing far more often in company names than in person names
        is a business word, not a given name. This is what separates
        "Thallapragada" (a real rare surname) from "Productions".
        """
        tokens = [t for t in normalize_name(name).tokens if len(t) > 2]
        if not tokens:
            return 1.0
        commercial = 0
        for token in tokens:
            as_person = self.token_counts.get(token, 0)
            as_company = self.company_token_counts.get(token, 0)
            if as_company >= 5 and as_company > as_person * 3:
                commercial += 1
        return commercial / len(tokens)

    # -- component scores, each in [0, 1] ---------------------------------
    def name_rarity(self, name: str) -> float:
        """How resolvable is this name inside this corpus?

        Driven by the *rarest* token, since one distinctive token is enough to
        collapse a search. "Kumar" appears 23,739 times and "Reddy" 22,355; a
        name made only of such tokens cannot be disambiguated at any confidence.
        """
        tokens = [t for t in normalize_name(name).tokens if len(t) > 2]
        if not tokens:
            return 0.0
        rarest = min(self.token_counts.get(t, 1) for t in tokens)
        # log scale: 1 occurrence -> 1.0, ~20k occurrences -> 0.0
        return max(0.0, min(1.0, 1.0 - math.log10(rarest) / math.log10(25_000)))

    def company_substance(self, company: str) -> float:
        """Director count as a proxy for the company being a real operation.

        Indian private limiteds need two directors, so two means nothing; the
        signal starts above that.
        """
        size = self.company_sizes.get(normalize_company(company).key, 0)
        if size <= 2:
            return 0.0
        return max(0.0, min(1.0, math.log10(size - 1) / math.log10(50)))

    def multi_board(self, name: str) -> float:
        boards = len(self.person_boards.get("-".join(normalize_name(name).canonical), ()))
        if boards <= 1:
            return 0.0
        return max(0.0, min(1.0, (boards - 1) / 5.0))


def role_score(designation: str) -> float:
    role = (designation or "").strip().lower()
    if role in STRICT_EXEC_ROLES:
        return 1.0
    if role in SENIOR_ROLES:
        return 0.7
    if role in OTHER_PREFERRED_ROLES:
        return 0.45
    return 0.0


def name_quality(name: str) -> float:
    """Penalise the artifacts that litter the Name column.

    "Rishav Specialist", "Rahat", "R Kk" and "Electronic Manufacturers" all
    appear in the top strata of the real file and are unresolvable in principle.
    """
    normalized = normalize_name(name)
    if not looks_like_person(normalized):
        return 0.0
    if len(normalized.tokens) < 2:
        return 0.1
    if len(normalized.tokens) == 2:
        return 0.8
    return 1.0


def is_preferred_role(designation: str) -> bool:
    return (designation or "").strip().lower() in PREFERRED_ROLES


def role_company_implausibility(designation: str, company_size: int) -> float:
    """Penalty in [0, 1] for role/company combinations that cannot be real.

    Nobody founds Apple India. A "Founder" or "CEO" recorded against a large
    multinational subsidiary is an MCA data artifact, and the first ranking pass
    put exactly these at the very top — "Founder, Apple India Private Limited"
    and "CEO, Apple India" outscored every genuine executive, because a junk name
    is rare, the company is huge and the title is senior.

    Large registered entities do have executive directors and managing directors,
    so only the founder-shaped titles are penalised.
    """
    role = (designation or "").strip().lower()
    if role not in {"founder", "co-founder", "cofounder", "promoter", "ceo"}:
        return 0.0
    if company_size >= 20:
        return 0.85
    if company_size >= 10:
        return 0.35
    return 0.0


def score_row(corpus: Corpus, name: str, company: str, designation: str) -> int:
    """Rank for one row, 0-1000. Higher is tried first."""
    quality = name_quality(name)
    if quality == 0.0:
        # A non-person can never resolve; park it at the very bottom rather than
        # letting a big company or senior title lift it.
        return 0

    # A name built from business words is a junk row however rare it looks.
    commercial = corpus.commercial_word_ratio(name)
    if commercial >= 0.5:
        return 0

    score = (
        W_NAME_RARITY * corpus.name_rarity(name)
        + W_COMPANY_SIZE * corpus.company_substance(company)
        + W_ROLE * role_score(designation)
        + W_MULTI_BOARD * corpus.multi_board(name)
        + W_NAME_QUALITY * quality
    )
    score *= (1.0 - commercial)

    company_size = corpus.company_sizes.get(normalize_company(company).key, 0)
    score *= (1.0 - 0.85 * role_company_implausibility(designation, company_size))
    # A title shared by many people at one company is not a title.
    score *= (1.0 - 0.80 * corpus.role_dilution(company, designation))

    return int(round(1000 * max(0.0, min(1.0, score))))
