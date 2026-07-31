"""The query ladder — how the pipeline spends its scarcest resource.

A naive implementation issues two or three queries per person: 312,160 people
means 600k–900k queries. On a free provider that is months of wall-clock; on a
paid one it is a four-figure bill. The ladder cuts that by roughly 4x without
weakening any matching rule.

    Tier 1  one roster query per *company*  ("site:linkedin.com/in \"<brand>\"")
            140,351 companies instead of 312,160 people, and the result is shared
            by everyone at that company. Micro-companies have few staff, so a
            single roster often covers all of them.

    Tier 1b negative cache. If no result in the roster carries the company's
            brand tokens, that company has no findable LinkedIn footprint. Since
            a >=95% match *requires* company evidence (see identity/reject.py),
            no person at that company can ever qualify — so every one of them is
            resolved to a blank with zero further queries. This is a logical
            consequence of the reject rule, not a heuristic shortcut.

    Tier 2  per-person query, issued only when the company *does* have a
            footprint but the subject was not in the roster. Live validation
            showed a roster query for Greytip Software returned nine employees
            but not the founder, so this tier is required for recall.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from ..ingest.normalize import NormalizedCompany, NormalizedName


class Tier(IntEnum):
    COMPANY_ROSTER = 1
    PERSON = 2


@dataclass(frozen=True)
class Query:
    text: str
    tier: Tier
    company_key: str = ""


def _quote(value: str) -> str:
    """Wrap a phrase in quotes, stripping any embedded quotes that would break it."""
    return '"' + value.replace('"', " ").strip() + '"'


def company_roster_query(company: NormalizedCompany) -> Query:
    """Tier 1: find any LinkedIn profile that mentions this company.

    Restricted to ``/in/`` so the result set is people rather than company pages,
    job posts and directory listings — those are filtered later anyway, but not
    wasting result slots on them materially improves roster recall.
    """
    return Query(
        text=f'site:linkedin.com/in {_quote(company.brand)}',
        tier=Tier.COMPANY_ROSTER,
        company_key=company.key,
    )


def person_query(
    name: NormalizedName, company: NormalizedCompany, location: str = ""
) -> Query:
    """Tier 2: find this specific person at this specific company.

    Both phrases are quoted because the conjunction is exactly what the scorer
    requires; an unquoted query returns results matching either term and wastes
    the ten result slots on near-misses. The city is added unquoted as a soft
    ranking hint — quoting it would exclude profiles that write "Bengaluru".
    """
    parts = [_quote(name.display), _quote(company.brand), "site:linkedin.com/in"]
    if location:
        parts.append(location)
    return Query(text=" ".join(parts), tier=Tier.PERSON, company_key=company.key)


def person_fallback_query(name: NormalizedName, company: NormalizedCompany) -> Query:
    """Tier 2 fallback without the ``site:`` operator.

    Some free providers (notably SearXNG instances whose upstream engines are
    partially blocked) return nothing for ``site:`` queries. Dropping the operator
    trades precision for recall; the reject rules still filter non-LinkedIn URLs,
    so this cannot introduce a false positive.
    """
    return Query(
        text=f"{_quote(name.display)} {_quote(company.brand)} LinkedIn",
        tier=Tier.PERSON,
        company_key=company.key,
    )


def person_query_variants(
    name: NormalizedName, company: NormalizedCompany, location: str = "",
) -> list[Query]:
    """Every query worth trying for one person, in descending expected value.

    A single query formulation misses a great deal: a person absent from
    ``site:linkedin.com/in`` results may be named on the company's leadership
    page, in a funding announcement, or in a Crunchbase entry. The ladder runs
    through all of these before a row is called a non-match — one failed search
    is not evidence of absence.

    Ordered so the cheapest, highest-yield formulations run first; the runner
    stops as soon as a match is confirmed, so later variants only cost anything
    for rows that would otherwise have been abandoned.
    """
    person, brand = _quote(name.display), _quote(company.brand)
    templates = [
        f"{person} {brand} site:linkedin.com/in",
        f"{person} {brand} LinkedIn",
        f"{person} {brand}",
        f"{person} {brand} leadership",
        f"{person} {brand} executive",
        f"{person} {brand} director",
        f"{person} {brand} press release",
        f"{person} {brand} crunchbase",
        f"{person} {brand} bloomberg",
        f"{person} {company.brand} profile",
    ]
    if location:
        templates.insert(2, f"{person} {brand} {location}")

    seen: set[str] = set()
    queries: list[Query] = []
    for text in templates:
        normalised = " ".join(text.split())
        if normalised in seen:
            continue
        seen.add(normalised)
        queries.append(Query(text=normalised, tier=Tier.PERSON, company_key=company.key))
    return queries


def roster_covers_company(results, company: NormalizedCompany, name_tokens=frozenset()) -> bool:
    """Does this roster show any real evidence of the company on LinkedIn?

    Deliberately *not* "did the search return anything". Live validation showed
    that a query for a company with no presence still returns unrelated noise —
    a search for "Natesh Impex" returned an unrelated sales director and a
    Wikipedia page. The test must therefore be that some result actually carries
    the company's brand tokens.

    ``name_tokens`` excludes tokens shared with the person's own name, so an
    eponymous micro-company cannot appear to corroborate itself.
    """
    from ..ingest.normalize import company_token_coverage
    from ..providers.base import is_profile_url

    for result in results:
        if not is_profile_url(result.url):
            continue
        coverage = company_token_coverage(company, result.text, exclude_tokens=name_tokens)
        if coverage >= 0.6:
            return True
    return False
