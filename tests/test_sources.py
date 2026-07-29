"""Transform tests, all against payloads captured from the live APIs."""

from __future__ import annotations

import pytest

from aerospacefunnel.sources.adsb import FEEDS, FeedUnavailable, SurveillanceSource
from aerospacefunnel.sources.airports import AirportsSource
from aerospacefunnel.sources.disruption import DisruptionSource, parse_update_time
from aerospacefunnel.sources.hazards import SigmetSource
from aerospacefunnel.sources.launches import LaunchUpdateSource, LaunchWindowSource, _flat
from aerospacefunnel.sources.orbital import OrbitalSource, _launch_designator
from aerospacefunnel.sources.spaceweather import SpaceWeatherSource
from aerospacefunnel.sources.weather import MetarSource, TafSource, _ceiling, _visibility


def surveillance() -> SurveillanceSource:
    return SurveillanceSource("KJFK", 40.64, -73.78, 250)


# --------------------------------------------------------------------------- ADS-B


def test_adsb_transform_reads_real_traffic(adsb_payload):
    rows = list(surveillance().transform(adsb_payload))
    assert len(rows) == 716

    dal = next(r for r in rows if r["callsign"] == "DAL1375")
    assert dal["registration"] == "N343NW"
    assert dal["aircraft_type"] == "A320"
    assert dal["alt_baro"] == 32000
    assert dal["ground_speed"] == pytest.approx(454.5)
    assert dal["source"] == "adsb.lol"
    assert dal["hub"] == "KJFK"


def test_ground_sentinel_becomes_a_flag_not_a_null_altitude(adsb_payload):
    """`alt_baro` is the string "ground" for taxiing aircraft - 116 of 716 in this sample.

    Treating it as a plain null would discard the only on-ground signal the feed carries,
    and flight-leg segmentation depends on it.
    """
    rows = list(surveillance().transform(adsb_payload))
    grounded = [r for r in rows if r["on_ground"]]
    assert len(grounded) == 116
    assert all(r["alt_baro"] is None for r in grounded)


def test_callsign_padding_is_stripped(adsb_payload):
    rows = list(surveillance().transform(adsb_payload))
    assert all(r["callsign"] == r["callsign"].strip() for r in rows if r["callsign"])


def test_snapshot_time_converts_milliseconds_to_seconds(adsb_payload):
    rows = list(surveillance().transform(adsb_payload))
    assert rows[0]["snapshot_time"] == int(adsb_payload["now"] / 1000)


def test_emergency_squawks_are_classified():
    payload = {
        "now": 1000,
        "_feed": "adsb.lol",
        "_hub": "KJFK",
        "ac": [
            {"hex": "a1", "squawk": "7700"},
            {"hex": "a2", "squawk": "7600"},
            {"hex": "a3", "squawk": "7500"},
            {"hex": "a4", "squawk": "1200"},
            {"hex": "a5", "squawk": "1200", "emergency": "none"},
        ],
    }
    rows = {r["hex"]: r["emergency"] for r in surveillance().transform(payload)}
    assert rows["a1"] == "emergency"
    assert rows["a2"] == "radio_failure"
    assert rows["a3"] == "unlawful_interference"
    assert rows["a4"] is None
    assert rows["a5"] is None


def test_rows_without_a_hex_are_skipped():
    payload = {"now": 1000, "ac": [{"flight": "GHOST"}, {"hex": "abc"}]}
    assert [r["hex"] for r in surveillance().transform(payload)] == ["abc"]


def test_failover_moves_to_the_next_feed():
    """A dead primary must not lose the poll - and the row must say which feed served it."""

    class FakeSession:
        def __init__(self):
            self.tried = []

        def get(self, url, **kw):
            self.tried.append(url)
            if "adsb.lol" in url:
                raise ConnectionError("primary down")
            return FakeResponse({"now": 5000, "ac": [{"hex": "abc"}]})

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    source = surveillance()
    session = FakeSession()
    payload = next(iter(source.extract(session)))

    assert source.feed_used == "airplanes.live"
    assert len(session.tried) == 2
    assert list(source.transform(payload))[0]["source"] == "airplanes.live"


def test_all_feeds_failing_raises():
    class DeadSession:
        def get(self, url, **kw):
            raise ConnectionError("down")

    with pytest.raises(FeedUnavailable):
        list(surveillance().extract(DeadSession()))


