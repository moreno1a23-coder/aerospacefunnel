"""Aerodrome weather from NOAA's Aviation Weather Center - observations and forecasts.

Keyless and fast (measured 0.17s for a METAR call). `fltCat` is the operationally decisive
field: VFR / MVFR / IFR / LIFR drives approach minima, diversion planning and delay risk.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests

METAR_URL = "https://aviationweather.gov/api/data/metar"
TAF_URL = "https://aviationweather.gov/api/data/taf"

# A ceiling is the lowest broken or overcast layer; scattered and few do not count.
CEILING_COVERS = {"BKN", "OVC", "OVX"}


def _visibility(value: Any) -> float | None:
    """`visib` is numeric, or a string like "10+" meaning "10 statute miles or more"."""
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().rstrip("+")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _ceiling(clouds: Any) -> int | None:
    """Lowest broken/overcast base in feet AGL, or None for no ceiling."""
    if not isinstance(clouds, list):
        return None
    bases = [
        layer.get("base")
        for layer in clouds
        if isinstance(layer, dict)
        and layer.get("cover") in CEILING_COVERS
        and isinstance(layer.get("base"), int | float)
    ]
    return int(min(bases)) if bases else None


class MetarSource:
    """Current observations for the configured stations."""

    name = "metar"
    table = "fct_weather_obs"
    keys = ("station", "obs_time")

    def __init__(self, stations: Iterable[str]) -> None:
        self.stations = [s.upper() for s in stations]

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(
            METAR_URL, params={"ids": ",".join(self.stations), "format": "json"}, timeout=45
        )
        response.raise_for_status()
        # The endpoint returns a bare list; wrap it so bronze payloads stay uniform objects.
        yield {"observations": response.json()}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for ob in payload.get("observations") or []:
            yield {
                "station": ob.get("icaoId"),
                "obs_time": ob.get("obsTime"),
                "report_time": ob.get("reportTime"),
                "name": ob.get("name"),
                "latitude": ob.get("lat"),
                "longitude": ob.get("lon"),
                "elevation_m": ob.get("elev"),
                "flight_category": ob.get("fltCat"),
                "temp_c": ob.get("temp"),
                "dewpoint_c": ob.get("dewp"),
                "wind_dir_deg": ob.get("wdir") if isinstance(ob.get("wdir"), int) else None,
                "wind_speed_kt": ob.get("wspd"),
                "wind_gust_kt": ob.get("wgst"),
                "visibility_sm": _visibility(ob.get("visib")),
                "ceiling_ft": _ceiling(ob.get("clouds")),
                "altimeter_hpa": ob.get("altim"),
                "sea_level_pressure": ob.get("slp"),
                "raw": ob.get("rawOb"),
            }


class TafSource:
    """Terminal aerodrome forecasts, flattened to one row per forecast period."""

    name = "taf"
    table = "fct_weather_forecast"
    keys = ("station", "issue_time", "period_from")

    def __init__(self, stations: Iterable[str]) -> None:
        self.stations = [s.upper() for s in stations]

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(
            TAF_URL, params={"ids": ",".join(self.stations), "format": "json"}, timeout=45
        )
        response.raise_for_status()
        yield {"forecasts": response.json()}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for taf in payload.get("forecasts") or []:
            station = taf.get("icaoId")
            issue_time = taf.get("issueTime")
            # A TAF carries several forecast periods; one row each keeps the grain usable
            # for "what was forecast for hour H" joins.
            for period in taf.get("fcsts") or []:
                yield {
                    "station": station,
                    "issue_time": issue_time,
                    "period_from": period.get("timeFrom"),
                    "period_to": period.get("timeTo"),
                    "valid_from": taf.get("validTimeFrom"),
                    "valid_to": taf.get("validTimeTo"),
                    "change_indicator": period.get("fcstChange"),
                    "probability": period.get("probability"),
                    "wind_dir_deg": period.get("wdir")
                    if isinstance(period.get("wdir"), int)
                    else None,
                    "wind_speed_kt": period.get("wspd"),
                    "wind_gust_kt": period.get("wgst"),
                    "wind_shear_hgt": period.get("wshearHgt"),
                    "visibility_sm": _visibility(period.get("visib")),
                    "ceiling_ft": _ceiling(period.get("clouds")),
                    "vertical_vis_ft": period.get("vertVis"),
                    "weather": period.get("wxString"),
                    "raw": taf.get("rawTAF"),
                }
