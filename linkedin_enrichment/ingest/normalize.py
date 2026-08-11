"""Normalisation of names, companies and locations.

This is the highest-leverage module in the pipeline: nearly all matching accuracy
is decided here. It exists because the input is Indian MCA/ROC registry data,
whose conventions differ systematically from how the same people and companies
appear on LinkedIn.

Three problems it solves, all observed in the real 312,160-row file:

1. **Registry names are legal long-forms.** "Melukote Shivaramu Lokesh" is how the
   Registrar of Companies records a person who writes "Lokesh M S" on LinkedIn.
   South Indian names commonly lead with a village or patronymic token that is
   dropped in everyday use, and the given name often comes *last*. Matching must
   therefore be order-insensitive and must treat an initial as a legitimate
   contraction of a full token.

2. **Company names are legal entity names, frequently corrupted.** LinkedIn shows
   the brand ("Greytip Software"), not the registered entity ("Greytip Software
   Private Limited"). The export also contains OCR damage — "Private Limi Ted",
   "Private Limite D" and trailing "Cn" all appear verbatim in the data.

3. **City names differ from their common forms.** The file only ever says
   "Bangalore" or "Hyderabad"; LinkedIn says "Bengaluru", "Karnataka",
   "Secunderabad" or "Telangana".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

import jellyfish
from rapidfuzz.distance import Indel, JaroWinkler
from unidecode import unidecode

# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

HONORIFICS = {
    "mr", "mrs", "ms", "miss", "dr", "prof", "shri", "sri", "smt", "smt.",
    "kum", "late", "capt", "col", "maj", "adv", "ca", "er", "md",
}

# Suffixes that are not part of the person's name.
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

# Transliteration families. Indian names romanise inconsistently; these groups
# are treated as equivalent tokens. Each set maps to its first member.
_TRANSLITERATION_GROUPS: tuple[tuple[str, ...], ...] = (
    ("lakshmi", "laxmi", "lakshmy", "laxmy"),
    ("krishna", "krishnan", "krsna", "kishan"),
    ("shankar", "sankar", "shanker", "sanker", "chandrashekar", "chandrasekhar"),
    ("reddy", "reddi", "readdy"),
    ("chandra", "chandhra", "chander"),
    ("prakash", "prakesh"),
    ("ramesh", "rameshh"),
    ("suresh", "sursh"),
    ("mohammed", "mohammad", "muhammad", "mohamed", "mohd"),
    ("syed", "sayed", "saiyed"),
    ("kumar", "kumaar"),
    ("venkat", "vengat", "venkata", "venkatesh", "venkatesha"),
    ("srinivas", "srinivasa", "sreenivas", "sreenivasa"),
    ("gopal", "gopala", "gopaal"),
    ("naidu", "nayudu"),
    ("achary", "acharya", "acharyulu"),
    ("rao", "raw"),
    ("sheikh", "shaik", "shaikh", "sheik"),
    ("abdul", "abdool"),
    ("prasad", "prasada"),
)

_TRANSLITERATION_MAP: dict[str, str] = {
    variant: group[0] for group in _TRANSLITERATION_GROUPS for variant in group
}

# Tokens that carry no identifying power in a person name.
_NAME_NOISE = {"and", "the", "of", "s", "o", "d", "w"}

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class NormalizedName:
    """A person name reduced to comparable tokens."""

    raw: str
    tokens: tuple[str, ...] = ()          # full tokens (length >= 2)
    initials: tuple[str, ...] = ()        # single-character tokens
    canonical: tuple[str, ...] = ()       # tokens after transliteration folding

    @property
    def is_empty(self) -> bool:
        return not self.tokens and not self.initials

    @property
    def display(self) -> str:
        return " ".join(self.tokens)


@lru_cache(maxsize=200_000)
def normalize_name(raw: str) -> NormalizedName:
    """Reduce a person name to comparable tokens.

    Handles the ALL-CAPS entries (2.8% of the file), strips honorifics, drops
    punctuation, and splits full tokens from bare initials so that "M S Lokesh"
    can later be aligned against "Melukote Shivaramu Lokesh".
    """
    if not raw:
        return NormalizedName(raw="")

    text = unidecode(raw).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    tokens: list[str] = []
    initials: list[str] = []
    for token in text.split(" "):
        if not token or token in HONORIFICS or token in NAME_SUFFIXES:
            continue
        if token.isdigit():
            continue
        if len(token) == 1:
            if token not in _NAME_NOISE or token in {"s", "d", "w"}:
                # A single letter is an initial. Even ambiguous ones ("s") carry
                # weak signal, so keep them — they are only ever used to *support*
                # a match, never to establish one on their own.
                initials.append(token)
            continue
        if token in _NAME_NOISE:
            continue
        tokens.append(token)

    canonical = tuple(_TRANSLITERATION_MAP.get(t, t) for t in tokens)
    return NormalizedName(
        raw=raw, tokens=tuple(tokens), initials=tuple(initials), canonical=canonical
    )


_VOWELS_AND_H = str.maketrans("", "", "aeiouyh")


def _consonant_skeleton(token: str) -> str:
    """Consonant-only form of a token, used to tell transliteration variants apart
    from genuinely different names.

    Indian romanisation varies almost entirely in *vowels* and in the optional
    aspirate 'h': "murthy"/"moorthi", "shivaramu"/"shivram", "venugopal"/
    "venugopala" all collapse to one skeleton. A **consonant** substitution is a
    different name: "rajesh"/"ramesh" (RJS vs RMS) and "debasheesh"/"debarshi"
    (DBSS vs DBRS) do not collapse. Both of those pairs occur in this dataset
    among the most common names, so the distinction matters a great deal.
    """
    return token.translate(_VOWELS_AND_H)


# Cap applied when two tokens disagree on consonants — they are treated as
# different name components regardless of orthographic similarity.
_CONSONANT_MISMATCH_CAP = 0.80


def _token_similarity(a: str, b: str) -> float:
    """Similarity of two full name tokens in [0, 1].

    Deliberately *not* plain Jaro-Winkler. JW's prefix bonus rates
    "debasheesh"/"debarshi" at 0.87 — two different real people who both appeared
    in the same live search result during design. Blending JW with a normalised
    indel (length-sensitive) similarity drops that pair to 0.77 while leaving
    genuine romanisation variants such as "krishnamurthy"/"krishnamoorthi" at
    0.87. Shared-prefix collisions are the dominant false-positive risk in this
    dataset, so the length-sensitive half is load-bearing.
    """
    if a == b:
        return 1.0
    ca = _TRANSLITERATION_MAP.get(a, a)
    cb = _TRANSLITERATION_MAP.get(b, b)
    if ca == cb:
        return 0.97

    score = 0.5 * JaroWinkler.similarity(a, b) + 0.5 * Indel.normalized_similarity(a, b)

    # Phonetic agreement rescues romanisation variants that edit distance misses,
    # but only lifts an already-plausible pair — it can never bridge two unrelated
    # names, because it is gated on the orthographic score first.
    if 0.75 <= score < 0.88 and len(a) >= 4 and len(b) >= 4:
        try:
            if jellyfish.metaphone(a) == jellyfish.metaphone(b):
                score = 0.88
        except Exception:  # pragma: no cover - jellyfish is defensive already
            pass

    # Consonant disagreement means a different name component, not a spelling
    # variant. One skeleton being a prefix of the other is a benign truncation
    # ("gurumurti" / "gurumurthy"), so only true divergence is capped.
    if len(a) >= 4 and len(b) >= 4:
        sa, sb = _consonant_skeleton(a), _consonant_skeleton(b)
        if sa and sb and not (sa == sb or sa.startswith(sb) or sb.startswith(sa)):
            score = min(score, _CONSONANT_MISMATCH_CAP)
    return score


# A token pair at or above this is treated as "the same name component".
_STRONG_TOKEN_MATCH = 0.90
# An initial matching the first letter of a full token is real but weak evidence.
_INITIAL_MATCH_SCORE = 0.85


def name_similarity(query: NormalizedName, candidate: NormalizedName) -> float:
    """Order-insensitive similarity between a registry name and a LinkedIn name.

    The comparison is deliberately asymmetric in what it forgives:

    * Tokens present in the registry name but absent from the candidate are
      only lightly penalised — dropped village/patronymic prefixes are the norm,
      not an error.
    * Tokens present in the *candidate* but absent from the registry name are
      penalised, because they indicate a different person.
    * At least one strong full-token match is mandatory. Without this rule a
      name consisting only of initials would align with almost anybody.
    """
    if query.is_empty or candidate.is_empty:
        return 0.0

    q_tokens = list(query.tokens)
    c_tokens = list(candidate.tokens)

    # Greedy best-first alignment without replacement. Name token counts are
    # tiny (<= 8), so the O(n*m) scan is free and avoids Hungarian-algorithm
    # machinery for no benefit.
    pairs: list[tuple[float, int, int]] = []
    for i, qt in enumerate(q_tokens):
        for j, ct in enumerate(c_tokens):
            pairs.append((_token_similarity(qt, ct), i, j))
    pairs.sort(key=lambda p: -p[0])

    used_q: set[int] = set()
    used_c: set[int] = set()
    matched: list[float] = []
    has_strong = False
    # The worst-matching pair of *substantial* tokens caps the final score. A name
    # is only as good as its weakest component: "Debasheesh Bagchi" vs "Debarshi
    # Bagchi" averages to 0.88 because the identical surname masks a mismatched
    # given name, and surnames here are dominated by a handful of very common
    # ones (Kumar, Reddy, Singh), so averaging systematically over-scores.
    weakest_substantial = 1.0
    for score, i, j in pairs:
        if i in used_q or j in used_c:
            continue
        if score < 0.70:
            break
        used_q.add(i)
        used_c.add(j)
        matched.append(score)
        if score >= _STRONG_TOKEN_MATCH and len(q_tokens[i]) >= 3:
            has_strong = True
        if len(q_tokens[i]) >= 4 and len(c_tokens[j]) >= 4:
            weakest_substantial = min(weakest_substantial, score)

    # Unmatched candidate tokens may still be explained by an initial on the
    # registry side ("Lokesh M S" vs "Melukote Shivaramu Lokesh" in reverse).
    leftover_c = [j for j in range(len(c_tokens)) if j not in used_c]
    leftover_q = [i for i in range(len(q_tokens)) if i not in used_q]

    q_initials = list(query.initials)
    for j in list(leftover_c):
        first = c_tokens[j][0]
        if first in q_initials:
            q_initials.remove(first)
            used_c.add(j)
            matched.append(_INITIAL_MATCH_SCORE)
            leftover_c.remove(j)

    c_initials = list(candidate.initials)
    for i in list(leftover_q):
        first = q_tokens[i][0]
        if first in c_initials:
            c_initials.remove(first)
            used_q.add(i)
            matched.append(_INITIAL_MATCH_SCORE)
            leftover_q.remove(i)

    if not matched or not has_strong:
        # No shared full name component: not the same person, whatever the
        # initials suggest.
        return 0.0

    base = sum(matched) / len(matched)

    # Penalise candidate tokens we could not explain at all. Each unexplained
    # token is a name component this person does not have.
    unexplained_ratio = len(leftover_c) / max(len(c_tokens), 1)
    base *= (1.0 - 0.55 * unexplained_ratio)

    # Mild penalty for registry tokens the candidate lacks. Kept small because
    # dropping a prefix is normal behaviour, not evidence of a different person.
    dropped_ratio = len(leftover_q) / max(len(q_tokens), 1)
    base *= (1.0 - 0.15 * dropped_ratio)

    # Weakest-link cap (see above).
    base = min(base, weakest_substantial)

    return max(0.0, min(1.0, base))


def name_slug_agreement(name: NormalizedName, url: str) -> float:
    """How well a LinkedIn URL slug agrees with the person's name.

    ``linkedin.com/in/girishrowjee`` -> "girishrowjee" contains both name tokens.
    Slugs are user-chosen and often carry a random suffix, so this is corroborating
    evidence only — never sufficient on its own.
    """
    match = re.search(r"/in/([^/?#]+)", url)
    if not match or name.is_empty:
        return 0.0

    slug = unidecode(match.group(1)).lower()
    # Strip LinkedIn's disambiguating hex/numeric tail (e.g. "-683b3b1").
    slug = re.sub(r"-[0-9a-f]{4,}$", "", slug)
    flat = re.sub(r"[^a-z]", "", slug)
    if not flat:
        return 0.0

    hits = sum(1 for token in name.tokens if len(token) >= 3 and token in flat)
    return hits / max(len(name.tokens), 1)


# ---------------------------------------------------------------------------
# Companies
# ---------------------------------------------------------------------------

# OCR/data-entry damage observed verbatim in the export. Applied before tokenising.
_OCR_REPAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\blimi\s+ted\b"), "limited"),
    (re.compile(r"\blimite\s+d\b"), "limited"),
    (re.compile(r"\blimit\s+ed\b"), "limited"),
    (re.compile(r"\bpriva\s+te\b"), "private"),
    (re.compile(r"\bpvt\.?\s*ltd\.?\b"), "private limited"),
    (re.compile(r"\bp\s*ltd\b"), "private limited"),
)

# Legal-form tokens carrying no brand information.
_LEGAL_TOKENS = {
    "private", "limited", "ltd", "pvt", "llp", "plc", "inc", "incorporated",
    "corporation", "corp", "company", "co", "and", "the", "of",
    # Trailing registry artefacts seen in the data ("... Private Limited Cn").
    "cn", "cin",
}

# Tokens that are real words but shared by thousands of companies. A match on
# these alone must not satisfy the company gate, so they are weighted down.
_GENERIC_BRAND_TOKENS = {
    "technologies", "technology", "tech", "solutions", "solution", "systems",
    "system", "services", "service", "software", "consultancy", "consulting",
    "consultants", "enterprises", "enterprise", "industries", "industry",
    "international", "global", "india", "indian", "group", "holdings",
    "ventures", "trading", "traders", "exports", "imports", "impex",
    "infra", "infrastructure", "projects", "developers", "builders",
    "associates", "partners", "agencies", "marketing", "distributors",
    "products", "manufacturing", "engineering", "engineers", "labs",
    "laboratories", "healthcare", "pharma", "pharmaceuticals", "foods",
    "food", "retail", "digital", "media", "entertainment", "capital",
    "finance", "financial", "investments", "realty", "estates", "properties",
    "management", "resources", "networks", "communications", "energy",
    "power", "motors", "automobiles", "packaging", "textiles", "apparels",
    "new", "sri", "shree", "shri", "star", "royal", "prime", "super",
}

_GENERIC_WEIGHT = 0.25
_DISTINCTIVE_WEIGHT = 1.0


@dataclass(frozen=True)
class NormalizedCompany:
    """A company reduced to its brand tokens."""

    raw: str
    tokens: tuple[str, ...] = ()
    key: str = ""
    brand: str = ""
    weights: tuple[float, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.tokens

    @property
    def has_distinctive_token(self) -> bool:
        """True if at least one token is not a generic industry word.

        A company whose every token is generic ("Technology Resources Private
        Limited") cannot be reliably anchored in a search result, and rows for it
        are held to a stricter standard downstream.
        """
        return any(w == _DISTINCTIVE_WEIGHT for w in self.weights)


@lru_cache(maxsize=200_000)
def normalize_company(raw: str) -> NormalizedCompany:
    """Reduce a registered entity name to the brand tokens LinkedIn would show."""
    if not raw:
        return NormalizedCompany(raw="")

    text = unidecode(raw).lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()

    for pattern, replacement in _OCR_REPAIRS:
        text = pattern.sub(replacement, text)

    tokens = [t for t in text.split(" ") if t and t not in _LEGAL_TOKENS and not t.isdigit()]

    # If stripping legal tokens emptied the name, fall back to the raw tokens so
    # the row is still processable rather than silently unmatched.
    if not tokens:
        tokens = [t for t in text.split(" ") if t]

    weights = tuple(
        _GENERIC_WEIGHT if t in _GENERIC_BRAND_TOKENS else _DISTINCTIVE_WEIGHT
        for t in tokens
    )
    return NormalizedCompany(
        raw=raw,
        tokens=tuple(tokens),
        key="-".join(tokens),
        brand=" ".join(tokens),
        weights=weights,
    )


# Qualifiers a multinational appends to its Indian subsidiary. LinkedIn shows the
# parent brand ("HP"), the registry shows the entity ("HP PPS Services India").
_SUBSIDIARY_QUALIFIERS = frozenset({
    "india", "indian", "asia", "apac", "global", "international", "worldwide",
    "services", "solutions", "technologies", "technology", "systems", "software",
    "development", "centre", "center", "gbs", "pps", "gcc", "rnd", "labs",
    "operations", "consulting", "holdings", "ventures", "enterprises",
})


def brand_lead_tokens(company: NormalizedCompany, limit: int = 2) -> tuple[str, ...]:
    """The leading tokens that carry the parent brand.

    "HP PPS Services India Private Limited" -> ("hp",); the rest are subsidiary
    qualifiers. Used to accept a LinkedIn employer string that names the parent
    rather than the registered entity.

    Note the deliberate limit of this approach: it strips *qualifiers*, so it
    handles HP PPS Services India -> HP. It cannot handle a *rename* such as
    "Sorting Hat Technologies" -> "Unacademy", which shares no tokens at all and
    would need an alias lookup this pipeline does not have. Those simply stay
    unmatched rather than being guessed at.
    """
    lead: list[str] = []
    for token in company.tokens:
        if token in _SUBSIDIARY_QUALIFIERS:
            continue
        lead.append(token)
        if len(lead) >= limit:
            break
    return tuple(lead)


def brand_alias_match(company: NormalizedCompany, text: str) -> bool:
    """True if ``text`` names the company's parent brand.

    Requires the *first* distinctive token as a standalone word. Short brands
    like "HP" are allowed here even though the general token scan skips tokens of
    two characters or fewer, because as a leading brand token they are
    meaningful rather than noise — but only as a whole word, so "hp" does not
    match inside "sharp".
    """
    lead = brand_lead_tokens(company, limit=1)
    if not lead or not text:
        return False
    token = lead[0]
    if len(token) < 2:
        return False
    return bool(re.search(rf"\b{re.escape(token)}\b", unidecode(text).lower()))


def company_token_coverage(
    company: NormalizedCompany,
    text: str,
    exclude_tokens: frozenset[str] | set[str] | None = None,
) -> float:
    """Weighted fraction of a company's brand tokens present in ``text``.

    Two weightings make this the pipeline's decisive gate:

    * Generic tokens ("technologies", "solutions") count for little, so a result
      that merely shares the word "Technologies" cannot clear the gate. This is
      what stops the "right name, wrong company" failure mode.
    * ``exclude_tokens`` removes tokens the company shares with the *person's own
      name*. Indian micro-companies are routinely named after their founder
      ("Zainab Fatima Fast Foods"), so without this a search result matching only
      the person's name would appear to corroborate the company as well —
      double-counting one piece of evidence and defeating the whole gate. A real
      case from design validation cleared the gate at 0.62 on exactly this bug.
    """
    if company.is_empty or not text:
        return 0.0

    excluded = exclude_tokens or frozenset()
    scored = [
        (token, weight)
        for token, weight in zip(company.tokens, company.weights)
        if len(token) > 2 and token not in excluded
    ]
    total = sum(weight for _, weight in scored)
    if total <= 0:
        # Every distinguishing token is also part of the person's name, so the
        # company carries no independent evidence at all.
        return 0.0

    haystack = unidecode(text).lower()
    hit = sum(
        weight for token, weight in scored
        if re.search(rf"\b{re.escape(token)}", haystack)
    )
    return min(1.0, hit / total)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

# The file only ever contains "Bangalore" or "Hyderabad"; LinkedIn uses these forms.
LOCATION_ALIASES: dict[str, frozenset[str]] = {
    "bangalore": frozenset({
        "bangalore", "bengaluru", "bangaluru", "banglore",
        "karnataka", "bangalore urban", "bengaluru urban",
    }),
    "hyderabad": frozenset({
        "hyderabad", "hyderbad", "secunderabad", "telangana",
        "andhra pradesh", "cyberabad", "hitec city", "hitech city",
    }),
}


def normalize_location(raw: str) -> str:
    return _WS_RE.sub(" ", unidecode(raw or "").lower().strip())


def location_match(location: str, text: str) -> float:
    """1.0 if the candidate text mentions the person's city or its region."""
    key = normalize_location(location)
    if not key or not text:
        return 0.0
    aliases = LOCATION_ALIASES.get(key, frozenset({key}))
    haystack = unidecode(text).lower()
    return 1.0 if any(alias in haystack for alias in aliases) else 0.0
