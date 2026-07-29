"""Launch Library 2 - launch windows, treated as an airspace-disruption input.

A launch closes airspace and range corridors, which forces reroutes and delays. That makes a
launch window an operational input to flight ops, not a separate curiosity.

Two rate-limit facts drive the design. Production is **15 requests/hour per IP** and there is
no free key above it (keys are Patreon-only), while the `lldev` mirror has no rate limit but
serves a stale, limited data set. Development and tests therefore point at `lldev` and
production quota is spent only on real pulls.

`mode=detailed` is used because it carries the fields the analytics need: pad latitude and
longitude (the join to weather), plus `probability`, `holdreason`, `failreason` and
`weather_concerns`.

Slip history comes free from the key design: rows are keyed on
``(launch_id, net, status)``, so each time a launch's NET moves it appends a new row instead
of overwriting the last one. The current state is a view over the newest row per launch.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests

PROD_URL = "https://ll.thespacedevs.com/2.2.0"
DEV_URL = "https://lldev.thespacedevs.com/2.2.0"
PAGE_SIZE = 100


def _flat(value: Any, key: str = "name") -> str | None:
    """Read a field the API returns as either a plain string or a nested object.

    `mode=list` gives bare strings where `mode=detailed` gives objects; accepting both means
    switching modes cannot silently null out a column.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        got = value.get(key)
        return str(got) if got is not None else None
    return str(value)


def _num(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class LaunchWindowSource:
    """Upcoming and recent launch windows with pad geography."""

    name = "launches"
    table = "fct_launch_window"
    keys = ("launch_id", "net", "status")

    def __init__(
        self,
        limit: int = 50,
        max_pages: int = 1,
        upcoming: bool = True,
        dev: bool = False,
        token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.limit = min(limit, PAGE_SIZE)
        self.max_pages = max_pages
        self.upcoming = upcoming
        self.dev = dev
        self.token = token
        root = base_url or (DEV_URL if dev else PROD_URL)
        self.base_url = f"{root}/launch/{'upcoming/' if upcoming else ''}"

    @property
    def throttle_key(self) -> str:
        """Which budget this pull spends - the dev mirror is not rate limited."""
        return "launchlibrary_dev" if self.dev else "launchlibrary"

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        headers = {"Authorization": f"Token {self.token}"} if self.token else {}
        params: dict[str, Any] = {"limit": self.limit, "mode": "detailed"}

        url: str | None = self.base_url
        for _ in range(self.max_pages):
            if url is None:
                break
            response = session.get(
                url,
                params=params if url == self.base_url else None,
                headers=headers,
                timeout=90,
            )
            response.raise_for_status()
            payload = response.json()
            yield payload
            # `next` already carries limit/offset/mode; re-applying params would reset the
            # offset and re-fetch page one forever.
            url = payload.get("next")

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for item in payload.get("results", []):
            pad = item.get("pad") or {}
            location = pad.get("location") or {}
            rocket = item.get("rocket") or {}
            configuration = rocket.get("configuration") if isinstance(rocket, dict) else None

            yield {
                "launch_id": item["id"],
                "net": item.get("net"),
                "status": _flat(item.get("status")),
                "status_abbrev": _flat(item.get("status"), "abbrev"),
                "observed_at": item.get("last_updated"),
                "name": item.get("name"),
                "slug": item.get("slug"),
                "provider": _flat(item.get("launch_service_provider")),
                "mission": _flat(item.get("mission")),
                "mission_type": _flat(item.get("mission"), "type"),
                "orbit": _flat(
                    (item.get("mission") or {}).get("orbit")
                    if isinstance(item.get("mission"), dict)
                    else None
                ),
                "vehicle": _flat(configuration) if configuration else _flat(rocket),
                "program": ", ".join(
                    p.get("name", "") for p in (item.get("program") or []) if isinstance(p, dict)
                )
                or None,
                "window_start": item.get("window_start"),
                "window_end": item.get("window_end"),
                "net_precision": _flat(item.get("net_precision")),
                # Weather-go probability and the reasons a launch held or failed - the
                # scrub-analysis columns.
                "probability": item.get("probability"),
                "weather_concerns": item.get("weather_concerns"),
                "hold_reason": item.get("holdreason") or None,
                "fail_reason": item.get("failreason") or None,
                "pad_name": pad.get("name"),
                "pad_latitude": _num(pad.get("latitude")),
                "pad_longitude": _num(pad.get("longitude")),
                "pad_location": location.get("name") if isinstance(location, dict) else None,
                "country_code": pad.get("country_code"),
                "pad_turnaround": item.get("pad_turnaround"),
                "orbital_attempt_count_year": item.get("orbital_launch_attempt_count_year"),
                "webcast_live": bool(item.get("webcast_live")),
            }


class LaunchUpdateSource(LaunchWindowSource):
    """The narrative update feed attached to each launch - why a date moved."""

    name = "launch_updates"
    table = "fct_launch_update"
    keys = ("update_id",)

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for item in payload.get("results", []):
            for update in item.get("updates") or []:
                yield {
                    "update_id": f"{item['id']}-{update.get('id')}",
                    "launch_id": item["id"],
                    "created_on": update.get("created_on"),
                    "comment": update.get("comment"),
                    "info_url": update.get("info_url"),
                    "created_by": update.get("created_by"),
                }
