"""Source protocol.

A source owns two steps of the funnel: ``extract`` (talk to the network, return raw
payloads) and ``transform`` (turn one raw payload into normalised rows). Keeping them
together means the payload shape only has to be understood in one file, and the
transform stays unit-testable against a saved fixture with no network involved.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, Protocol

import requests


class Source(Protocol):
    """Everything the pipeline needs to know about a data source."""

    name: str
    table: str

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        """Yield raw JSON payloads (one per HTTP page)."""

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        """Turn one raw payload into rows matching ``self.table``."""
