from __future__ import annotations

from aerospacefunnel.sources.flights import FlightsSource


def test_positional_states_map_to_named_columns(flight_payload):
    rows = list(FlightsSource().transform(flight_payload))
    assert rows, "fixture should contain at least one state vector"

    row = rows[0]
    assert row["icao24"] == "51116f"
    assert row["origin_country"] == "Estonia"
    assert row["snapshot_time"] == flight_payload["time"]
    assert isinstance(row["longitude"], float)
    assert row["on_ground"] in (0, 1)


def test_callsign_is_stripped_of_transponder_padding(flight_payload):
    rows = list(FlightsSource().transform(flight_payload))
    # The raw fixture value is "MBU2QV  " with trailing spaces.
    assert rows[0]["callsign"] == "MBU2QV"
    assert all(c is None or c == c.strip() for c in (r["callsign"] for r in rows))


def test_short_rows_do_not_raise():
    # OpenSky has grown the state vector over time; a shorter row must still load.
    payload = {"time": 1, "states": [["abc123", "TEST    ", "Norway"]]}
    (row,) = FlightsSource().transform(payload)
    assert row["icao24"] == "abc123"
    assert row["longitude"] is None
    assert row["position_source"] is None


def test_sensors_list_becomes_a_count():
    payload = {
        "time": 1,
        "states": [
            [
                "abc123",
                "X",
                "Norway",
                1,
                1,
                0.0,
                0.0,
                0.0,
                False,
                0.0,
                0.0,
                0.0,
                [7, 8, 9],
                0.0,
                "1000",
                False,
                0,
            ]
        ],
    }
    (row,) = FlightsSource().transform(payload)
    assert "sensors" not in row
    assert row["sensor_count"] == 3


def test_null_states_payload_is_empty_not_an_error():
    # OpenSky returns {"time": ..., "states": null} when a bbox matches nothing.
    assert list(FlightsSource().transform({"time": 1, "states": None})) == []