def test_every_configured_feed_has_a_url_template():
    assert set(surveillance().feed_order) <= set(FEEDS)


# ------------------------------------------------------------------------- weather


def test_metar_transform(metar_payload):
    rows = {r["station"]: r for r in MetarSource(["KJFK"]).transform(metar_payload)}
    assert rows["KJFK"]["flight_category"] == "VFR"
    assert rows["KLAX"]["flight_category"] == "MVFR"
    assert rows["KJFK"]["visibility_sm"] == 10.0


def test_visibility_handles_the_ten_plus_string():
    assert _visibility("10+") == 10.0
    assert _visibility(9) == 9.0
    assert _visibility("junk") is None
    assert _visibility(None) is None


def test_ceiling_uses_only_broken_and_overcast_layers():
    """Scattered and few layers are not a ceiling - using them would overstate risk."""
    clouds = [
        {"cover": "FEW", "base": 500},
        {"cover": "SCT", "base": 900},
        {"cover": "BKN", "base": 3300},
        {"cover": "OVC", "base": 5000},
    ]
    assert _ceiling(clouds) == 3300
    assert _ceiling([{"cover": "FEW", "base": 500}]) is None
    assert _ceiling(None) is None


def test_mvfr_ceiling_is_derived_from_real_data(metar_payload):
    klax = next(r for r in MetarSource([]).transform(metar_payload) if r["station"] == "KLAX")
    # MVFR is a 1,000-3,000 ft ceiling; the derived value must agree with the reported class.
    assert 1000 <= klax["ceiling_ft"] < 3000


def test_taf_flattens_to_one_row_per_forecast_period(taf_payload):
    rows = list(TafSource(["KJFK"]).transform(taf_payload))
    assert len(rows) == len(taf_payload["forecasts"][0]["fcsts"])
    assert all(r["station"] == "KJFK" for r in rows)
    assert all(r["period_from"] is not None for r in rows)


# ------------------------------------------------------------------------- hazards


def test_sigmet_transform_classifies_hazards(sigmet_payload):
    rows = list(SigmetSource().transform(sigmet_payload))
    assert len(rows) > 100
    names = {r["hazard_name"] for r in rows}
    assert "thunderstorm" in names
    assert all(r["hazard_id"] for r in rows)


def test_sigmet_bbox_is_within_valid_coordinates(sigmet_payload):
    for row in SigmetSource().transform(sigmet_payload):
        if row["min_lat"] is not None:
            assert -90 <= row["min_lat"] <= row["max_lat"] <= 90
            assert -180 <= row["min_lon"] <= 180
            assert -180 <= row["max_lon"] <= 180
            # Only an antimeridian-crossing box may have min > max.
            if not row["crosses_antimeridian"]:
                assert row["min_lon"] <= row["max_lon"]


def test_antimeridian_polygons_are_flagged_not_silently_wrapped(sigmet_payload):
    """Pacific FIRs report longitudes past 180 - a live sample carried 183.8.

    Naively wrapping into [-180, 180] and taking min/max would produce a box spanning
    almost the whole globe, quietly matching every flight on earth.
    """
    rows = list(SigmetSource().transform(sigmet_payload))
    crossing = [r for r in rows if r["crosses_antimeridian"]]
    assert crossing, "this fixture is expected to contain an antimeridian-crossing hazard"
    for row in crossing:
        assert row["min_lon"] > row["max_lon"], "crossing boxes wrap: min east, max west"


def test_bbox_of_an_ordinary_polygon_is_not_flagged():
    from aerospacefunnel.sources.hazards import _bbox

    box = _bbox([{"lat": 10, "lon": -20}, {"lat": 20, "lon": -10}])
    assert box == {
        "min_lat": 10,
        "max_lat": 20,
        "min_lon": -20.0,
        "max_lon": -10.0,
        "crosses_antimeridian": False,
    }


# ---------------------------------------------------------------------- disruption


def test_disruption_parses_ground_delay_programmes(disruption_payload):
    rows = list(DisruptionSource().transform(disruption_payload))
    gdp = next(r for r in rows if r["delay_type"] == "Ground Delay Programs")
    assert gdp["airport"] == "SFO"
    assert gdp["reason"] == "low ceilings"
    assert gdp["avg_delay"] == "41 minutes"


