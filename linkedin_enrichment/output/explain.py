"""Human-readable trace of how one row was decided.

Answers, for every row: what was searched, what came back, and why each candidate
was accepted or rejected. This is what makes a small `--limit 10` run auditable
before committing to 312,160 of them.

It renders only what the engine already computed — the feature values on
``ScoredCandidate`` and the ``RejectReason`` chosen by the gates. It never
re-derives a score, so what you read here is exactly what the pipeline used.
"""

from __future__ import annotations

from ..identity.scorer import Decision, MatchResult
from ..workers.runner import RowOutcome

BAR = "=" * 78

# Feature order for the per-candidate line: the two that decide the outcome first.
_FEATURE_ORDER = (
    ("name_similarity", "name"),
    ("company_coverage", "company"),
    ("slug_agreement", "slug"),
    ("location_match", "city"),
    ("country_subdomain", "in-domain"),
    ("industry_match", "industry"),
    ("designation_match", "role"),
)


def render_row(outcome: RowOutcome, settings, index: int = 0, total: int = 0) -> str:
    """Render one row's full decision trace."""
    lines: list[str] = []
    add = lines.append

    subject = outcome.subject
    header = f"ROW {index}/{total}" if total else "ROW"
    if subject is not None:
        add(f"{header}  {subject.name.raw} | {subject.company.raw} | {subject.location}")
    else:
        add(f"{header}  {outcome.row_uid}")

    # ---- queries issued ----
    if not outcome.traces:
        add("  QUERIES  none issued (resolved from cache or skipped before searching)")
    for trace in outcome.traces:
        label = "TIER 1" if trace.tier == 1 else f"TIER {trace.tier}"
        cached = " [cached]" if trace.from_cache else ""
        add(f"  {label}  {trace.text}{cached}")
        status = f"HTTP {trace.http_status}" if trace.http_status is not None else "no status"
        detail = (
            f"          -> {trace.backend or 'n/a'} {status}, "
            f"{trace.result_count} result(s) in {trace.elapsed_ms:.0f}ms"
        )
        if trace.outcome and trace.outcome != "success":
            detail += f"  [{trace.outcome.upper()}]"
        add(detail)
        if trace.error:
            add(f"             error: {trace.error}")

    # ---- candidates ----
    match: MatchResult = outcome.match
    if match.candidates:
        add("  CANDIDATES")
        for position, candidate in enumerate(match.candidates, start=1):
            add(f"    [{position}] {candidate.result.url}")
            title = (candidate.result.title or "").strip()
            if title:
                add(f'        "{title[:96]}"')

            if candidate.features:
                parts = [
                    f"{label}={candidate.features.get(key, 0.0):.2f}"
                    for key, label in _FEATURE_ORDER
                    if key in candidate.features
                ]
                add(
                    f"        {'  '.join(parts)}"
                    f"   raw={candidate.raw_score:.3f} conf={candidate.confidence:.4f}"
                )

            if candidate.rejected:
                add(f"        REJECT  {candidate.reason}")
            elif match.linkedin_url and candidate.url == match.linkedin_url:
                add(
                    f"        ACCEPT  {candidate.confidence:.4f} >= threshold "
                    f"{settings.confidence_threshold:.2f}"
                )
            else:
                add(
                    f"        passed gates, not selected "
                    f"(conf {candidate.confidence:.4f})"
                )
    else:
        add("  CANDIDATES  none returned")

    # ---- decision ----
    if match.decision is Decision.MATCHED:
        add(f"  DECISION  matched -> {match.linkedin_url}  ({match.confidence:.4f})")
    else:
        add(f"  DECISION  {match.decision.value}  — {match.notes}")

    if match.runner_up_score:
        add(
            f"            top={match.top_score:.3f} runner_up={match.runner_up_score:.3f} "
            f"margin={match.margin:.3f} (minimum {settings.reject.min_margin})"
        )
    add("")
    return "\n".join(lines)


def render_header(provider: str, limit: int | None, threshold: float) -> str:
    return "\n".join([
        BAR,
        "  EXPLAIN MODE — every query and every accept/reject decision",
        BAR,
        f"  provider  : {provider}",
        f"  row limit : {limit if limit else 'none'}",
        f"  threshold : {threshold:.2f}  (below this the LinkedIn column stays blank)",
        BAR,
        "",
    ])


def render_footer(progress, client) -> str:
    """Closing summary for a limited explain run."""
    stats = client.stats()
    inconclusive = stats.get("inconclusive_searches", 0)
    lines = [
        BAR,
        f"  {progress.processed} row(s) processed — "
        f"{progress.matched} matched, {progress.blank} blank",
        f"  queries issued {stats['queries_issued']} "
        f"(+{stats['queries_from_cache']} from cache), "
        f"errors {stats['search_errors']}, inconclusive {inconclusive}",
    ]
    if inconclusive:
        lines += [
            "",
            "  WARNING: some searches returned HTTP 200 with nothing parseable.",
            "  That is NOT evidence these people have no profile — it usually means",
            "  the backend was throttled or its markup changed. Do not run in bulk",
            "  until `python main.py preflight` reports SUCCESS.",
        ]
    if progress.matched == 0 and progress.processed:
        lines += [
            "",
            "  NOTE: zero matches in this sample. On this dataset that can be",
            "  genuine (most subjects are directors of tiny companies with no web",
            "  presence), but check the per-row output above: if no query returned",
            "  any results at all, the problem is the search layer, not the data.",
        ]
    lines.append(BAR)
    return "\n".join(lines)
