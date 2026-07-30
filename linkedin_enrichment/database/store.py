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
        self._conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

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
                    updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                payload,
            )
            return cursor.rowcount

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

    def claim_batch(self, limit: int) -> list[sqlite3.Row]:
        """Atomically claim up to ``limit`` pending rows.

        Rows are ordered by ``company_key`` so a batch tends to contain people
        from the same company. That maximises company-cache hits within the batch
        and keeps the Tier-1 roster query shared rather than repeated.
        """
        now = time.time()
        with self._tx() as conn:
            rows = conn.execute(
                """
                SELECT row_uid FROM records
                WHERE status = 'pending'
                ORDER BY company_key
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
                    top_score, runner_up_score, margin, provider, queries_used, resolved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(row_uid) DO UPDATE SET
                    linkedin_url=excluded.linkedin_url, confidence=excluded.confidence,
                    notes=excluded.notes, source_urls=excluded.source_urls,
                    decision=excluded.decision, top_score=excluded.top_score,
                    runner_up_score=excluded.runner_up_score, margin=excluded.margin,
                    provider=excluded.provider, queries_used=excluded.queries_used,
                    resolved_at=excluded.resolved_at
                """,
                (
                    row_uid,
                    fields.get("linkedin_url", ""), float(fields.get("confidence", 0.0)),
                    fields.get("notes", ""), fields.get("source_urls", ""),
                    fields.get("decision", ""), float(fields.get("top_score", 0.0)),
                    float(fields.get("runner_up_score", 0.0)), float(fields.get("margin", 0.0)),
                    fields.get("provider", ""), int(fields.get("queries_used", 0)), now,
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

    def source_files(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT source_file FROM records ORDER BY source_file"
            ).fetchall()
        return [r["source_file"] for r in rows]

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
