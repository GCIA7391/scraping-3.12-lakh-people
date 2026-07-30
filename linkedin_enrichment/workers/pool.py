"""Async worker pool with checkpointing and graceful shutdown.

asyncio rather than threads or processes: the workload is entirely I/O-bound and
strictly rate-limited, so the limiting factor is how fast the provider will answer,
not CPU. Coroutines make the shared adaptive rate limiter trivially correct, where
threads would need locking around it and processes could not share it at all.

Shutdown contract: on SIGINT/SIGTERM the pool stops claiming new work, lets
in-flight rows finish, returns any unstarted claims to the pending pool, and
exits cleanly. A resumed run then re-reads the database and continues exactly
where it stopped — nothing is lost, nothing is done twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from dataclasses import dataclass, field

from ..identity.scorer import Decision
from .runner import LadderRunner, RowOutcome

logger = logging.getLogger(__name__)


@dataclass
class RunProgress:
    """Live counters, shared with the dashboard."""

    total: int = 0
    processed: int = 0
    matched: int = 0
    blank: int = 0
    errors: int = 0
    #: Transient failures parked for a later pass.
    deferred: int = 0
    #: Rows that used up their attempt budget and were finalised.
    exhausted: int = 0
    started_at: float = field(default_factory=time.time)
    decisions: dict[str, int] = field(default_factory=dict)
    confidence_sum: float = 0.0

    @property
    def elapsed(self) -> float:
        return max(1e-6, time.time() - self.started_at)

    @property
    def rate_per_second(self) -> float:
        return self.processed / self.elapsed

    @property
    def average_confidence(self) -> float:
        return (self.confidence_sum / self.matched) if self.matched else 0.0

    @property
    def eta_seconds(self) -> float:
        remaining = max(0, self.total - self.processed)
        rate = self.rate_per_second
        return (remaining / rate) if rate > 0 else float("inf")

    def record(self, outcome: RowOutcome) -> None:
        self.processed += 1
        key = outcome.match.decision.value
        self.decisions[key] = self.decisions.get(key, 0) + 1
        if outcome.match.matched:
            self.matched += 1
            self.confidence_sum += outcome.match.confidence
        else:
            self.blank += 1
        if outcome.match.decision is Decision.BLANK_ERROR:
            self.errors += 1


class WorkerPool:
    """Claims batches, runs them concurrently, and checkpoints every result."""

    def __init__(
        self, store, runner: LadderRunner, settings, progress: RunProgress,
        *, limit: int | None = None, on_row=None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.settings = settings
        self.progress = progress
        #: Stop after this many rows (``--limit``). None means process everything.
        self.limit = limit
        #: Optional callback invoked with each RowOutcome (``--explain``).
        self.on_row = on_row

        self._shutdown = asyncio.Event()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=settings.workers * 2)
        self._inflight: set[str] = set()
        self._dispatched = 0

    # ------------------------------------------------------------------
    def request_shutdown(self, reason: str = "signal") -> None:
        if not self._shutdown.is_set():
            logger.warning("shutdown requested (%s); draining in-flight work", reason)
            self._shutdown.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, self.request_shutdown, sig.name)

    # ------------------------------------------------------------------
    async def run(self) -> RunProgress:
        """Process every pending row, or until shutdown is requested."""
        self.install_signal_handlers()

        producer = asyncio.create_task(self._produce(), name="producer")
        consumers = [
            asyncio.create_task(self._consume(i), name=f"worker-{i}")
            for i in range(self.settings.workers)
        ]

        try:
            await producer
            await self._queue.join()
        finally:
            for task in consumers:
                task.cancel()
            await asyncio.gather(*consumers, return_exceptions=True)
            # Anything claimed but never started goes back to pending so a
            # resumed run picks it up.
            if self._inflight:
                self.store.release(sorted(self._inflight))
                logger.info("released %d unfinished claim(s)", len(self._inflight))

        return self.progress

    async def _produce(self) -> None:
        """Claim batches and feed the queue until work runs out or we stop."""
        while not self._shutdown.is_set():
            remaining = None if self.limit is None else self.limit - self._dispatched
            if remaining is not None and remaining <= 0:
                break
            size = self.settings.batch_size if remaining is None else min(
                self.settings.batch_size, remaining
            )
            batch = await asyncio.to_thread(self.store.claim_batch, size)
            if not batch:
                break
            for record in batch:
                if self._shutdown.is_set() or (
                    self.limit is not None and self._dispatched >= self.limit
                ):
                    # Return the rest of this batch rather than holding claims.
                    unstarted = [
                        r["row_uid"] for r in batch
                        if r["row_uid"] not in self._inflight
                    ]
                    await asyncio.to_thread(self.store.release, unstarted)
                    return
                self._inflight.add(record["row_uid"])
                self._dispatched += 1
                await self._queue.put(record)

    async def _consume(self, worker_id: int) -> None:
        while True:
            record = await self._queue.get()
            row_uid = record["row_uid"]
            try:
                outcome = await self.runner.resolve_row(record)
            except asyncio.CancelledError:
                # Cancelled mid-row: leave the claim in place; the stale-claim
                # reclaim on the next startup will return it to pending.
                self._queue.task_done()
                raise
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the run
                logger.exception("worker %d failed on row %s", worker_id, row_uid)
                # Deferred, not marked 'error': nothing ever reclaimed 'error',
                # so a row that threw once was stranded for good.
                await asyncio.to_thread(
                    self.store.defer_row, row_uid,
                    error=f"{type(exc).__name__}: {exc}",
                    max_attempts=self.settings.max_row_attempts,
                )
                self.progress.processed += 1
                self.progress.errors += 1
                self._inflight.discard(row_uid)
                self._queue.task_done()
                continue

            try:
                await asyncio.to_thread(self._persist, outcome)
                self.progress.record(outcome)
                if self.on_row is not None:
                    # Presentation only — a failure here must not lose a result
                    # that has already been persisted.
                    try:
                        self.on_row(outcome, self.progress.processed)
                    except Exception:  # noqa: BLE001
                        logger.exception("explain callback failed for row %s", row_uid)
            finally:
                self._inflight.discard(row_uid)
                self._queue.task_done()

    def _persist(self, outcome: RowOutcome) -> None:
        """Write the decision, then any review candidates. Checkpoint per row.

        A transient failure is *deferred* rather than written as a final answer:
        a search that errored or came back unparseable tells us nothing about the
        person, and recording it as a settled blank would bake an infrastructure
        fault into the deliverable.
        """
        match = outcome.match

        if match.decision is Decision.BLANK_ERROR:
            deferred = self.store.defer_row(
                outcome.row_uid,
                error=outcome.error or match.notes or "transient search failure",
                max_attempts=self.settings.max_row_attempts,
            )
            if deferred:
                self.progress.deferred += 1
                return
            # Budget exhausted: defer_row has finalised it with an honest note.
            self.progress.exhausted += 1
            return

        self.store.write_result(
            outcome.row_uid,
            linkedin_url=match.linkedin_url,
            confidence=round(match.confidence, 6),
            notes=match.notes,
            source_urls=" | ".join(match.source_urls),
            decision=match.decision.value,
            top_score=match.top_score,
            runner_up_score=match.runner_up_score,
            margin=match.margin,
            provider=self.runner.client.provider.name,
            queries_used=outcome.queries_used,
        )

        # Sub-threshold candidates go to a separate review file only. They are
        # never written into the LinkedIn Profile column.
        floor = self.settings.review_queue_floor
        candidates = [
            {
                "linkedin_url": c.url,
                "confidence": round(c.confidence, 6),
                "reason": c.reason or "below confidence threshold",
                "evidence": c.evidence(),
            }
            for c in match.review_candidates
            if c.url and c.confidence >= floor
        ]
        if candidates:
            self.store.add_review_candidates(outcome.row_uid, candidates)
