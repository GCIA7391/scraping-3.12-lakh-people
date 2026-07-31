"""Pilot report — measure on a slice, then project the file.

The point of a pilot is to replace an estimate with a measurement before
committing 312,160 rows to a run that may take days. Everything here is either
counted from the pilot slice or derived from those counts by stated arithmetic;
nothing is assumed.

The projection carries one honest caveat, printed with it: a pilot is only
representative of the rows it drew from. A pilot over the ranked head of the file
will convert better than the tail, so a ranked pilot's projection is an upper
bound and is labelled as one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BAR = "=" * 78


@dataclass
class Pilot:
    rows: int = 0
    profiles: int = 0
    contact_only: int = 0
    with_routes: int = 0
    routes_found: int = 0
    errors: int = 0
    deferred: int = 0
    elapsed: float = 0.0
    queries: int = 0
    route_types: dict[str, int] = field(default_factory=dict)
    file_rows: int = 0
    target: int = 0
    ranked: bool = False

    @property
    def delivered(self) -> int:
        return self.profiles + self.contact_only

    @property
    def conversion(self) -> float:
        return self.delivered / self.rows if self.rows else 0.0

    @property
    def route_rate(self) -> float:
        return self.with_routes / self.rows if self.rows else 0.0

    @property
    def routes_per_row(self) -> float:
        return self.routes_found / self.with_routes if self.with_routes else 0.0

    @property
    def rows_per_second(self) -> float:
        return self.rows / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def queries_per_row(self) -> float:
        return self.queries / self.rows if self.rows else 0.0

    @property
    def projected_total(self) -> int:
        """Deliverables if the whole file converted at the measured rate."""
        return int(self.file_rows * self.conversion)

    @property
    def rows_to_target(self) -> int:
        """Rows that must be worked to reach the target at the measured rate."""
        if self.conversion <= 0:
            return 0
        return int(self.target / self.conversion)

    @property
    def seconds_to_target(self) -> float:
        rate = self.rows_per_second
        if rate <= 0 or not self.rows_to_target:
            return float("inf")
        return self.rows_to_target / rate


def build(progress, store, settings, *, file_rows: int = 0, queries: int = 0) -> Pilot:
    return Pilot(
        rows=progress.processed,
        profiles=progress.matched,
        contact_only=progress.contact_only,
        with_routes=progress.with_routes,
        routes_found=progress.routes_found,
        errors=progress.errors,
        deferred=progress.deferred,
        elapsed=progress.elapsed,
        queries=queries,
        route_types=store.route_type_counts(),
        file_rows=file_rows or (store.scalar("SELECT COUNT(*) FROM records") or 0),
        target=settings.target_matches or 0,
        ranked=settings.rank_claim_order,
    )


def render(p: Pilot) -> str:
    lines = [
        BAR,
        f"  PILOT — {p.rows:,} rows worked",
        BAR,
        "",
        "  WHAT CAME OUT",
        f"    LinkedIn profiles confirmed   : {p.profiles:>9,}"
        f"  {p.profiles / max(p.rows, 1):>7.2%}",
        f"    Delivered on contact routes   : {p.contact_only:>9,}"
        f"  {p.contact_only / max(p.rows, 1):>7.2%}",
        f"    {'Total deliverables':<30}: {p.delivered:>9,}"
        f"  {p.conversion:>7.2%}",
        "",
        f"    Rows with >=1 contact route   : {p.with_routes:>9,}"
        f"  {p.route_rate:>7.2%}",
        f"    Routes per such row           : {p.routes_per_row:>9.2f}",
    ]

    if p.route_types:
        lines += ["", "  ROUTE MIX"]
        total = sum(p.route_types.values()) or 1
        for route_type, count in sorted(p.route_types.items(), key=lambda kv: -kv[1]):
            lines.append(f"    {route_type:<30}: {count:>9,}  {count / total:>7.1%}")

    lines += [
        "",
        "  THROUGHPUT",
        f"    Rows per second               : {p.rows_per_second:>9.2f}",
        f"    Queries per row               : {p.queries_per_row:>9.2f}",
        f"    Search failures               : {p.errors:>9,}",
        f"    Deferred for retry            : {p.deferred:>9,}",
    ]

    if p.errors and p.rows and p.errors / p.rows > 0.2:
        lines += [
            "",
            "  WARNING",
            f"    {p.errors / p.rows:.0%} of rows failed at the SEARCH layer. This pilot measures",
            "    the search backend, not the data. Fix it and re-run the pilot before",
            "    reading anything below as a property of the file.",
        ]

    lines += ["", "  PROJECTION"]
    if p.conversion <= 0:
        lines += [
            "    Nothing converted, so there is no rate to project from.",
            "    Run `preflight` — a zero-conversion pilot is almost always a broken",
            "    search backend rather than a file with no findable people in it.",
        ]
    else:
        lines += [
            f"    Measured conversion           : {p.conversion:>9.2%}",
            f"    File size                     : {p.file_rows:>9,}",
            f"    Projected deliverables        : {p.projected_total:>9,}",
        ]
        if p.target:
            lines.append(f"    Target                        : {p.target:>9,}")
            if p.projected_total >= p.target:
                lines.append(
                    f"    Rows needed for the target    : {p.rows_to_target:>9,}"
                    f"  ({_duration(p.seconds_to_target)} at this rate)"
                )
            else:
                lines.append(
                    f"    SHORTFALL: the whole file projects to {p.projected_total:,}, "
                    f"below the {p.target:,} target."
                )
        lines += [
            "",
            "    A pilot is only representative of the rows it drew from.",
        ]
        if p.ranked:
            lines.append(
                "    This pilot was RANKED — it worked the most promising rows first,\n"
                "    so the projection above is an upper bound, not a central estimate."
            )
        else:
            lines.append(
                "    This pilot was unranked (file order), so it is a fair sample."
            )

    lines.append(BAR)
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    if seconds == float("inf"):
        return "unknown"
    if seconds >= 86_400:
        return f"{seconds / 86_400:,.1f}d"
    if seconds >= 3_600:
        return f"{seconds / 3_600:,.1f}h"
    return f"{seconds / 60:,.1f}m"
