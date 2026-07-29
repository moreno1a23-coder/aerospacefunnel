"""Operational metadata: run bookkeeping, lineage and data-quality results.

Deliberately SQLite, not the DuckDB warehouse. These are tiny rows written frequently and
transactionally, one per pipeline step; the columnar store is for analytical volume and would
be the wrong tool for a hot metadata path. This file supersedes the original ``load.py``,
whose SQLite fact tables are replaced by the Parquet layers in :mod:`storage`.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_run (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    layer        TEXT NOT NULL,
    target       TEXT,
    partition    TEXT,
    feed_used    TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    pages        INTEGER NOT NULL DEFAULT 0,
    rows_read    INTEGER NOT NULL DEFAULT 0,
    rows_written INTEGER NOT NULL DEFAULT 0,
    bytes_raw    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_source ON pipeline_run (source, started_at DESC);

-- Which bronze payloads produced which silver partition.
CREATE TABLE IF NOT EXISTS lineage (
    run_id      INTEGER NOT NULL,
    bronze_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    PRIMARY KEY (run_id, bronze_path),
    FOREIGN KEY (run_id) REFERENCES pipeline_run (id)
);

CREATE TABLE IF NOT EXISTS dq_result (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER,
    table_name  TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    passed      INTEGER NOT NULL,
    observed    TEXT,
    expected    TEXT,
    severity    TEXT NOT NULL DEFAULT 'error',
    checked_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dq_table ON dq_result (table_name, checked_at DESC);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_run(conn: sqlite3.Connection, source: str, layer: str, target: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO pipeline_run (source, layer, target, started_at, status) "
        "VALUES (?,?,?,?,'running')",
        (source, layer, target, utcnow()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    pages: int = 0,
    rows_read: int = 0,
    rows_written: int = 0,
    bytes_raw: int = 0,
    partition: str | None = None,
    feed_used: str | None = None,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE pipeline_run SET finished_at=?, status=?, pages=?, rows_read=?, "
        "rows_written=?, bytes_raw=?, partition=?, feed_used=?, error=? WHERE id=?",
        (
            utcnow(),
            status,
            pages,
            rows_read,
            rows_written,
            bytes_raw,
            partition,
            feed_used,
            error,
            run_id,
        ),
    )
    conn.commit()


def record_lineage(conn: sqlite3.Connection, run_id: int, bronze: str, target: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lineage (run_id, bronze_path, target_path) VALUES (?,?,?)",
        (run_id, bronze, target),
    )


def record_dq(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    table_name: str,
    check_name: str,
    passed: bool,
    observed: str,
    expected: str,
    severity: str = "error",
) -> None:
    conn.execute(
        "INSERT INTO dq_result (run_id, table_name, check_name, passed, observed, expected, "
        "severity, checked_at) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, table_name, check_name, int(passed), observed, expected, severity, utcnow()),
    )
    conn.commit()
