"""SQLite warehouse: schema, idempotent upserts, and run bookkeeping.

Loads are idempotent by design — re-running the same pull updates rows in place
rather than duplicating them, so a cron that overlaps itself is harmless.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS launch (
    id            TEXT PRIMARY KEY,
    slug          TEXT,
    name          TEXT,
    status        TEXT,
    status_abbrev TEXT,
    net           TEXT,
    window_start  TEXT,
    window_end    TEXT,
    provider      TEXT,
    mission       TEXT,
    mission_type  TEXT,
    pad           TEXT,
    location      TEXT,
    orbit         TEXT,
    launcher      TEXT,
    image_url     TEXT,
    last_updated  TEXT,
    ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_launch_net ON launch (net DESC);
CREATE INDEX IF NOT EXISTS idx_launch_provider ON launch (provider);

CREATE TABLE IF NOT EXISTS flight_state (
    icao24          TEXT NOT NULL,
    snapshot_time   INTEGER NOT NULL,
    callsign        TEXT,
    origin_country  TEXT,
    time_position   INTEGER,
    last_contact    INTEGER,
    longitude       REAL,
    latitude        REAL,
    baro_altitude   REAL,
    geo_altitude    REAL,
    on_ground       INTEGER,
    velocity        REAL,
    true_track      REAL,
    vertical_rate   REAL,
    squawk          TEXT,
    spi             INTEGER,
    position_source INTEGER,
    sensor_count    INTEGER,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (icao24, snapshot_time)
);
CREATE INDEX IF NOT EXISTS idx_flight_snapshot ON flight_state (snapshot_time DESC);
CREATE INDEX IF NOT EXISTS idx_flight_country ON flight_state (origin_country);

CREATE TABLE IF NOT EXISTS ingest_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    pages       INTEGER NOT NULL DEFAULT 0,
    rows_read   INTEGER NOT NULL DEFAULT 0,
    rows_loaded INTEGER NOT NULL DEFAULT 0,
    error       TEXT
);
"""

# Natural keys, used to build the ON CONFLICT clause per table.
KEYS = {
    "launch": ("id",),
    "flight_state": ("icao24", "snapshot_time"),
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open the warehouse, creating it and its schema if absent."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]


def upsert(conn: sqlite3.Connection, table: str, rows: Iterable[dict[str, Any]]) -> int:
    """Insert rows, updating on primary-key conflict. Returns the number written.

    Row keys are intersected with the real table columns before they reach SQL, so an
    upstream API that adds a field doesn't break the load (and can't inject SQL).
    """
    if table not in KEYS:
        raise ValueError(f"unknown table: {table}")

    valid = _columns(conn, table)
    keys = KEYS[table]
    stamp = utcnow()
    written = 0

    for row in rows:
        record = {k: v for k, v in row.items() if k in valid}
        record["ingested_at"] = stamp

        # A row missing part of its key can't be deduplicated, so it is dropped rather
        # than silently accumulating duplicates on every run.
        if any(record.get(k) is None for k in keys):
            continue

        cols = list(record)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
        sql = (
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
        )
        conn.execute(sql, [record[c] for c in cols])
        written += 1

    return written


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_run (source, started_at, status) VALUES (?, ?, 'running')",
        (source, utcnow()),
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
    rows_loaded: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        "UPDATE ingest_run SET finished_at=?, status=?, pages=?, rows_read=?, "
        "rows_loaded=?, error=? WHERE id=?",
        (utcnow(), status, pages, rows_read, rows_loaded, error, run_id),
    )
    conn.commit()
