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
    #: One-off discovery of a company's published contact routes, shared by
    #: every executive at that company.
    COMPANY_CONTACT = 3


@dataclass(frozen=True)
class Query:
    text: str
    tier: Tier
    company_key: str = ""
    #: Which rung of the ladder produced this query, for the variant-hit stats.
    step: str = ""


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


#: The person-level source ladder, in descending expected value.
#:
#: ``{person}`` and ``{brand}`` are pre-quoted phrases; ``{location}`` is left
#: unquoted as a soft ranking hint. A step whose template needs a field the row
#: does not have is dropped rather than emitted with a hole in it.
#:
#: Ordering is the only budget control. The runner works down the list and stops
#: the moment a match is confirmed, so the expensive tail is paid only for rows
#: that would otherwise have been abandoned as non-matches — which is exactly
#: when it is worth paying.
PERSON_LADDER: tuple[tuple[str, str], ...] = (
    # --- the person, directly ---
    ("linkedin_scoped",     "{person} {brand} site:linkedin.com/in"),
    ("linkedin_named",      "{person} {brand} LinkedIn"),
    ("open_web",            "{person} {brand}"),
    ("location",            "{person} {brand} {location}"),
    # --- the company's own publications ---
    ("leadership_page",     "{person} {brand} leadership team"),
    ("board_page",          '{person} {brand} "board of directors"'),
    ("executive",           "{person} {brand} executive profile"),
    ("annual_report",       "{person} {brand} annual report filetype:pdf"),
    ("investor_relations",  "{person} {brand} investor relations"),
    # --- third-party editorial ---
    ("press_release",       "{person} {brand} press release"),
    ("conference_speaker",  "{person} {brand} speaker conference"),
    ("economic_times",      "{person} {brand} site:economictimes.indiatimes.com"),
    ("business_standard",   "{person} {brand} site:business-standard.com"),
    ("moneycontrol",        "{person} {brand} site:moneycontrol.com"),
    ("bloomberg",           "{person} {brand} site:bloomberg.com"),
    ("crunchbase",          "{person} {brand} site:crunchbase.com"),
    # --- statutory filings ---
    ("mca_din",             "{person} {brand} director DIN"),
    ("exchanges",           "{person} {brand} site:bseindia.com OR site:nseindia.com"),
    # --- contact surfaces and socials ---
    ("contact",             "{person} {brand} email contact"),
    ("socials",             "{person} {brand} site:twitter.com OR site:x.com"),
)


def person_query_variants(
    name: NormalizedName, company: NormalizedCompany, location: str = "",
) -> list[Query]:
    """Every query worth trying for one person, in descending expected value.

    A single query formulation misses a great deal: a person absent from
    ``site:linkedin.com/in`` results may be named on the company's leadership
    page, in an annual report, in a funding announcement, on a conference
    programme, or in an exchange filing. The ladder runs through all of them
    before a row is called a non-match — one failed search is not evidence of
    absence, and neither are twenty, but twenty is a far better test than one.
    """
    fields = {
        "person": _quote(name.display),
        "brand": _quote(company.brand),
        "location": location or "",
    }

    seen: set[str] = set()
    queries: list[Query] = []
    for step, template in PERSON_LADDER:
        if "{location}" in template and not location:
            continue
        normalised = " ".join(template.format(**fields).split())
        if normalised in seen:
            continue
        seen.add(normalised)
        queries.append(Query(
            text=normalised, tier=Tier.PERSON, company_key=company.key, step=step,
        ))
    return queries


#: The company-level ladder. Run **once per company** and shared by every
#: executive there, which is what makes it affordable: 312,160 people belong to
#: 140,351 companies, so each query here is amortised ~2.2 ways — and far more
#: at the large employers, which are exactly the rows a wealth-management team
#: most wants.
COMPANY_CONTACT_LADDER: tuple[tuple[str, str], ...] = (
    ("website",            "{brand} official website contact"),
    ("leadership",         "{brand} leadership team management"),
    ("linkedin_company",   "{brand} site:linkedin.com/company"),
    ("investor_relations", "{brand} investor relations contact"),
    ("registered_office",  "{brand} registered office corporate office address"),
)


def company_contact_queries(
    company: NormalizedCompany, location: str = "", limit: int = 0,
) -> list[Query]:
    """Queries that find how to *reach* a company, rather than who works there."""
    fields = {"brand": _quote(company.brand), "location": location or ""}
    seen: set[str] = set()
    queries: list[Query] = []
    for step, template in COMPANY_CONTACT_LADDER:
        normalised = " ".join(template.format(**fields).split())
        if normalised in seen:
            continue
        seen.add(normalised)
        queries.append(Query(
            text=normalised, tier=Tier.COMPANY_CONTACT,
            company_key=company.key, step=step,
        ))
    return queries[:limit] if limit else queries


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
