"""Token bucket that survives process exit.

Launch Library 2's limit is 15 requests/hour *per IP*. An in-process limiter cannot enforce
that, because a cron or a repeated CLI invocation starts with a fresh allowance every time
and walks straight into a 429. The bucket therefore lives in SQLite alongside the other
operational metadata, and is checked before the request rather than after the rejection.

Operational metadata (throttle state, run bookkeeping, quality results) stays in SQLite:
tiny rows, frequent small transactional writes. Analytical volume goes to Parquet/DuckDB.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS rate_budget (
    source      TEXT PRIMARY KEY,
    capacity    REAL NOT NULL,
    refill_per_s REAL NOT NULL,
    tokens      REAL NOT NULL,
    updated_at  REAL NOT NULL
);
"""

# Published limits, converted to a per-second refill. Keyless community feeds get a
# politeness budget rather than a published one - they are donated infrastructure.
LIMITS: dict[str, tuple[float, float]] = {
    # source: (capacity, refill_tokens_per_second)
    "launchlibrary": (15, 15 / 3600),  # VERIFIED: 15/hr per IP
    "launchlibrary_dev": (10_000, 100),  # dev mirror has no documented limit
    "opensky_anon": (400, 400 / 86400),  # VERIFIED: 400 credits/day
    "opensky_auth": (4000, 4000 / 86400),  # VERIFIED: 4,000 credits/day
    "nasa_demo": (10, 10 / 3600),  # measured x-ratelimit-limit: 10
    "nasa_key": (1000, 1000 / 3600),  # VERIFIED: 1,000/hr
    "spacetrack": (300, 300 / 3600),  # VERIFIED: 300/hr (also 30/min)
    "adsb": (120, 1.0),  # politeness budget: ~1 req/s sustained
    "aviationweather": (600, 2.0),  # politeness budget
    "faa": (120, 0.5),  # politeness budget
    "default": (60, 1.0),
}


class RateLimited(Exception):
    """Raised instead of making a request that would exceed the budget."""

    def __init__(self, source: str, wait_seconds: float):
        self.source = source
        self.wait_seconds = wait_seconds
        super().__init__(f"{source}: budget exhausted, retry in {wait_seconds:.0f}s")


@dataclass
class Throttle:
    """Persistent token bucket keyed by source."""

    db_path: str | Path

    def _connect(self) -> sqlite3.Connection:
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        return conn

    def acquire(self, source: str, cost: float = 1.0, *, now: float | None = None) -> None:
        """Spend `cost` tokens or raise RateLimited. Never sleeps - the caller decides."""
        capacity, refill = LIMITS.get(source, LIMITS["default"])
        now = time.time() if now is None else now

        conn = self._connect()
        try:
            # IMMEDIATE takes the write lock up front so two concurrent pollers cannot both
            # read the same token count and each decide they may spend it.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT tokens, updated_at FROM rate_budget WHERE source=?", (source,)
            ).fetchone()

            if row is None:
                tokens = capacity
            else:
                elapsed = max(0.0, now - row["updated_at"])
                tokens = min(capacity, row["tokens"] + elapsed * refill)

            if tokens < cost:
                conn.rollback()
                raise RateLimited(source, (cost - tokens) / refill if refill > 0 else 3600)

            conn.execute(
                "INSERT INTO rate_budget (source, capacity, refill_per_s, tokens, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
                "tokens=excluded.tokens, updated_at=excluded.updated_at, "
                "capacity=excluded.capacity, refill_per_s=excluded.refill_per_s",
                (source, capacity, refill, tokens - cost, now),
            )
            conn.commit()
        finally:
            conn.close()

    def remaining(self, source: str, *, now: float | None = None) -> float:
        """Tokens available right now, without spending any."""
        capacity, refill = LIMITS.get(source, LIMITS["default"])
        now = time.time() if now is None else now
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT tokens, updated_at FROM rate_budget WHERE source=?", (source,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return capacity
        return min(capacity, row["tokens"] + max(0.0, now - row["updated_at"]) * refill)
