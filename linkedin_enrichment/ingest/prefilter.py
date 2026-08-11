"""Tier 0: decide which rows are worth spending a search query on.

Every query costs time (and, on a paid provider, money), and the free providers
this pipeline defaults to are the scarcest resource of all. Rows that cannot
possibly yield a >=95%-confidence match are therefore rejected before any network
call — the cheapest query is the one never issued.

None of these rules guess at an answer; they only recognise inputs that carry no
resolvable identity. Every skipped row is still written to the output with a
blank LinkedIn column and an explicit reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .normalize import NormalizedCompany, NormalizedName, normalize_company, normalize_name
from .reader import InputRow


class SkipReason(str, Enum):
    """Why a row was not searched. Written verbatim to Verification Notes."""

    NONE = ""
    MISSING_FIELDS = "missing name or company"
    NAME_NOT_A_PERSON = "name does not look like a person"
    NAME_TOO_SHORT = "name has too little signal to disambiguate"
    COMPANY_NOT_DISTINCTIVE = "company name is entirely generic"
    DUPLICATE = "duplicate of an earlier row"


# Tokens that mark a "name" as an organisation or a data-entry artefact rather
# than a person. All of these appear in the Name column of the real export
# (e.g. "Electronic Manufacturers", "Unni Cnc", "Ramanj Renaissance").
_NON_PERSON_TOKENS = {
    "private", "limited", "ltd", "pvt", "llp", "inc", "corp", "corporation",
    "company", "enterprises", "enterprise", "industries", "manufacturers",
    "manufacturing", "traders", "trading", "exports", "imports", "impex",
    "technologies", "solutions", "systems", "services", "associates",
    "consultants", "agencies", "distributors", "suppliers", "electronic",
    "electronics", "engineering", "engineers", "infrastructure", "developers",
    "builders", "properties", "estates", "cnc", "and", "co",
}

# A name made only of these is an artefact, not a person.
_PLACEHOLDER_NAMES = {
    "na", "n a", "nil", "none", "null", "unknown", "not available", "test",
    "director", "directors", "owner", "proprietor", "partner", "self",
}

_ALPHA_RE = re.compile(r"[a-z]")


@dataclass
class PrefilterResult:
    """Outcome of Tier 0 for one row."""

    searchable: bool
    reason: SkipReason
    name: NormalizedName
    company: NormalizedCompany
    dedup_key: str = ""


def looks_like_person(name: NormalizedName) -> bool:
    """Heuristic gate: does this look like a human name?

    Conservative by design — it only rejects when there is positive evidence of a
    non-person (organisational tokens, placeholders, no letters at all). A name it
    is unsure about is allowed through to the scorer, which has far more context.
    """
    if name.is_empty:
        return False

    joined = " ".join(name.tokens)
    if joined in _PLACEHOLDER_NAMES:
        return False
    if not _ALPHA_RE.search(joined):
        return False

    org_tokens = sum(1 for token in name.tokens if token in _NON_PERSON_TOKENS)
    if org_tokens == 0:
        return True
    # One organisational token can be a genuine surname collision; a majority of
    # them means the cell holds a company, not a person.
    return org_tokens < max(1, len(name.tokens) / 2)


def evaluate(row: InputRow, seen_keys: set[str] | None = None) -> PrefilterResult:
    """Apply Tier 0 to a single row.

    ``seen_keys`` accumulates (name, company) pairs already queued. Passing it
    marks later repeats as duplicates so they reuse the canonical row's result
    instead of paying for the same search twice — 3,660 rows in the real file.
    """
    name = normalize_name(row.name)
    company = normalize_company(row.company)

    if not row.has_minimum_fields or name.is_empty or company.is_empty:
        return PrefilterResult(False, SkipReason.MISSING_FIELDS, name, company)

    if not looks_like_person(name):
        return PrefilterResult(False, SkipReason.NAME_NOT_A_PERSON, name, company)

    # A single-token name ("Naganna") cannot be disambiguated among the millions
    # of LinkedIn profiles that share it, so it can never reach 95% confidence.
    if len(name.tokens) < 2 and not name.initials:
        return PrefilterResult(False, SkipReason.NAME_TOO_SHORT, name, company)

    # A company with no distinctive token ("Technology Resources Private Limited")
    # cannot anchor a search result, and the company gate would reject every
    # candidate anyway. Skipping it up front saves the query.
    if not company.has_distinctive_token:
        return PrefilterResult(False, SkipReason.COMPANY_NOT_DISTINCTIVE, name, company)

    dedup_key = f"{'-'.join(name.canonical)}|{company.key}"
    if seen_keys is not None:
        if dedup_key in seen_keys:
            return PrefilterResult(False, SkipReason.DUPLICATE, name, company, dedup_key)
        seen_keys.add(dedup_key)

    return PrefilterResult(True, SkipReason.NONE, name, company, dedup_key)
