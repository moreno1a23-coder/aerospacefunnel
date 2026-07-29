"""OpenSky Network — live ADS-B state vectors.

Docs: https://openskynetwork.github.io/opensky-api/rest.html

``/states/all`` returns each aircraft as a positional array, not an object, so the
field order below *is* the schema. OpenSky has appended fields over time (``category``
at index 17 is newer than the original 17-element row), which is why every read goes
through :meth:`_at` instead of unpacking — a short row yields ``None``, not IndexError.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import requests

BASE_URL = "https://opensky-network.org/api/states/all"

# Index -> column, per the REST docs' state-vector table.
FIELDS = [
    "icao24",
    "callsign",
    "origin_country",
    "time_position",
    "last_contact",
    "longitude",
    "latitude",
    "baro_altitude",
    "on_ground",
    "velocity",
    "true_track",
    "vertical_rate",
    "sensors",
    "geo_altitude",
    "squawk",
    "spi",
    "position_source",
]


class FlightsSource:
    """One snapshot of every aircraft currently inside a bounding box."""

    name = "flights"
    table = "flight_state"

    def __init__(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        # (lamin, lomin, lamax, lomax) — omit for global, which is a much heavier call.
        self.bbox = bbox
        self.base_url = base_url

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if self.bbox:
            lamin, lomin, lamax, lomax = self.bbox
            params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}
        response = session.get(self.base_url, params=params, timeout=60)
        response.raise_for_status()
        yield response.json()

    @staticmethod
    def _at(state: Sequence[Any], index: int) -> Any:
        return state[index] if index < len(state) else None

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        # `time` is the server's snapshot instant; it completes the primary key, since
        # one aircraft legitimately appears in many snapshots.
        snapshot_time = payload.get("time")
        for state in payload.get("states") or []:
            row: dict[str, Any] = {
                field: self._at(state, index) for index, field in enumerate(FIELDS)
            }
            row["snapshot_time"] = snapshot_time
            # Callsigns are space-padded to 8 chars by the transponder.
            if isinstance(row["callsign"], str):
                row["callsign"] = row["callsign"].strip() or None
            # `sensors` is a serial-number array; the warehouse keeps a count, not the list.
            sensors = row.pop("sensors")
            row["sensor_count"] = len(sensors) if isinstance(sensors, list) else None
            row["on_ground"] = _as_int_bool(row["on_ground"])
            row["spi"] = _as_int_bool(row["spi"])
            yield row


def _as_int_bool(value: Any) -> int | None:
    """SQLite has no bool type; store 0/1 and keep NULL distinct from False."""
    if value is None:
        return None
    return int(bool(value))
