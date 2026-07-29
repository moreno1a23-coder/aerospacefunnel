"""Layered storage: immutable bronze payloads, idempotent Parquet partitions, DuckDB views.

Layout::

    data/
      bronze/<source>/dt=YYYY-MM-DD/hh=HH/<epoch_ms>.json.gz   immutable, replayable
      silver/<table>/dt=YYYY-MM-DD/hh=HH/data.parquet          typed, deduplicated
      gold/<table>/dt=YYYY-MM-DD/data.parquet                  conformed facts/dims

Bronze uses gzip rather than zstd so the format is readable on any supported Python with no
extra dependency (stdlib `compression.zstd` only arrived in 3.14). Measured on a real ADS-B
payload: gzip 6.5x, zstd 8.2x - the extra ratio is not worth a dependency here, and Parquet
below already compresses with zstd where the volume actually is.

Partition writes are atomic: a partition is a single ``data.parquet`` written to a temp file,
fsynced, then ``os.replace``d into place. ``os.replace`` is atomic on POSIX, so a crash
mid-write leaves the previous partition intact rather than truncated.
"""

from __future__ import annotations

import gzip
import json
import os
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

PARQUET_COMPRESSION = "zstd"

# Derived from the directory path by Arrow's hive partitioning, never stored in the file.
PARTITION_COLUMNS = frozenset({"dt", "hh"})


def _partition_dir(root: Path, layer: str, name: str, ts: datetime, hourly: bool) -> Path:
    parts = [root, layer, name, f"dt={ts:%Y-%m-%d}"]
    if hourly:
        parts.append(f"hh={ts:%H}")
    return Path(*parts)


# --------------------------------------------------------------------------- bronze


def write_bronze(root: str | Path, source: str, payload: Any, ts: datetime | None = None) -> Path:
    """Persist a payload exactly as received. Returns the file written."""
    ts = ts or datetime.now(UTC)
    directory = _partition_dir(Path(root), "bronze", source, ts, hourly=True)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{int(ts.timestamp() * 1000)}.json.gz"
    # Fixed mtime keeps the gzip bytes deterministic, so replaying the same payload
    # produces an identical file instead of a spurious diff.
    with gzip.GzipFile(filename=str(target), mode="wb", mtime=0) as fh:
        fh.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return target


def read_bronze(path: str | Path) -> Any:
    with gzip.open(path, "rb") as fh:
        return json.loads(fh.read().decode())


def iter_bronze(root: str | Path, source: str) -> Iterator[Path]:
    """Every archived payload for a source, oldest first."""
    base = Path(root) / "bronze" / source
    if not base.exists():
        return
    yield from sorted(base.rglob("*.json.gz"))


# --------------------------------------------------------------------- silver / gold


def _dedupe(rows: Sequence[dict[str, Any]], keys: Sequence[str]) -> list[dict[str, Any]]:
    """Keep the last row per key tuple. Rows missing any key part are dropped."""
    if not keys:
        return list(rows)
    seen: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if any(row.get(k) is None for k in keys):
            continue
        seen[tuple(row[k] for k in keys)] = row
    return list(seen.values())


def _align(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give every row the same key set so Arrow infers one consistent schema."""
    columns: set[str] = set()
    for row in rows:
        columns.update(row)
    return [{c: row.get(c) for c in sorted(columns)} for row in rows]


def write_partition(
    root: str | Path,
    layer: str,
    table: str,
    rows: Iterable[dict[str, Any]],
    *,
    ts: datetime,
    keys: Sequence[str] = (),
    hourly: bool = True,
) -> tuple[Path, int]:
    """Merge rows into one partition atomically. Returns (path, total rows in partition).

    Re-running the same load is a no-op: existing rows are read back, merged, deduplicated
    on `keys`, and the partition is rewritten wholesale. Partitions stay small enough
    (an hour of one hub is tens of thousands of rows) that rewriting beats append-and-compact.
    """
    rows = list(rows)
    directory = _partition_dir(Path(root), layer, table, ts, hourly)
    target = directory / "data.parquet"

    existing: list[dict[str, Any]] = []
    if target.exists():
        # Reading a file under dt=/hh= directories makes Arrow materialise those as columns.
        # They are path metadata, not data - keeping them would bake a redundant copy into
        # the next write and collide with the inferred columns on the read after that.
        existing = [
            {k: v for k, v in row.items() if k not in PARTITION_COLUMNS}
            for row in pq.read_table(target).to_pylist()
        ]

    merged = _dedupe([*existing, *rows], keys)
    if not merged:
        return target, 0

    directory.mkdir(parents=True, exist_ok=True)
    table_arrow = pa.Table.from_pylist(_align(merged))

    tmp = directory / f".data.parquet.tmp.{os.getpid()}"
    try:
        pq.write_table(table_arrow, tmp, compression=PARQUET_COMPRESSION)
        # fsync before the rename, so the rename cannot expose a partially-written file.
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    return target, len(merged)


def read_table(root: str | Path, layer: str, table: str) -> list[dict[str, Any]]:
    """Read every partition of a table. Convenience for tests and small tables."""
    base = Path(root) / layer / table
    files = sorted(base.rglob("data.parquet")) if base.exists() else []
    if not files:
        return []
    return pq.read_table(files).to_pylist()


# ------------------------------------------------------------------------- warehouse


def glob_for(root: str | Path, layer: str, table: str) -> str:
    return str(Path(root) / layer / table / "**" / "data.parquet")


def attach_views(conn, root: str | Path, tables: dict[str, str]) -> list[str]:
    """Create a DuckDB view per table over its Parquet partitions.

    `tables` maps table name -> layer. Tables with no data yet are skipped rather than
    creating a view that errors on every query.
    """
    created = []
    for table, layer in tables.items():
        pattern = glob_for(root, layer, table)
        if not list(Path(root).joinpath(layer, table).rglob("data.parquet")):
            continue
        conn.execute(
            f"CREATE OR REPLACE VIEW {table} AS "
            f"SELECT * FROM read_parquet('{pattern}', union_by_name=true)"
        )
        created.append(table)
    return created
