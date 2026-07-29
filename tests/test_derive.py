from __future__ import annotations

import pytest

from aerospacefunnel.derive import Airport, flight_legs, haversine_nm, nearest_airport

JFK = Airport("KJFK", 40.6398, -73.7789)
LGA = Airport("KLGA", 40.7772, -73.8726)


def fix(hex_id="abc123", t=0, lat=40.64, lon=-73.78, alt=None, ground=False, **kw):
    return {
        "hex": hex_id,
        "snapshot_time": t,
        "latitude": lat,
        "longitude": lon,
        "alt_baro": alt,
        "on_ground": ground,
        "hub": "KJFK",
        **kw,
    }


def test_haversine_matches_a_known_distance():
    # JFK to LAX is ~2,144 nm great circle.
    assert haversine_nm(40.6398, -73.7789, 33.9425, -118.4081) == pytest.approx(2144, abs=15)


def test_haversine_of_a_point_with_itself_is_zero():
    assert haversine_nm(40.0, -73.0, 40.0, -73.0) == pytest.approx(0)


def test_nearest_airport_within_radius():
    ident, dist = nearest_airport(40.6400, -73.7790, [JFK, LGA])
    assert ident == "KJFK"
    assert dist < 1


def test_nearest_airport_refuses_to_guess_beyond_the_radius():
    """An airborne fix mid-ocean must not be assigned to the nearest continent."""
    assert nearest_airport(0.0, 0.0, [JFK, LGA]) == (None, None)
    assert nearest_airport(None, None, [JFK]) == (None, None)
    assert nearest_airport(40.64, -73.78, []) == (None, None)


def test_a_complete_leg_gets_both_endpoints():
    fixes = [
        fix(t=0, ground=True, lat=40.6398, lon=-73.7789),
        fix(t=300, alt=10000, lat=40.70, lon=-73.80),
        fix(t=900, alt=30000, lat=40.75, lon=-73.85),
        fix(t=1500, alt=8000, lat=40.77, lon=-73.87),
        fix(t=1800, ground=True, lat=40.7772, lon=-73.8726),
    ]
    (leg,) = flight_legs(fixes, [JFK, LGA])
    assert leg["dep_airport"] == "KJFK"
    assert leg["arr_airport"] == "KLGA"
    assert leg["complete"] is True
    assert leg["duration_s"] == 1800
    assert leg["max_alt_ft"] == 30000


def test_a_transit_gets_null_endpoints_rather_than_a_guess():
    """The core honesty rule: an aircraft only seen airborne has no known origin.

    Assigning it the nearest airport would silently corrupt every utilisation and
    punctuality figure downstream.
    """
    fixes = [fix(t=t, alt=35000, lat=40.6 + t / 10000, lon=-73.7) for t in range(0, 1800, 300)]
    (leg,) = flight_legs(fixes, [JFK, LGA])
    assert leg["dep_airport"] is None
    assert leg["arr_airport"] is None
    assert leg["dep_observed"] is False
    assert leg["arr_observed"] is False
    assert leg["complete"] is False


def test_a_contact_gap_splits_one_aircraft_into_two_legs():
    early = [fix(t=t, alt=30000) for t in range(0, 1200, 300)]
    late = [fix(t=t, alt=30000) for t in range(10000, 11200, 300)]
    legs = flight_legs(early + late, [])
    assert len(legs) == 2
    assert legs[0]["end_time"] < legs[1]["start_time"]


def test_no_leg_spans_a_gap_longer_than_the_threshold():
    fixes = [fix(t=t, alt=30000) for t in range(0, 1200, 300)]
    fixes += [fix(t=t, alt=30000) for t in range(50000, 51200, 300)]
    for leg in flight_legs(fixes, []):
        assert leg["duration_s"] <= 15 * 60 + 1200


def test_two_aircraft_do_not_merge():
    fixes = [fix("aaa", t, alt=30000) for t in range(0, 1800, 300)]
    fixes += [fix("bbb", t, alt=30000) for t in range(0, 1800, 300)]
    legs = flight_legs(fixes, [])
    assert {leg["hex"] for leg in legs} == {"aaa", "bbb"}


def test_ground_only_fixes_produce_no_leg():
    fixes = [fix(t=t, ground=True) for t in range(0, 1800, 300)]
    assert flight_legs(fixes, []) == []


def test_very_short_segments_are_discarded_as_ground_noise():
    fixes = [fix(t=0, alt=1000), fix(t=60, alt=1200)]
    assert flight_legs(fixes, []) == []


def test_track_efficiency_detects_a_diversion():
    """Flying a dogleg must show more distance flown than the direct line."""
    fixes = [
        fix(t=0, ground=True, lat=40.0, lon=-73.0),
        fix(t=600, alt=30000, lat=41.0, lon=-73.0),
        fix(t=1200, alt=30000, lat=41.0, lon=-72.0),
        fix(t=1800, ground=True, lat=40.0, lon=-72.0),
    ]
    (leg,) = flight_legs(fixes, [])
    assert leg["track_distance_nm"] > leg["direct_distance_nm"]
    assert leg["track_efficiency"] > 1.0


def test_leg_ids_are_unique_and_stable():
    fixes = [fix("aaa", t, alt=30000) for t in range(0, 1800, 300)]
    first = flight_legs(fixes, [])
    second = flight_legs(fixes, [])
    assert [leg["leg_id"] for leg in first] == [leg["leg_id"] for leg in second]
    assert len({leg["leg_id"] for leg in first}) == len(first)


def test_emergency_propagates_to_the_leg():
    fixes = [
        fix(t=t, alt=30000, emergency="emergency" if t == 600 else None)
        for t in range(0, 1800, 300)
    ]
    (leg,) = flight_legs(fixes, [])
    assert leg["emergency"] == "emergency"


def test_metadata_is_taken_from_whichever_fix_has_it():
    """ADS-B fills in registration and type only on some messages."""
    fixes = [
        fix(t=0, alt=30000),
        fix(t=600, alt=30000, registration="N123AB", aircraft_type="B738"),
        fix(t=1200, alt=30000),
    ]
    (leg,) = flight_legs(fixes, [])
    assert leg["registration"] == "N123AB"
    assert leg["aircraft_type"] == "B738"


def test_empty_input():
    assert flight_legs([], []) == []


def test_fixes_missing_a_timestamp_are_ignored():
    assert flight_legs([{"hex": "abc"}, {"snapshot_time": 1}], []) == []
