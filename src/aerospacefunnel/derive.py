"""Derive flight legs from raw position fixes.

This turns a stream of independent observations into the unit airline analytics actually
reason about: a leg, with an origin, a destination, a block time and a flown distance.

Honesty about partial observation matters more here than anywhere else in the platform.
Hub-radius polling only sees an aircraft inside the bubble, so many legs are *transits* whose
real origin or destination was never observed. Every leg therefore carries `dep_observed` and
`arr_observed` flags, and an airport is only assigned when the aircraft was actually seen on
the ground near one. A leg that entered and left the bubble airborne gets NULL endpoints
rather than an invented nearest airport - a guessed origin would silently corrupt every
punctuality and utilisation figure downstream.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

EARTH_RADIUS_NM = 3440.065

# A contact gap longer than this means the aircraft left coverage; the next fixes are a
# different leg rather than an implausible teleport.
DEFAULT_GAP_SECONDS = 15 * 60
# How close to an airport an on-ground aircraft must be to be assigned to it.
DEFAULT_AIRPORT_RADIUS_NM = 3.0
# Legs shorter than this are ground noise, not flights.
MIN_LEG_SECONDS = 5 * 60


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_NM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Airport:
    ident: str
    latitude: float
    longitude: float


def nearest_airport(
    lat: float | None,
    lon: float | None,
    airports: Sequence[Airport],
    max_nm: float = DEFAULT_AIRPORT_RADIUS_NM,
) -> tuple[str | None, float | None]:
    """Closest airport within `max_nm`, else (None, None). Never guesses beyond the radius."""
    if lat is None or lon is None or not airports:
        return None, None
    best: tuple[str | None, float | None] = (None, None)
    best_d = max_nm
    for ap in airports:
        d = haversine_nm(lat, lon, ap.latitude, ap.longitude)
        if d <= best_d:
            best_d, best = d, (ap.ident, d)
    return best


def _segments(fixes: list[dict[str, Any]], gap_seconds: int) -> list[list[dict[str, Any]]]:
    """Split one aircraft's fixes into legs on contact gaps and ground dwells."""
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    airborne_seen = False

    for fix in fixes:
        if current:
            gap = fix["snapshot_time"] - current[-1]["snapshot_time"]
            # A gap in coverage, or a return to the ground after having been airborne,
            # both end the leg.
            ended_on_ground = (
                airborne_seen and fix.get("on_ground") and current[-1].get("on_ground")
            )
            if gap > gap_seconds or ended_on_ground:
                segments.append(current)
                current, airborne_seen = [], False
        current.append(fix)
        if not fix.get("on_ground") and (fix.get("alt_baro") or 0) > 0:
            airborne_seen = True

    if current:
        segments.append(current)
    return segments


def _track_distance(fixes: Sequence[dict[str, Any]]) -> float:
    """Distance actually flown, summed fix to fix."""
    total = 0.0
    previous = None
    for fix in fixes:
        lat, lon = fix.get("latitude"), fix.get("longitude")
        if lat is None or lon is None:
            continue
        if previous is not None:
            total += haversine_nm(previous[0], previous[1], lat, lon)
        previous = (lat, lon)
    return total


def flight_legs(
    positions: Iterable[dict[str, Any]],
    airports: Sequence[Airport] = (),
    *,
    gap_seconds: int = DEFAULT_GAP_SECONDS,
    airport_radius_nm: float = DEFAULT_AIRPORT_RADIUS_NM,
    min_leg_seconds: int = MIN_LEG_SECONDS,
) -> list[dict[str, Any]]:
    """Group position fixes into legs. Returns rows for ``fct_flight_leg``."""
    by_aircraft: dict[str, list[dict[str, Any]]] = {}
    for row in positions:
        hex_id = row.get("hex")
        if hex_id and row.get("snapshot_time") is not None:
            by_aircraft.setdefault(hex_id, []).append(row)

    legs: list[dict[str, Any]] = []
    for hex_id, fixes in by_aircraft.items():
        fixes.sort(key=lambda r: r["snapshot_time"])

        for segment in _segments(fixes, gap_seconds):
            airborne = [f for f in segment if not f.get("on_ground")]
            if not airborne:
                continue

            start, end = segment[0], segment[-1]
            duration = end["snapshot_time"] - start["snapshot_time"]
            if duration < min_leg_seconds:
                continue

            # Endpoints are only claimed where the aircraft was genuinely seen on the ground.
            dep_observed = bool(start.get("on_ground"))
            arr_observed = bool(end.get("on_ground"))
            dep_airport, _ = (
                nearest_airport(
                    start.get("latitude"), start.get("longitude"), airports, airport_radius_nm
                )
                if dep_observed
                else (None, None)
            )
            arr_airport, _ = (
                nearest_airport(
                    end.get("latitude"), end.get("longitude"), airports, airport_radius_nm
                )
                if arr_observed
                else (None, None)
            )

            flown = _track_distance(segment)
            direct = None
            if all(
                v is not None
                for v in (
                    start.get("latitude"),
                    start.get("longitude"),
                    end.get("latitude"),
                    end.get("longitude"),
                )
            ):
                direct = haversine_nm(
                    start["latitude"], start["longitude"], end["latitude"], end["longitude"]
                )

            altitudes = [f["alt_baro"] for f in segment if isinstance(f.get("alt_baro"), int)]
            speeds = [
                f["ground_speed"] for f in segment if isinstance(f.get("ground_speed"), int | float)
            ]
            callsigns = [f["callsign"] for f in segment if f.get("callsign")]

            legs.append(
                {
                    "leg_id": f"{hex_id}-{start['snapshot_time']}",
                    "hex": hex_id,
                    "callsign": callsigns[0] if callsigns else None,
                    "registration": next(
                        (f["registration"] for f in segment if f.get("registration")), None
                    ),
                    "aircraft_type": next(
                        (f["aircraft_type"] for f in segment if f.get("aircraft_type")), None
                    ),
                    "hub": start.get("hub"),
                    "start_time": start["snapshot_time"],
                    "end_time": end["snapshot_time"],
                    "duration_s": duration,
                    "dep_airport": dep_airport,
                    "arr_airport": arr_airport,
                    "dep_observed": dep_observed,
                    "arr_observed": arr_observed,
                    # True only when both ends were actually witnessed on the ground.
                    "complete": dep_observed and arr_observed,
                    "fix_count": len(segment),
                    "max_alt_ft": max(altitudes) if altitudes else None,
                    "max_ground_speed_kt": max(speeds) if speeds else None,
                    "track_distance_nm": round(flown, 2),
                    "direct_distance_nm": round(direct, 2) if direct is not None else None,
                    # >1 means the aircraft flew further than the direct line between the first
                    # and last fix observed - vectoring, holding, or a track through the bubble.
                    "track_efficiency": round(flown / direct, 3) if direct and direct > 1 else None,
                    "emergency": next(
                        (f["emergency"] for f in segment if f.get("emergency")), None
                    ),
                }
            )

    legs.sort(key=lambda leg: (leg["start_time"], leg["hex"]))
    return legs
