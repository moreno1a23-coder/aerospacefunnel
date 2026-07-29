"""Config, credentials, throttling, quality gates and metadata."""

from __future__ import annotations

import pytest

from aerospacefunnel import config as config_mod
from aerospacefunnel import metadata, quality
from aerospacefunnel.credentials import Credentials, parse_env_file
from aerospacefunnel.throttle import RateLimited, Throttle

# ------------------------------------------------------------------------- config

VALID = """
[storage]
root = "data"
[surveillance]
cadence_seconds = 60
primary = "adsb.lol"
failover = ["airplanes.live"]
[[hubs]]
icao = "kjfk"
radius_nm = 250
"""


def write_config(tmp_path, text):
    p = tmp_path / "platform.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_config_loads_and_normalises(tmp_path):
    cfg = config_mod.load(write_config(tmp_path, VALID))
    assert cfg.hubs[0].icao == "KJFK"  # upper-cased
    assert cfg.feed_order == ("adsb.lol", "airplanes.live")
    assert cfg.hub("kjfk").radius_nm == 250


def test_unknown_hub_raises(tmp_path):
    cfg = config_mod.load(write_config(tmp_path, VALID))
    with pytest.raises(KeyError):
        cfg.hub("KZZZ")


def test_config_without_hubs_is_rejected(tmp_path):
    text = VALID.split("[[hubs]]")[0]
    with pytest.raises(ValueError, match="no \\[\\[hubs\\]\\]"):
        config_mod.load(write_config(tmp_path, text))


def test_absurd_cadence_is_rejected(tmp_path):
    """Polling faster than upstream refreshes only wastes community feed capacity."""
    with pytest.raises(ValueError, match="faster than"):
        config_mod.load(write_config(tmp_path, VALID.replace("= 60", "= 1")))


def test_missing_config_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_mod.load(tmp_path / "nope.toml")


# -------------------------------------------------------------------- credentials


def test_env_file_parsing():
    parsed = parse_env_file("""
# a comment
A=1
export B = "two"
C='three'
D=
malformed line
""")
    assert parsed["A"] == "1"
    assert parsed["B"] == "two"
    assert parsed["C"] == "three"
    assert parsed["D"] == ""
    assert "malformed line" not in parsed


def test_environment_beats_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NASA_API_KEY=from_file\n", encoding="utf-8")
    creds = Credentials(env_file=env, environ={"NASA_API_KEY": "from_env"})
    assert creds.get("NASA_API_KEY") == "from_env"


