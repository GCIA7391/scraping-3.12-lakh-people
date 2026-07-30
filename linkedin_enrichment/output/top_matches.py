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
