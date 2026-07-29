from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from aerospacefunnel import storage

TS = datetime(2026, 7, 29, 14, 30, tzinfo=UTC)


def test_bronze_roundtrip(storage_root):
    path = storage.write_bronze(storage_root, "adsb", {"ac": [{"hex": "abc"}], "now": 1}, TS)
    assert storage.read_bronze(path)["now"] == 1
    assert "dt=2026-07-29" in str(path) and "hh=14" in str(path)


def test_bronze_bytes_are_deterministic(storage_root):
    """Replaying an identical payload must not produce a spurious diff."""
    a = storage.write_bronze(storage_root, "s", {"b": 2, "a": 1}, TS).read_bytes()
    b = storage.write_bronze(storage_root, "s", {"a": 1, "b": 2}, TS).read_bytes()
    assert a == b


def test_write_is_idempotent(storage_root):
    rows = [{"hex": "abc", "ts": 1}, {"hex": "def", "ts": 1}]
    _, first = storage.write_partition(storage_root, "silver", "t", rows, ts=TS, keys=["hex", "ts"])
    _, second = storage.write_partition(
        storage_root, "silver", "t", rows, ts=TS, keys=["hex", "ts"]
    )
    assert first == second == 2


def test_conflicting_key_updates_in_place(storage_root):
    storage.write_partition(
        storage_root,
        "silver",
        "t",
        [{"hex": "abc", "ts": 1, "alt": 100}],
        ts=TS,
        keys=["hex", "ts"],
    )
    storage.write_partition(
        storage_root,
        "silver",
        "t",
        [{"hex": "abc", "ts": 1, "alt": 999}],
        ts=TS,
        keys=["hex", "ts"],
    )
    rows = storage.read_table(storage_root, "silver", "t")
    assert len(rows) == 1
    assert rows[0]["alt"] == 999


def test_rows_missing_key_parts_are_dropped(storage_root):
    _, total = storage.write_partition(
        storage_root,
        "silver",
        "t",
        [{"hex": "abc", "ts": None}, {"hex": None, "ts": 1}, {"hex": "ok", "ts": 1}],
        ts=TS,
        keys=["hex", "ts"],
    )
    assert total == 1


def test_partition_columns_are_not_stored_in_the_file(storage_root):
    """dt/hh come from the directory path; storing them too would duplicate and collide."""
    for i in range(3):
        storage.write_partition(
            storage_root, "silver", "t", [{"hex": f"h{i}", "ts": i}], ts=TS, keys=["hex", "ts"]
        )
    file = next((Path(storage_root) / "silver" / "t").rglob("data.parquet"))
    assert set(pq.read_table(file).schema.names) == {"hex", "ts"}
    # ...but they are still available when reading through the hive-partitioned path.
    assert "dt" in storage.read_table(storage_root, "silver", "t")[0]


def test_new_columns_appearing_later_do_not_break_the_merge(storage_root):
    storage.write_partition(
        storage_root, "silver", "t", [{"hex": "a", "ts": 1}], ts=TS, keys=["hex", "ts"]
    )
    storage.write_partition(
        storage_root,
        "silver",
        "t",
        [{"hex": "b", "ts": 2, "squawk": "7700"}],
        ts=TS,
        keys=["hex", "ts"],
    )
    rows = storage.read_table(storage_root, "silver", "t")
    assert len(rows) == 2
    assert all("squawk" in r for r in rows)


def test_no_temp_files_are_left_behind(storage_root):
    storage.write_partition(
        storage_root, "silver", "t", [{"hex": "a", "ts": 1}], ts=TS, keys=["hex", "ts"]
    )
    assert not list((Path(storage_root) / "silver" / "t").rglob(".data.parquet.tmp*"))


def test_empty_rows_write_nothing(storage_root):
    _, total = storage.write_partition(storage_root, "silver", "t", [], ts=TS, keys=["hex"])
    assert total == 0
    assert storage.read_table(storage_root, "silver", "t") == []


def test_reading_a_missing_table_is_empty_not_an_error(storage_root):
    assert storage.read_table(storage_root, "silver", "nope") == []


@pytest.mark.parametrize("hourly,expected", [(True, "hh="), (False, "dt=")])
def test_partition_granularity(storage_root, hourly, expected):
    path, _ = storage.write_partition(
        storage_root, "silver", "t", [{"hex": "a", "ts": 1}], ts=TS, keys=["hex"], hourly=hourly
    )
    assert expected in str(path)
    if not hourly:
        assert "hh=" not in str(path)
