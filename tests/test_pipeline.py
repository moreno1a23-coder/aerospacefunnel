"""End-to-end pipeline tests with stand-in sources - nothing here touches the network."""

from __future__ import annotations

import pytest

from aerospacefunnel import metadata, pipeline, storage
from aerospacefunnel.sources.adsb import SurveillanceSource
from aerospacefunnel.throttle import Throttle


class FakeSource:
    name = "surveillance"
    table = "fct_position"
    keys = ("hex", "snapshot_time")
    feed_used = "adsb.lol"

    def __init__(self, payloads, boom: Exception | None = None):
        self.payloads = payloads
        self.boom = boom

    def extract(self, session):
        yield from self.payloads
        if self.boom:
            raise self.boom

    def transform(self, payload):
        return SurveillanceSource("KJFK", 40.6, -73.7, 250).transform(payload)


def run(source, storage_root, metadata_db, **kw):
    return pipeline.run(
        source, storage_root=storage_root, metadata_db=metadata_db, session=object(), **kw
    )


def test_full_run_writes_bronze_silver_and_bookkeeping(storage_root, metadata_db, adsb_payload):
    result = run(FakeSource([adsb_payload]), storage_root, metadata_db)

    assert result.ok
    assert result.rows_read == result.rows_written == 716
    assert result.bytes_raw > 0

    assert len(list(storage.iter_bronze(storage_root, "surveillance"))) == 1
    assert len(storage.read_table(storage_root, "silver", "fct_position")) == 716

    with metadata.connect(metadata_db) as conn:
        row = conn.execute("SELECT * FROM pipeline_run ORDER BY id DESC").fetchone()
        lineage = conn.execute("SELECT COUNT(*) c FROM lineage").fetchone()["c"]
    assert row["status"] == "ok"
    assert row["feed_used"] == "adsb.lol"
    assert lineage == 1


def test_rerunning_is_idempotent(storage_root, metadata_db, adsb_payload):
    first = run(FakeSource([adsb_payload]), storage_root, metadata_db)
    run(FakeSource([adsb_payload]), storage_root, metadata_db)

    rows = storage.read_table(storage_root, "silver", "fct_position")
    assert len(rows) == first.rows_written, "second run must merge, not duplicate"

    with metadata.connect(metadata_db) as conn:
        assert conn.execute("SELECT COUNT(*) c FROM pipeline_run").fetchone()["c"] == 2


def test_dry_run_archives_bronze_but_writes_no_silver(storage_root, metadata_db, adsb_payload):
    """Bronze still lands: capturing the payload is what makes a later replay possible."""
    result = run(FakeSource([adsb_payload]), storage_root, metadata_db, dry_run=True)
    assert result.rows_read > 0
    assert result.rows_written == 0
    assert list(storage.iter_bronze(storage_root, "surveillance"))
    assert storage.read_table(storage_root, "silver", "fct_position") == []


def test_upstream_failure_is_recorded_not_raised(storage_root, metadata_db, adsb_payload):
    result = run(
        FakeSource([adsb_payload], boom=TimeoutError("upstream gone")), storage_root, metadata_db
    )
    assert result.status == "error"
    assert "upstream gone" in result.error

    with metadata.connect(metadata_db) as conn:
        row = conn.execute("SELECT * FROM pipeline_run ORDER BY id DESC").fetchone()
    assert row["status"] == "error"
    assert "TimeoutError" in row["error"]


def test_throttling_is_reported_distinctly_from_failure(storage_root, metadata_db, adsb_payload):
    """A spent budget is the limiter working, not an outage - it must not read as an error."""
    throttle = Throttle(metadata_db)
    for _ in range(15):
        throttle.acquire("launchlibrary")

    result = run(
        FakeSource([adsb_payload]),
        storage_root,
        metadata_db,
        throttle=throttle,
        throttle_key="launchlibrary",
    )
    assert result.status == "throttled"
    assert result.rows_written == 0


def test_replay_rebuilds_silver_from_bronze_without_network(
    storage_root, metadata_db, adsb_payload
):
    """The payoff for archiving raw bytes: fix a transform, reprocess history, spend nothing."""
    run(FakeSource([adsb_payload]), storage_root, metadata_db)

    # Wipe silver, keeping bronze - as if a transform bug had produced bad rows.
    import shutil

    shutil.rmtree(f"{storage_root}/silver")
    assert storage.read_table(storage_root, "silver", "fct_position") == []

    result = pipeline.replay(FakeSource([]), storage_root=storage_root)
    assert result.pages == 1
    assert len(storage.read_table(storage_root, "silver", "fct_position")) == 716


def test_replay_is_itself_idempotent(storage_root, metadata_db, adsb_payload):
    run(FakeSource([adsb_payload]), storage_root, metadata_db)
    a = pipeline.replay(FakeSource([]), storage_root=storage_root)
    b = pipeline.replay(FakeSource([]), storage_root=storage_root)
    assert a.rows_written == b.rows_written
    assert len(storage.read_table(storage_root, "silver", "fct_position")) == 716


def test_replay_partitions_by_payload_time_not_now(storage_root, metadata_db, adsb_payload):
    """History must rebuild into its original partitions, not collapse into today."""
    run(FakeSource([adsb_payload]), storage_root, metadata_db)
    before = {
        p.parent
        for p in (__import__("pathlib").Path(storage_root) / "silver").rglob("data.parquet")
    }
    import shutil

    shutil.rmtree(f"{storage_root}/silver")
    pipeline.replay(FakeSource([]), storage_root=storage_root)
    after = {
        p.parent
        for p in (__import__("pathlib").Path(storage_root) / "silver").rglob("data.parquet")
    }
    assert before == after


def test_empty_payload_is_not_an_error(storage_root, metadata_db):
    result = run(FakeSource([{"now": 1000, "ac": []}]), storage_root, metadata_db)
    assert result.ok
    assert result.rows_read == 0


@pytest.mark.parametrize("hourly,marker", [(True, "hh="), (False, "dt=")])
def test_partition_granularity_is_configurable(
    storage_root, metadata_db, adsb_payload, hourly, marker
):
    result = run(FakeSource([adsb_payload]), storage_root, metadata_db, hourly=hourly)
    assert marker in result.partitions[0]
