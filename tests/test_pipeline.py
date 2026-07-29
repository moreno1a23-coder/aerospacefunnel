"""End-to-end tests with a stand-in source, so nothing here touches the network."""

from __future__ import annotations

from aerospacefunnel.load import connect
from aerospacefunnel.pipeline import run
from aerospacefunnel.sources.launches import LaunchesSource


class FakeSource:
    name = "launches"
    table = "launch"

    def __init__(self, payloads, boom: Exception | None = None):
        self.payloads = payloads
        self.boom = boom

    def extract(self, session):
        yield from self.payloads
        if self.boom:
            raise self.boom

    def transform(self, payload):
        return LaunchesSource().transform(payload)


def test_full_funnel_writes_rows_and_records_the_run(db, launch_payload):
    result = run(FakeSource([launch_payload]), db)

    assert result.status == "ok"
    assert result.pages == 1
    assert result.rows_read == result.rows_loaded > 0

    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"] == result.rows_loaded
        run_row = conn.execute("SELECT * FROM ingest_run ORDER BY id DESC").fetchone()
    assert run_row["status"] == "ok"
    assert run_row["rows_loaded"] == result.rows_loaded


def test_rerunning_is_idempotent(db, launch_payload):
    first = run(FakeSource([launch_payload]), db)
    run(FakeSource([launch_payload]), db)

    with connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"]
        runs = conn.execute("SELECT COUNT(*) n FROM ingest_run").fetchone()["n"]
    assert total == first.rows_loaded, "second run must update, not duplicate"
    assert runs == 2, "but both runs are still recorded"


def test_dry_run_reads_without_writing(db, launch_payload):
    result = run(FakeSource([launch_payload]), db, dry_run=True)

    assert result.rows_read > 0
    assert result.rows_loaded == 0
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"] == 0


def test_upstream_failure_is_recorded_not_raised(db, launch_payload):
    result = run(FakeSource([launch_payload], boom=TimeoutError("upstream went away")), db)

    assert result.status == "error"
    assert "upstream went away" in result.error
    with connect(db) as conn:
        row = conn.execute("SELECT * FROM ingest_run ORDER BY id DESC").fetchone()
        # The partial page was rolled back, so the run is all-or-nothing.
        assert conn.execute("SELECT COUNT(*) n FROM launch").fetchone()["n"] == 0
    assert row["status"] == "error"
    assert "TimeoutError" in row["error"]


def test_raw_payloads_are_archived_when_asked(db, tmp_path, launch_payload):
    raw = tmp_path / "raw"
    run(FakeSource([launch_payload]), db, raw_dir=raw)

    archived = list(raw.glob("launches-*.json"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8").startswith("{")
