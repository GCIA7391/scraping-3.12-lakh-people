"""Professional contact routes — the deliverable, alongside the LinkedIn URL.

A sales team does not need a LinkedIn URL. It needs *a way to reach the person*.
Those are not the same thing, and conflating them is what capped this pipeline:
a row could only succeed if **the person** was individually findable, when in
practice the company is findable far more often than the individual is.

So a row now carries two independent outputs:

* ``LinkedIn Profile`` — a probabilistic identity claim, still gated on
  confidence and corroboration, because writing the wrong person's profile is
  worse than writing nothing.
* ``Contact Routes`` — published, checkable facts about how to reach an
  executive at that company. Not confidence-gated, because "Acme publishes
  ir@acme.com on acme.com/investors" is not a guess about identity.

Ordered by directness, most direct first:

    direct_corporate_email -> executive_office -> investor_relations ->
    board_office -> assistant -> reception -> linkedin_profile ->
    linkedin_company -> official_social -> contact_form -> conference_page

Scope guardrail
---------------
Only routes an organisation has **published** for professional contact. In
particular this module will not emit:

* personal email addresses (a free-provider address whose local part is the
  person's own name),
* mobile numbers — an Indian 10-digit 6/7/8/9-series number is a personal
  handset far more often than a published switchboard, so the whole class is
  refused,
* **anything constructed rather than observed.** There is no address-pattern
  inference here on purpose. Every route below is a substring of text a source
  actually published, and carries the URL it came from so any entry can be
  checked by hand.

What this can see
-----------------
The pipeline never fetches pages (see ``providers/base``), so extraction runs
over search-result titles and snippets. That limits *literal* contact details to
whatever a snippet happens to expose — but the highest-volume routes are URLs
(leadership page, contact page, LinkedIn company page, conference profile), and
those come through the SERP intact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from ..identity.corroborate import domain_of, is_aggregator, looks_like_company_site
from ..ingest.normalize import (
    NormalizedCompany,
    NormalizedName,
    company_token_coverage,
    name_similarity,
    normalize_name,
)


class RouteType(str, Enum):
    """A way to reach an executive, in descending order of directness."""

    DIRECT_CORPORATE_EMAIL = "direct_corporate_email"
    EXECUTIVE_OFFICE = "executive_office"
    INVESTOR_RELATIONS = "investor_relations"
    BOARD_OFFICE = "board_office"
    ASSISTANT = "assistant"
    #: General corporate desk — switchboard, info@, press/media desk.
    RECEPTION = "reception"
    LINKEDIN_PROFILE = "linkedin_profile"
    LINKEDIN_COMPANY = "linkedin_company"
    OFFICIAL_SOCIAL = "official_social"
    CONTACT_FORM = "contact_form"
    CONFERENCE_PAGE = "conference_page"


#: Canonical directness order. Index 0 is the most direct.
DIRECTNESS: tuple[RouteType, ...] = (
    RouteType.DIRECT_CORPORATE_EMAIL,
    RouteType.EXECUTIVE_OFFICE,
    RouteType.INVESTOR_RELATIONS,
    RouteType.BOARD_OFFICE,
    RouteType.ASSISTANT,
    RouteType.RECEPTION,
    RouteType.LINKEDIN_PROFILE,
    RouteType.LINKEDIN_COMPANY,
    RouteType.OFFICIAL_SOCIAL,
    RouteType.CONTACT_FORM,
    RouteType.CONFERENCE_PAGE,
)

_RANK: dict[RouteType, int] = {t: i for i, t in enumerate(DIRECTNESS)}

#: Routes that reach a named individual rather than the organisation.
#:
#: ``EXECUTIVE_OFFICE`` is deliberately *not* here: a leadership page or a
#: ``ceo@`` mailbox reaches the office, which is a fact about the company and
#: survives the person moving on.
PERSON_SCOPED = frozenset({
    RouteType.DIRECT_CORPORATE_EMAIL,
    RouteType.ASSISTANT,
    RouteType.LINKEDIN_PROFILE,
    RouteType.CONFERENCE_PAGE,
})


@dataclass(frozen=True)
class ContactRoute:
    """One published way to reach someone, with the source that published it."""

    type: RouteType
    #: The route itself: an email address, a phone number, or a URL.
    value: str
    #: Where it was observed. Never empty — an unverifiable route is not emitted.
    source_url: str
    #: Short human description, e.g. "Investor relations page".
    label: str = ""
    #: ``person`` when the route reaches the individual, ``company`` when it
    #: reaches the organisation. A company route is still a usable lead.
    scope: str = "company"

    @property
    def rank(self) -> int:
        return _RANK.get(self.type, len(DIRECTNESS))

    @property
    def key(self) -> tuple[str, str]:
        """Dedupe key: the same address found twice is one route."""
        return (self.type.value, self.value.strip().lower())

    def render(self) -> str:
        """One line for the CSV: ``type: value (source)``."""
        return f"{self.type.value}: {self.value} ({self.source_url})"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

#: Consumer mailbox providers. An address here is a personal mailbox unless its
#: local part is unambiguously a role — many Indian micro-companies genuinely
#: publish info@gmail-style addresses as their only business contact.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.in", "yahoo.in",
    "hotmail.com", "outlook.com", "live.com", "msn.com", "rediffmail.com",
    "rediff.com", "aol.com", "icloud.com", "me.com", "protonmail.com",
    "proton.me", "zoho.com", "mail.com", "ymail.com", "gmx.com",
})

#: Local parts that identify an organisational mailbox rather than a person.
_ROLE_LOCALS: dict[str, RouteType] = {}


def _register(route: RouteType, *locals_: str) -> None:
    for item in locals_:
        _ROLE_LOCALS[item] = route


_register(
    RouteType.INVESTOR_RELATIONS,
    "ir", "investor", "investors", "investorrelations", "investorrelation",
    "investorservices", "shareholder", "shareholders", "grievance",
    "grievances", "investorgrievance",
)
_register(
    RouteType.BOARD_OFFICE,
    "board", "boardoffice", "boardsecretariat", "companysecretary", "cosec",
    "cs", "secretarial", "compliance", "complianceofficer", "governance",
)
_register(
    RouteType.EXECUTIVE_OFFICE,
    "ceo", "ceooffice", "officeoftheceo", "md", "mdoffice", "managingdirector",
    "chairman", "chairmanoffice", "chairmansoffice", "president",
    "executiveoffice", "execoffice", "cfo", "coo", "cto", "cxo", "leadership",
    "directors", "director",
)
_register(
    RouteType.ASSISTANT,
    "ea", "eatoceo", "eatomd", "pa", "patoceo", "patomd", "assistant",
    "executiveassistant", "secretary",
)
_register(
    RouteType.RECEPTION,
    "info", "contact", "contactus", "enquiry", "enquiries", "inquiry",
    "inquiries", "hello", "hi", "mail", "office", "admin", "reception",
    "frontdesk", "reachus", "connect", "general", "corporate", "corp",
    "sales", "support", "helpdesk", "customercare", "care", "service",
    "press", "media", "pr", "communications", "corpcomm", "newsroom",
)

_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9][A-Za-z0-9._%+-]{0,63})@"
    r"([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z]{2,24})+)"
)

#: Deliberately strict: a number is only recognised when it is *written* as a
#: phone number — an explicit +91, a trunk 0, or a 1800/1860 service prefix.
#: Anything looser turns turnover figures, PIN codes and years into "contacts".
_PHONE_RE = re.compile(
    r"(?:(?<=\D)|^)("
    r"\+91[\s.\-]?\d[\d\s.\-]{8,13}\d"      # +91 ...
    r"|\+\d{1,3}[\s.\-]?\d[\d\s.\-]{7,13}\d"  # other international
    r"|0\d{2,4}[\s.\-]\d[\d\s.\-]{5,10}\d"    # STD trunk, e.g. 080-2345 6789
    r"|1(?:800|860)[\s.\-]?\d[\d\s.\-]{4,10}\d"  # toll free / service
    r")(?=\D|$)"
)

_DIGITS_RE = re.compile(r"\D")


def is_free_mail_domain(domain: str) -> bool:
    return (domain or "").strip().lower().lstrip("@") in FREE_MAIL_DOMAINS


def _local_key(local: str) -> str:
    """Normalise an email local part for role lookup: ``ceo.office`` -> ``ceooffice``."""
    return re.sub(r"[^a-z0-9]", "", (local or "").lower())


def is_role_mailbox(local: str) -> bool:
    """Is this local part an organisational mailbox rather than an individual?"""
    key = _local_key(local)
    if key in _ROLE_LOCALS:
        return True
    # Compound role addresses: ceo.office, investor.relations.india, md-secretariat.
    return any(key.startswith(role) or key.endswith(role)
               for role in ("ir", "info", "contact", "enquiry", "sales", "support",
                            "office", "admin", "secretarial", "investor", "board"))


def looks_personal(local: str, name: NormalizedName | None) -> bool:
    """Does this local part look like the subject's own private address?

    ``rajesh.kumar@gmail.com`` for Rajesh Kumar is a personal mailbox and is out
    of scope. The same local part on the *company's* domain is a published
    corporate address and is in scope — that distinction is made by the caller,
    which only reaches here for non-corporate domains.
    """
    if name is None:
        return False
    key = _local_key(local)
    if not key:
        return False
    tokens = [t for t in name.tokens if len(t) >= 3]
    if not tokens:
        return False
    return any(t in key for t in tokens)


#: A landline is *written* with its area code split off — ``080-4123 4567``,
#: ``040 2345 6789``, ``+91 80 4123 4567``. A mobile is written as one 10-digit
#: block. That formatting is the only reliable discriminator available from a
#: snippet, and it is the same one a human reads.
_STD_SPLIT_RE = re.compile(r"^(?:\+?91[\s.\-]?|0)(\d{2,4})[\s.\-]\d")
_SERVICE_PREFIXES = ("1800", "1860")


def is_mobile_number(raw: str) -> bool:
    """Does this reduce to an Indian mobile — 10 significant digits, 6-9 series?

    A number of that shape is a personal handset far more often than a
    switchboard, and nothing in a search snippet proves which. It is refused as
    a class.
    """
    digits = _DIGITS_RE.sub("", raw or "")
    digits = digits[2:] if digits.startswith("91") and len(digits) == 12 else digits
    digits = digits.lstrip("0")
    return len(digits) == 10 and digits[0] in "6789"


def is_publishable_phone(raw: str) -> bool:
    """May this number be emitted as an organisational contact route?

    Accepted: toll-free/service lines, non-Indian international numbers, and
    numbers written with the area code split off from the local part — the form
    an organisation uses for its switchboard.

    Refused: everything else, including a bare 10-digit number and ``+91``
    followed by one. Those cannot be told apart from a personal handset, and the
    brief's line is drawn at personal phone numbers. This costs some legitimate
    small-company main lines; that is the intended trade.
    """
    text = " ".join((raw or "").split())
    digits = _DIGITS_RE.sub("", text)
    if not 8 <= len(digits) <= 15:
        return False
    if digits.startswith(_SERVICE_PREFIXES):
        return True
    if text.startswith("+") and not digits.startswith("91"):
        return True
    return bool(_STD_SPLIT_RE.match(text))


# ---------------------------------------------------------------------------
# Page classification
# ---------------------------------------------------------------------------

_PATH_ROUTES: tuple[tuple[re.Pattern[str], RouteType, str], ...] = (
    (re.compile(r"/(investor|investors|investor-relations|ir)\b", re.I),
     RouteType.INVESTOR_RELATIONS, "Investor relations page"),
    (re.compile(r"/(board|corporate-governance|governance|board-of-directors)\b", re.I),
     RouteType.BOARD_OFFICE, "Board / governance page"),
    (re.compile(r"/(leadership|management|our-team|team|our-people|people|"
                r"founders?|executives?|directors?|who-we-are|about-us|about)\b", re.I),
     RouteType.EXECUTIVE_OFFICE, "Leadership page"),
    (re.compile(r"/(contact|contact-us|contactus|reach-us|get-in-touch|enquiry|"
                r"enquire|write-to-us)\b", re.I),
     RouteType.CONTACT_FORM, "Contact page"),
    (re.compile(r"/(speakers?|speaker-profile|sessions?|agenda|conference|summit|"
                r"events?|awards?|panell?ists?)\b", re.I),
     RouteType.CONFERENCE_PAGE, "Conference / speaker page"),
)

#: Official social presences worth recording as a route.
_SOCIAL_DOMAINS: dict[str, str] = {
    "twitter.com": "X / Twitter", "x.com": "X / Twitter",
    "facebook.com": "Facebook", "instagram.com": "Instagram",
    "youtube.com": "YouTube", "medium.com": "Medium",
}

_LINKEDIN_COMPANY_RE = re.compile(r"linkedin\.com/(company|school|showcase)/", re.I)
_LINKEDIN_PROFILE_RE = re.compile(r"linkedin\.com/in/", re.I)


def classify_page(url: str, company: NormalizedCompany) -> tuple[RouteType, str] | None:
    """What kind of contact route, if any, does this URL itself represent?"""
    if not url or is_aggregator(url):
        return None

    if _LINKEDIN_PROFILE_RE.search(url):
        return RouteType.LINKEDIN_PROFILE, "LinkedIn profile"
    if _LINKEDIN_COMPANY_RE.search(url):
        return RouteType.LINKEDIN_COMPANY, "LinkedIn company page"

    domain = domain_of(url)
    for social, label in _SOCIAL_DOMAINS.items():
        if domain == social or domain.endswith("." + social):
            return RouteType.OFFICIAL_SOCIAL, label

    try:
        path = urlparse(url).path or "/"
    except ValueError:
        return None

    on_company_site = looks_like_company_site(url, company)
    for pattern, route, label in _PATH_ROUTES:
        if pattern.search(path):
            # A leadership-shaped path on someone else's domain is an article
            # about the company, not a way to reach it.
            if not on_company_site and route in (
                RouteType.EXECUTIVE_OFFICE, RouteType.INVESTOR_RELATIONS,
                RouteType.BOARD_OFFICE, RouteType.CONTACT_FORM,
            ):
                continue
            return route, label

    if on_company_site and path in ("", "/"):
        return RouteType.CONTACT_FORM, "Company website"
    return None


def classify_email(local: str, domain: str, *, on_company_domain: bool) -> RouteType:
    """Which route type does this mailbox represent?"""
    key = _local_key(local)
    if key in _ROLE_LOCALS:
        return _ROLE_LOCALS[key]
    for role, route in _ROLE_LOCALS.items():
        if len(role) >= 4 and (key.startswith(role) or key.endswith(role)):
            return route
    if on_company_domain:
        # A named mailbox on the company's own domain, published by the company.
        return RouteType.DIRECT_CORPORATE_EMAIL
    return RouteType.RECEPTION


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_emails(text: str) -> list[tuple[str, str]]:
    """Return ``(local, domain)`` pairs literally present in ``text``."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for local, domain in _EMAIL_RE.findall(text or ""):
        address = f"{local}@{domain}".lower()
        if address in seen:
            continue
        seen.add(address)
        out.append((local, domain.lower()))
    return out


