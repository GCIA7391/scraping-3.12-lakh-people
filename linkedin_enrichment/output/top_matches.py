"""The precision-mode deliverable: the best N matches, ranked by confidence.

Separate from the full enriched CSVs on purpose. Those mirror the input row for
row and are mostly blanks; this is the short, dense list that gets acted on. Every
row here cleared the confidence threshold *and* carried an independent
corroborating source, so each one can be defended individually.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

COLUMNS = (
    "Rank",
    "Name",
    "Company",
    "Designation",
    "Location",
    "LinkedIn Profile",
    "Confidence Score",
    "Corroborating Source",
    "Verification Notes",
    "Row UID",
)


def write_top_matches(
    store, output_dir: str | Path, *, limit: int = 1000,
    filename: str = "top_matches.csv",
) -> tuple[Path, int]:
    """Write the ranked deliverable. Returns (path, rows written)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename

    rows = store.top_matches(limit)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for rank, row in enumerate(rows, start=1):
            writer.writerow([
                rank,
                row["name"],
                row["company"],
                row["designation"],
                row["location"],
                row["linkedin_url"],
                f"{float(row['confidence']):.4f}",
                _corroborating_source(row["source_urls"]),
                row["notes"],
                row["row_uid"],
            ])

    logger.info("wrote %s ranked match(es) to %s", f"{len(rows):,}", destination)
    return destination, len(rows)


PROSPECT_COLUMNS = (
    "Rank",
    "Name",
    "Company",
    "Designation",
    "Location",
    "LinkedIn Profile",
    "Confidence Score",
    "Best Contact Type",
    "Contact Routes",
    "Contact Source URL(s)",
    "Verification Notes",
    "Row UID",
)


def write_prospects(
    store, output_dir: str | Path, *, limit: int = 3000,
    filename: str = "prospects.csv",
) -> tuple[Path, int]:
    """The deliverable a sales team works: every row with a way in.

    Wider than ``top_matches.csv``, which lists confirmed identities only. A
    director of a company that publishes a leadership page and an IR address is
    a usable lead whether or not their personal profile was ever confirmed — and
    every route here carries the URL that published it, so any entry can be
    checked in one click.
    """
    from . import contacts as contacts_mod

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename

    rows = store.delivered_prospects(limit)
    routes_index = store.routes_by_row()

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(PROSPECT_COLUMNS)
        for rank, row in enumerate(rows, start=1):
            routes = contacts_mod.deserialise([
                {"type": r["route_type"], "value": r["value"],
                 "source_url": r["source_url"], "label": r["label"], "scope": r["scope"]}
                for r in routes_index.get(row["row_uid"], ())
            ])
            best = contacts_mod.best_route(routes)
            confidence = float(row["confidence"] or 0.0)
            writer.writerow([
                rank,
                row["name"],
                row["company"],
                row["designation"],
                row["location"],
                row["linkedin_url"],
                # Blank rather than 0.0000 for a contact-only row: a numeric zero
                # reads as "scored and rejected" when nothing was scored at all.
                f"{confidence:.4f}" if row["linkedin_url"] else "",
                best.type.value if best else "",
                contacts_mod.render_routes(routes),
                contacts_mod.render_sources(routes),
                row["notes"],
                row["row_uid"],
            ])

    logger.info("wrote %s prospect(s) to %s", f"{len(rows):,}", destination)
    return destination, len(rows)


def _corroborating_source(source_urls: str) -> str:
    """The non-LinkedIn source, which is the independent evidence.

    ``source_urls`` holds the candidate profile URLs followed by the
    corroborating URL appended at verification time; the interesting one for a
    reviewer is the source that is *not* LinkedIn.
    """
    urls = [u.strip() for u in (source_urls or "").split("|") if u.strip()]
    for url in reversed(urls):
        if "linkedin.com" not in url:
            return url
    return ""
