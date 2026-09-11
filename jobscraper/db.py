"""SQLite storage. One file, no server -- right size for a personal tracker.

Two-tier storage by design: every posting on a board gets a lightweight row so
that close-detection is accurate, but descriptions and raw payloads are only
kept for postings that match the role filter.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_TABLES = """
CREATE TABLE IF NOT EXISTS source (
    id                   INTEGER PRIMARY KEY,
    ats                  TEXT NOT NULL,
    token                TEXT NOT NULL,
    company              TEXT NOT NULL,
    config               TEXT,
    enabled              INTEGER NOT NULL DEFAULT 1,
    poll_interval_s      INTEGER NOT NULL DEFAULT 3600,
    last_polled_at       TEXT,
    last_etag            TEXT,
    last_hash            TEXT,
    last_job_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    h1b_approvals        INTEGER,
    UNIQUE (ats, token)
);

CREATE TABLE IF NOT EXISTS job (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    external_id      TEXT NOT NULL,
    url              TEXT,
    title            TEXT NOT NULL,
    department       TEXT,
    team             TEXT,
    location         TEXT,
    is_remote        INTEGER,
    is_us            INTEGER,
    employment_type  TEXT,
    salary_min       REAL,
    salary_max       REAL,
    salary_currency  TEXT,
    salary_raw       TEXT,
    description_text TEXT,
    posted_at        TEXT,
    role_category    TEXT,
    seniority        TEXT,
    min_years_exp    INTEGER,
    match_score      REAL NOT NULL DEFAULT 0,
    is_match         INTEGER NOT NULL DEFAULT 0,
    first_seen_at    TEXT NOT NULL,
    first_seen_batch TEXT,
    last_seen_at     TEXT NOT NULL,
    closed_at        TEXT,
    content_hash     TEXT,
    raw              TEXT,
    UNIQUE (source_id, external_id)
);


CREATE TABLE IF NOT EXISTS event (
    id        INTEGER PRIMARY KEY,
    job_id    INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    kind      TEXT NOT NULL,           -- new | updated | closed | reopened
    at        TEXT NOT NULL,
    detail    TEXT
);

CREATE TABLE IF NOT EXISTS poll_run (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES source(id) ON DELETE CASCADE,
    at          TEXT NOT NULL,
    status      TEXT NOT NULL,         -- ok | not_modified | unchanged | error | suspect
    listed      INTEGER DEFAULT 0,
    matched     INTEGER DEFAULT 0,
    new_jobs    INTEGER DEFAULT 0,
    closed_jobs INTEGER DEFAULT 0,
    detail      TEXT
);
"""


SCHEMA_INDEXES = """
CREATE INDEX IF NOT EXISTS job_open_match ON job (is_match, closed_at, first_seen_at DESC);
CREATE INDEX IF NOT EXISTS job_source     ON job (source_id, closed_at);
CREATE INDEX IF NOT EXISTS job_category   ON job (role_category, seniority);
CREATE INDEX IF NOT EXISTS job_posted     ON job (posted_at DESC) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS job_experience ON job (min_years_exp) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS job_batch      ON job (first_seen_batch) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS event_at ON event (at DESC, kind);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path = "jobs.db") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")   # wait, don't fail, on a busy db
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_TABLES)
    _migrate(conn)                 # add columns before indexing them
    conn.executescript(SCHEMA_INDEXES)
    _ensure_fts(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(job)")}
    for column, ddl in (("min_years_exp", "INTEGER"), ("is_us", "INTEGER"),
                        ("first_seen_batch", "TEXT")):
        if column not in have:
            conn.execute(f"ALTER TABLE job ADD COLUMN {column} {ddl}")
    have_src = {r["name"] for r in conn.execute("PRAGMA table_info(source)")}
    for column, ddl in (("h1b_approvals", "INTEGER"),):
        if column not in have_src:
            conn.execute(f"ALTER TABLE source ADD COLUMN {column} {ddl}")
    conn.commit()


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Full-text search over matched jobs. Skipped silently if FTS5 is absent."""
    try:
        conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS job_fts USING fts5(
            title, description_text, company, content='');
        """)
    except sqlite3.OperationalError:
        pass


def _norm_company(name: str) -> str:
    """Whole-word suffix stripping for blocklist identity matching."""
    import re
    drop = {"inc", "llc", "llp", "ltd", "limited", "corp", "corporation",
            "company", "co", "group", "technologies", "technology", "the",
            "us", "usa", "america", "americas", "services", "solutions"}
    words = [w for w in re.split(r"[^A-Za-z0-9]+", (name or "").lower()) if w]
    kept = [w for w in words if w not in drop]
    return "".join(kept or words)


def load_blocklist(path: str | Path = "blocklist.yaml") -> set[str]:
    try:
        import yaml
        data = yaml.safe_load(Path(path).read_text()) or {}
    except Exception:
        return set()
    return {_norm_company(n) for n in (data.get("blocked") or [])}


def is_blocked(company: str, blocked: set[str]) -> bool:
    return _norm_company(company) in blocked


def add_source(conn, ats: str, token: str, company: str, config: dict | None = None,
               poll_interval_s: int = 3600, h1b_approvals: int | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO source (ats, token, company, config, poll_interval_s, h1b_approvals)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(ats, token) DO UPDATE SET company=excluded.company,
                                                 config=excluded.config,
                                                 enabled=1,
                                                 h1b_approvals=COALESCE(excluded.h1b_approvals,
                                                                        source.h1b_approvals)
           RETURNING id""",
        (ats, token, company, json.dumps(config or {}), poll_interval_s, h1b_approvals))
    sid = cur.fetchone()[0]
    conn.commit()
    return sid


def due_sources(conn, force: bool = False) -> list[sqlite3.Row]:
    if force:
        return conn.execute("SELECT * FROM source WHERE enabled=1 ORDER BY id").fetchall()
    return conn.execute(
        """SELECT * FROM source
           WHERE enabled = 1
             AND (last_polled_at IS NULL
                  OR (julianday('now') - julianday(last_polled_at)) * 86400 >= poll_interval_s)
           ORDER BY last_polled_at IS NOT NULL, last_polled_at""").fetchall()


def open_jobs(conn, source_id: int) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM job WHERE source_id=? AND closed_at IS NULL", (source_id,)).fetchall()
    return {r["external_id"]: r for r in rows}


def record_event(conn, job_id: int, kind: str, detail: str | None = None) -> None:
    conn.execute("INSERT INTO event (job_id, kind, at, detail) VALUES (?,?,?,?)",
                 (job_id, kind, now(), detail))


def index_fts(conn, job_id: int, title: str, description: str | None, company: str) -> None:
    try:
        conn.execute("INSERT INTO job_fts (rowid, title, description_text, company) VALUES (?,?,?,?)",
                     (job_id, title, description or "", company))
    except sqlite3.OperationalError:
        pass
