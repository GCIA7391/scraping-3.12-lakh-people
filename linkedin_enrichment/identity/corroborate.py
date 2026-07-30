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

# Republications of the MCA registry. Agreeing with these is circular.
AGGREGATOR_DOMAINS = frozenset({
    "zaubacorp.com", "tofler.in", "indiamart.com", "thecompanycheck.com",
    "instafinancials.com", "quickcompany.in", "mca.gov.in", "companycheck.co.in",
    "falconebiz.com", "b2bhint.com", "startupindia.gov.in", "opencorporates.com",
    "indiafilings.com", "cleartax.in", "corpwiz.in", "registrationwala.com",
})

# Contact-scraper sites: they restate LinkedIn rather than corroborate it.
SCRAPER_DOMAINS = frozenset({
    "rocketreach.co", "zoominfo.com", "signalhire.com", "apollo.io",
    "lusha.com", "contactout.com", "leadiq.com", "aroundeal.com", "hunter.io",
})

# Editorially maintained or primary sources — these do carry independent weight.
AUTHORITATIVE_DOMAINS = frozenset({
    "crunchbase.com", "bloomberg.com", "reuters.com", "economictimes.indiatimes.com",
    "business-standard.com", "livemint.com", "forbes.com", "inc42.com",
    "yourstory.com", "entrackr.com", "moneycontrol.com", "thehindubusinessline.com",
    "legal500.com", "chambers.com", "theorg.com", "tracxn.com",
})

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


def is_aggregator(url: str) -> bool:
    """True for registry republications and contact scrapers — never independent."""
    domain = domain_of(url)
    return any(
        domain == d or domain.endswith("." + d)
        for d in (AGGREGATOR_DOMAINS | SCRAPER_DOMAINS)
    )


def is_authoritative(url: str) -> bool:
    domain = domain_of(url)
    return any(
        domain == d or domain.endswith("." + d) for d in AUTHORITATIVE_DOMAINS
    )


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

        # And the company must be corroborated independently of the person's name.
        if company_token_coverage(company, text, exclude_tokens=name_tokens) < min_company_coverage:
            continue

        if looks_like_company_site(url, company):
            kind = "official_site"
            if LEADERSHIP_PATH_RE.search(urlparse(url).path or ""):
                kind = "official_site_leadership"
            return Corroboration(True, url, kind, result.title[:160])

        if is_authoritative(url):
            return Corroboration(True, url, "directory_or_press", result.title[:160])

    return Corroboration(False, "", "none", "no independent source named both the person and the company")


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