def extract_phones(text: str) -> list[str]:
    """Return phone numbers literally present in ``text``, as written."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _PHONE_RE.findall(text or ""):
        number = " ".join(raw.split()).strip(" .-")
        digits = _DIGITS_RE.sub("", number)
        if not 8 <= len(digits) <= 15:
            continue
        if digits in seen:
            continue
        seen.add(digits)
        out.append(number)
    return out


def names_person(text: str, name: NormalizedName | None) -> bool:
    """Is this specific person actually named in the result?

    The same test corroboration uses. Without it a person-scoped route is just a
    page that turned up in a query — a conference programme listing somebody
    else, or a stranger's profile — and emitting that would be exactly the class
    of false positive the rest of the pipeline exists to prevent.
    """
    if name is None:
        return False
    lowered = (text or "").lower()
    substantial = [t for t in name.tokens if len(t) >= 3]
    if not substantial:
        return False
    if all(t in lowered for t in substantial):
        return True
    return name_similarity(name, normalize_name(_leading_phrase(text))) >= 0.88


def _leading_phrase(text: str) -> str:
    head = (text or "").split("|")[0]
    return head.split(" - ")[0].strip() or head.strip()


def names_company(result, company: NormalizedCompany) -> bool:
    """Does this result actually concern the company we are working on?

    Guards the company-scoped routes that are not already pinned to the
    company's own domain — a LinkedIn company page or a social account found in
    a person query may belong to an entirely different organisation.
    """
    url = (result.url or "").lower()
    slug = url.replace("-", "").replace("_", "")
    if any(
        weight >= 1.0 and len(token) >= 4 and token in slug
        for token, weight in zip(company.tokens, company.weights)
    ):
        return True
    return company_token_coverage(company, result.text) >= 0.60


def routes_from_result(
    result,
    company: NormalizedCompany,
    name: NormalizedName | None = None,
) -> list[ContactRoute]:
    """Every publishable contact route carried by a single search result.

    ``result`` is a ``SerpResult``; only its ``url`` and ``text`` are read.

    Two things are never produced here:

    * **A LinkedIn profile route.** A ``/in/`` URL in a result set is a candidate
      identity, not an established one, and the whole pipeline exists to avoid
      asserting the wrong one. The runner adds a profile route only for a match
      that cleared the confidence and corroboration gates.
    * **A person-scoped route on a page that does not name the person.**
    """
    url = (result.url or "").strip()
    if not url or is_aggregator(url):
        return []

    routes: list[ContactRoute] = []
    text = result.text
    on_company_site = looks_like_company_site(url, company)
    site_domain = domain_of(url)
    person_named = names_person(text, name)

    page = classify_page(url, company)
    if page is not None:
        route_type, label = page
        scope = "person" if route_type in PERSON_SCOPED else "company"
        if route_type is RouteType.LINKEDIN_PROFILE:
            page = None                      # see the docstring
        elif scope == "person" and not person_named:
            page = None
        elif route_type in (RouteType.LINKEDIN_COMPANY, RouteType.OFFICIAL_SOCIAL) \
                and not names_company(result, company):
            page = None
        if page is not None:
            routes.append(ContactRoute(
                type=route_type, value=url, source_url=url, label=label, scope=scope,
            ))

    for local, domain in extract_emails(text):
        address = f"{local}@{domain}".lower()
        on_company_domain = bool(
            site_domain and (domain == site_domain or domain.endswith("." + site_domain))
        ) or _domain_carries_brand(domain, company)

        if is_free_mail_domain(domain):
            # Consumer mailbox: only as a company route, never as a person's.
            if not is_role_mailbox(local) or looks_personal(local, name):
                continue
            on_company_domain = False
        elif not on_company_domain and looks_personal(local, name):
            # A named address on an unrelated domain is not a published
            # corporate route for this person.
            continue

        route_type = classify_email(local, domain, on_company_domain=on_company_domain)
        scope = "person" if route_type in PERSON_SCOPED else "company"
        if scope == "person" and not person_named:
            # A named corporate mailbox on a page that does not name this person
            # belongs to somebody else at the company.
            continue
        routes.append(ContactRoute(
            type=route_type, value=address, source_url=url,
            label=f"Published on {site_domain}" if site_domain else "Published address",
            scope=scope,
        ))

    # Phone numbers are only taken from the organisation's own pages. A number
    # in a news article is as likely to be the reporter's desk as the company's.
    if on_company_site:
        for number in extract_phones(text):
            if not is_publishable_phone(number):
                continue
            routes.append(ContactRoute(
                type=RouteType.RECEPTION, value=number, source_url=url,
                label=f"Published on {site_domain}", scope="company",
            ))

    return routes


def _domain_carries_brand(domain: str, company: NormalizedCompany) -> bool:
    """Is this email domain the company's own, e.g. ``@greytip.com``?"""
    stem = (domain or "").split(".")[0].replace("-", "")
    if not stem:
        return False
    return any(
        weight >= 1.0 and len(token) >= 4 and token in stem
        for token, weight in zip(company.tokens, company.weights)
    )


