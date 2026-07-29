"""The funnel itself: extract -> transform -> load, with per-run bookkeeping."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .http import build_session
from .load import connect, finish_run, start_run, upsert
from .sources.base import Source

log = logging.getLogger("aerospacefunnel")


@dataclass
class RunResult:
    source: str
    pages: int
    rows_read: int
    rows_loaded: int
    status: str
    error: str | None = None


def _archive(raw_dir: Path, source: str, page: int, payload: dict[str, Any]) -> None:
    """Keep the untouched payload so a transform bug can be replayed, not re-fetched."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{source}-{page:04d}.json"
    target.write_text(json.dumps(payload), encoding="utf-8")


def run(
    source: Source,
    db_path: str | Path,
    *,
    raw_dir: str | Path | None = None,
    dry_run: bool = False,
) -> RunResult:
    """Pull one source end-to-end. Returns counts; never raises on upstream failure."""
    session = build_session()
    pages = rows_read = rows_loaded = 0

    with connect(db_path) as conn:
        run_id = start_run(conn, source.name)
        try:
            for payload in source.extract(session):
                pages += 1
                if raw_dir:
                    _archive(Path(raw_dir), source.name, pages, payload)

                rows = list(source.transform(payload))
                rows_read += len(rows)
                if not dry_run:
                    rows_loaded += upsert(conn, source.table, rows)
                log.info("%s page %d: %d rows", source.name, pages, len(rows))

            conn.commit()
            finish_run(
                conn,
                run_id,
                status="ok",
                pages=pages,
                rows_read=rows_read,
                rows_loaded=rows_loaded,
            )
            return RunResult(source.name, pages, rows_read, rows_loaded, "ok")

        except Exception as exc:  # network, JSON, or schema drift
            # Bookkeeping must survive the failure, so the partial counts are recorded
            # and the exception is returned as data rather than propagated.
            conn.rollback()
            log.error("%s failed after %d pages: %s", source.name, pages, exc)
            finish_run(
                conn,
                run_id,
                status="error",
                pages=pages,
                rows_read=rows_read,
                rows_loaded=rows_loaded,
                error=f"{type(exc).__name__}: {exc}",
            )
            return RunResult(source.name, pages, rows_read, rows_loaded, "error", str(exc))
        finally:
            session.close()
