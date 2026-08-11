"""Offline value-tier export.

Two passes over the real workbooks. No network, no search, no enrichment:
every column here is derived arithmetically from the input file itself.
The LinkedIn and Contact columns are deliberately emitted empty, because
nothing has been scraped.

Usage::

    python scripts/value_rank.py --input part1.csv part2.csv --output ranked.csv
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from linkedin_enrichment.identity.priority import Corpus, score_row
from linkedin_enrichment.ingest.normalize import normalize_company, normalize_name
from linkedin_enrichment.ingest.prefilter import evaluate
from linkedin_enrichment.ingest.reader import read_all


# A board count is only evidence of ONE person holding many directorships when
# the name is distinctive enough that collapsing rows by name is safe. Without
# this gate "Manoj Kumar" reports 54 boards -- that is 54 different people, and
# it put 71,057 rows into the serial-director tier on a homonym artifact.
# 0.5 on priority.name_rarity means the rarest token appears <=158 times.
RARITY_GATE = 0.5


def tier_of(boards: int, company_size: int, rarity: float) -> str:
    """Tiers as specified in docs/clv-model.md, built only from real signals."""
    serial = boards >= 3 and rarity >= RARITY_GATE
    substantial = company_size >= 5
    if serial and substantial:
        return "A"
    if substantial:
        return "B"
    if serial:
        return "C"
    return "D"


TIER_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", nargs="+", required=True, dest="paths",
                        help="input .csv/.xlsx workbooks")
    parser.add_argument("--output", required=True, help="destination .csv")
    args = parser.parse_args()
    PATHS, OUT = args.paths, args.output

    # ---- pass 1: corpus statistics -------------------------------------
    corpus = Corpus()
    name_rows: Counter = Counter()
    total = 0
    for row in read_all(PATHS):
        corpus.observe(row.name, row.company, row.designation)
        name_rows["-".join(normalize_name(row.name).canonical)] += 1
        total += 1
        if total % 50_000 == 0:
            print(f"  pass 1: {total:,} rows", flush=True)
    print(f"pass 1 complete: {total:,} rows, "
          f"{len(corpus.company_sizes):,} companies", flush=True)

    # ---- pass 2: score, tier, collect ----------------------------------
    records = []
    skipped = Counter()
    seen: set[str] = set()
    for row in read_all(PATHS):
        verdict = evaluate(row, seen)
        if not verdict.searchable:
            skipped[verdict.reason.value] += 1
            continue

        company_key = normalize_company(row.company).key
        name_key = "-".join(normalize_name(row.name).canonical)
        boards = len(corpus.person_boards.get(name_key, ()))
        size = corpus.company_sizes.get(company_key, 0)
        rarity = corpus.name_rarity(row.name)
        sharing = name_rows.get(name_key, 1)
        score = score_row(corpus, row.name, row.company, row.designation)
        # Only report a board count the identity gate actually trusts.
        trusted_boards = boards if rarity >= RARITY_GATE else ""
        records.append((
            tier_of(boards, size, rarity), -score, row.name, row.company,
            row.designation, row.industry, row.location, trusted_boards, size,
            sharing, round(rarity, 3), row.row_uid, row.pc_url,
        ))

    print(f"pass 2 complete: {len(records):,} eligible, "
          f"{sum(skipped.values()):,} set aside", flush=True)

    records.sort(key=lambda r: (TIER_ORDER[r[0]], r[1]))

    tier_counts = Counter(r[0] for r in records)

    with open(OUT, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "Outreach Rank", "Value Tier", "Priority Score", "Name", "Company",
            "Designation", "Industry", "Location", "Boards Held (trusted)",
            "Directors At Company", "Rows Sharing This Name",
            "Name Distinctiveness", "LinkedIn Profile", "Contact Routes",
            "Enrichment Status", "Row UID", "PrivateCircle URL",
        ])
        for rank, r in enumerate(records, 1):
            writer.writerow([
                rank, r[0], -r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8],
                r[9], r[10], "", "", "NOT SCRAPED", r[11], r[12],
            ])

    print(f"\nwrote {OUT}")
    print("\ntier distribution:")
    for tier in ("A", "B", "C", "D"):
        n = tier_counts[tier]
        print(f"  {tier}: {n:>7,}  ({n / len(records) * 100:5.2f}%)")
    print("\nset aside:")
    for reason, n in skipped.most_common():
        print(f"  {reason}: {n:,}")


if __name__ == "__main__":
    main()
