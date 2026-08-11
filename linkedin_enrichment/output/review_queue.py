"""Review queue: candidates that scored well but not well enough.

These rows are *not* matches and are never written into the enriched output's
LinkedIn column. They exist because a strict 95% threshold necessarily discards
some correct answers along with the wrong ones, and the search spend on those
rows is otherwise lost. An analyst can confirm or reject them by eye far faster
than re-running the search.

Every entry carries the reason it was rejected and the feature evidence behind
its score, so a reviewer can see *why* the pipeline hesitated.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

REVIEW_COLUMNS = (
    "Row UID", "Name", "Company", "Location", "Designation",
    "Candidate LinkedIn URL", "Score", "Rejection Reason", "Evidence",
)


def write_review_queue(store, output_dir: str | Path, filename: str = "review_queue.csv") -> Path:
    """Write every sub-threshold candidate, highest score first."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / filename

    count = 0
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(REVIEW_COLUMNS)
        for row in store.iter_review_queue():
            writer.writerow([
                row["row_uid"], row["name"], row["company"], row["location"],
                row["designation"], row["linkedin_url"],
                f"{float(row['confidence']):.4f}", row["reason"], row["evidence"],
            ])
            count += 1

    logger.info("wrote %s review candidate(s) to %s", f"{count:,}", destination)
    return destination
