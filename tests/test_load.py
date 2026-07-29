from __future__ import annotations

import pytest

from aerospacefunnel.load import connect, finish_run, start_run, upsert


def test_reloading_the_same_rows_updates_instead_of_duplicating(db):
    row = {"id": "abc", "name": "Falcon 9 | Starlink", "status": "TBD"}
    with connect(db) as conn:
        assert upsert(conn, "launch", [row]) == 1
        assert upsert(conn, "launch", [{**row, "status": "Launch Successful"}]) == 1

        got = conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"]
        status = conn.execute("SELECT status FROM launch WHERE id='abc'").fetchone()["status"]
    assert got == 1
    assert status == "Launch Successful"


def test_unknown_upstream_fields_are_ignored(db):
    with connect(db) as conn:
        # A new API field must not break the load.
        assert upsert(conn, "launch", [{"id": "x", "brand_new_field": "surprise"}]) == 1
        assert conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"] == 1


def test_rows_missing_part_of_the_key_are_dropped(db):
    with connect(db) as conn:
        # Without both key parts the row cannot be deduplicated, so it is skipped.
        written = upsert(
            conn,
            "flight_state",
            [
                {"icao24": "abc", "snapshot_time": None},
                {"icao24": None, "snapshot_time": 5},
                {"icao24": "def", "snapshot_time": 5},
            ],
        )
        assert written == 1
        assert conn.execute("SELECT COUNT(*) n FROM flight_state").fetchone()["n"] == 1


def test_composite_key_keeps_one_row_per_aircraft_per_snapshot(db):
    with connect(db) as conn:
        upsert(
            conn,
            "flight_state",
            [
                {"icao24": "abc", "snapshot_time": 100, "callsign": "AAA"},
                {"icao24": "abc", "snapshot_time": 200, "callsign": "AAA"},
                {"icao24": "abc", "snapshot_time": 200, "callsign": "BBB"},
            ],
        )
        rows = conn.execute(
            "SELECT snapshot_time, callsign FROM flight_state ORDER BY 1"
        ).fetchall()
    assert [(r["snapshot_time"], r["callsign"]) for r in rows] == [(100, "AAA"), (200, "BBB")]


def test_ingested_at_is_stamped(db):
    with connect(db) as conn:
        upsert(conn, "launch", [{"id": "abc"}])
        assert conn.execute("SELECT ingested_at FROM launch").fetchone()["ingested_at"]


def test_unknown_table_is_rejected(db):
    with connect(db) as conn:
        with pytest.raises(ValueError):
            upsert(conn, "launch; DROP TABLE launch", [{"id": "x"}])


def test_run_bookkeeping_records_outcome(db):
    with connect(db) as conn:
        run_id = start_run(conn, "launches")
        finish_run(conn, run_id, status="ok", pages=2, rows_read=10, rows_loaded=10)
        row = conn.execute("SELECT * FROM ingest_run WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["pages"] == 2
    assert row["finished_at"]
