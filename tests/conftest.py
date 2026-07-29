from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# Every fixture below was captured from the live API on 2026-07-29, so transforms are tested
# against the shapes these services actually return rather than invented ones.


@pytest.fixture
def adsb_payload() -> dict:
    payload = load_json("adsb_lol.json")
    payload["_feed"] = "adsb.lol"
    payload["_hub"] = "KJFK"
    return payload


@pytest.fixture
def metar_payload() -> dict:
    return {"observations": load_json("metar.json")}


@pytest.fixture
def taf_payload() -> dict:
    return {"forecasts": load_json("taf.json")}


@pytest.fixture
def sigmet_payload() -> dict:
    return {"sigmets": load_json("sigmet.json")}


@pytest.fixture
def disruption_payload() -> dict:
    return {"xml": load_text("faa_nas_status.xml")}


@pytest.fixture
def launch_payload() -> dict:
    return load_json("ll2_detailed.json")


@pytest.fixture
def orbital_payload() -> dict:
    return {"objects": load_json("celestrak_gp.json"), "_group": "last-30-days"}


@pytest.fixture
def spaceweather_payload() -> dict:
    return {"kp": load_json("swpc_kp.json")}


@pytest.fixture
def airports_payload() -> dict:
    return {"csv": load_text("airports.csv")}


@pytest.fixture
def storage_root(tmp_path) -> str:
    return str(tmp_path / "data")


@pytest.fixture
def metadata_db(tmp_path) -> str:
    return str(tmp_path / "metadata.db")
