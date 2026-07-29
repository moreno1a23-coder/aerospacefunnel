"""Platform configuration, loaded from TOML via the standard library.

`tomllib` ships with Python 3.11+, so configuration costs no dependency.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path("config/platform.toml")


@dataclass(frozen=True)
class Hub:
    """An airport we watch, and how far out."""

    icao: str
    radius_nm: float
    name: str = ""


@dataclass(frozen=True)
class Config:
    storage_root: Path
    warehouse: Path
    cadence_seconds: int
    primary_feed: str
    failover_feeds: tuple[str, ...]
    raw_days: int
    rollup_days: int
    ll2_base_url: str
    ll2_dev_url: str
    hubs: tuple[Hub, ...] = field(default_factory=tuple)

    def hub(self, icao: str) -> Hub:
        for h in self.hubs:
            if h.icao.upper() == icao.upper():
                return h
        raise KeyError(f"hub {icao!r} is not in the config; add it to [[hubs]]")

    @property
    def feed_order(self) -> tuple[str, ...]:
        """Primary first, then failovers - the order the poller tries them in."""
        return (self.primary_feed, *self.failover_feeds)


def load(path: str | Path = DEFAULT_CONFIG) -> Config:
    """Read and validate the platform config."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")

    with p.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    storage = raw.get("storage", {})
    surv = raw.get("surveillance", {})
    ret = raw.get("retention", {})
    ll2 = raw.get("launch_library", {})

    hubs = tuple(
        Hub(
            icao=h["icao"].upper(),
            radius_nm=float(h.get("radius_nm", 250)),
            name=h.get("name", ""),
        )
        for h in raw.get("hubs", [])
    )
    if not hubs:
        raise ValueError("config defines no [[hubs]]; surveillance would poll nothing")

    cadence = int(surv.get("cadence_seconds", 60))
    if cadence < 5:
        # Below the upstream refresh rate this only wastes requests on community feeds.
        raise ValueError(f"cadence_seconds={cadence} is faster than any upstream refreshes")

    return Config(
        storage_root=Path(storage.get("root", "data")),
        warehouse=Path(storage.get("warehouse", "data/warehouse.duckdb")),
        cadence_seconds=cadence,
        primary_feed=surv.get("primary", "adsb.lol"),
        failover_feeds=tuple(surv.get("failover", [])),
        raw_days=int(ret.get("raw_days", 30)),
        rollup_days=int(ret.get("rollup_days", 365)),
        ll2_base_url=ll2.get("base_url", "https://ll.thespacedevs.com/2.2.0"),
        ll2_dev_url=ll2.get("dev_url", "https://lldev.thespacedevs.com/2.2.0"),
        hubs=hubs,
    )