def test_disruption_captures_departure_delays_with_their_attribute(disruption_payload):
    """`<Arrival_Departure Type="Departure">` - the attribute is what makes it a departure."""
    rows = list(DisruptionSource().transform(disruption_payload))
    delay = next(r for r in rows if r["departure_delay_min"])
    assert delay["departure_delay_min"] == "31 minutes"
    assert delay["departure_delay_max"] == "45 minutes"


def test_disruption_parses_closures(disruption_payload):
    rows = list(DisruptionSource().transform(disruption_payload))
    assert any(r["delay_type"] == "Airport Closures" for r in rows)


def test_update_time_parsing():
    assert parse_update_time("Wed Jul 29 14:30:34 2026 GMT") == 1785335434
    assert parse_update_time(None) is None
    assert parse_update_time("nonsense") is None


def test_empty_disruption_xml_yields_nothing():
    assert list(DisruptionSource().transform({"xml": ""})) == []


# ------------------------------------------------------------------------ launches


def test_launch_transform_carries_pad_geography(launch_payload):
    rows = list(LaunchWindowSource().transform(launch_payload))
    assert rows
    geo = [r for r in rows if r["pad_latitude"] is not None]
    assert geo, "pad coordinates are the join to weather - they must survive the transform"
    assert all(-90 <= r["pad_latitude"] <= 90 for r in geo)


def test_launch_key_makes_slip_history_append_only(launch_payload):
    """Keying on (launch_id, net, status) means a moved NET appends rather than overwrites."""
    assert LaunchWindowSource().keys == ("launch_id", "net", "status")


def test_flat_accepts_both_list_and_detailed_shapes():
    assert _flat("Low Earth Orbit") == "Low Earth Orbit"
    assert _flat({"name": "LEO", "abbrev": "L"}) == "LEO"
    assert _flat({"name": "LEO", "abbrev": "L"}, "abbrev") == "L"
    assert _flat(None) is None
    assert _flat({}) is None


def test_dev_mirror_spends_a_different_budget():
    """Production is 15 req/hr; the dev mirror is unlimited. They must not share a bucket."""
    assert LaunchWindowSource(dev=True).throttle_key == "launchlibrary_dev"
    assert LaunchWindowSource(dev=False).throttle_key == "launchlibrary"
    assert "lldev" in LaunchWindowSource(dev=True).base_url


def test_launch_updates_are_extracted(launch_payload):
    rows = list(LaunchUpdateSource().transform(launch_payload))
    assert rows
    assert all(r["update_id"] and r["launch_id"] for r in rows)


# ------------------------------------------------------------------------- orbital


def test_orbital_transform(orbital_payload):
    rows = list(OrbitalSource().transform(orbital_payload))
    assert len(rows) > 100
    row = rows[0]
    assert row["norad_cat_id"]
    assert row["period_minutes"] and 60 < row["period_minutes"] < 2000


def test_launch_designator_links_objects_to_their_launch():
    assert _launch_designator("2026-045A") == "2026-045"
    assert _launch_designator("2026-045BK") == "2026-045"
    assert _launch_designator("nonsense") is None
    assert _launch_designator(None) is None


# -------------------------------------------------------------------- space weather


def test_spaceweather_transform_and_storm_flag(spaceweather_payload):
    rows = list(SpaceWeatherSource().transform(spaceweather_payload))
    assert len(rows) > 100
    assert all(r["observed_at"] for r in rows)
    assert all(r["storm"] is False for r in rows if r["estimated_kp"] < 5)


def test_storm_threshold_flags_severe_activity():
    payload = {"kp": [{"time_tag": "2026-07-29T08:39:00", "kp_index": 7, "estimated_kp": 7.33}]}
    assert list(SpaceWeatherSource().transform(payload))[0]["storm"] is True


# ------------------------------------------------------------------------ airports


def test_airports_transform(airports_payload):
    rows = {r["ident"]: r for r in AirportsSource().transform(airports_payload)}
    assert "KJFK" in rows
    assert rows["KJFK"]["iata"] == "JFK"
    assert rows["KJFK"]["kind"] == "large_airport"
    assert -90 <= rows["KJFK"]["latitude"] <= 90
