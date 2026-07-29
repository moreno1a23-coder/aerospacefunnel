from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def launch_payload() -> dict:
    """A real Launch Library 2 `mode=list` response, captured from the live API."""
    return load_fixture("launchlibrary_list.json")


@pytest.fixture
def flight_payload() -> dict:
    """A real OpenSky /states/all response, captured from the live API."""
    return load_fixture("opensky_states.json")


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "test.db")
