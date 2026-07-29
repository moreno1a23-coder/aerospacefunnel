"""The ingest spine: extract -> bronze -> transform -> silver, with bookkeeping.

Every payload is archived to bronze before it is parsed, so a transform bug is fixed by
replaying archived bytes rather than re-fetching from a rate-limited upstream. Silver writes
go through the atomic, deduplicating partition writer, which makes re-running a load a no-op.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from . import metadata, storage
from .http import build_session
from .throttle import RateLimited, Throttle

log = logging.getLogger("aerospacefunnel.pipeline")


class Source(Protocol):
    """extract() yields raw payloads; transform() turns one payload into rows."""

    name: str
    table: str
    keys: tuple[str, ...]

    def extract(self, session) -> Any: ...
    def transform(self, payload: dict[str, Any]) -> Any: ...


@dataclass
class RunResult:
    source: str
    table: str
    status: str
    pages: int = 0
    rows_read: int = 0
    rows_written: int = 0
    bytes_raw: int = 0
    feed_used: str | None = None
    partitions: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def run(
    source: Source,
    *,
    storage_root: str | Path,
    metadata_db: str | Path,
    throttle: Throttle | None = None,
    throttle_key: str | None = None,
    layer: str = "silver",
    hourly: bool = True,
    dry_run: bool = False,
    session=None,
) -> RunResult:
    """Pull one source end to end. Upstream failure is returned as data, never raised."""
    owns_session = session is None
    session = session or build_session()
    result = RunResult(source=source.name, table=source.table, status="ok")
    now = datetime.now(UTC)

    with metadata.connect(metadata_db) as conn:
        run_id = metadata.start_run(conn, source.name, layer, source.table)
        try:
            if throttle is not None:
                # Refuse before the request rather than earning a 429 from upstream.
                throttle.acquire(throttle_key or source.name)

            for payload in source.extract(session):
                result.pages += 1

                bronze_path = storage.write_bronze(storage_root, source.name, payload, now)
                result.bytes_raw += bronze_path.stat().st_size

                rows = list(source.transform(payload))
                result.rows_read += len(rows)

                if not dry_run and rows:
                    target, total = storage.write_partition(
                        storage_root,
                        layer,
                        source.table,
                        rows,
                        ts=now,
                        keys=getattr(source, "keys", ()),
                        hourly=hourly,
                    )
                    result.rows_written += len(rows)
                    result.partitions.append(str(target))
                    metadata.record_lineage(conn, run_id, str(bronze_path), str(target))
                    log.info(
                        "%s -> %s (%d rows, partition now %d)",
                        source.name,
                        target,
                        len(rows),
                        total,
                    )

            result.feed_used = getattr(source, "feed_used", None)
            metadata.finish_run(
                conn,
                run_id,
                status="ok",
                pages=result.pages,
                rows_read=result.rows_read,
                rows_written=result.rows_written,
                bytes_raw=result.bytes_raw,
                partition=result.partitions[-1] if result.partitions else None,
                feed_used=result.feed_used,
            )

        except RateLimited as exc:
            # Not a failure: the budget did its job. Distinct status so it is not alarming.
            result.status, result.error = "throttled", str(exc)
            log.warning("%s throttled: %s", source.name, exc)
            metadata.finish_run(conn, run_id, status="throttled", error=str(exc))

        except Exception as exc:  # noqa: BLE001 - upstream failures are expected operationally
            result.status = "error"
            result.error = f"{type(exc).__name__}: {exc}"
            log.error("%s failed after %d page(s): %s", source.name, result.pages, exc)
            metadata.finish_run(
                conn,
                run_id,
                status="error",
                pages=result.pages,
                rows_read=result.rows_read,
                rows_written=result.rows_written,
                bytes_raw=result.bytes_raw,
                error=result.error,
            )
        finally:
            if owns_session:
                session.close()

    return result


def replay(
    source: Source,
    *,
    storage_root: str | Path,
    layer: str = "silver",
    hourly: bool = True,
) -> RunResult:
    """Rebuild silver from archived bronze, with no network access at all.

    This is the payoff for archiving raw payloads: a transform fix is applied to history
    without spending a single upstream request.
    """
    result = RunResult(source=source.name, table=source.table, status="ok")

    for path in storage.iter_bronze(storage_root, source.name):
        try:
            payload = storage.read_bronze(path)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("skipping unreadable bronze %s: %s", path, exc)
            continue

        result.pages += 1
        rows = list(source.transform(payload))
        result.rows_read += len(rows)
        if not rows:
            continue

        # Partition by the payload's own hour, not now, so history rebuilds in place.
        ts = _bronze_timestamp(path)
        target, _ = storage.write_partition(
            storage_root,
            layer,
            source.table,
            rows,
            ts=ts,
            keys=getattr(source, "keys", ()),
            hourly=hourly,
        )
        result.rows_written += len(rows)
        if str(target) not in result.partitions:
            result.partitions.append(str(target))

    return result


def _bronze_timestamp(path: Path) -> datetime:
    """Recover the payload's timestamp from its filename (epoch milliseconds)."""
    try:
        return datetime.fromtimestamp(int(path.stem.split(".")[0]) / 1000, tz=UTC)
    except (ValueError, OSError):
        return datetime.now(UTC)
