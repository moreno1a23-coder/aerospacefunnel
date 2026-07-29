"""Space weather from NOAA SWPC - keyless, and operationally relevant to polar routes.

Geomagnetic storms degrade HF communication and satellite navigation at high latitudes, which
is why carriers reroute polar flights during severe events. Kp is the standard index: 0-3 is
quiet, 5+ is a geomagnetic storm.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

import requests

KP_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

# Kp >= 5 is the storm threshold at which polar HF degradation becomes operationally material.
STORM_THRESHOLD = 5


def _epoch(time_tag: str | None) -> int | None:
    if not time_tag:
        return None
    try:
        return int(datetime.fromisoformat(time_tag).replace(tzinfo=UTC).timestamp())
    except ValueError:
        return None


class SpaceWeatherSource:
    """Planetary K-index, one-minute cadence."""

    name = "spaceweather"
    table = "fct_space_weather"
    keys = ("observed_at",)

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(KP_URL, timeout=45)
        response.raise_for_status()
        yield {"kp": response.json()}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for entry in payload.get("kp") or []:
            estimated = entry.get("estimated_kp")
            yield {
                "observed_at": _epoch(entry.get("time_tag")),
                "time_tag": entry.get("time_tag"),
                "kp_index": entry.get("kp_index"),
                "estimated_kp": estimated,
                "kp_label": entry.get("kp"),
                "storm": bool(estimated is not None and estimated >= STORM_THRESHOLD),
                "source": "noaa_swpc",
            }
