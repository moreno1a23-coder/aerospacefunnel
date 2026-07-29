"""Orbital catalogue from CelesTrak - keyless GP (general perturbation) element sets.

``OBJECT_ID`` is the international designator (e.g. ``2026-045A``), whose first component is
the launch year and sequence. That is the join key from a launch window to the objects it
actually placed in orbit.

Repeated epochs for the same NORAD id are what expose decay: mean motion rises and BSTAR
grows as an object loses altitude, so the table is keyed on ``(norad_cat_id, epoch)`` and
accumulates history rather than overwriting.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests

GP_URL = "https://celestrak.org/NORAD/elements/gp.php"

# Minutes per day / mean motion (revs per day) = orbital period in minutes.
MINUTES_PER_DAY = 1440.0


def _f(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _launch_designator(object_id: str | None) -> str | None:
    """``2026-045A`` -> ``2026-045``: the launch, with the per-object suffix dropped.

    Every object from one launch shares this prefix, so it is the grain that joins the
    orbital catalogue back to a launch window.
    """
    if not object_id or "-" not in object_id:
        return None
    year, _, sequence = object_id.partition("-")
    digits = "".join(c for c in sequence if c.isdigit())
    return f"{year}-{digits}" if digits else None


class OrbitalSource:
    """GP element sets for a CelesTrak group."""

    name = "orbital"
    table = "fct_orbital_element"
    keys = ("norad_cat_id", "epoch")

    def __init__(self, group: str = "last-30-days", url: str = GP_URL) -> None:
        self.group = group
        self.url = url

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(
            self.url, params={"GROUP": self.group, "FORMAT": "json"}, timeout=120
        )
        response.raise_for_status()
        yield {"objects": response.json(), "_group": self.group}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        group = payload.get("_group", self.group)
        for obj in payload.get("objects") or []:
            mean_motion = _f(obj.get("MEAN_MOTION"))
            object_id = obj.get("OBJECT_ID")
            yield {
                "norad_cat_id": obj.get("NORAD_CAT_ID"),
                "epoch": obj.get("EPOCH"),
                "object_name": obj.get("OBJECT_NAME"),
                "object_id": object_id,
                "launch_designator": _launch_designator(object_id),
                "launch_year": int(object_id[:4])
                if object_id and object_id[:4].isdigit()
                else None,
                "mean_motion": mean_motion,
                "eccentricity": _f(obj.get("ECCENTRICITY")),
                "inclination": _f(obj.get("INCLINATION")),
                "ra_of_asc_node": _f(obj.get("RA_OF_ASC_NODE")),
                "arg_of_pericenter": _f(obj.get("ARG_OF_PERICENTER")),
                "mean_anomaly": _f(obj.get("MEAN_ANOMALY")),
                "bstar": _f(obj.get("BSTAR")),
                "mean_motion_dot": _f(obj.get("MEAN_MOTION_DOT")),
                "rev_at_epoch": obj.get("REV_AT_EPOCH"),
                "period_minutes": round(MINUTES_PER_DAY / mean_motion, 3) if mean_motion else None,
                "group": group,
            }
