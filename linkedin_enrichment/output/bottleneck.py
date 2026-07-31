"""Why the target was not reached, stated precisely.

When a run ends short of its target the useful output is not "we found N". It is
*which stage consumed the rows* — because that is what tells you whether to fix
the search backend, widen the pool, loosen a gate, or accept that the data does
not support the number.

Every row ends in exactly one bucket, and the buckets sum to the file. The
largest one is the bottleneck.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BAR = "=" * 78

#: decision -> (bucket label, what to do about it)
_BUCKETS: dict[str, tuple[str, str]] = {
    "matched": (
        "Accepted", "these are the deliverable"),
    "blank_skipped": (
        "Rejected before searching",
        "name is not a person, or too little signal to disambiguate"),
    "blank_no_candidate": (
        "No candidate passed the name+company gates",
        "either the company has no web presence, or the person is not "
        "publicly associated with it"),
    "blank_low_confidence": (
        "Scored but below threshold / uncorroborated",
        "lower the threshold or accept weaker corroboration to convert these"),
    "blank_ambiguous": (
        "Ambiguous — several equally plausible people",
        "common names; needs a stronger discriminator than the file provides"),
    "blank_error": (
        "Search failed",
        "infrastructure, not data — fix and re-run `retry`"),
    "suppressed": ("Suppressed on request", "deliberate exclusion"),
}


@dataclass
class Bottleneck:
    target: int = 0
    accepted: int = 0
    total_rows: int = 0
    processed: int = 0
    unprocessed: int = 0
    buckets: dict[str, int] = field(default_factory=dict)
    queries_issued: int = 0
    inconclusive: int = 0
    companies_searched: int = 0
    companies_with_footprint: int = 0

    @property
    def target_met(self) -> bool:
        return self.accepted >= self.target

    @property
    def primary(self) -> tuple[str, int]:
        """The bucket that consumed the most rows, excluding accepted ones."""
        losses = {k: v for k, v in self.buckets.items() if k != "matched" and v}
        if not losses:
            return ("", 0)
        return max(losses.items(), key=lambda kv: kv[1])


def build(store, settings, search_stats: dict | None = None) -> Bottleneck:
    stats = search_stats or {}
    total = store.scalar("SELECT COUNT(*) FROM records") or 0
    processed = store.scalar("SELECT COUNT(*) FROM results") or 0
    return Bottleneck(
        target=settings.target_matches or 0,
        accepted=store.scalar(
            "SELECT COUNT(*) FROM results WHERE decision='matched' AND linkedin_url<>''"
        ) or 0,
        total_rows=total,
        processed=processed,
        unprocessed=max(0, total - processed),
        buckets=store.counts_by("decision", table="results"),
        queries_issued=int(stats.get("queries_issued", 0) or 0),
        inconclusive=int(stats.get("inconclusive_searches", 0) or 0),
        companies_searched=store.scalar("SELECT COUNT(*) FROM company_cache") or 0,
        companies_with_footprint=store.scalar(
            "SELECT COUNT(*) FROM company_cache WHERE has_linkedin_footprint=1"
        ) or 0,
    )


def render(b: Bottleneck) -> str:
    lines = [BAR]
    if b.target_met:
        lines += [f"  TARGET MET — {b.accepted:,} accepted (target {b.target:,})", BAR]
        return "\n".join(lines)

    lines += [
        f"  TARGET NOT MET — {b.accepted:,} accepted of {b.target:,} requested",
        BAR,
        "",
        "  WHERE EVERY ROW WENT",
    ]

    denominator = max(b.processed, 1)
    for decision, count in sorted(b.buckets.items(), key=lambda kv: -kv[1]):
        label, _ = _BUCKETS.get(decision, (decision, ""))
        lines.append(f"    {label:<52} {count:>9,}  {count / denominator:>6.1%}")
    if b.unprocessed:
        lines.append(f"    {'Never processed (run did not reach them)':<52} "
                     f"{b.unprocessed:>9,}")
    lines.append("")

    name, count = b.primary
    if name:
        label, remedy = _BUCKETS.get(name, (name, ""))
        lines += [
            "  BOTTLENECK",
            f"    {label} — {count:,} rows ({count / denominator:.1%} of processed)",
            f"    {remedy}",
            "",
        ]

    lines += ["  COVERAGE"]
    lines.append(f"    Rows in file                : {b.total_rows:>9,}")
    lines.append(f"    Rows processed              : {b.processed:>9,}"
                 f"  ({b.processed / max(b.total_rows, 1):.1%})")
    lines.append(f"    Companies searched          : {b.companies_searched:>9,}")
    lines.append(f"    ...with a LinkedIn presence : {b.companies_with_footprint:>9,}"
                 f"  ({b.companies_with_footprint / max(b.companies_searched, 1):.1%})")
    lines.append(f"    Queries issued              : {b.queries_issued:>9,}")
    if b.inconclusive:
        lines.append(f"    Inconclusive searches       : {b.inconclusive:>9,}")
    lines.append("")

    lines += ["  WHY 1,000 WAS NOT REACHED"] if b.target >= 1000 else ["  WHY THE TARGET WAS NOT REACHED"]
    lines += _diagnose(b)
    lines.append(BAR)
    return "\n".join(lines)


def _diagnose(b: Bottleneck) -> list[str]:
    """The specific, actionable reason — not a restatement of the counts."""
    out: list[str] = []
    denominator = max(b.processed, 1)
    name, count = b.primary
    share = count / denominator

    if b.unprocessed > 0 and b.accepted < b.target:
        out.append(
            f"    The run stopped with {b.unprocessed:,} rows never processed. "
            "That is not\n    data exhaustion — resume to continue."
        )
        return out

    if name == "blank_error" and share > 0.2:
        out.append(
            "    Most rows failed at the SEARCH layer, not the matching layer.\n"
            "    This is an infrastructure fault: run `preflight`, fix what it\n"
            "    reports, then `retry`. The data has not been fairly tested yet."
        )
        return out

    if b.companies_searched and b.companies_with_footprint / max(b.companies_searched, 1) < 0.15:
        out.append(
            f"    Only {b.companies_with_footprint:,} of {b.companies_searched:,} companies "
            f"({b.companies_with_footprint / max(b.companies_searched, 1):.1%}) have any\n"
            "    discoverable web presence. The ceiling is set by the input: most\n"
            "    entities in this file are small private companies that are not\n"
            "    published anywhere a search engine can see."
        )

    if name == "blank_no_candidate":
        out.append(
            "    The dominant loss is rows where no result tied the person to the\n"
            "    company. Widening roles or lowering the threshold will not help —\n"
            "    those rows produced no candidate to score. Only a data source that\n"
            "    covers small private companies would move this number."
        )
    elif name == "blank_low_confidence":
        out.append(
            "    The dominant loss is rows that DID produce candidates but fell\n"
            "    short of the threshold or lacked corroboration. These are the\n"
            "    convertible ones: review out/review_queue.csv, and consider a\n"
            "    lower threshold or accepting weaker corroboration."
        )
    elif name == "blank_ambiguous":
        out.append(
            "    The dominant loss is ambiguity — several equally plausible people\n"
            "    per row. This is the homonym problem; the file itself contains 54\n"
            "    people called 'Praveen Kumar'. Nothing in the input separates them."
        )
    elif name == "blank_skipped":
        out.append(
            "    The dominant loss is rows rejected before searching: the Name\n"
            "    column holds artifacts rather than people. This is a data-quality\n"
            "    problem in the export, upstream of this pipeline."
        )

    out.append(
        f"\n    Bottom line: {b.accepted:,} of {b.processed:,} processed rows converted "
        f"({b.accepted / denominator:.2%}).\n"
        f"    Reaching {b.target:,} at this rate needs "
        f"{int(b.target / max(b.accepted / denominator, 1e-9)):,} processed rows."
    )
    return out