def test_env_file_used_when_environment_is_absent(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NASA_API_KEY=from_file\n", encoding="utf-8")
    assert Credentials(env_file=env, environ={}).get("NASA_API_KEY") == "from_file"


def test_missing_credential_is_none_not_an_error(tmp_path):
    creds = Credentials(env_file=tmp_path / "absent", environ={})
    assert creds.get("NASA_API_KEY") is None
    assert creds.has("NASA_API_KEY") is False


def test_empty_string_counts_as_absent(tmp_path):
    """A blank line in .env.example must not read as a configured credential."""
    assert (
        Credentials(env_file=tmp_path / "x", environ={"EIA_API_KEY": ""}).has("EIA_API_KEY")
        is False
    )


def test_paired_credentials_are_only_ready_together(tmp_path):
    half = Credentials(env_file=tmp_path / "x", environ={"OPENSKY_CLIENT_ID": "a"})
    assert half.has("OPENSKY_CLIENT_ID") is True
    assert half.group_ready("OPENSKY_CLIENT_ID") is False

    both = Credentials(
        env_file=tmp_path / "x",
        environ={"OPENSKY_CLIENT_ID": "a", "OPENSKY_CLIENT_SECRET": "b"},
    )
    assert both.group_ready("OPENSKY_CLIENT_ID") is True


def test_status_covers_every_known_credential(tmp_path):
    from aerospacefunnel.credentials import CREDENTIALS

    assert len(Credentials(env_file=tmp_path / "x", environ={}).status()) == len(CREDENTIALS)


# ----------------------------------------------------------------------- throttle


def test_budget_is_spent_and_refills(tmp_path):
    t = Throttle(tmp_path / "m.db")
    assert t.remaining("nasa_demo") == 10
    t.acquire("nasa_demo", now=1000)
    assert t.remaining("nasa_demo", now=1000) == pytest.approx(9)
    # 10/hr refill means an hour restores the full bucket.
    assert t.remaining("nasa_demo", now=1000 + 3600) == pytest.approx(10)


def test_exhausted_budget_refuses_before_the_request(tmp_path):
    """Refusing locally is the point: a 429 from upstream is the failure we avoid."""
    t = Throttle(tmp_path / "m.db")
    for _ in range(15):
        t.acquire("launchlibrary", now=1000)
    with pytest.raises(RateLimited) as err:
        t.acquire("launchlibrary", now=1000)
    assert err.value.wait_seconds > 0


def test_budget_persists_across_instances(tmp_path):
    """The whole reason this is on disk: a cron restart must not reset the allowance."""
    db = tmp_path / "m.db"
    for _ in range(15):
        Throttle(db).acquire("launchlibrary", now=1000)
    with pytest.raises(RateLimited):
        Throttle(db).acquire("launchlibrary", now=1000)


def test_dev_mirror_budget_is_independent_of_production(tmp_path):
    t = Throttle(tmp_path / "m.db")
    for _ in range(15):
        t.acquire("launchlibrary", now=1000)
    t.acquire("launchlibrary_dev", now=1000)  # must not raise


def test_unknown_source_gets_the_default_budget(tmp_path):
    assert Throttle(tmp_path / "m.db").remaining("something_new") == 60


# ------------------------------------------------------------------------ quality


def test_position_suite_passes_on_good_rows():
    rows = [
        {
            "hex": "a",
            "snapshot_time": 1,
            "latitude": 40.0,
            "longitude": -73.0,
            "alt_baro": 30000,
            "ground_speed": 450,
            "track": 90,
        }
    ]
    assert quality.blocking_failures(quality.run_suite("fct_position", rows)) == []


def test_out_of_range_values_are_caught():
    rows = [{"hex": "a", "snapshot_time": 1, "latitude": 999, "longitude": -73.0}]
    failures = {f.name for f in quality.blocking_failures(quality.run_suite("fct_position", rows))}
    assert "in_range[latitude]" in failures


def test_duplicate_keys_are_caught():
    rows = [{"hex": "a", "snapshot_time": 1, "latitude": 1.0, "longitude": 1.0}] * 2
    failures = {f.name for f in quality.blocking_failures(quality.run_suite("fct_position", rows))}
    assert "unique[hex+snapshot_time]" in failures


def test_empty_table_is_a_blocking_failure():
    assert quality.blocking_failures(quality.run_suite("fct_position", []))


def test_null_rate_tolerance_is_respected():
    """Some ADS-B rows genuinely lack a position; a small fraction must not fail the load."""
    rows = [
        {"hex": f"h{i}", "snapshot_time": i, "latitude": 40.0, "longitude": -73.0}
        for i in range(100)
    ]
    rows[0]["latitude"] = None
    assert quality.blocking_failures(quality.run_suite("fct_position", rows)) == []
    for r in rows[:10]:
        r["latitude"] = None
    assert quality.blocking_failures(quality.run_suite("fct_position", rows))


def test_freshness_check_detects_a_stalled_feed():
    exp = quality.fresh_within("snapshot_time", 300, now=10_000)
    passed, _, _ = exp.check([{"snapshot_time": 9900}])
    assert passed
    passed, _, _ = exp.check([{"snapshot_time": 1000}])
    assert not passed


def test_a_check_that_raises_counts_as_a_failure():
    """A broken expectation must not be mistaken for clean data."""

    def boom(rows):
        raise RuntimeError("bad check")

    results = quality.run_suite(
        "fct_position",
        [{"hex": "a", "snapshot_time": 1}],
        extra=[quality.Expectation("boom", boom)],
    )
    assert any(r.name == "boom" and not r.passed for r in results)


def test_warn_severity_does_not_block():
    results = quality.run_suite("x", [], extra=[quality.row_count_between(5, 10)])
    assert results and results[0].passed is False
    assert quality.blocking_failures(results) == []


def test_unknown_table_has_no_suite():
    assert quality.run_suite("not_a_table", [{"a": 1}]) == []


# ----------------------------------------------------------------------- metadata


def test_run_bookkeeping(metadata_db):
    with metadata.connect(metadata_db) as conn:
        run_id = metadata.start_run(conn, "surveillance", "silver", "fct_position")
        metadata.finish_run(
            conn, run_id, status="ok", rows_read=10, rows_written=10, feed_used="adsb.lol"
        )
        row = conn.execute("SELECT * FROM pipeline_run WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["feed_used"] == "adsb.lol"
    assert row["finished_at"]


def test_lineage_and_dq_are_recorded(metadata_db):
    with metadata.connect(metadata_db) as conn:
        run_id = metadata.start_run(conn, "s", "silver", "t")
        metadata.record_lineage(conn, run_id, "bronze/a.json.gz", "silver/t/data.parquet")
        metadata.record_dq(
            conn,
            run_id=run_id,
            table_name="t",
            check_name="c",
            passed=False,
            observed="1",
            expected="0",
            severity="error",
        )
        assert conn.execute("SELECT COUNT(*) c FROM lineage").fetchone()["c"] == 1
        assert conn.execute("SELECT passed FROM dq_result").fetchone()["passed"] == 0
