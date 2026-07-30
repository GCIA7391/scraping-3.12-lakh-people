"""SQLite schema for pipeline state.

SQLite (not Parquet or JSONL) because the pipeline's defining requirement is
*exact, crash-safe resume* across a run that may span days on a free search
provider. That needs atomic read-modify-write on individual rows from several
concurrent workers, which a flat file cannot give.

WAL mode lets readers (progress/dashboard) run concurrently with writers.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

PRAGMAS = (
    "PRAGMA journal_mode=WAL",       # concurrent readers during long runs
    "PRAGMA synchronous=NORMAL",     # durable enough with WAL; far faster than FULL
    "PRAGMA foreign_keys=ON",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-64000",      # 64 MB page cache
    "PRAGMA busy_timeout=30000",     # wait rather than raise under worker contention
)

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per input row. The input side is immutable once loaded.
CREATE TABLE IF NOT EXISTS records (
    row_uid      TEXT PRIMARY KEY,
    source_file  TEXT NOT NULL,
    sheet_name   TEXT NOT NULL DEFAULT '',
    row_index    INTEGER NOT NULL,

    name         TEXT NOT NULL DEFAULT '',
    company      TEXT NOT NULL DEFAULT '',
    designation  TEXT NOT NULL DEFAULT '',
    location     TEXT NOT NULL DEFAULT '',
    industry     TEXT NOT NULL DEFAULT '',
    pc_url       TEXT NOT NULL DEFAULT '',

    name_norm    TEXT NOT NULL DEFAULT '',
    company_key  TEXT NOT NULL DEFAULT '',
    dedup_key    TEXT NOT NULL DEFAULT '',
    -- Points at the canonical row_uid when this row is an exact repeat.
    dup_of       TEXT,

    -- pending  : waiting to be claimed
    -- claimed  : a worker holds it right now
    -- done     : terminal, a result row exists
    -- skipped  : rejected by Tier 0, never searched
    -- deferred : transient failure; retried on the next pass while attempts remain
    -- error    : legacy/unexpected failure (requeued by `main.py retry --reset`)
    status       TEXT NOT NULL DEFAULT 'pending',
    skip_reason  TEXT NOT NULL DEFAULT '',
    tier         INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    claimed_at   REAL,
    updated_at   REAL
);

-- Partial index: the hot query is "give me pending work", and the pending set
-- shrinks to nothing as the run completes, so indexing only pending rows keeps
-- the claim query fast at 312k scale.
CREATE INDEX IF NOT EXISTS idx_records_pending
    ON records(company_key) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_records_status  ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_company ON records(company_key);
CREATE INDEX IF NOT EXISTS idx_records_dedup   ON records(dedup_key);
CREATE INDEX IF NOT EXISTS idx_records_source  ON records(source_file, sheet_name, row_index);

-- The decision for each row. Absence of a row here means "not yet decided".
CREATE TABLE IF NOT EXISTS results (
    row_uid         TEXT PRIMARY KEY REFERENCES records(row_uid) ON DELETE CASCADE,
    linkedin_url    TEXT NOT NULL DEFAULT '',
    confidence      REAL NOT NULL DEFAULT 0.0,
    notes           TEXT NOT NULL DEFAULT '',
    source_urls     TEXT NOT NULL DEFAULT '',
    -- matched | blank_low_confidence | blank_ambiguous | blank_no_candidate
    -- | blank_skipped | blank_error | suppressed
    decision        TEXT NOT NULL DEFAULT '',
    top_score       REAL NOT NULL DEFAULT 0.0,
    runner_up_score REAL NOT NULL DEFAULT 0.0,
    margin          REAL NOT NULL DEFAULT 0.0,
    provider        TEXT NOT NULL DEFAULT '',
    queries_used    INTEGER NOT NULL DEFAULT 0,
    resolved_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_results_decision ON results(decision);
CREATE INDEX IF NOT EXISTS idx_results_url      ON results(linkedin_url)
    WHERE linkedin_url <> '';

-- Sub-threshold candidates, kept for human review. These are NEVER promoted into
-- results.linkedin_url — they exist so the search spend is not wasted.
CREATE TABLE IF NOT EXISTS review_candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    row_uid       TEXT NOT NULL REFERENCES records(row_uid) ON DELETE CASCADE,
    linkedin_url  TEXT NOT NULL,
    confidence    REAL NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    evidence      TEXT NOT NULL DEFAULT '',
    created_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_review_row ON review_candidates(row_uid);

-- Company-level cache. The single biggest saving in the pipeline: 312,160 people
-- belong to only 140,351 companies, and a company with no LinkedIn footprint
-- disqualifies every one of its people without further queries.
CREATE TABLE IF NOT EXISTS company_cache (
    company_key            TEXT PRIMARY KEY,
    brand_name             TEXT NOT NULL DEFAULT '',
    -- 1 = at least one profile carries this company's tokens
    -- 0 = searched, nothing found  => negative cache, skip all its people
    has_linkedin_footprint INTEGER,
    roster_json            TEXT NOT NULL DEFAULT '[]',
    company_page_url       TEXT NOT NULL DEFAULT '',
    queried_at             REAL,
    query_count            INTEGER NOT NULL DEFAULT 0
);

-- Raw provider responses, so re-scoring after a tuning change costs nothing.
CREATE TABLE IF NOT EXISTS serp_cache (
    query_hash    TEXT PRIMARY KEY,
    query_text    TEXT NOT NULL,
    provider      TEXT NOT NULL,
    response_json TEXT NOT NULL,
    fetched_at    REAL
);

CREATE INDEX IF NOT EXISTS idx_serp_fetched ON serp_cache(fetched_at);

-- Rows explicitly suppressed (data-subject deletion requests). Tombstoned so a
-- later re-run cannot resurrect them.
CREATE TABLE IF NOT EXISTS suppressions (
    row_uid      TEXT PRIMARY KEY,
    suppressed_at REAL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS run_stats (
    run_id TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, key)
);
"""
