"""Thread-safe SQLite state store implementing the claim/resume protocol.

Concurrency model: one connection guarded by a re-entrant lock. The workload is
network-bound (one search query takes seconds; a SQLite write takes microseconds),
so serialising database access costs nothing measurable and removes an entire
class of concurrency bugs.

Resume guarantee: a row is only ever moved out of ``pending`` inside a
transaction, and it is only moved to ``done`` after its result has been written.
A process killed at any instant therefore leaves every row either completed or
reclaimable — never silently lost, never double-counted.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .schema import DDL, PRAGMAS, SCHEMA_VERSION

logger = logging.getLogger(__name__)


class Store:
    """All pipeline persistence. Safe to share across threads."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        for pragma in PRAGMAS:
            self._conn.execute(pragma)
        self._conn.executescript(DDL)
        self._migrate()
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` silently leaves an existing table alone,
        so a database from an earlier version would otherwise be missing the new
        columns and every query touching them would fail.
        """
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(records)")
        }
        for column, ddl in (
            ("priority", "ALTER TABLE records ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"),
            ("preferred", "ALTER TABLE records ADD COLUMN preferred INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in existing:
                self._conn.execute(ddl)
                logger.info("migrated: added records.%s", column)

        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(results)")
        }
        for column, ddl in (
            ("route_count",
             "ALTER TABLE results ADD COLUMN route_count INTEGER NOT NULL DEFAULT 0"),
            ("best_route_type",
             "ALTER TABLE results ADD COLUMN best_route_type TEXT NOT NULL DEFAULT ''"),
        ):
            if column not in existing:
                self._conn.execute(ddl)
                logger.info("migrated: added results.%s", column)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Serialise and commit a unit of work; roll back on any failure."""
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def insert_records(self, rows: Iterable[dict[str, Any]]) -> int:
        """Idempotently insert input rows.

        ``INSERT OR IGNORE`` means re-running ingestion over the same files is a
        no-op rather than an error or a duplicate — which is what makes ``run``
        on an existing database behave as ``resume``.
        """
        payload = [
            (
                r["row_uid"], r["source_file"], r.get("sheet_name", ""), r["row_index"],
                r.get("name", ""), r.get("company", ""), r.get("designation", ""),
                r.get("location", ""), r.get("industry", ""), r.get("pc_url", ""),
                r.get("name_norm", ""), r.get("company_key", ""), r.get("dedup_key", ""),
                r.get("dup_of"), r.get("status", "pending"), r.get("skip_reason", ""),
                int(r.get("priority", 0)), int(bool(r.get("preferred", 0))),
                time.time(),
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self._tx() as conn:
            cursor = conn.executemany(
                """
                INSERT OR IGNORE INTO records (
                    row_uid, source_file, sheet_name, row_index,
                    name, company, designation, location, industry, pc_url,
                    name_norm, company_key, dedup_key, dup_of, status, skip_reason,
                    priority, preferred, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
            return cursor.rowcount

    def set_priorities(self, scored: Iterable[tuple[str, int, int]]) -> int:
        """Bulk-assign (row_uid, priority, preferred). Used by ``main.py rank``."""
        payload = [(int(p), int(bool(pref)), uid) for uid, p, pref in scored]
        if not payload:
            return 0
        with self._tx() as conn:
            conn.executemany(
                "UPDATE records SET priority = ?, preferred = ? WHERE row_uid = ?",
                payload,
            )
        return len(payload)

    def has_records(self) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM records LIMIT 1").fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Claim / resume
    # ------------------------------------------------------------------
    def reclaim_stale(self, stale_seconds: float) -> int:
        """Return abandoned claims to the pending pool.

        Called at startup. A row still ``claimed`` after ``stale_seconds`` belongs
        to a worker that died; without this, a crash would strand work forever.
        """
        cutoff = time.time() - stale_seconds
        with self._tx() as conn:
            cursor = conn.execute(
                """
                UPDATE records SET status = 'pending', claimed_at = NULL
                WHERE status = 'claimed' AND (claimed_at IS NULL OR claimed_at < ?)
                """,
                (cutoff,),
            )
        if cursor.rowcount:
            logger.info("reclaimed %s stale claim(s)", f"{cursor.rowcount:,}")
        return cursor.rowcount

    def claim_batch(
        self, limit: int, *, ranked: bool = False, preferred_only: bool = False,
    ) -> list[sqlite3.Row]:
        """Atomically claim up to ``limit`` pending rows.

        Default order is by ``company_key`` so a batch tends to contain people
        from the same company, maximising company-cache hits and keeping the
        Tier-1 roster query shared rather than repeated.

        ``ranked`` switches to highest-``priority``-first for precision mode,
        which works the pool top-down and stops at a target. Company grouping is
        kept as the secondary key so cache locality is not lost entirely.
        """
        now = time.time()
        where = "status = 'pending'"
        if preferred_only:
            where += " AND preferred = 1"
        order = "priority DESC, company_key" if ranked else "company_key"

        with self._tx() as conn:
            rows = conn.execute(
                f"""
                SELECT row_uid FROM records
                WHERE {where}
                ORDER BY {order}
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            if not rows:
                return []
            uids = [r["row_uid"] for r in rows]
            placeholders = ",".join("?" * len(uids))
            conn.execute(
                f"""
                UPDATE records SET status = 'claimed', claimed_at = ?, updated_at = ?
                WHERE row_uid IN ({placeholders})
                """,
                (now, now, *uids),
            )
            return conn.execute(
                f"SELECT * FROM records WHERE row_uid IN ({placeholders})",
                uids,
            ).fetchall()

    def release(self, row_uids: Sequence[str]) -> None:
        """Return claimed rows to pending (graceful shutdown)."""
        if not row_uids:
            return
        placeholders = ",".join("?" * len(row_uids))
        with self._tx() as conn:
            conn.execute(
                f"""
                UPDATE records SET status = 'pending', claimed_at = NULL
                WHERE row_uid IN ({placeholders}) AND status = 'claimed'
                """,
                row_uids,
            )

    def mark_status(
        self, row_uid: str, status: str, *, skip_reason: str = "",
        error: str = "", tier: int | None = None, bump_attempts: bool = False,
    ) -> None:
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, time.time()]
        if skip_reason:
            sets.append("skip_reason = ?")
            params.append(skip_reason)
        if error:
            sets.append("last_error = ?")
            params.append(error[:500])
        if tier is not None:
            sets.append("tier = ?")
            params.append(tier)
        if bump_attempts:
            sets.append("attempts = attempts + 1")
        params.append(row_uid)
        with self._tx() as conn:
            conn.execute(f"UPDATE records SET {', '.join(sets)} WHERE row_uid = ?", params)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------
    def write_result(self, row_uid: str, **fields: Any) -> None:
        """Record a decision and mark the row done, atomically.

        Both statements share one transaction: a row can never be ``done`` without
        a result, nor carry a result while still claimable.
        """
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO results (
                    row_uid, linkedin_url, confidence, notes, source_urls, decision,
                    top_score, runner_up_score, margin, provider, queries_used,
                    route_count, best_route_type, resolved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(row_uid) DO UPDATE SET
                    linkedin_url=excluded.linkedin_url, confidence=excluded.confidence,
                    notes=excluded.notes, source_urls=excluded.source_urls,
                    decision=excluded.decision, top_score=excluded.top_score,
                    runner_up_score=excluded.runner_up_score, margin=excluded.margin,
                    provider=excluded.provider, queries_used=excluded.queries_used,
                    route_count=excluded.route_count,
                    best_route_type=excluded.best_route_type,
                    resolved_at=excluded.resolved_at
                """,
                (
                    row_uid,
                    fields.get("linkedin_url", ""), float(fields.get("confidence", 0.0)),
                    fields.get("notes", ""), fields.get("source_urls", ""),
                    fields.get("decision", ""), float(fields.get("top_score", 0.0)),
                    float(fields.get("runner_up_score", 0.0)), float(fields.get("margin", 0.0)),
                    fields.get("provider", ""), int(fields.get("queries_used", 0)),
                    int(fields.get("route_count", 0)), fields.get("best_route_type", ""),
                    now,
                ),
            )
            conn.execute(
                "UPDATE records SET status = 'done', updated_at = ? WHERE row_uid = ?",
                (now, row_uid),
            )

    def add_review_candidates(self, row_uid: str, candidates: Iterable[dict[str, Any]]) -> None:
        payload = [
            (row_uid, c["linkedin_url"], float(c["confidence"]),
             c.get("reason", ""), c.get("evidence", ""), time.time())
            for c in candidates
        ]
        if not payload:
            return
        with self._tx() as conn:
            conn.executemany(
                """INSERT INTO review_candidates
                   (row_uid, linkedin_url, confidence, reason, evidence, created_at)
                   VALUES (?,?,?,?,?,?)""",
                payload,
            )

    # ------------------------------------------------------------------
    # Transient-failure retry
    # ------------------------------------------------------------------
    def defer_row(self, row_uid: str, *, error: str, max_attempts: int) -> bool:
        """Park a transiently-failed row for a later pass.

        Returns True if the row was deferred, False if its attempt budget is
        exhausted and it has been finalised instead.

        A deferred row deliberately gets **no result row** while it still has
        attempts left, so a later pass can resolve it properly. Rows that run out
        of attempts are finalised with a note stating what actually happened —
        never a promise of a retry that will not come.
        """
        now = time.time()
        with self._tx() as conn:
            row = conn.execute(
                "SELECT attempts FROM records WHERE row_uid = ?", (row_uid,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1

            if attempts < max_attempts:
                conn.execute(
                    """
                    UPDATE records
                    SET status = 'deferred', attempts = ?, last_error = ?,
                        claimed_at = NULL, updated_at = ?
                    WHERE row_uid = ?
                    """,
                    (attempts, error[:500], now, row_uid),
                )
                return True

            # Budget exhausted: finalise honestly.
            conn.execute(
                """
                INSERT INTO results (row_uid, decision, notes, resolved_at)
                VALUES (?, 'blank_error', ?, ?)
                ON CONFLICT(row_uid) DO UPDATE SET
                    decision = 'blank_error', notes = excluded.notes,
                    linkedin_url = '', confidence = 0.0, resolved_at = excluded.resolved_at
                """,
                (
                    row_uid,
                    f"search did not succeed after {attempts} attempt(s); "
                    f"last error: {error[:200]}",
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE records
                SET status = 'done', attempts = ?, last_error = ?,
                    claimed_at = NULL, updated_at = ?
                WHERE row_uid = ?
                """,
                (attempts, error[:500], now, row_uid),
            )
            return False

    def requeue_deferred(self, max_attempts: int) -> int:
        """Return deferred rows to the pending pool. Called at run startup.

        Deferring parks a row until the *next* pass rather than putting it
        straight back to pending, because an immediate requeue would be
        re-claimed by the same producer against the same broken backend in a hot
        loop. This is the other half of that: at startup, conditions may have
        changed, so give them another go.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                """
                UPDATE records SET status = 'pending', claimed_at = NULL
                WHERE status = 'deferred' AND attempts < ?
                """,
                (max_attempts,),
            )
        if cursor.rowcount:
            logger.info("requeued %s deferred row(s)", f"{cursor.rowcount:,}")
        return cursor.rowcount

    def requeue_failed(self, *, reset_attempts: bool = False) -> int:
        """Requeue rows that failed, for ``main.py retry``.

        With ``reset_attempts`` the budget is cleared too, which is what you want
        after fixing the underlying cause (an egress allowlist, a dead SearXNG):
        the rows exhausted their attempts against a problem that no longer exists.
        """
        with self._tx() as conn:
            if reset_attempts:
                cursor = conn.execute(
                    """
                    UPDATE records SET status = 'pending', attempts = 0, claimed_at = NULL
                    WHERE status IN ('deferred', 'error')
                       OR row_uid IN (SELECT row_uid FROM results WHERE decision = 'blank_error')
                    """
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE records SET status = 'pending', claimed_at = NULL
                    WHERE status IN ('deferred', 'error')
                    """
                )
        logger.info("requeued %s failed row(s)", f"{cursor.rowcount:,}")
        return cursor.rowcount

    def record_unresolved_results(self) -> int:
        """Give still-deferred rows an output line explaining themselves.

        Mirrors ``record_skipped_results``. Without this a deferred row would
        appear in the enriched CSV as a bare "not processed". The next pass
        upserts over whatever is written here.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO results (row_uid, decision, notes, resolved_at)
                SELECT r.row_uid, 'blank_error',
                       'search did not complete; this row is queued for retry — '
                       || 'run `python main.py retry` after fixing the cause',
                       ?
                FROM records r
                LEFT JOIN results res ON res.row_uid = r.row_uid
                WHERE r.status = 'deferred' AND res.row_uid IS NULL
                """,
                (time.time(),),
            )
        return cursor.rowcount

    def record_skipped_results(self) -> int:
        """Give every pre-filtered row a result row carrying its skip reason.

        Skipped rows never reach a worker, but they must still appear in the
        output with an explanation rather than an unexplained blank. Idempotent:
        rows that already have a result are left alone.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO results (row_uid, decision, notes, resolved_at)
                SELECT r.row_uid, 'blank_skipped', r.skip_reason, ?
                FROM records r
                LEFT JOIN results res ON res.row_uid = r.row_uid
                WHERE r.status = 'skipped' AND res.row_uid IS NULL
                """,
                (time.time(),),
            )
        return cursor.rowcount

    def force_claimed_at(self, timestamp: float) -> None:
        """Test hook: backdate every claim so ``reclaim_stale`` can be exercised."""
        with self._tx() as conn:
            conn.execute("UPDATE records SET claimed_at = ?", (timestamp,))

    def get_result(self, row_uid: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM results WHERE row_uid = ?", (row_uid,)
            ).fetchone()

    def find_shared_urls(self) -> list[tuple[str, int]]:
        """LinkedIn URLs claimed as a match by more than one *distinct person*.

        Counting distinct ``dedup_key`` rather than distinct rows is essential:
        the same person legitimately appears on several rows (3,660 such rows in
        the production file), and those rows *should* all carry the same profile.
        Counting rows would retract every duplicated match as a false collision.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT res.linkedin_url AS url,
                       COUNT(DISTINCT COALESCE(NULLIF(r.dedup_key, ''), r.row_uid)) AS people
                FROM results res
                JOIN records r ON r.row_uid = res.row_uid
                WHERE res.decision = 'matched' AND res.linkedin_url <> ''
                GROUP BY res.linkedin_url
                HAVING people > 1
                """
            ).fetchall()
        return [(r["url"], r["people"]) for r in rows]

    def retract_shared_urls(self) -> int:
        """Blank every match whose URL is claimed by more than one person.

        Run as a final pass: a collision can only be detected once all rows are
        resolved, and the requirement is to never emit a wrong profile.
        """
        shared = self.find_shared_urls()
        if not shared:
            return 0
        urls = [u for u, _ in shared]
        placeholders = ",".join("?" * len(urls))
        with self._tx() as conn:
            cursor = conn.execute(
                f"""
                UPDATE results
                SET decision = 'blank_ambiguous',
                    notes = notes || ' | retracted: this profile was also the best '
                                  || 'match for another person in the file',
                    linkedin_url = '', confidence = 0.0
                WHERE decision = 'matched' AND linkedin_url IN ({placeholders})
                """,
                urls,
            )
        logger.warning(
            "retracted %s match(es) across %s shared profile URL(s)",
            f"{cursor.rowcount:,}", f"{len(shared):,}",
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Company cache (incl. the negative cache)
    # ------------------------------------------------------------------
    def get_company(self, company_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM company_cache WHERE company_key = ?", (company_key,)
            ).fetchone()

    def put_company(
        self, company_key: str, *, brand_name: str = "",
        has_footprint: bool | None = None, roster: Any = None,
        company_page_url: str = "", queries: int = 1,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO company_cache (
                    company_key, brand_name, has_linkedin_footprint, roster_json,
                    company_page_url, queried_at, query_count
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(company_key) DO UPDATE SET
                    brand_name=excluded.brand_name,
                    has_linkedin_footprint=excluded.has_linkedin_footprint,
                    roster_json=excluded.roster_json,
                    company_page_url=excluded.company_page_url,
                    queried_at=excluded.queried_at,
                    query_count=company_cache.query_count + excluded.query_count
                """,
                (
                    company_key, brand_name,
                    None if has_footprint is None else int(has_footprint),
                    json.dumps(roster or []), company_page_url, time.time(), queries,
                ),
            )

    # ------------------------------------------------------------------
    # Company contact routes (discovered once, reused by every executive)
    # ------------------------------------------------------------------
    def get_company_contacts(self, company_key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM company_contacts WHERE company_key = ?", (company_key,)
            ).fetchone()

    def put_company_contacts(
        self, company_key: str, *, brand_name: str = "", website: str = "",
        routes: Any = None, discovered: bool | None = None, queries: int = 1,
    ) -> None:
        """Cache one company's published contact routes.

        ``discovered=None`` means the lookup was inconclusive. It is stored as
        SQL NULL so the next pass retries rather than treating a broken search as
        proof that the company publishes nothing — the same rule that governs
        ``company_cache.has_linkedin_footprint``.
        """
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO company_contacts (
                    company_key, brand_name, website, routes_json, discovered,
                    queried_at, query_count
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(company_key) DO UPDATE SET
                    brand_name=excluded.brand_name,
                    website=excluded.website,
                    routes_json=excluded.routes_json,
                    discovered=excluded.discovered,
                    queried_at=excluded.queried_at,
                    query_count=company_contacts.query_count + excluded.query_count
                """,
                (
                    company_key, brand_name, website, json.dumps(routes or []),
                    None if discovered is None else int(discovered),
                    time.time(), queries,
                ),
            )

    def add_contact_routes(self, row_uid: str, routes: Iterable[dict[str, Any]]) -> int:
        """Attach contact routes to a row. Idempotent — re-running adds nothing.

        ``INSERT OR IGNORE`` against the ``(row_uid, route_type, value)`` unique
        index, so a retried row cannot accumulate duplicate copies of the same
        published address.
        """
        now = time.time()
        payload = [
            (row_uid, r["type"], r["value"], r.get("source_url", ""),
             r.get("label", ""), r.get("scope", "company"), int(r.get("rank", 0)), now)
            for r in routes
        ]
        if not payload:
            return 0
        with self._tx() as conn:
            cursor = conn.executemany(
                """INSERT OR IGNORE INTO contact_routes
                   (row_uid, route_type, value, source_url, label, scope, rank, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                payload,
            )
            return cursor.rowcount

    def routes_for_row(self, row_uid: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                "SELECT * FROM contact_routes WHERE row_uid = ? ORDER BY rank, id",
                (row_uid,),
            ))

    def routes_by_row(self, source_file: str = "") -> dict[str, list[sqlite3.Row]]:
        """Every row's routes, indexed for the writer's single pass over a file."""
        sql = (
            "SELECT cr.* FROM contact_routes cr "
            "JOIN records r ON r.row_uid = cr.row_uid "
        )
        params: tuple[Any, ...] = ()
        if source_file:
            sql += "WHERE r.source_file = ? "
            params = (source_file,)
        sql += "ORDER BY cr.rank, cr.id"

        index: dict[str, list[sqlite3.Row]] = {}
        with self._lock:
            for row in self._conn.execute(sql, params):
                index.setdefault(row["row_uid"], []).append(row)
        return index

    def route_type_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                row["route_type"]: row["n"] for row in self._conn.execute(
                    "SELECT route_type, COUNT(*) AS n FROM contact_routes "
                    "GROUP BY route_type ORDER BY n DESC"
                )
            }

    def rows_with_routes(self) -> int:
        return int(self.scalar(
            "SELECT COUNT(DISTINCT row_uid) FROM contact_routes"
        ) or 0)

    # ------------------------------------------------------------------
    # SERP cache
    # ------------------------------------------------------------------
    def get_serp(self, query_hash: str, ttl_seconds: float | None = None) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT response_json, fetched_at FROM serp_cache WHERE query_hash = ?",
                (query_hash,),
            ).fetchone()
        if row is None:
            return None
        if ttl_seconds is not None and (time.time() - (row["fetched_at"] or 0)) > ttl_seconds:
            return None
        try:
            return json.loads(row["response_json"])
        except json.JSONDecodeError:
            return None

    def put_serp(self, query_hash: str, query_text: str, provider: str, response: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO serp_cache (query_hash, query_text, provider, response_json, fetched_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(query_hash) DO UPDATE SET
                    response_json=excluded.response_json, fetched_at=excluded.fetched_at
                """,
                (query_hash, query_text, provider, json.dumps(response), time.time()),
            )

    # ------------------------------------------------------------------
    # Suppression (data-subject deletion)
    # ------------------------------------------------------------------
    def suppress(self, row_uid: str, note: str = "") -> None:
        """Erase a person's enrichment and tombstone the row against re-runs."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO suppressions (row_uid, suppressed_at, note) VALUES (?,?,?)",
                (row_uid, time.time(), note),
            )
            conn.execute("DELETE FROM review_candidates WHERE row_uid = ?", (row_uid,))
            conn.execute(
                """
                INSERT INTO results (row_uid, decision, notes, resolved_at)
                VALUES (?, 'suppressed', 'suppressed on request', ?)
                ON CONFLICT(row_uid) DO UPDATE SET
                    linkedin_url='', confidence=0.0, source_urls='',
                    decision='suppressed', notes='suppressed on request'
                """,
                (row_uid, time.time()),
            )
            conn.execute(
                "UPDATE records SET status='skipped', skip_reason='suppressed' WHERE row_uid = ?",
                (row_uid,),
            )

    def is_suppressed(self, row_uid: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM suppressions WHERE row_uid = ?", (row_uid,)
            ).fetchone() is not None

    # ------------------------------------------------------------------
    # Reporting / output
    # ------------------------------------------------------------------
    def counts_by(self, column: str, table: str = "records") -> dict[str, int]:
        if table not in {"records", "results"} or not column.isidentifier():
            raise ValueError("illegal identifier")
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
            ).fetchall()
        return {(r["k"] or ""): r["n"] for r in rows}

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def iter_output_rows(self, source_file: str) -> Iterator[sqlite3.Row]:
        """Every row of one input file, in original order, joined to its result."""
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT r.row_uid, r.sheet_name, r.row_index, r.skip_reason, r.status,
                       COALESCE(res.linkedin_url, '')  AS linkedin_url,
                       COALESCE(res.confidence, 0.0)   AS confidence,
                       COALESCE(res.notes, '')         AS notes,
                       COALESCE(res.source_urls, '')   AS source_urls,
                       COALESCE(res.decision, '')      AS decision
                FROM records r
                LEFT JOIN results res ON res.row_uid = r.row_uid
                WHERE r.source_file = ?
                ORDER BY r.sheet_name, r.row_index
                """,
                (source_file,),
            )
            yield from cursor

    def top_matches(self, limit: int = 1000) -> list[sqlite3.Row]:
        """The highest-confidence matches, best first — the precision deliverable."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.row_uid, r.name, r.company, r.designation, r.location,
                       r.priority,
                       res.linkedin_url, res.confidence, res.notes, res.source_urls
                FROM results res
                JOIN records r ON r.row_uid = res.row_uid
                WHERE res.decision = 'matched' AND res.linkedin_url <> ''
                ORDER BY res.confidence DESC, r.priority DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def delivered_prospects(self, limit: int = 3000) -> list[sqlite3.Row]:
        """Every row a sales team can act on, best first.

        Wider than ``top_matches``: a row with a published executive-office
        address but no confirmed LinkedIn profile is a usable lead, and excluding
        it was the single largest self-inflicted loss in earlier runs.

        Ordering: confirmed profiles first (the strongest deliverable), then by
        how direct the best contact route is, then by offline priority.
        """
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.row_uid, r.name, r.company, r.designation, r.location,
                       r.priority,
                       res.linkedin_url, res.confidence, res.notes, res.source_urls,
                       res.decision, res.route_count, res.best_route_type
                FROM results res
                JOIN records r ON r.row_uid = res.row_uid
                WHERE (res.decision = 'matched' AND res.linkedin_url <> '')
                   OR (res.decision = 'contact_route_only' AND res.route_count > 0)
                ORDER BY (res.linkedin_url <> '') DESC,
                         res.confidence DESC,
                         r.priority DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def delivered_count(self) -> int:
        """Rows carrying a profile or at least one contact route."""
        return int(self.scalar(
            "SELECT COUNT(*) FROM results "
            "WHERE (decision = 'matched' AND linkedin_url <> '') "
            "   OR (decision = 'contact_route_only' AND route_count > 0)"
        ) or 0)

    def iter_review_queue(self) -> Iterator[sqlite3.Row]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT rc.row_uid, r.name, r.company, r.location, r.designation,
                       rc.linkedin_url, rc.confidence, rc.reason, rc.evidence
                FROM review_candidates rc
                JOIN records r ON r.row_uid = rc.row_uid
                ORDER BY rc.confidence DESC
                """
            )
            yield from cursor

    def iter_rankable(self) -> Iterator[sqlite3.Row]:
        """Every record's ranking inputs, for the two-pass priority scorer."""
        with self._lock:
            cursor = self._conn.execute(
                "SELECT row_uid, name, company, designation FROM records"
            )
            yield from cursor

    def priority_summary(self, top: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                """
                SELECT name, company, designation, priority, preferred
                FROM records ORDER BY priority DESC, row_uid LIMIT ?
                """,
                (top,),
            ).fetchall()

    def source_files(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT source_file FROM records ORDER BY source_file"
            ).fetchall()
        return [r["source_file"] for r in rows]

    # ------------------------------------------------------------------
    # Validation labels
    # ------------------------------------------------------------------
    def put_label(self, row_uid: str, correct: bool, note: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO validation_labels (row_uid, correct, labelled_at, note)
                VALUES (?,?,?,?)
                ON CONFLICT(row_uid) DO UPDATE SET
                    correct=excluded.correct, labelled_at=excluded.labelled_at,
                    note=excluded.note
                """,
                (row_uid, int(bool(correct)), time.time(), note),
            )

    def label_counts(self) -> tuple[int, int]:
        """(correct, incorrect) over all stored labels."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(correct),0) AS c, COUNT(*) AS n FROM validation_labels"
            ).fetchone()
        correct = int(row["c"] or 0)
        return correct, int(row["n"] or 0) - correct

    def iter_labelled_scores(self) -> list[tuple[float, int]]:
        """(raw_score, correct) for every hand-labelled row — the calibration fit set.

        This is the join that turns hours of human labelling into an improved
        model: ``validation_labels`` supplies the verdict, ``results.top_score``
        the raw score that produced it. Both were already stored; nothing
        connected them until now.
        """
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT res.top_score AS score, v.correct AS correct
                FROM validation_labels v
                JOIN results res ON res.row_uid = v.row_uid
                WHERE res.top_score > 0
                ORDER BY res.top_score
                """
            ).fetchall()
        return [(float(r["score"]), int(r["correct"])) for r in rows]

    def unlabelled_matches(self, limit: int) -> list[sqlite3.Row]:
        """Delivered matches not yet judged, highest confidence first."""
        with self._lock:
            return self._conn.execute(
                """
                SELECT r.row_uid, r.name, r.company, r.designation, r.location,
                       res.linkedin_url, res.confidence, res.notes, res.source_urls
                FROM results res
                JOIN records r ON r.row_uid = res.row_uid
                LEFT JOIN validation_labels v ON v.row_uid = res.row_uid
                WHERE res.decision = 'matched' AND res.linkedin_url <> ''
                  AND v.row_uid IS NULL
                ORDER BY res.confidence DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def set_stat(self, run_id: str, key: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_stats (run_id, key, value) VALUES (?,?,?)",
                (run_id, key, str(value)),
            )

    def get_stats(self, run_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM run_stats WHERE run_id = ?", (run_id,)
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}
