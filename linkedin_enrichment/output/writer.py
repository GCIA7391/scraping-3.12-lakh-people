"""Enriched output writer.

Guarantee: the original columns are reproduced exactly as they arrived, and the
four new columns are *appended*. Nothing is reordered, reformatted or dropped.

This is achieved by re-reading the source file rather than reconstructing rows
from the database. The database holds only the fields the pipeline parsed; the
source file holds the truth, including columns the pipeline never looked at.
Re-reading also means a row that was skipped, errored or never processed still
appears in the output, in its original position, with blank enrichment columns.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Iterator

from ..ingest.reader import make_row_uid, read_file
from . import contacts as contacts_mod

logger = logging.getLogger(__name__)

# Appended in this order. Names match the specification exactly.
OUTPUT_COLUMNS = ("LinkedIn Profile", "Confidence Score", "Verification Notes", "Source URL(s)")

# Then the contact-route columns. Appended after the original four so a consumer
# reading only the specified columns is unaffected.
CONTACT_COLUMNS = contacts_mod.CONTACT_COLUMNS

# Flush to disk every N rows so a crash costs at most this many rows of output.
FLUSH_EVERY = 2_000


def load_results(store, source_file: str) -> dict[str, dict[str, Any]]:
    """Index this file's decisions by row_uid for O(1) lookup during writing."""
    index: dict[str, dict[str, Any]] = {}
    for row in store.iter_output_rows(source_file):
        index[row["row_uid"]] = {
            "linkedin_url": row["linkedin_url"],
            "confidence": row["confidence"],
            "notes": row["notes"],
            "source_urls": row["source_urls"],
            "decision": row["decision"],
            "skip_reason": row["skip_reason"],
            "status": row["status"],
        }
    return index


def _enrichment_cells(entry: dict[str, Any] | None) -> tuple[str, str, str, str]:
    """Render the four appended cells for one row.

    The LinkedIn column is populated *only* for a decision of ``matched``. Every
    other outcome leaves it empty and explains itself in Verification Notes —
    which is the whole point of the exercise.
    """
    if entry is None:
        return "", "", "not processed", ""

    if entry["decision"] == "matched" and entry["linkedin_url"]:
        return (
            entry["linkedin_url"],
            f"{float(entry['confidence']):.4f}",
            entry["notes"],
            entry["source_urls"],
        )

    note = entry["notes"] or entry["skip_reason"] or entry["decision"] or "not processed"
    # Confidence is deliberately blank rather than 0.0 for unmatched rows: a
    # numeric zero invites a downstream reader to treat the row as scored-and-rejected
    # when it may simply never have been searched.
    return "", "", note, entry["source_urls"]


def write_enriched_csv(
    store, source_file: str, output_dir: str | Path, suffix: str = "_enriched"
) -> Path:
    """Write one enriched CSV mirroring one input file. Returns the output path."""
    source = Path(source_file)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / f"{source.stem}{suffix}.csv"

    index = load_results(store, source_file)
    routes_index = store.routes_by_row(source_file)
    written = 0
    matched = 0
    with_routes = 0

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer: csv.DictWriter | None = None
        columns = OUTPUT_COLUMNS + CONTACT_COLUMNS

        for row in read_file(source):
            if writer is None:
                fieldnames = list(row.headers) + [
                    c for c in columns if c not in row.headers
                ]
                writer = csv.DictWriter(
                    handle, fieldnames=fieldnames, extrasaction="ignore"
                )
                writer.writeheader()

            uid = make_row_uid(source_file, row.sheet_name, row.row_index)
            profile, confidence, notes, sources = _enrichment_cells(index.get(uid))
            if profile:
                matched += 1

            routes = contacts_mod.deserialise([
                {"type": r["route_type"], "value": r["value"],
                 "source_url": r["source_url"], "label": r["label"], "scope": r["scope"]}
                for r in routes_index.get(uid, ())
            ])
            if routes:
                with_routes += 1
            best = contacts_mod.best_route(routes)

            record = dict(row.raw)
            record[OUTPUT_COLUMNS[0]] = profile
            record[OUTPUT_COLUMNS[1]] = confidence
            record[OUTPUT_COLUMNS[2]] = notes
            record[OUTPUT_COLUMNS[3]] = sources
            record[CONTACT_COLUMNS[0]] = contacts_mod.render_routes(routes)
            record[CONTACT_COLUMNS[1]] = best.type.value if best else ""
            record[CONTACT_COLUMNS[2]] = contacts_mod.render_sources(routes)
            writer.writerow(record)

            written += 1
            if written % FLUSH_EVERY == 0:
                handle.flush()

    logger.info(
        "wrote %s rows (%s with a profile, %s with a contact route) to %s",
        f"{written:,}", f"{matched:,}", f"{with_routes:,}", destination,
    )
    return destination


def write_all(store, output_dir: str | Path) -> list[Path]:
    """Write an enriched file for every source file in the database."""
    return [
        write_enriched_csv(store, source_file, output_dir)
        for source_file in store.source_files()
    ]
