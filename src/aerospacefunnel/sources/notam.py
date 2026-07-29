"""FAA NOTAM API - notices to airmen.

Requires a free FAA developer key (probed live: HTTP 401 without one). Without credentials
this source is registered but inert: :meth:`extract` yields nothing and the run reports as
skipped, never as an error. No credential is ever required to run the platform.

NOTAMs are the authoritative record of what is unusable at an aerodrome right now - closed
runways, out-of-service approach aids, obstacles, temporary flight restrictions. They pair
with the FAA NAS disruption feed: that one says a delay programme exists, this one says why
the runway it depends on is shut.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from typing import Any

import requests

log = logging.getLogger("aerospacefunnel.notam")

BASE_URL = "https://external-api.faa.gov/notamapi/v1/notams"
PAGE_SIZE = 50


class NotamSource:
    """NOTAMs for the configured aerodromes."""

    name = "notam"
    table = "fct_notam"
    keys = ("notam_id",)

    def __init__(
        self,
        locations: Iterable[str],
        client_id: str | None = None,
        client_secret: str | None = None,
        max_pages: int = 4,
        base_url: str = BASE_URL,
    ) -> None:
        self.locations = [loc.upper() for loc in locations]
        self.client_id = client_id
        self.client_secret = client_secret
        self.max_pages = max_pages
        self.base_url = base_url

    @property
    def configured(self) -> bool:
        """Both halves of the credential pair are required."""
        return bool(self.client_id and self.client_secret)

    @property
    def throttle_key(self) -> str:
        return "faa"

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        if not self.configured:
            log.info("notam: no FAA credentials configured, skipping")
            return

        headers = {"client_id": self.client_id, "client_secret": self.client_secret}
        for location in self.locations:
            page = 1
            while page <= self.max_pages:
                response = session.get(
                    self.base_url,
                    params={
                        "icaoLocation": location,
                        "pageNum": page,
                        "pageSize": PAGE_SIZE,
                    },
                    headers=headers,
                    timeout=60,
                )
                response.raise_for_status()
                payload = response.json()
                payload["_location"] = location
                yield payload

                # The API reports total pages; stop as soon as the last one is in.
                total = payload.get("totalPages") or 1
                if page >= total:
                    break
                page += 1

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        location = payload.get("_location")
        for item in payload.get("items") or []:
            # The payload nests the NOTAM proper inside properties.coreNOTAMData.notam.
            core = ((item.get("properties") or {}).get("coreNOTAMData") or {}).get("notam") or {}
            translations = ((item.get("properties") or {}).get("coreNOTAMData") or {}).get(
                "notamTranslation"
            ) or []
            text = next(
                (t.get("formattedText") or t.get("simpleText") for t in translations if t), None
            )

            notam_id = core.get("id") or core.get("number")
            if not notam_id:
                continue

            yield {
                "notam_id": str(notam_id),
                "number": core.get("number"),
                "location": core.get("location") or location,
                "icao_location": core.get("icaoLocation") or location,
                "type": core.get("type"),
                "classification": core.get("classification"),
                "effective_start": core.get("effectiveStart"),
                "effective_end": core.get("effectiveEnd"),
                "issued": core.get("issued"),
                "affected_fir": core.get("affectedFIR"),
                "selection_code": core.get("selectionCode"),
                "minimum_fl": core.get("minimumFL"),
                "maximum_fl": core.get("maximumFL"),
                "text": core.get("text"),
                "formatted_text": text,
            }
