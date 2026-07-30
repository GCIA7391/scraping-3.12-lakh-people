#!/usr/bin/env python3
"""LinkedIn enrichment pipeline — command line entry point.

Commands
--------
    ingest      Load workbooks into the state database (idempotent).
    run         Ingest, then enrich. Re-running resumes automatically.
    resume      Continue an interrupted run without re-reading the inputs.
    dry-run     Project query volume and pre-filter drops. No network.
    estimate    Cost and wall-clock table per provider. No network.
    write       Regenerate the enriched CSVs and the review queue from the database.
    report      Regenerate the QC report from the database.
    calibrate   Fit the raw-score to probability mapping from labelled data.
    suppress    Erase one person's enrichment and tombstone the row.

Every command is safe to re-run. Nothing is ever recomputed that is already done.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path

from linkedin_enrichment.cache.company_cache import CompanyCache
from linkedin_enrichment.cache.serp_cache import SerpCache
from linkedin_enrichment.config.settings import Settings
from linkedin_enrichment.database.store import Store
from linkedin_enrichment.identity.scorer import Decision
from linkedin_enrichment.ingest import prefilter
from linkedin_enrichment.ingest.reader import read_all
from linkedin_enrichment.logging import dashboard as dash
from linkedin_enrichment.logging import setup as log_setup
from linkedin_enrichment.output import explain as explain_mod
from linkedin_enrichment.output import report as report_mod
from linkedin_enrichment.output import review_queue, writer
from linkedin_enrichment.providers import REGISTRY, build_provider
from linkedin_enrichment.providers.base import ProviderError
from linkedin_enrichment.search import preflight as preflight_mod
from linkedin_enrichment.search.client import SearchClient
from linkedin_enrichment.workers.pool import RunProgress, WorkerPool
from linkedin_enrichment.workers.runner import LadderRunner

logger = logging.getLogger("linkedin_enrichment")

INGEST_BATCH = 5_000


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def ingest(store: Store, settings: Settings) -> dict[str, int]:
    """Stream every input row into the database.

    Tier 0 runs here so unsearchable rows never reach a worker. Duplicates are
    *not* skipped — they are flagged and left pending, because they must still
    appear in the output. They cost nothing, since by the time a duplicate is
    processed both the company cache and the SERP cache already hold its answer.
    """
    counts = {"read": 0, "inserted": 0, "searchable": 0, "skipped": 0, "duplicates": 0}
    seen: dict[str, str] = {}
    batch: list[dict] = []

    for row in read_all(settings.input_paths):
        counts["read"] += 1
        verdict = prefilter.evaluate(row)

        dup_of = None
        if verdict.searchable:
            canonical = seen.get(verdict.dedup_key)
            if canonical is None:
                seen[verdict.dedup_key] = row.row_uid
            else:
                dup_of = canonical
                counts["duplicates"] += 1
            counts["searchable"] += 1
            status, skip_reason = "pending", ""
        else:
            counts["skipped"] += 1
            status, skip_reason = "skipped", verdict.reason.value

        batch.append({
            "row_uid": row.row_uid,
            "source_file": row.source_file, "sheet_name": row.sheet_name,
            "row_index": row.row_index,
            "name": row.name, "company": row.company,
            "designation": row.designation, "location": row.location,
            "industry": row.industry, "pc_url": row.pc_url,
            "name_norm": verdict.name.display, "company_key": verdict.company.key,
            "dedup_key": verdict.dedup_key, "dup_of": dup_of,
            "status": status, "skip_reason": skip_reason,
        })

        if len(batch) >= INGEST_BATCH:
            counts["inserted"] += store.insert_records(batch)
            batch.clear()

    if batch:
        counts["inserted"] += store.insert_records(batch)

    # Skipped rows still need an output line explaining themselves.
    store.record_skipped_results()
    return counts


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------

async def enrich(
    store: Store, settings: Settings, run_id: str,
    *, limit: int | None = None, explain: bool = False,
) -> RunProgress:
    """Run the worker pool over every pending row."""
    provider = build_provider(settings.provider.name, settings.provider)
    try:
        provider.validate()
    except ProviderError as exc:
        logger.error("provider not usable: %s", exc)
        raise

    serp_cache = SerpCache(store, settings.serp_cache_ttl_days)
    company_cache = CompanyCache(store)
    client = SearchClient(provider, serp_cache, settings)
    runner = LadderRunner(client, company_cache, settings)

    pending = store.scalar("SELECT COUNT(*) FROM records WHERE status = 'pending'") or 0
    total = min(pending, limit) if limit else pending
    progress = RunProgress(total=total)

    on_row = None
    if explain:
        print(explain_mod.render_header(provider.name, limit, settings.confidence_threshold))

        def on_row(outcome, position):  # noqa: F811 - deliberate local binding
            print(explain_mod.render_row(outcome, settings, position, total))

    pool = WorkerPool(store, runner, settings, progress, limit=limit, on_row=on_row)

    logger.info(
        "starting run %s: %s pending rows, provider=%s, workers=%d, threshold=%.2f",
        run_id, f"{pending:,}", provider.name, settings.workers, settings.confidence_threshold,
    )

    try:
        # The live dashboard and the explain stream both own stdout; explain wins.
        async with dash.Dashboard(
            progress, client, runner, enabled=settings.dashboard and not explain
        ):
            await pool.run()
        if explain:
            print(explain_mod.render_footer(progress, client))
    finally:
        await client.close()

    # Cross-row consistency: one profile cannot belong to two people.
    if settings.reject.reject_shared_url:
        store.retract_shared_urls()

    for key, value in {**client.stats(), **runner.stats(),
                       **company_cache.stats(), **serp_cache.stats()}.items():
        store.set_stat(run_id, key, value)

    return progress


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_ingest(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        counts = ingest(store, settings)
    print(
        f"read {counts['read']:,} rows | inserted {counts['inserted']:,} new | "
        f"searchable {counts['searchable']:,} | skipped {counts['skipped']:,} | "
        f"duplicates {counts['duplicates']:,}"
    )
    return 0


def cmd_preflight(args, settings: Settings) -> int:
    """Prove the search layer works before committing to a long run."""
    report = preflight_mod.preflight_blocking(settings, deep=not getattr(args, "shallow", False))
    print(preflight_mod.render(report))
    return report.exit_code


def cmd_run(args, settings: Settings) -> int:
    run_id = settings.run_id or uuid.uuid4().hex[:12]
    started = time.time()
    limit = getattr(args, "limit", None)
    explain = bool(getattr(args, "explain", False))
    force = bool(getattr(args, "force", False))

    # Gate: a run that cannot search produces blanks that look like findings.
    # Skipped for the cassette provider, which is offline by design.
    if settings.provider.name != "cassette" and not force:
        report = preflight_mod.preflight_blocking(settings)
        if report.exit_code != 0:
            print(preflight_mod.render(report), file=sys.stderr)
            print(
                "\nRefusing to start: searches are not working, so every row would be "
                "blanked for the wrong reason.\nFix the problem above, or re-run with "
                "--force if you are certain.",
                file=sys.stderr,
            )
            return report.exit_code
        logger.info("preflight passed; starting run")

    with Store(settings.db_path) as store:
        if settings.input_paths:
            counts = ingest(store, settings)
            logger.info(
                "ingest: %s read, %s searchable, %s skipped",
                f"{counts['read']:,}", f"{counts['searchable']:,}", f"{counts['skipped']:,}",
            )
        elif not store.has_records():
            print("no input files given and the database is empty; nothing to do", file=sys.stderr)
            return 2

        # Reclaim work abandoned by a previous crash before claiming anything new.
        store.reclaim_stale(settings.stale_claim_seconds)

        asyncio.run(enrich(store, settings, run_id, limit=limit, explain=explain))

        outputs = writer.write_all(store, settings.output_dir)
        review_path = review_queue.write_review_queue(store, settings.output_dir)

        stats = store.get_stats(run_id)
        qc = report_mod.build_report(
            store, settings, run_id=run_id, elapsed=time.time() - started,
            search_stats={k: _num(v) for k, v in stats.items() if k.startswith(("quer", "search", "throttle", "current"))},
            ladder_stats={k: _num(v) for k, v in stats.items() if k.startswith(("tier", "negative", "resolved"))},
            cache_stats={k: _num(v) for k, v in stats.items() if "cache" in k},
        )
        report_mod.save(qc, settings.output_dir)
        dash.print_report(report_mod.render_text(qc))

    print(f"\nenriched files : {', '.join(str(p) for p in outputs)}")
    print(f"review queue   : {review_path}")
    return 0


def cmd_resume(args, settings: Settings) -> int:
    settings.input_paths = []
    return cmd_run(args, settings)


def cmd_dry_run(args, settings: Settings) -> int:
    """Project the work without touching the network.

    Tier-1 volume is exact (one query per unique company). Tier-2 volume depends
    on how many companies turn out to have a LinkedIn presence, which is only
    knowable by searching — so it is reported as a range with the assumption
    stated, not as a single fabricated number.
    """
    if not settings.input_paths:
        print("dry-run needs --input", file=sys.stderr)
        return 2

    total = searchable = duplicates = 0
    skip_counts: dict[str, int] = {}
    companies: set[str] = set()
    seen: set[str] = set()

    for row in read_all(settings.input_paths):
        total += 1
        verdict = prefilter.evaluate(row)
        if not verdict.searchable:
            skip_counts[verdict.reason.value] = skip_counts.get(verdict.reason.value, 0) + 1
            continue
        searchable += 1
        companies.add(verdict.company.key)
        if verdict.dedup_key in seen:
            duplicates += 1
        else:
            seen.add(verdict.dedup_key)

    tier1 = len(companies)
    naive = searchable * 2

    print("=" * 74)
    print("  DRY RUN — projected work (no network calls made)")
    print("=" * 74)
    print(f"  Total rows read              : {total:>12,}")
    print(f"  Searchable rows              : {searchable:>12,}")
    print(f"  Skipped before searching     : {total - searchable:>12,}")
    for reason, count in sorted(skip_counts.items(), key=lambda kv: -kv[1]):
        print(f"      {reason:<46} {count:>10,}")
    print(f"  Duplicate (name, company)    : {duplicates:>12,}  (0 extra queries)")
    print(f"  Unique companies             : {tier1:>12,}")
    print()
    print("  QUERY PROJECTION")
    print(f"    Tier 1 (one per company)   : {tier1:>12,}  exact")
    for rate in (0.15, 0.25, 0.40):
        tier2 = int(searchable * rate)
        print(f"    Tier 2 @ {rate:.0%} footprint    : {tier2:>12,}  -> total {tier1 + tier2:>10,}")
    print()
    print(f"  Naive baseline (2/person)    : {naive:>12,}")
    print(f"  Saving at 25% footprint      : {1 - (tier1 + int(searchable * 0.25)) / max(naive, 1):>11.1%}")
    print("=" * 74)
    print("  Tier-2 volume depends on how many companies have a LinkedIn presence,")
    print("  which cannot be known without searching. Three scenarios are shown.")
    return 0


def cmd_estimate(args, settings: Settings) -> int:
    """Cost and wall-clock per provider for the projected query volume."""
    queries = args.queries
    if queries is None:
        if not settings.input_paths:
            print("estimate needs --queries N or --input FILE...", file=sys.stderr)
            return 2
        companies, searchable = set(), 0
        for row in read_all(settings.input_paths):
            verdict = prefilter.evaluate(row)
            if verdict.searchable:
                searchable += 1
                companies.add(verdict.company.key)
        queries = len(companies) + int(searchable * 0.25)
        print(f"projected queries (Tier 1 + Tier 2 @ 25% footprint): {queries:,}\n")

    rate = settings.rate_limit.requests_per_second
    print(f"{'provider':<14}{'$/1k':>9}{'total $':>12}{'daily cap':>12}{'wall-clock':>16}")
    print("-" * 63)
    for name, cls in sorted(REGISTRY.items(), key=lambda kv: kv[1].cost_per_1k):
        if name == "cassette":
            continue
        cost = queries / 1000 * cls.cost_per_1k
        cap = cls.daily_query_cap
        if cap:
            days = queries / cap
            clock = f"{days:,.1f}d (capped)"
        else:
            clock = _duration(queries / rate)
        cap_text = f"{cap:,}" if cap else "none"
        print(f"{name:<14}{cls.cost_per_1k:>9.2f}{cost:>12,.2f}{cap_text:>12}{clock:>16}")
    print("-" * 63)
    print(f"wall-clock assumes the configured {rate} req/s and ignores cache hits,")
    print("which in practice reduce the issued-query count substantially.")
    return 0


def cmd_write(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        outputs = writer.write_all(store, settings.output_dir)
        path = review_queue.write_review_queue(store, settings.output_dir)
    for output in outputs:
        print(output)
    print(path)
    return 0


def cmd_report(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        qc = report_mod.build_report(store, settings, run_id=settings.run_id or "-", elapsed=0.0)
        json_path, text_path = report_mod.save(qc, settings.output_dir)
        dash.print_report(report_mod.render_text(qc))
    print(f"\nsaved: {json_path}\n       {text_path}")
    return 0


def cmd_suppress(args, settings: Settings) -> int:
    with Store(settings.db_path) as store:
        for row_uid in args.row_uid:
            store.suppress(row_uid, note=args.note or "")
            print(f"suppressed {row_uid}")
        writer.write_all(store, settings.output_dir)
    return 0


def cmd_calibrate(args, settings: Settings) -> int:
    """Fit the raw-score to probability mapping from a hand-labelled sample.

    Expects a CSV with columns ``raw_score`` and ``correct`` (1/0). Produces the
    isotonic knots to paste into ``calibration.isotonic_points`` and reports the
    raw-score cut point achieving the requested precision.
    """
    from linkedin_enrichment.identity.calibrate import fit_from_csv

    result = fit_from_csv(args.labels, target_precision=settings.confidence_threshold)
    print(result.render())
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Enrich prospect workbooks with public LinkedIn profile URLs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", help="path to a YAML config file")
    parser.add_argument("--db", dest="db_path", help="state database path")
    parser.add_argument("--output-dir", help="where to write enriched files")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--no-dashboard", action="store_true", help="disable the live panel")

    sub = parser.add_subparsers(dest="command", required=True)

    def add_input(sp):
        sp.add_argument("--input", nargs="+", dest="input_paths",
                        help="input .csv/.xlsx workbooks")

    def add_run_opts(sp):
        sp.add_argument("--workers", type=int, help="concurrent workers")
        sp.add_argument("--provider", choices=sorted(REGISTRY), help="search provider")
        sp.add_argument("--threshold", type=float, dest="confidence_threshold",
                        help="minimum confidence to write a profile (default 0.95)")
        sp.add_argument("--rate", type=float, help="requests per second")
        sp.add_argument("--limit", type=int,
                        help="process at most N rows — use this for a small first test")
        sp.add_argument("--explain", action="store_true",
                        help="print every query, result and accept/reject reason per row")
        sp.add_argument("--force", action="store_true",
                        help="run even if preflight says searches are not working")

    p = sub.add_parser("ingest", help="load workbooks into the database")
    add_input(p)

    p = sub.add_parser("run", help="ingest and enrich (resumes automatically)")
    add_input(p)
    add_run_opts(p)

    p = sub.add_parser("resume", help="continue an interrupted run")
    add_run_opts(p)

    p = sub.add_parser(
        "preflight",
        help="check that searches actually work, and say precisely why if not",
    )
    p.add_argument("--provider", choices=sorted(REGISTRY), help="provider to test")
    p.add_argument("--shallow", action="store_true",
                   help="skip the DNS/TCP/TLS stage checks")

    p = sub.add_parser("dry-run", help="project query volume without searching")
    add_input(p)

    p = sub.add_parser("estimate", help="cost and wall-clock per provider")
    add_input(p)
    p.add_argument("--queries", type=int, help="query count to price (skips reading inputs)")

    sub.add_parser("write", help="regenerate enriched CSVs and the review queue")
    sub.add_parser("report", help="regenerate the QC report")

    p = sub.add_parser("suppress", help="erase a person's enrichment (deletion request)")
    p.add_argument("--row-uid", nargs="+", required=True)
    p.add_argument("--note", help="reason, stored with the tombstone")

    p = sub.add_parser("calibrate", help="fit the score-to-probability mapping")
    p.add_argument("--labels", required=True, help="CSV with raw_score,correct columns")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    overrides = {
        "input_paths": getattr(args, "input_paths", None),
        "db_path": args.db_path,
        "output_dir": args.output_dir,
        "log_level": args.log_level,
        "workers": getattr(args, "workers", None),
        "confidence_threshold": getattr(args, "confidence_threshold", None),
    }
    if args.no_dashboard:
        overrides["dashboard"] = False
    if getattr(args, "provider", None):
        overrides["provider"] = {"name": args.provider}
    if getattr(args, "rate", None):
        overrides["rate_limit"] = {"requests_per_second": args.rate}

    try:
        settings = Settings.load(args.config, overrides)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    log_setup.configure(
        settings.log_dir, settings.log_level,
        quiet_console=settings.dashboard and args.command in {"run", "resume"},
    )

    handlers = {
        "ingest": cmd_ingest, "run": cmd_run, "resume": cmd_resume,
        "preflight": cmd_preflight,
        "dry-run": cmd_dry_run, "estimate": cmd_estimate, "write": cmd_write,
        "report": cmd_report, "suppress": cmd_suppress, "calibrate": cmd_calibrate,
    }
    try:
        return handlers[args.command](args, settings)
    except KeyboardInterrupt:
        print("\ninterrupted — progress is checkpointed; run `resume` to continue",
              file=sys.stderr)
        return 130
    except ProviderError as exc:
        print(f"provider error: {exc}", file=sys.stderr)
        return 3
    except FileNotFoundError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2


def _num(value: str):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return value


def _duration(seconds: float) -> str:
    if seconds >= 86_400:
        return f"{seconds / 86_400:,.1f}d"
    if seconds >= 3_600:
        return f"{seconds / 3_600:,.1f}h"
    return f"{seconds / 60:,.1f}m"


if __name__ == "__main__":
    sys.exit(main())
