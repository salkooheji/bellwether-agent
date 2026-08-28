"""The agent's own state database.

This is a separate SQLite file owned by bellwether-agent, never the
portfolio-lens database, which this project opens strictly read-only.

Two tables:
  runs               one row per execution: the audit trail of when the
                     agent ran, what it examined, and how it ended
  reported_findings  one row per finding ever reported, keyed by a
                     deterministic fingerprint, so the agent does not
                     raise the same finding twice across runs
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    trigger          TEXT NOT NULL,
    quarter_examined TEXT,
    findings_total   INTEGER,
    findings_new     INTEGER,
    memos_produced   INTEGER,
    status           TEXT NOT NULL DEFAULT 'running',
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS reported_findings (
    fingerprint  TEXT PRIMARY KEY,
    finding_type TEXT NOT NULL,
    cik          INTEGER,
    cusip        TEXT,
    period       TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    reported_at  TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(state_db_path: Path) -> sqlite3.Connection:
    """Open the state database, creating the file and schema if needed."""
    state_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def start_run(conn: sqlite3.Connection, trigger: str) -> int:
    """Record the start of a run. trigger is 'manual', 'scheduled', or 'eval'."""
    cur = conn.execute(
        "INSERT INTO runs (started_at, trigger) VALUES (?, ?)",
        (_now(), trigger),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    quarter_examined: str | None,
    findings_total: int,
    findings_new: int,
    memos_produced: int,
    notes: str = "",
) -> None:
    """Close out a run. status is 'ok', 'quiet', or 'error'."""
    conn.execute(
        "UPDATE runs SET finished_at = ?, status = ?, quarter_examined = ?, "
        "findings_total = ?, findings_new = ?, memos_produced = ?, notes = ? "
        "WHERE run_id = ?",
        (_now(), status, quarter_examined, findings_total,
         findings_new, memos_produced, notes, run_id),
    )
    conn.commit()


def last_examined_quarter(conn: sqlite3.Connection) -> str | None:
    """The most recent quarter any successful run has examined."""
    row = conn.execute(
        "SELECT MAX(quarter_examined) AS q FROM runs "
        "WHERE status IN ('ok', 'quiet')"
    ).fetchone()
    return row["q"]


def make_fingerprint(finding_type: str, cik: int | None,
                     cusip: str | None, period: str) -> str:
    """Deterministic identity of a finding, readable on purpose."""
    return f"{finding_type}:{cik if cik is not None else 'multi'}:" \
           f"{cusip if cusip is not None else 'portfolio'}:{period}"


def is_reported(conn: sqlite3.Connection, fingerprint: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM reported_findings WHERE fingerprint = ?",
        (fingerprint,),
    ).fetchone()
    return row is not None


def mark_reported(
    conn: sqlite3.Connection,
    fingerprint: str,
    finding_type: str,
    cik: int | None,
    cusip: str | None,
    period: str,
    run_id: int,
) -> None:
    """Remember a finding. INSERT OR IGNORE makes repeat calls harmless."""
    conn.execute(
        "INSERT OR IGNORE INTO reported_findings "
        "(fingerprint, finding_type, cik, cusip, period, run_id, reported_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fingerprint, finding_type, cik, cusip, period, run_id, _now()),
    )
    conn.commit()
