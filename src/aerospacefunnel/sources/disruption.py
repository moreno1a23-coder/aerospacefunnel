"""FAA National Airspace System status - the live operational disruption picture.

This is the feed that says *why* the network is degrading right now: ground delay programs,
ground stops, arrival/departure delays and airport closures, each with a cause. A live sample
carried a San Francisco ground delay programme, reason "low ceilings", average 41 minutes.

The response is XML whose shape differs per delay category (``Ground_Delay_List``,
``Ground_Stop_List``, ``Arrival_Departure_Delay_List``, ``Airport_Closure_List``). Rather than
hard-code each container, the parser walks for any element carrying an ``ARPT`` child and
flattens its leaves - so a category the FAA adds later still lands instead of being dropped.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

import requests

NAS_STATUS_URL = "https://nasstatus.faa.gov/api/airport-status-information"

# e.g. "Wed Jul 29 14:41:01 2026 GMT"
UPDATE_TIME_FORMAT = "%a %b %d %H:%M:%S %Y %Z"


def parse_update_time(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = text.strip().removesuffix(" GMT").strip()
    try:
        return int(
            datetime.strptime(cleaned, "%a %b %d %H:%M:%S %Y").replace(tzinfo=UTC).timestamp()
        )
    except ValueError:
        return None


def _leaves(element: ET.Element, prefix: str = "") -> dict[str, str]:
    """Flatten an element's descendants into ``{tag: text}``.

    Attributes matter here: ``<Arrival_Departure Type="Departure">`` is what distinguishes an
    arrival delay from a departure delay, so the attribute becomes part of the key.
    """
    out: dict[str, str] = {}
    for child in element:
        name = child.tag.lower()
        if child.attrib:
            name = f"{name}_{'_'.join(str(v).lower() for v in child.attrib.values())}"
        key = f"{prefix}{name}"
        text = (child.text or "").strip()
        if text:
            out[key] = text
        out.update(_leaves(child, prefix=f"{key}_"))
    return out


class DisruptionSource:
    """Current ground delay programmes, ground stops, delays and closures."""

    name = "disruption"
    table = "fct_disruption"
    keys = ("airport", "delay_type", "observed_at")

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(NAS_STATUS_URL, timeout=45)
        response.raise_for_status()
        # Bronze keeps the XML verbatim; parsing happens in transform so a parser fix can be
        # replayed against archived payloads.
        yield {"xml": response.text}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        raw = payload.get("xml")
        if not raw:
            return

        root = ET.fromstring(raw)
        observed_at = parse_update_time(root.findtext("Update_Time"))

        for block in root.findall("Delay_type"):
            category = (block.findtext("Name") or "").strip()

            for element in block.iter():
                arpt = element.findtext("ARPT")
                if not arpt:
                    continue
                fields = _leaves(element)
                fields.pop("arpt", None)

                yield {
                    "airport": arpt.strip().upper(),
                    "delay_type": category,
                    "observed_at": observed_at,
                    "reason": fields.pop("reason", None),
                    "avg_delay": fields.pop("avg", None),
                    "max_delay": fields.pop("max", None),
                    "min_delay": fields.pop("min", None),
                    "arrival_delay_min": fields.pop("arrival_departure_arrival_min", None),
                    "arrival_delay_max": fields.pop("arrival_departure_arrival_max", None),
                    "departure_delay_min": fields.pop("arrival_departure_departure_min", None),
                    "departure_delay_max": fields.pop("arrival_departure_departure_max", None),
                    "end_time": fields.pop("end_time", None),
                    "reopen_time": fields.pop("reopen", None),
                    # Anything the FAA adds that this schema does not name yet.
                    "extra": "; ".join(f"{k}={v}" for k, v in sorted(fields.items())) or None,
                }
