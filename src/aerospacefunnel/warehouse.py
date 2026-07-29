"""DuckDB warehouse: views over the Parquet layers, plus the analytics marts.

The warehouse holds no data of its own. Every table is a view over Parquet files, so the
database file is disposable and can be rebuilt from the layers at any time - which keeps
Parquet, not DuckDB, as the system of record.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

from . import marts, storage
from .sources import TABLE_LAYERS


@contextmanager
def connect(db_path: str | Path, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path), read_only=read_only)
    try:
        yield conn
    finally:
        conn.close()


def build(conn, storage_root: str | Path) -> dict[str, list[str]]:
    """Attach a view per populated table, then create every mart whose deps are met."""
    tables = storage.attach_views(conn, storage_root, TABLE_LAYERS)
    created, skipped = marts.refresh(conn, set(tables))
    return {"tables": tables, "marts": created, "skipped": skipped}


def refresh(storage_root: str | Path, db_path: str | Path) -> dict[str, list[str]]:
    with connect(db_path) as conn:
        return build(conn, storage_root)
