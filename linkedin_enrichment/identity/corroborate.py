"""Independent corroboration — the second source a 99% claim requires.

At the shipped calibration, an exact name plus an exact company scores 0.9644.
That clears 95% but **fails 99%**. So at the precision bar every delivered match
must carry evidence beyond the LinkedIn result itself, and this module finds it.

That is not an arbitrary hurdle. Live measurement produced a textbook false
positive: the registry lists a "co-founder" of eMudhra who is not one of
eMudhra's founders, while a differently-employed person of that exact name does
exist on LinkedIn. Name and company agreement alone would have written it.

**What counts as independent.** The input came from the Indian MCA/ROC registry.
Company-data aggregators — ZaubaCorp, Tofler, IndiaMART, TheCompanyCheck — are
republications of that same registry. Agreeing with them is the input agreeing
with itself, which is worth nothing. Only a source with a separate provenance
counts: the company's own site, press, or an editorially-maintained directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..ingest.normalize import (
    NormalizedCompany,
    NormalizedName,
    company_token_coverage,
    name_similarity,
    normalize_name,
)
from ..providers.base import SerpResult

# Contact-scraper sites: they restate LinkedIn rather than corroborate it, and
# their data is frequently stale. These remain excluded — they are the one source
# class that adds no information at all.
SCRAPER_DOMAINS = frozenset({
    "rocketreach.co", "zoominfo.com", "signalhire.com", "apollo.io",
    "lusha.com", "contactout.com", "leadiq.com", "aroundeal.com", "hunter.io",
})

# Statutory and registry filings. These ARE accepted as corroboration: for an HNI
# prospect list, an MCA/NSE/BSE filing naming the person against the company is
# exactly the confirmation a sales team needs.
#
# Noted honestly: the input was itself derived from MCA data, so a registry hit
# is weaker evidence than an independent one — it can confirm the directorship is
# real but not that the LinkedIn profile is the same human. The source class is
# recorded on every row so this is visible downstream rather than hidden.
REGISTRY_DOMAINS = frozenset({
    "mca.gov.in", "nseindia.com", "bseindia.com", "sebi.gov.in",
    "zaubacorp.com", "tofler.in", "thecompanycheck.com", "instafinancials.com",
    "quickcompany.in", "falconebiz.com", "opencorporates.com", "indiafilings.com",
})

# Editorially maintained, press, and financial-data sources.
AUTHORITATIVE_DOMAINS = frozenset({
    # financial / company data
    "crunchbase.com", "pitchbook.com", "bloomberg.com", "reuters.com",
    "tracxn.com", "theorg.com", "owler.com", "cbinsights.com",
    # Indian business press
    "economictimes.indiatimes.com", "business-standard.com", "livemint.com",
    "financialexpress.com", "moneycontrol.com", "thehindubusinessline.com",
    "businesstoday.in", "cnbctv18.com", "vccircle.com", "entrackr.com",
    "inc42.com", "yourstory.com", "medianama.com", "thehindu.com",
    "indianexpress.com", "timesofindia.indiatimes.com", "ndtv.com",
    # international press
    "forbes.com", "ft.com", "wsj.com", "techcrunch.com", "fortune.com",
    "businessinsider.com", "cnbc.com",
    # professional directories
    "legal500.com", "chambers.com", "iflr1000.com",
})

# Page shapes that indicate a person-and-company assertion regardless of domain:
# conference speaker pages, press releases, startup team pages.
_CORROBORATING_PATH_RE = re.compile(
    r"/(speakers?|press|news|newsroom|media|announcements?|blog|awards?|"
    r"conference|summit|events?|team|about|leadership|management|people)\b",
    re.IGNORECASE,
)

# Paths that indicate a company's own leadership/about page.
LEADERSHIP_PATH_RE = re.compile(
    r"/(about|team|leadership|management|board|our-people|people|founders?|"
    r"who-we-are|company/leadership|governance|directors?)\b",
    re.IGNORECASE,
)


@dataclass
class Corroboration:
    """The outcome of looking for a second, independent source."""

    found: bool = False
    url: str = ""
    kind: str = ""          # official_site | press | directory | none
    detail: str = ""

    @property
    def summary(self) -> str:
        return f"{self.kind}: {self.url}" if self.found else "none found"


def domain_of(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _matches(url: str, domains: frozenset[str]) -> bool:
    domain = domain_of(url)
    return any(domain == d or domain.endswith("." + d) for d in domains)


def is_aggregator(url: str) -> bool:
    """True only for contact scrapers, which restate LinkedIn and add nothing."""
    return _matches(url, SCRAPER_DOMAINS)


def is_registry(url: str) -> bool:
    """Statutory/registry filings — accepted, but recorded as the weaker class."""
    return _matches(url, REGISTRY_DOMAINS)


def is_authoritative(url: str) -> bool:
    return _matches(url, AUTHORITATIVE_DOMAINS)


def is_linkedin(url: str) -> bool:
    return _matches(url, frozenset({"linkedin.com"}))


def looks_like_editorial_page(url: str) -> bool:
    """A press release, speaker page, or team page on any domain."""
    try:
        return bool(_CORROBORATING_PATH_RE.search(urlparse(url).path or ""))
    except ValueError:
        return False


def looks_like_company_site(url: str, company: NormalizedCompany) -> bool:
    """Is this the company's own website?

    Matched on the domain carrying a distinctive brand token, so
    ``greythr.com``/``greytip.com`` counts for "Greytip Software" but a random
    blog mentioning it does not.
    """
    domain = domain_of(url)
    if not domain or is_aggregator(url):
        return False
    stem = domain.split(".")[0].replace("-", "")
    for token, weight in zip(company.tokens, company.weights):
        if weight >= 1.0 and len(token) >= 4 and token in stem:
            return True
    return False


def assess(
    results: list[SerpResult],
    name: NormalizedName,
    company: NormalizedCompany,
    *,
    min_name_similarity: float = 0.82,
    min_company_coverage: float = 0.60,
) -> Corroboration:
    """Find an independent source naming both this person and this company.

    Both must appear in the *same* result. A page that mentions the company and
    happens to list a different person is not evidence about this person.
    """
    name_tokens = frozenset(name.tokens)
    best: Corroboration | None = None

    for result in results:
        url = result.url or ""
        if not url or is_aggregator(url):
            continue

        text = result.text
        if not text:
            continue

        # The person must actually be named on the page.
        if name_similarity(name, normalize_name(_leading_name(text))) < min_name_similarity \
                and not _contains_full_name(text, name):
            continue

        # And the company must be named too, independently of the person's name —
        # otherwise an eponymous company corroborates itself.
        if company_token_coverage(company, text, exclude_tokens=name_tokens) < min_company_coverage:
            continue

        # Any one of these is sufficient. They are checked strongest-first so the
        # recorded source is the best available, but the first hit ends the search
        # — requiring several sources per row costs recall and buys little.
        if looks_like_company_site(url, company):
            kind = ("official_site_leadership"
                    if LEADERSHIP_PATH_RE.search(urlparse(url).path or "")
                    else "official_site")
            return Corroboration(True, url, kind, result.title[:160])

        if is_authoritative(url):
            return Corroboration(True, url, "press_or_financial_data", result.title[:160])

        if looks_like_editorial_page(url):
            return Corroboration(True, url, "press_release_or_speaker_page", result.title[:160])

        if is_linkedin(url):
            # A second LinkedIn surface (company page, post, or a different
            # profile view) naming both. Weaker, so held back in case something
            # better appears later in the result set.
            best = best or Corroboration(True, url, "linkedin_secondary", result.title[:160])
            continue

        if is_registry(url):
            # Accepted, but flagged: the input came from MCA data, so this
            # confirms the directorship rather than the person's identity.
            best = best or Corroboration(True, url, "registry_filing", result.title[:160])
            continue

        # An unclassified domain that names both is still real evidence.
        best = best or Corroboration(True, url, "other_public_source", result.title[:160])

    if best is not None:
        return best
    return Corroboration(
        False, "", "none",
        "no public source found naming both this person and this company",
    )


def _leading_name(text: str) -> str:
    head = (text or "").split("|")[0]
    return head.split(" - ")[0].strip() or head.strip()


def _contains_full_name(text: str, name: NormalizedName) -> bool:
    """Do all substantial name tokens appear somewhere in the page text?"""
    lowered = (text or "").lower()
    substantial = [t for t in name.tokens if len(t) >= 3]
    if not substantial:
        return False
    return all(t in lowered for t in substantial)
