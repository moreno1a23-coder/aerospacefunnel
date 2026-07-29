"""OpenSky aircraft database - keyless fleet registry, used as a gap-filler.

ADS-B already transmits registration and type inline, so this is *not* the primary source of
fleet identity. It does two things surveillance cannot:

* supplies identity for hexes that never transmitted a registration, and
* supplies **operator**, which ADS-B does not carry at all - the field any airline-level
  grouping depends on.

The CSV wraps values in single quotes rather than the double quotes stdlib `csv` strips, so
values are unquoted explicitly. The full file is large and changes slowly: partition daily
and refresh at most once a day.
"""

from __future__ import annotations

import csv
import io
import sys
from collections.abc import Iterable, Iterator
from typing import Any

import requests

# This dataset quotes with ' rather than ". Parsing it with the default quotechar makes any
# stray " swallow the rest of the file as a single field, which blows the csv field limit.
QUOTECHAR = "'"

# Free-text fields (notes, owner) occasionally exceed the stdlib default of 128 KiB.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

# Dated snapshots; the platform pins one rather than tracking "latest" so a rebuild is
# reproducible.
DEFAULT_URL = "https://opensky-network.org/datasets/metadata/aircraft-database-complete-2025-01.csv"


def _unquote(value: str | None) -> str | None:
    """Strip the single quotes this dataset wraps values in, and drop empties."""
    if value is None:
        return None
    cleaned = value.strip().strip("'").strip()
    return cleaned or None


class RegistrySource:
    """icao24 -> registration, type, operator, owner."""

    name = "registry"
    table = "dim_aircraft_registry"
    keys = ("icao24",)

    def __init__(self, url: str = DEFAULT_URL, only_identified: bool = True) -> None:
        self.url = url
        # Most of the file is placeholder rows with no usable identity at all.
        self.only_identified = only_identified

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        response = session.get(self.url, timeout=600)
        response.raise_for_status()
        yield {"csv": response.text}

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        reader = csv.DictReader(io.StringIO(payload.get("csv", "")), quotechar=QUOTECHAR)
        for row in reader:
            icao24 = _unquote(row.get("icao24"))
            if not icao24:
                continue

            registration = _unquote(row.get("registration"))
            typecode = _unquote(row.get("typecode"))
            # Deliberately no fallback to operatorIcao: `operator` is a display name and
            # operator_icao is the controlled vocabulary. Coalescing them splits one
            # carrier into two ("SWA" and "Southwest Airlines"), which is exactly what
            # airline-level grouping must not do. operator_icao is the grouping key.
            operator = _unquote(row.get("operator"))

            if self.only_identified and not any((registration, typecode, operator)):
                continue

            yield {
                "icao24": icao24.lower(),
                "registration": registration,
                "aircraft_type": typecode,
                "model": _unquote(row.get("model")),
                "manufacturer": _unquote(row.get("manufacturerName")),
                "operator": operator,
                "operator_icao": _unquote(row.get("operatorIcao")),
                "operator_iata": _unquote(row.get("operatorIata")),
                "operator_callsign": _unquote(row.get("operatorCallsign")),
                "owner": _unquote(row.get("owner")),
                "country": _unquote(row.get("country")),
                "category": _unquote(row.get("categoryDescription")),
                "built": _unquote(row.get("built")),
                "serial_number": _unquote(row.get("serialNumber")),
            }
