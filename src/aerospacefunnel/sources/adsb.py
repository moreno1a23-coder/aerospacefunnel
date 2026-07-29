"""Live ADS-B surveillance from keyless community networks.

Three independent feeds serve the same shape (``{"ac": [...], "now": <epoch_ms>}``). They are
tried in configured order and the first success wins; the feed that answered is recorded on
every row, so a coverage gap is attributable to a specific network rather than appearing as
traffic that simply did not exist.

These are donated, community-run networks. Poll them politely.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

import requests

log = logging.getLogger("aerospacefunnel.adsb")

# Each feed exposes a point-radius query; only the path shape differs.
FEEDS: dict[str, str] = {
    "adsb.lol": "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{radius}",
    "airplanes.live": "https://api.airplanes.live/v2/point/{lat}/{lon}/{radius}",
    "adsb.fi": "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{radius}",
}

# Transponder codes that mean something is wrong, in every airspace on earth.
EMERGENCY_SQUAWKS = {"7500": "unlawful_interference", "7600": "radio_failure", "7700": "emergency"}


class FeedUnavailable(Exception):
    """Every configured feed failed."""


class SurveillanceSource:
    """One point-radius snapshot of a hub, from the first feed that answers."""

    name = "surveillance"
    table = "fct_position"
    keys = ("hex", "snapshot_time")

    def __init__(
        self,
        hub_icao: str,
        latitude: float,
        longitude: float,
        radius_nm: float,
        feed_order: Iterable[str] = ("adsb.lol", "airplanes.live", "adsb.fi"),
    ) -> None:
        self.hub_icao = hub_icao.upper()
        self.latitude = latitude
        self.longitude = longitude
        self.radius_nm = radius_nm
        self.feed_order = tuple(feed_order)
        self.feed_used: str | None = None

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        errors: list[str] = []
        for feed in self.feed_order:
            template = FEEDS.get(feed)
            if template is None:
                errors.append(f"{feed}: not a known feed")
                continue
            url = template.format(lat=self.latitude, lon=self.longitude, radius=int(self.radius_nm))
            try:
                response = session.get(url, timeout=45)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - any failure means try the next feed
                errors.append(f"{feed}: {type(exc).__name__}: {exc}")
                log.warning("feed %s failed for %s: %s", feed, self.hub_icao, exc)
                continue

            self.feed_used = feed
            payload["_feed"] = feed
            payload["_hub"] = self.hub_icao
            yield payload
            return

        raise FeedUnavailable(f"all feeds failed for {self.hub_icao}: {'; '.join(errors)}")

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        # `now` is epoch milliseconds; the warehouse keeps whole seconds.
        snapshot_time = int(payload.get("now", 0) / 1000)
        feed = payload.get("_feed", "unknown")
        hub = payload.get("_hub", self.hub_icao)

        for ac in payload.get("ac") or []:
            hex_id = ac.get("hex")
            if not hex_id:
                continue

            alt_baro, on_ground = _altitude(ac.get("alt_baro"))
            squawk = ac.get("squawk")

            yield {
                "hex": hex_id.strip().lower(),
                "snapshot_time": snapshot_time,
                "hub": hub,
                "source": feed,
                "callsign": _clean(ac.get("flight")),
                "registration": _clean(ac.get("r")),
                "aircraft_type": _clean(ac.get("t")),
                "category": ac.get("category"),
                "latitude": _as_float(ac.get("lat")),
                "longitude": _as_float(ac.get("lon")),
                "alt_baro": alt_baro,
                "alt_geom": _as_int(ac.get("alt_geom")),
                "on_ground": on_ground,
                "ground_speed": _as_float(ac.get("gs")),
                "track": _as_float(ac.get("track")),
                "baro_rate": _as_int(ac.get("baro_rate")),
                "geom_rate": _as_int(ac.get("geom_rate")),
                "squawk": squawk,
                "emergency": _emergency(ac.get("emergency"), squawk),
                "nav_altitude_mcp": _as_int(ac.get("nav_altitude_mcp")),
                "nav_qnh": _as_float(ac.get("nav_qnh")),
                "distance_nm": _as_float(ac.get("dst")),
                "bearing_deg": _as_float(ac.get("dir")),
                "rssi": _as_float(ac.get("rssi")),
                "messages": _as_int(ac.get("messages")),
                "seen_pos": _as_float(ac.get("seen_pos")),
                "mlat": bool(ac.get("mlat")),
            }


def _altitude(value: Any) -> tuple[int | None, bool]:
    """readsb reports a ground aircraft as the string "ground", not a number.

    Measured on a live sample: 116 of 716 aircraft. Treating it as a null altitude would
    discard the only on-ground signal in the feed, which flight-leg derivation depends on.
    """
    if isinstance(value, str):
        return (None, True) if value.lower() == "ground" else (None, False)
    return _as_int(value), False


def _emergency(flag: Any, squawk: Any) -> str | None:
    """Normalise the emergency state, preferring an explicit squawk code."""
    if squawk in EMERGENCY_SQUAWKS:
        return EMERGENCY_SQUAWKS[squawk]
    if isinstance(flag, str) and flag.lower() not in ("none", ""):
        return flag.lower()
    return None


def _clean(value: Any) -> str | None:
    """Transponder strings are fixed-width and space padded."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
