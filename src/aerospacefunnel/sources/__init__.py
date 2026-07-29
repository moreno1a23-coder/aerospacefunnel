"""Data sources, grouped by the operational domain they serve.

Every source implements the same contract: `extract` yields raw payloads, `transform` turns
one payload into rows, `table` names the target and `keys` gives its natural key. Keeping
extract and transform together means a payload shape is understood in exactly one file, and
the transform stays testable against a saved fixture with no network involved.
"""

from __future__ import annotations

from .adsb import SurveillanceSource
from .airports import AirportsSource, RunwaysSource
from .disruption import DisruptionSource
from .flights import OpenSkySource, TokenProvider
from .hazards import GairmetSource, SigmetSource
from .launches import LaunchUpdateSource, LaunchWindowSource
from .orbital import OrbitalSource
from .spaceweather import SpaceWeatherSource
from .weather import MetarSource, TafSource

# Sources the CLI can run by name. Surveillance is excluded because it is instantiated
# per hub from config rather than as a single global source.
SOURCES = {
    "metar": MetarSource,
    "taf": TafSource,
    "sigmet": SigmetSource,
    "gairmet": GairmetSource,
    "disruption": DisruptionSource,
    "airports": AirportsSource,
    "runways": RunwaysSource,
    "launches": LaunchWindowSource,
    "launch_updates": LaunchUpdateSource,
    "orbital": OrbitalSource,
    "spaceweather": SpaceWeatherSource,
    "opensky": OpenSkySource,
}

# Which layer each table belongs to, for warehouse view creation.
TABLE_LAYERS = {
    "fct_position": "silver",
    "fct_weather_obs": "silver",
    "fct_weather_forecast": "silver",
    "fct_hazard": "silver",
    "fct_disruption": "silver",
    "fct_launch_window": "silver",
    "fct_launch_update": "silver",
    "fct_orbital_element": "silver",
    "fct_space_weather": "silver",
    "fct_opensky_state": "silver",
    "dim_airport": "silver",
    "dim_runway": "silver",
    "fct_flight_leg": "gold",
}

__all__ = [
    "SOURCES",
    "TABLE_LAYERS",
    "AirportsSource",
    "DisruptionSource",
    "GairmetSource",
    "LaunchUpdateSource",
    "LaunchWindowSource",
    "MetarSource",
    "OpenSkySource",
    "OrbitalSource",
    "RunwaysSource",
    "SigmetSource",
    "SpaceWeatherSource",
    "SurveillanceSource",
    "TafSource",
    "TokenProvider",
]