def collect_routes(
    results,
    company: NormalizedCompany,
    name: NormalizedName | None = None,
    *,
    limit: int = 12,
) -> list[ContactRoute]:
    """All routes across a result set, deduped and ordered most-direct first.

    Ties are broken by the order the results arrived in, so the higher-ranked
    search result wins — which is the closest thing a SERP gives us to authority.
    """
    ordered: list[ContactRoute] = []
    seen: set[tuple[str, str]] = set()
    for result in results or []:
        for route in routes_from_result(result, company, name):
            if route.key in seen:
                continue
            seen.add(route.key)
            ordered.append(route)
    ordered.sort(key=lambda r: r.rank)
    return ordered[:limit]


def merge_routes(*groups, limit: int = 12) -> list[ContactRoute]:
    """Combine route lists (person-level and company-level), deduped and ranked."""
    out: list[ContactRoute] = []
    seen: set[tuple[str, str]] = set()
    for group in groups:
        for route in group or []:
            if route.key in seen:
                continue
            seen.add(route.key)
            out.append(route)
    out.sort(key=lambda r: r.rank)
    return out[:limit]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: Appended to the deliverable alongside the four original enrichment columns.
CONTACT_COLUMNS = (
    "Contact Routes", "Best Contact Type", "Contact Source URL(s)",
)


