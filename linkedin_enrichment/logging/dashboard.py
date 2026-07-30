"""Live progress dashboard.

Shows the numbers that actually matter during a long run: how fast rows are being
resolved, how many queries that is costing, whether the provider is throttling,
and how much of the work the caches are absorbing. Degrades to periodic log lines
when ``rich`` is unavailable or the output is not a terminal.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class Dashboard:
    """Renders ``RunProgress`` plus search/cache stats until stopped."""

    def __init__(self, progress, client, runner, *, enabled: bool = True, interval: float = 1.0):
        self.progress = progress
        self.client = client
        self.runner = runner
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        self.enabled = enabled and self._rich_available()

    @staticmethod
    def _rich_available() -> bool:
        try:
            import rich  # noqa: F401
            return True
        except ImportError:
            return False

    async def __aenter__(self) -> "Dashboard":
        self._task = asyncio.create_task(self._run(), name="dashboard")
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _run(self) -> None:
        if self.enabled:
            await self._run_rich()
        else:
            await self._run_plain()

    async def _run_plain(self) -> None:
        """Fallback: a log line every 30 seconds."""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                p = self.progress
                logger.info(
                    "progress %s/%s rows | %s matched | %.2f rows/s | %s queries",
                    f"{p.processed:,}", f"{p.total:,}", f"{p.matched:,}",
                    p.rate_per_second, f"{self.client.queries_issued:,}",
                )

    async def _run_rich(self) -> None:
        from rich.console import Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.progress import (
            BarColumn, Progress, TaskProgressColumn, TextColumn, TimeRemainingColumn,
        )
        from rich.table import Table

        bar = Progress(
            TextColumn("[bold blue]{task.description}"),
            BarColumn(bar_width=None),
            TaskProgressColumn(),
            TextColumn("{task.completed:,}/{task.total:,}"),
            TimeRemainingColumn(),
            expand=True,
        )
        task_id = bar.add_task("Enriching", total=max(self.progress.total, 1))

        with Live(refresh_per_second=4) as live:
            while not self._stop.is_set():
                bar.update(task_id, completed=self.progress.processed)
                live.update(Group(bar, Panel(self._stats_table(Table), title="Live statistics")))
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    continue

    def _stats_table(self, Table):  # noqa: N803 - injected to keep rich import lazy
        p = self.progress
        search = self.client.stats()
        ladder = self.runner.stats()

        table = Table.grid(padding=(0, 3))
        table.add_column(justify="right", style="cyan")
        table.add_column(justify="left")
        table.add_column(justify="right", style="cyan")
        table.add_column(justify="left")

        rows = [
            ("Matched", f"{p.matched:,}", "Blank", f"{p.blank:,}"),
            ("Match rate", f"{(p.matched / p.processed if p.processed else 0):.2%}",
             "Avg confidence", f"{p.average_confidence:.4f}"),
            ("Rows/sec", f"{p.rate_per_second:.2f}",
             "ETA", _eta(p.eta_seconds)),
            ("Queries issued", f"{search['queries_issued']:,}",
             "From cache", f"{search['queries_from_cache']:,}"),
            ("Tier 1 (company)", f"{ladder['tier1_company_queries']:,}",
             "Tier 2 (person)", f"{ladder['tier2_person_queries']:,}"),
            ("Negative-cache skips", f"{ladder['negative_cache_skips']:,}",
             "Solved from roster", f"{ladder['resolved_from_roster']:,}"),
            ("Rate (req/s)", f"{search['current_rate_per_second']:.3f}",
             "Throttles", f"{search['throttle_events']:,}"),
            ("Search errors", f"{search['search_errors']:,}",
             "Row errors", f"{p.errors:,}"),
        ]
        for left_label, left_value, right_label, right_value in rows:
            table.add_row(left_label, left_value, right_label, right_value)
        return table


def _eta(seconds: float) -> str:
    if seconds == float("inf") or seconds != seconds:
        return "—"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 48:
        return f"{hours // 24}d {hours % 24}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def print_report(text: str) -> None:
    """Print the final report, using rich when available."""
    try:
        from rich.console import Console
        Console().print(text)
    except ImportError:
        print(text)
