"""Warehouse views, marts and the CLI - all offline."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aerospacefunnel import cli, marts, storage, warehouse
from aerospacefunnel.sources.adsb import SurveillanceSource

TS = datetime(2026, 7, 29, 14, 0, tzinfo=UTC)

CONFIG = """
[storage]
root = "{root}"
warehouse = "{wh}"
[surveillance]
cadence_seconds = 60
primary = "adsb.lol"
[[hubs]]
icao = "KJFK"
radius_nm = 250
"""


@pytest.fixture
def platform(tmp_path, adsb_payload):
    """A warehouse populated with one real surveillance snapshot."""
    root = tmp_path / "data"
    rows = list(SurveillanceSource("KJFK", 40.6, -73.7, 250).transform(adsb_payload))
    storage.write_partition(
        root, "silver", "fct_position", rows, ts=TS, keys=("hex", "snapshot_time")
    )
    cfg_path = tmp_path / "platform.toml"
    cfg_path.write_text(
        CONFIG.format(root=root.as_posix(), wh=(tmp_path / "wh.duckdb").as_posix()),
        encoding="utf-8",
    )
    return {"root": root, "db": tmp_path / "wh.duckdb", "config": str(cfg_path)}


def test_views_and_marts_are_created(platform):
    built = warehouse.refresh(platform["root"], platform["db"])
    assert "fct_position" in built["tables"]
    assert "mart_traffic_density" in built["marts"]


def test_marts_without_data_are_skipped_not_broken(platform):
    """A partially populated warehouse must still answer what it can."""
    built = warehouse.refresh(platform["root"], platform["db"])
    assert "mart_fleet_utilisation" in built["skipped"]
    assert "fct_flight_leg" not in built["tables"]


def test_traffic_density_mart_returns_real_numbers(platform):
    with warehouse.connect(platform["db"]) as conn:
        warehouse.build(conn, platform["root"])
        aircraft, fixes = conn.execute(
            "SELECT SUM(aircraft), SUM(fixes) FROM mart_traffic_density"
        ).fetchone()
    assert aircraft == 716
    assert fixes == 716


def test_every_mart_sql_is_valid_against_its_dependencies(platform, tmp_path):
    """Guards against a mart that only fails the first time real data appears."""
    root = platform["root"]
    # Give every declared dependency at least one row so all marts can be created.
    seed = {
        "fct_flight_leg": (
            "gold",
            {
                "leg_id": "x",
                "hex": "a",
                "callsign": "AAL1",
                "registration": "N1",
                "aircraft_type": "B738",
                "start_time": 1785336000,
                "duration_s": 3600,
                "complete": True,
                "track_distance_nm": 100.0,
                "direct_distance_nm": 90.0,
                "track_efficiency": 1.11,
            },
        ),
        "fct_weather_obs": (
            "silver",
            {
                "station": "KJFK",
                "obs_time": 1785336000,
                "flight_category": "VFR",
                "visibility_sm": 10.0,
                "ceiling_ft": 3000,
                "wind_speed_kt": 7,
                "wind_gust_kt": None,
            },
        ),
        "fct_disruption": (
            "silver",
            {
                "airport": "SFO",
                "delay_type": "Ground Delay Programs",
                "observed_at": 1785336000,
                "reason": "low ceilings",
                "avg_delay": "41 minutes",
                "max_delay": "1 hour",
            },
        ),
        "fct_launch_window": (
            "silver",
            {
                "launch_id": "l1",
                "net": "2026-08-01T00:00:00Z",
                "status": "Go",
                "observed_at": "2026-07-29T00:00:00Z",
                "name": "N",
                "provider": "P",
                "vehicle": "V",
                "window_start": None,
                "window_end": None,
                "pad_name": "LC-39A",
                "pad_location": "KSC",
                "pad_latitude": 28.6,
                "pad_longitude": -80.6,
                "probability": 80,
                "weather_concerns": None,
                "hold_reason": None,
            },
        ),
        "fct_orbital_element": (
            "silver",
            {
                "norad_cat_id": 1,
                "epoch": "2026-07-29T00:00:00",
                "object_name": "SAT",
                "launch_designator": "2026-045",
                "period_minutes": 95.0,
                "mean_motion": 15.1,
                "bstar": 0.0001,
                "inclination": 53.0,
            },
        ),
        "dim_aircraft": (
            "gold",
            {
                "hex": "a3c9b4",
                "registration": "N343NW",
                "aircraft_type": "A320",
                "operator": "Delta Air Lines",
                "operator_icao": "DAL",
                "valid_from": 1785336000,
                "valid_to": None,
                "is_current": True,
                "first_seen": 1785336000,
                "last_seen": 1785336600,
            },
        ),
        "fct_fuel_price": (
            "silver",
            {
                "period": "2026-07-28",
                "series": "EER_EPJK_PF4_RGC_DPG",
                "product": "EPJK",
                "product_name": "Kerosene-Type Jet Fuel",
                "area": "GULF COAST",
                "price": 2.31,
                "units": "$/GAL",
            },
        ),
        "fct_notam": (
            "silver",
            {
                "notam_id": "NOTAM_1_1",
                "number": "07/123",
                "location": "KJFK",
                "icao_location": "KJFK",
                "type": "N",
                "classification": "DOM",
                "effective_start": "2026-07-29T00:00:00Z",
                "effective_end": "2026-08-01T00:00:00Z",
            },
        ),
    }
    key_columns = (
        "leg_id", "station", "airport", "launch_id", "norad_cat_id",
        "hex", "period", "notam_id",
    )
    for table, (layer, row) in seed.items():
        storage.write_partition(
            root,
            layer,
            table,
            [row],
            ts=TS,
            keys=tuple(k for k in row if k in key_columns),
            hourly=False,
        )

    with warehouse.connect(tmp_path / "all.duckdb") as conn:
        built = warehouse.build(conn, root)
        assert not built["skipped"], f"unexpectedly skipped: {built['skipped']}"
        for mart in marts.MARTS:
            conn.execute(f"SELECT * FROM {mart.name} LIMIT 1").fetchall()


def test_every_mart_declares_its_dependencies():
    for mart in marts.MARTS:
        assert mart.depends_on, f"{mart.name} declares no dependencies"
        assert mart.description


# ----------------------------------------------------------------------------- CLI


def test_warehouse_command(platform, capsys):
    assert cli.main(["--config", platform["config"], "warehouse"]) == 0
    assert "mart_traffic_density" in capsys.readouterr().out


def test_query_command(platform, capsys):
    code = cli.main(
        ["--config", platform["config"], "query", "SELECT COUNT(*) AS n FROM fct_position"]
    )
    assert code == 0
    assert "716" in capsys.readouterr().out


def test_query_failure_is_reported_not_raised(platform, capsys):
    assert cli.main(["--config", platform["config"], "query", "SELECT * FROM nope"]) == 1
    assert "query failed" in capsys.readouterr().err


def test_check_command_runs_quality_gates(platform, capsys):
    code = cli.main(["--config", platform["config"], "check"])
    out = capsys.readouterr().out
    assert "fct_position" in out
    assert code == 0


def test_keys_command_exits_zero_with_no_credentials(platform, capsys, monkeypatch, tmp_path):
    """Missing credentials are a normal state - every source has an anonymous path."""
    monkeypatch.chdir(tmp_path)
    for name in (
        "OPENSKY_CLIENT_ID",
        "OPENSKY_CLIENT_SECRET",
        "NASA_API_KEY",
        "EIA_API_KEY",
        "LL2_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)

    assert cli.main(["--config", platform["config"], "keys"]) == 0
    out = capsys.readouterr().out
    assert "None are required" in out
    assert "opensky-network.org" in out


def test_stats_command(platform, capsys):
    assert cli.main(["--config", platform["config"], "stats"]) == 0
    assert "fct_position" in capsys.readouterr().out


def test_bad_config_is_reported_cleanly(capsys):
    assert cli.main(["--config", "/nonexistent/platform.toml", "stats"]) == 2
    assert "config error" in capsys.readouterr().err


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_legs_command_without_positions(tmp_path, capsys):
    cfg = tmp_path / "p.toml"
    cfg.write_text(
        CONFIG.format(root=(tmp_path / "empty").as_posix(), wh=(tmp_path / "w.duckdb").as_posix()),
        encoding="utf-8",
    )
    assert cli.main(["--config", str(cfg), "legs"]) == 1
    assert "no positions yet" in capsys.readouterr().out
