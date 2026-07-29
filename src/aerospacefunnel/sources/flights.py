"""OpenSky Network state vectors, with OAuth2 when credentials are available.

OpenSky removed basic authentication; the API now accepts only the OAuth2 client-credentials
flow. Anonymous access still works and is the default here, but it is capped at 400 credits
per day at 10-second resolution, against 4,000 per day at 5 seconds once authenticated.

The keyless ADS-B networks in :mod:`.adsb` are the platform's primary surveillance feed.
OpenSky is retained as an independent cross-check with different ground-station coverage.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

import requests

log = logging.getLogger("aerospacefunnel.opensky")

BASE_URL = "https://opensky-network.org/api/states/all"
TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)
# Tokens last 30 minutes; renew early so a request never races the expiry.
TOKEN_SAFETY_MARGIN_S = 120

# Index -> column, per the REST docs' state-vector table. The array has gained fields over
# time, so every read is bounds-checked rather than unpacked.
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


class TokenProvider:
    """Fetches and caches an OAuth2 bearer token."""

    def __init__(self, client_id: str, client_secret: str, token_url: str = TOKEN_URL):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self, session: requests.Session, *, force: bool = False) -> str:
        if not force and self._token and time.time() < self._expires_at:
            return self._token

        response = session.post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + payload.get("expires_in", 1800) - TOKEN_SAFETY_MARGIN_S
        log.info("acquired OpenSky token, valid ~%ss", payload.get("expires_in", 1800))
        return self._token


class OpenSkySource:
    """A snapshot of state vectors, optionally within a bounding box."""

    name = "opensky"
    table = "fct_opensky_state"
    keys = ("icao24", "snapshot_time")

    def __init__(
        self,
        bbox: tuple[float, float, float, float] | None = None,
        token_provider: TokenProvider | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.bbox = bbox
        self.token_provider = token_provider
        self.base_url = base_url

    @property
    def authenticated(self) -> bool:
        return self.token_provider is not None

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {}
        if self.bbox:
            lamin, lomin, lamax, lomax = self.bbox
            params = {"lamin": lamin, "lomin": lomin, "lamax": lamax, "lomax": lomax}

        response = self._get(session, params)
        # A token can be revoked before its stated expiry; retry once with a fresh one
        # rather than failing an otherwise healthy poll.
        if response.status_code == 401 and self.token_provider is not None:
            log.info("OpenSky returned 401; refreshing token and retrying once")
            response = self._get(session, params, force_token=True)

        response.raise_for_status()
        payload = response.json()
        payload["_authenticated"] = self.authenticated
        yield payload

    def _get(self, session, params, *, force_token: bool = False):
        headers = {}
        if self.token_provider is not None:
            headers["Authorization"] = (
                f"Bearer {self.token_provider.token(session, force=force_token)}"
            )
        return session.get(self.base_url, params=params, headers=headers, timeout=60)

    @staticmethod
    def _at(state: Sequence[Any], index: int) -> Any:
        return state[index] if index < len(state) else None

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        snapshot_time = payload.get("time")
        for state in payload.get("states") or []:
            row: dict[str, Any] = {f: self._at(state, i) for i, f in enumerate(FIELDS)}
            row["snapshot_time"] = snapshot_time
            row["authenticated"] = bool(payload.get("_authenticated"))

            if isinstance(row["callsign"], str):
                row["callsign"] = row["callsign"].strip() or None
            sensors = row.pop("sensors")
            row["sensor_count"] = len(sensors) if isinstance(sensors, list) else None
            row["on_ground"] = _as_bool(row["on_ground"])
            row["spi"] = _as_bool(row["spi"])
            yield row


def _as_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)
