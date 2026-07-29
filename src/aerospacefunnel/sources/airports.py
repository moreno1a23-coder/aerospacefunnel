"""Aerodrome and runway reference data from OurAirports (public domain).

This is the conformed geography every other fact joins to: without it a position fix is a
coordinate, not an arrival. Full files are ~12.7 MB (airports) and ~4.0 MB (runways), so this
is a slow-changing reference refreshed daily at most, never on the surveillance cadence.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from typing import Any

import requests

AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

# Aerodromes that can take commercial traffic; heliports and closed strips are noise here.
OPERATIONAL_TYPES = {"large_airport", "medium_airport", "small_airport"}


def _f(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _i(value: str | None) -> int | None:
    try:
        return int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class AirportsSource:
    """Global aerodrome reference."""

    name = "airports"
    table = "dim_airport"
    keys = ("ident",)

    def __init__(self, url: str = AIRPORTS_URL, operational_only: bool = True) -> None:
        self.url = url
        self.operational_only = operational_only

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(self.url, timeout=180)
        response.raise_for_status()
        yield {"csv": response.text}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for row in csv.DictReader(io.StringIO(payload.get("csv", ""))):
            kind = row.get("type")
            if self.operational_only and kind not in OPERATIONAL_TYPES:
                continue
            yield {
                "ident": row.get("ident"),
                "icao": row.get("icao_code") or None,
                "iata": row.get("iata_code") or None,
                "name": row.get("name"),
                "kind": kind,
                "latitude": _f(row.get("latitude_deg")),
                "longitude": _f(row.get("longitude_deg")),
                "elevation_ft": _i(row.get("elevation_ft")),
                "continent": row.get("continent") or None,
                "iso_country": row.get("iso_country") or None,
                "iso_region": row.get("iso_region") or None,
                "municipality": row.get("municipality") or None,
                "scheduled_service": row.get("scheduled_service") == "yes",
            }


class RunwaysSource:
    """Runway reference - lengths and surfaces bound which types can operate where."""

    name = "runways"
    table = "dim_runway"
    keys = ("runway_id",)

    def __init__(self, url: str = RUNWAYS_URL) -> None:
        self.url = url

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(self.url, timeout=180)
        response.raise_for_status()
        yield {"csv": response.text}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for row in csv.DictReader(io.StringIO(payload.get("csv", ""))):
            yield {
                "runway_id": row.get("id"),
                "airport_ident": row.get("airport_ident"),
                "length_ft": _i(row.get("length_ft")),
                "width_ft": _i(row.get("width_ft")),
                "surface": row.get("surface") or None,
                "lighted": row.get("lighted") == "1",
                "closed": row.get("closed") == "1",
                "le_ident": row.get("le_ident") or None,
                "le_latitude": _f(row.get("le_latitude_deg")),
                "le_longitude": _f(row.get("le_longitude_deg")),
                "le_heading_deg": _f(row.get("le_heading_degT")),
                "he_ident": row.get("he_ident") or None,
                "he_latitude": _f(row.get("he_latitude_deg")),
                "he_longitude": _f(row.get("he_longitude_deg")),
                "he_heading_deg": _f(row.get("he_heading_degT")),
            }