def best_route(routes) -> ContactRoute | None:
    return min(routes, key=lambda r: r.rank) if routes else None


def render_routes(routes) -> str:
    """The ``Contact Routes`` cell: one route per line, most direct first."""
    return "\n".join(r.render() for r in routes)


def render_sources(routes) -> str:
    """The ``Contact Source URL(s)`` cell — deduped, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for route in routes:
        if route.source_url and route.source_url not in seen:
            seen.add(route.source_url)
            out.append(route.source_url)
    return " | ".join(out)


def serialise(routes) -> list[dict[str, str]]:
    """JSON-ready form, for the ``contact_routes`` table."""
    return [
        {"type": r.type.value, "value": r.value, "source_url": r.source_url,
         "label": r.label, "scope": r.scope}
        for r in routes
    ]


def deserialise(rows) -> list[ContactRoute]:
    out: list[ContactRoute] = []
    for row in rows or []:
        try:
            route_type = RouteType(row["type"])
        except (ValueError, KeyError, TypeError):
            continue
        out.append(ContactRoute(
            type=route_type, value=row.get("value", ""),
            source_url=row.get("source_url", ""), label=row.get("label", ""),
            scope=row.get("scope", "company"),
        ))
    out.sort(key=lambda r: r.rank)
    return out


@dataclass
class RouteSummary:
    """Aggregate route statistics, for the pilot report."""

    rows_with_routes: int = 0
    total_routes: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def add(self, routes) -> None:
        if routes:
            self.rows_with_routes += 1
        for route in routes:
            self.total_routes += 1
            key = route.type.value
            self.by_type[key] = self.by_type.get(key, 0) + 1

    @property
    def routes_per_row(self) -> float:
        return self.total_routes / self.rows_with_routes if self.rows_with_routes else 0.0
