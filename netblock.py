"""Pytest plugin that denies all network access.

The suite is supposed to be entirely offline - every transform is tested against a captured
fixture, and the failover and OAuth2 paths use stand-in sessions. This plugin proves it
rather than assuming it, so a test that quietly starts calling a live API fails loudly
instead of making the build depend on someone else's uptime.

Run with:  pytest -p netblock
"""

from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise RuntimeError("network access attempted in an offline test suite")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
