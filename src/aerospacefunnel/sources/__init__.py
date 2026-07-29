"""Available sources, keyed by the name the CLI accepts."""

from __future__ import annotations

from .base import Source
from .flights import FlightsSource
from .launches import LaunchesSource

SOURCES = {
    LaunchesSource.name: LaunchesSource,
    FlightsSource.name: FlightsSource,
}

__all__ = ["SOURCES", "FlightsSource", "LaunchesSource", "Source"]
