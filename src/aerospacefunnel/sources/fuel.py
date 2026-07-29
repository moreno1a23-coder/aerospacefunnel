"""EIA jet fuel spot prices - the cost side of route efficiency.

Requires a free EIA key (probed live: HTTP 403 without one). Without it this source is
registered but inert, exactly like :mod:`.notam`.

Why it matters: `mart_route_efficiency` measures excess miles flown against the direct
great-circle distance. Excess miles are burnt fuel, and fuel has a price - so this is what
turns a dimensionless ratio into a number an accountant recognises.

Series ``EPJK`` is kerosene-type jet fuel, US Gulf Coast spot, quoted in dollars per gallon.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

import requests

log = logging.getLogger("aerospacefunnel.fuel")

BASE_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
JET_FUEL_PRODUCT = "EPJK"


class FuelPriceSource:
    """Daily jet fuel spot price."""

    name = "fuel"
    table = "fct_fuel_price"
    keys = ("period", "series")

    def __init__(
        self,
        api_key: str | None = None,
        product: str = JET_FUEL_PRODUCT,
        length: int = 365,
        base_url: str = BASE_URL,
    ) -> None:
        self.api_key = api_key
        self.product = product
        self.length = length
        self.base_url = base_url

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def throttle_key(self) -> str:
        return "eia"

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        if not self.configured:
            log.info("fuel: no EIA_API_KEY configured, skipping")
            return

        response = session.get(
            self.base_url,
            params={
                "api_key": self.api_key,
                "frequency": "daily",
                "data[0]": "value",
                "facets[product][]": self.product,
                "sort[0][column]": "period",
                "sort[0][direction]": "desc",
                "length": self.length,
            },
            timeout=90,
        )
        response.raise_for_status()
        yield response.json()

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        # EIA v2 wraps everything in a `response` envelope.
        for row in (payload.get("response") or {}).get("data") or []:
            period = row.get("period")
            series = row.get("series") or row.get("duoarea") or self.product
            if not period:
                continue
            yield {
                "period": period,
                "series": str(series),
                "product": row.get("product") or self.product,
                "product_name": row.get("product-name"),
                "area": row.get("area-name") or row.get("duoarea"),
                "price": _f(row.get("value")),
                "units": row.get("units"),
            }


def _f(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
