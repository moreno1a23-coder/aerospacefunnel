"""Shared HTTP session: retries, backoff, and a polite User-Agent.

Both upstream APIs rate-limit anonymous callers (Launch Library 2 especially —
roughly 15 requests/hour without a token), so 429 is an expected response, not an
exception. The retry policy below honours ``Retry-After`` when the server sends it.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__

USER_AGENT = f"aerospacefunnel/{__version__} (+https://github.com/moreno1a23-coder/aerospacefunnel)"

RETRY_STATUSES = (429, 500, 502, 503, 504)


def build_session(total_retries: int = 4, backoff_factor: float = 1.5) -> requests.Session:
    """Return a Session that retries transient failures with exponential backoff."""
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset(["GET"]),
        backoff_factor=backoff_factor,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session
