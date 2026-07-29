"""Credential resolution: explicit argument -> environment -> .env file -> absent.

Absent is a normal state, never an error. Every source that can take a credential also has
an anonymous path, so the platform runs end-to-end with an empty `.env`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")


@dataclass(frozen=True)
class Credential:
    """One credential and where a user goes to get it."""

    name: str
    signup_url: str
    purpose: str
    required_with: tuple[str, ...] = ()  # names that must all be set together


# Everything the platform can use. None of it is required to run.
CREDENTIALS = (
    Credential(
        "OPENSKY_CLIENT_ID",
        "https://opensky-network.org/",
        "OpenSky OAuth2 - lifts 400 credits/day to 4,000 and 10s to 5s resolution",
        required_with=("OPENSKY_CLIENT_SECRET",),
    ),
    Credential(
        "OPENSKY_CLIENT_SECRET",
        "https://opensky-network.org/",
        "OpenSky OAuth2 client secret",
        required_with=("OPENSKY_CLIENT_ID",),
    ),
    Credential(
        "FAA_NOTAM_CLIENT_ID",
        "https://api.faa.gov/s/",
        "FAA NOTAM API - live notices to airmen",
        required_with=("FAA_NOTAM_CLIENT_SECRET",),
    ),
    Credential(
        "FAA_NOTAM_CLIENT_SECRET",
        "https://api.faa.gov/s/",
        "FAA NOTAM API client secret",
        required_with=("FAA_NOTAM_CLIENT_ID",),
    ),
    Credential(
        "EIA_API_KEY",
        "https://www.eia.gov/opendata/register.php",
        "EIA - jet fuel spot prices for cost analytics",
    ),
    Credential(
        "NASA_API_KEY",
        "https://api.nasa.gov/",
        "NASA - space weather (DONKI). Free key is 1,000/hr vs DEMO_KEY's 10",
    ),
    Credential(
        "LL2_TOKEN",
        "https://www.patreon.com/TheSpaceDevs",
        "Launch Library 2 - PAID (Patreon). No free tier above 15 req/hr",
    ),
)

BY_NAME = {c.name: c for c in CREDENTIALS}


def parse_env_file(text: str) -> dict[str, str]:
    """Parse KEY=value lines. Blank lines, `#` comments and `export ` prefixes are ignored."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        # Strip one matched pair of surrounding quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


class Credentials:
    """Resolved credential store. Missing values are `None`, never an exception."""

    def __init__(self, env_file: str | Path = DEFAULT_ENV_FILE, environ: dict | None = None):
        self._environ = dict(os.environ if environ is None else environ)
        self._file: dict[str, str] = {}
        path = Path(env_file)
        if path.exists():
            self._file = parse_env_file(path.read_text(encoding="utf-8"))

    def get(self, name: str, override: str | None = None) -> str | None:
        """Resolve one credential. Empty strings count as absent."""
        for candidate in (override, self._environ.get(name), self._file.get(name)):
            if candidate:
                return candidate
        return None

    def has(self, name: str) -> bool:
        return self.get(name) is not None

    def group_ready(self, name: str) -> bool:
        """True when this credential and everything it must pair with are all present."""
        cred = BY_NAME.get(name)
        if cred is None:
            return self.has(name)
        return self.has(name) and all(self.has(n) for n in cred.required_with)

    def status(self) -> list[tuple[Credential, bool]]:
        return [(c, self.has(c.name)) for c in CREDENTIALS]
