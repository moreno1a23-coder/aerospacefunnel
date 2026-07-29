"""En-route hazards: international SIGMETs and G-AIRMETs.

A live sample carried 135 active SIGMETs covering ICE, TS, TURB, MTW, VA and TC. Each has a
polygon, so hazards can be joined against actual tracks to answer "which of our flights was
inside a volcanic ash advisory" rather than merely "one existed somewhere".
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests

SIGMET_URL = "https://aviationweather.gov/api/data/isigmet"
GAIRMET_URL = "https://aviationweather.gov/api/data/gairmet"

HAZARD_NAMES = {
    "TS": "thunderstorm",
    "TURB": "turbulence",
    "ICE": "icing",
    "MTW": "mountain_wave",
    "VA": "volcanic_ash",
    "TC": "tropical_cyclone",
    "DS": "dust_storm",
    "SS": "sand_storm",
    "IFR": "instrument_conditions",
    "LLWS": "low_level_wind_shear",
}


EMPTY_BBOX: dict[str, Any] = {
    "min_lat": None,
    "max_lat": None,
    "min_lon": None,
    "max_lon": None,
    "crosses_antimeridian": False,
}


def _normalise_lon(lon: float) -> float:
    """Wrap a longitude into [-180, 180]."""
    return ((lon + 180) % 360) - 180


def _bbox(coords: Any) -> dict[str, Any]:
    """Bounding box of the hazard polygon - cheap to index, enough to prefilter tracks.

    Pacific oceanic FIRs report longitudes past 180 (a live sample from Anchorage Oceanic
    carried 183.8). Wrapping those into [-180, 180] and then taking a naive min/max would
    produce a box spanning almost the whole globe, so an antimeridian crossing is detected
    and flagged instead: consumers must use ``lon >= min_lon OR lon <= max_lon`` for those
    rows rather than a plain BETWEEN.
    """
    if not isinstance(coords, list) or not coords:
        return dict(EMPTY_BBOX)

    lats = [c["lat"] for c in coords if isinstance(c, dict) and c.get("lat") is not None]
    raw_lons = [c["lon"] for c in coords if isinstance(c, dict) and c.get("lon") is not None]
    if not lats or not raw_lons:
        return dict(EMPTY_BBOX)

    # Crossing shows up either as an out-of-range reported value or as a wrapped span so
    # wide it can only be the short way round the other side.
    lons = [_normalise_lon(x) for x in raw_lons]
    crosses = max(raw_lons) > 180 or min(raw_lons) < -180 or (max(lons) - min(lons)) > 180

    if crosses:
        # Split at the antimeridian: the box runs east from min_lon to +180, then from
        # -180 to max_lon.
        east = [x for x in lons if x >= 0]
        west = [x for x in lons if x < 0]
        min_lon = min(east) if east else min(lons)
        max_lon = max(west) if west else max(lons)
    else:
        min_lon, max_lon = min(lons), max(lons)

    return {
        "min_lat": min(lats),
        "max_lat": max(lats),
        "min_lon": min_lon,
        "max_lon": max_lon,
        "crosses_antimeridian": crosses,
    }


class SigmetSource:
    """Active international SIGMETs."""

    name = "sigmet"
    table = "fct_hazard"
    keys = ("hazard_id",)

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(SIGMET_URL, params={"format": "json"}, timeout=60)
        response.raise_for_status()
        yield {"sigmets": response.json()}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for s in payload.get("sigmets") or []:
            hazard = s.get("hazard")
            coords = s.get("coords")
            # No stable id is published, so one is composed from the fields that together
            # identify a single issuance.
            hazard_id = f"{s.get('firId')}-{s.get('seriesId')}-{s.get('validTimeFrom')}-{hazard}"
            yield {
                "hazard_id": hazard_id,
                "kind": "SIGMET",
                "fir_id": s.get("firId"),
                "fir_name": s.get("firName"),
                "icao_id": s.get("icaoId"),
                "series": s.get("seriesId"),
                "hazard": hazard,
                "hazard_name": HAZARD_NAMES.get(hazard or "", None),
                "qualifier": s.get("qualifier"),
                "valid_from": s.get("validTimeFrom"),
                "valid_to": s.get("validTimeTo"),
                "altitude_base_ft": s.get("base"),
                "altitude_top_ft": s.get("top"),
                "movement_dir": s.get("dir"),
                "movement_speed_kt": s.get("spd"),
                "change": s.get("chng"),
                "raw": s.get("rawSigmet"),
                **_bbox(coords),
            }


class GairmetSource:
    """G-AIRMETs - graphical airmen's meteorological advisories (US)."""

    name = "gairmet"
    table = "fct_hazard"
    keys = ("hazard_id",)

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(GAIRMET_URL, params={"format": "json"}, timeout=60)
        response.raise_for_status()
        yield {"gairmets": response.json()}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for g in payload.get("gairmets") or []:
            hazard = g.get("hazard")
            hazard_id = f"gairmet-{g.get('productId')}-{g.get('validTime')}-{hazard}-{g.get('id')}"
            yield {
                "hazard_id": hazard_id,
                "kind": "GAIRMET",
                "hazard": hazard,
                "hazard_name": HAZARD_NAMES.get(hazard or "", None),
                "valid_from": g.get("validTime"),
                "valid_to": g.get("expireTime"),
                "issue_time": g.get("issueTime"),
                "altitude_base_ft": g.get("base"),
                "altitude_top_ft": g.get("top"),
                "severity": g.get("severity"),
                "frequency": g.get("frequency"),
                **_bbox(g.get("coords")),
            }
