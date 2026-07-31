"""Final quality-control report.

Reports what actually happened, including the parts that look bad. A run over
this dataset is *expected* to produce far more blanks than matches — most
subjects are directors of two-person private limited companies with no LinkedIn
presence — so the report breaks blanks down by cause rather than presenting a
single success percentage that would invite the wrong conclusion.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Human-readable explanation for every decision the pipeline can reach.
#: Above this share of inconclusive searches, the run's blanks are not evidence.
INCONCLUSIVE_WARN_THRESHOLD = 0.20

DECISION_LABELS = {
    "matched": "Matched (>= threshold, written to output)",
    "blank_no_candidate": "Blank — no candidate passed the name+company gates",
    "blank_low_confidence": "Blank — best candidate below the confidence threshold",
    "blank_ambiguous": "Blank — two or more candidates too close to separate",
    "blank_skipped": "Blank — row skipped before searching (see skip reasons)",
    "blank_error": "Blank — search failed after retries",
    "suppressed": "Suppressed on request",
}


@dataclass
class QCReport:
    """Every figure the specification asks for, plus the cost drivers."""

    run_id: str = ""
    total_rows: int = 0
    processed_rows: int = 0
    matched: int = 0
    blank: int = 0
    ambiguous: int = 0
    errors: int = 0
    average_confidence: float = 0.0
    elapsed_seconds: float = 0.0

    decisions: dict[str, int] = field(default_factory=dict)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    statuses: dict[str, int] = field(default_factory=dict)
    search: dict[str, Any] = field(default_factory=dict)
    ladder: dict[str, Any] = field(default_factory=dict)
    caches: dict[str, Any] = field(default_factory=dict)
    review_queue_size: int = 0
    unique_companies: int = 0
    companies_with_footprint: int = 0
    duplicate_rows: int = 0
    deferred_rows: int = 0
    inconclusive_searches: int = 0
    queries_issued: int = 0

    # --- precision provenance ---
    confidence_threshold: float = 0.95
    calibration_source: str = ""
    labels_correct: int = 0
    labels_incorrect: int = 0

    @property
    def labelled(self) -> int:
        return self.labels_correct + self.labels_incorrect

    @property
    def measured_precision(self) -> float:
        return self.labels_correct / self.labelled if self.labelled else 0.0

    @property
    def precision_lower_bound(self) -> float:
        from ..identity.calibrate import wilson_lower_bound
        return wilson_lower_bound(self.labels_correct, self.labelled)

    @property
    def match_rate(self) -> float:
        return (self.matched / self.processed_rows) if self.processed_rows else 0.0

    @property
    def inconclusive_rate(self) -> float:
        return (
            self.inconclusive_searches / self.queries_issued
            if self.queries_issued else 0.0
        )

    @property
    def trustworthy(self) -> bool:
        """Can the blanks in this run be believed?

        A blank means "we searched and found nothing convincing". That claim only
        holds if the searches actually worked. When a large share came back
        unparseable, or rows are still queued for retry, the blanks reflect the
        state of the search backend rather than the state of the world.
        """
        return self.inconclusive_rate <= INCONCLUSIVE_WARN_THRESHOLD and not self.deferred_rows

    def to_dict(self) -> dict[str, Any]:
        data = {
            "run_id": self.run_id,
            "total_rows": self.total_rows,
            "processed_rows": self.processed_rows,
            "successful_linkedin_matches": self.matched,
            "blank_rows": self.blank,
            "ambiguous_matches": self.ambiguous,
            "errors_encountered": self.errors,
            "average_confidence": round(self.average_confidence, 4),
            "match_rate": round(self.match_rate, 4),
            "processing_time_seconds": round(self.elapsed_seconds, 2),
            "review_queue_size": self.review_queue_size,
            "confidence_threshold": self.confidence_threshold,
            "calibration_source": self.calibration_source,
            "labels_correct": self.labels_correct,
            "labels_incorrect": self.labels_incorrect,
            "measured_precision": round(self.measured_precision, 4) if self.labelled else None,
            "precision_lower_bound": round(self.precision_lower_bound, 4) if self.labelled else None,
            "deferred_rows": self.deferred_rows,
            "inconclusive_searches": self.inconclusive_searches,
            "inconclusive_rate": round(self.inconclusive_rate, 4),
            "run_trustworthy": self.trustworthy,
            "unique_companies": self.unique_companies,
            "companies_with_linkedin_footprint": self.companies_with_footprint,
            "duplicate_rows_detected": self.duplicate_rows,
            "decisions": self.decisions,
            "skip_reasons": self.skip_reasons,
            "record_statuses": self.statuses,
            "search": self.search,
            "ladder": self.ladder,
            "caches": self.caches,
        }
        return data


def _label_counts(store) -> tuple[int, int]:
    """(correct, incorrect) hand-labels, tolerating a store without the table."""
    try:
        return store.label_counts()
    except Exception:  # noqa: BLE001 - reporting must never fail the run
        return (0, 0)


def build_report(store, settings, *, run_id: str, elapsed: float,
                 search_stats: dict | None = None,
                 ladder_stats: dict | None = None,
                 cache_stats: dict | None = None) -> QCReport:
    """Assemble the report from database state (so it works after a resume too)."""
    decisions = store.counts_by("decision", table="results")
    statuses = store.counts_by("status", table="records")
    skips = {k: v for k, v in store.counts_by("skip_reason", table="records").items() if k}

    total = store.scalar("SELECT COUNT(*) FROM records") or 0
    processed = store.scalar("SELECT COUNT(*) FROM results") or 0
    matched = decisions.get("matched", 0)
    avg_conf = store.scalar(
        "SELECT AVG(confidence) FROM results WHERE decision = 'matched'"
    ) or 0.0

    return QCReport(
        run_id=run_id,
        total_rows=total,
        processed_rows=processed,
        matched=matched,
        blank=processed - matched,
        ambiguous=decisions.get("blank_ambiguous", 0),
        errors=decisions.get("blank_error", 0) + statuses.get("error", 0),
        average_confidence=float(avg_conf),
        elapsed_seconds=elapsed,
        decisions=decisions,
        skip_reasons=skips,
        statuses=statuses,
        search=search_stats or {},
        ladder=ladder_stats or {},
        caches=cache_stats or {},
        confidence_threshold=settings.confidence_threshold,
        calibration_source=getattr(settings.calibration, "source", ""),
        labels_correct=_label_counts(store)[0],
        labels_incorrect=_label_counts(store)[1],
        deferred_rows=store.scalar(
            "SELECT COUNT(*) FROM records WHERE status = 'deferred'"
        ) or 0,
        inconclusive_searches=int((search_stats or {}).get("inconclusive_searches", 0) or 0),
        queries_issued=int((search_stats or {}).get("queries_issued", 0) or 0),
        review_queue_size=store.scalar("SELECT COUNT(*) FROM review_candidates") or 0,
        unique_companies=store.scalar(
            "SELECT COUNT(DISTINCT company_key) FROM records WHERE company_key <> ''"
        ) or 0,
        companies_with_footprint=store.scalar(
            "SELECT COUNT(*) FROM company_cache WHERE has_linkedin_footprint = 1"
        ) or 0,
        duplicate_rows=store.scalar(
            """
            SELECT COUNT(*) FROM records WHERE dedup_key IN (
                SELECT dedup_key FROM records
                WHERE dedup_key <> '' GROUP BY dedup_key HAVING COUNT(*) > 1
            )
            """
        ) or 0,
    )


def render_text(report: QCReport) -> str:
    """Plain-text report for the console and the run log."""
    lines: list[str] = []
    add = lines.append

    add("=" * 74)
    add("  LINKEDIN ENRICHMENT — QUALITY CONTROL REPORT")
    add("=" * 74)
    add(f"  Run ID                 : {report.run_id}")
    add(f"  Processing time        : {_duration(report.elapsed_seconds)}")
    add("")
    # Placed first: if the run cannot be believed, nothing below it should be
    # quoted to anyone before the cause is fixed.
    add("  RUN TRUSTWORTHINESS")
    if report.queries_issued:
        add(
            f"    Inconclusive searches: {report.inconclusive_searches:>12,}"
            f"  ({report.inconclusive_rate:.1%} of {report.queries_issued:,} issued)"
        )
    add(f"    Rows queued for retry: {report.deferred_rows:>12,}")
    if report.trustworthy:
        add("    OK — searches completed normally; the blanks below are real findings.")
    else:
        add("")
        if report.inconclusive_rate > INCONCLUSIVE_WARN_THRESHOLD:
            add(
                f"    WARNING: {report.inconclusive_rate:.1%} of searches returned no "
                "parseable results."
            )
            add("    The blank rows in this run are NOT evidence that these people have")
            add("    no LinkedIn profile — they reflect the state of the search backend.")
        if report.deferred_rows:
            add(
                f"    WARNING: {report.deferred_rows:,} row(s) are still queued for retry "
                "and remain unresolved."
            )
        add("")
        add("    Do this: `python main.py preflight`, fix what it reports, then")
        add("             `python main.py retry` (add --reset if attempts were exhausted).")
    add("")

    # Whether the headline confidence is a measurement or a model output is the
    # first thing a reader needs, because it decides whether the number can be
    # quoted to anyone.
    add("  PRECISION")
    add(f"    Confidence threshold : {report.confidence_threshold:>12.2f}")
    if report.calibration_source:
        add(f"    Calibration in force : {report.calibration_source}")
    if report.labelled:
        add(f"    Hand-labelled        : {report.labelled:>12,}"
            f"  (correct {report.labels_correct:,}, incorrect {report.labels_incorrect:,})")
        add(f"    Measured precision   : {report.measured_precision:>12.4f}")
        add(f"    Wilson 95% lower bnd : {report.precision_lower_bound:>12.4f}"
            "   <- quote THIS number")
        if report.precision_lower_bound < report.confidence_threshold:
            add("")
            add(f"    NOTE: the measured lower bound is below the "
                f"{report.confidence_threshold:.2f} threshold.")
            add("    Label more rows, or the threshold is not yet supported by evidence.")
    else:
        add("    Measured precision   :          none — NOT YET VALIDATED")
        add("")
        add("    The confidence above is a calibrated MODEL SCORE, not a measured")
        add("    precision. Do not quote it as one. Run `python main.py validate`:")
        add("    381 rows labelled with zero errors give a 95% lower bound of 0.9900.")
    add("")

    add("  ROWS")
    add(f"    Total rows           : {report.total_rows:>12,}")
    add(f"    Processed rows       : {report.processed_rows:>12,}")
    add(f"    Duplicate rows       : {report.duplicate_rows:>12,}  (served from cache, 0 extra queries)")
    add("")
    add("  OUTCOMES")
    add(f"    Matched (written)    : {report.matched:>12,}  ({report.match_rate:.2%} of processed)")
    add(f"    Blank                : {report.blank:>12,}")
    add(f"    ...of which ambiguous: {report.ambiguous:>12,}")
    add(f"    Errors               : {report.errors:>12,}")
    add(f"    Average confidence   : {report.average_confidence:>12.4f}  (matched rows only)")
    add(f"    Review queue         : {report.review_queue_size:>12,}  (near-misses for human review)")
    add("")

    if report.decisions:
        add("  DECISION BREAKDOWN")
        for key, count in sorted(report.decisions.items(), key=lambda kv: -kv[1]):
            add(f"    {DECISION_LABELS.get(key, key):<56} {count:>10,}")
        add("")

    if report.skip_reasons:
        add("  SKIPPED BEFORE SEARCHING (no query spent)")
        for key, count in sorted(report.skip_reasons.items(), key=lambda kv: -kv[1]):
            add(f"    {key:<56} {count:>10,}")
        add("")

    add("  COMPANIES")
    add(f"    Unique companies     : {report.unique_companies:>12,}")
    add(f"    With LinkedIn presence:{report.companies_with_footprint:>12,}")
    add("")

    if report.search:
        add("  SEARCH")
        for key, value in report.search.items():
            add(f"    {key:<56} {_fmt(value):>10}")
        add("")

    if report.ladder:
        add("  QUERY LADDER")
        for key, value in report.ladder.items():
            add(f"    {key:<56} {_fmt(value):>10}")
        add("")

    if report.caches:
        add("  CACHES")
        for key, value in report.caches.items():
            add(f"    {key:<56} {_fmt(value):>10}")
        add("")

    add("=" * 74)
    return "\n".join(lines)


def save(report: QCReport, output_dir: str | Path) -> tuple[Path, Path]:
    """Write the report as both JSON (machine) and text (human)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    json_path = output_dir / f"qc_report_{stamp}.json"
    json_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    text_path = output_dir / f"qc_report_{stamp}.txt"
    text_path.write_text(render_text(report), encoding="utf-8")

    return json_path, text_path


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.4f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
