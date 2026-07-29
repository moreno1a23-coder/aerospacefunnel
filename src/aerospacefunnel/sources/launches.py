"""Launch Library 2 — orbital and suborbital launch records.

Docs: https://ll.thespacedevs.com/2.2.0/swagger/

``mode=list`` returns flat scalars for the nested objects (``orbit`` is the string
"Low Earth Orbit" rather than ``{"name": ...}``). ``mode=detailed`` returns the nested
objects instead, so :func:`_flat` accepts either shape — that way switching modes
later doesn't silently write ``None`` into every one of those columns.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

import requests

BASE_URL = "https://ll.thespacedevs.com/2.2.0/launch/"
PAGE_SIZE = 100


def _flat(value: Any, key: str = "name") -> str | None:
    """Read a field that the API returns as either a plain string or a nested object."""
    if value is None:
        return None
    if isinstance(value, dict):
        got = value.get(key)
        return str(got) if got is not None else None
    return str(value)


class LaunchesSource:
    """Paginated pull of launch records, newest-first."""

    name = "launches"
    table = "launch"

    def __init__(
        self,
        limit: int = PAGE_SIZE,
        max_pages: int = 1,
        search: str | None = None,
        base_url: str = BASE_URL,
    ) -> None:
        self.limit = min(limit, PAGE_SIZE)
        self.max_pages = max_pages
        self.search = search
        self.base_url = base_url

    def extract(self, session: requests.Session) -> Iterator[dict[str, Any]]:
        params: dict[str, Any] = {"limit": self.limit, "mode": "list", "ordering": "-net"}
        if self.search:
            params["search"] = self.search

        url: str | None = self.base_url
        for _ in range(self.max_pages):
            if url is None:
                break
            response = session.get(url, params=params if url == self.base_url else None, timeout=60)
            response.raise_for_status()
            payload = response.json()
            yield payload
            # The `next` link already carries limit/offset/mode, so params must not be
            # re-applied to it or the offset gets overwritten and page 2 repeats page 1.
            url = payload.get("next")

    def transform(self, payload: dict[str, Any]) -> Iterable[dict[str, Any]]:
        for item in payload.get("results", []):
            yield {
                "id": item["id"],
                "slug": item.get("slug"),
                "name": item.get("name"),
                "status": _flat(item.get("status")),
                "status_abbrev": _flat(item.get("status"), "abbrev"),
                "net": item.get("net"),
                "window_start": item.get("window_start"),
                "window_end": item.get("window_end"),
                "provider": _flat(item.get("lsp_name") or item.get("launch_service_provider")),
                "mission": _flat(item.get("mission")),
                "mission_type": item.get("mission_type"),
                "pad": _flat(item.get("pad")),
                "location": _flat(item.get("location")),
                "orbit": _flat(item.get("orbit")),
                "launcher": _flat(item.get("launcher") or item.get("rocket")),
                "image_url": item.get("image"),
                "last_updated": item.get("last_updated"),
            }
