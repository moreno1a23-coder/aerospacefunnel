"""Credential honesty, the SCD2 aircraft dimension, and the three credentialed sources."""

from __future__ import annotations

from pathlib import Path

import pytest

from aerospacefunnel.credentials import CREDENTIALS, Credentials
from aerospacefunnel.derive import aircraft_dimension
from aerospacefunnel.sources import SOURCES
from aerospacefunnel.sources.fuel import FuelPriceSource
from aerospacefunnel.sources.notam import NotamSource
from aerospacefunnel.sources.registry import RegistrySource

FIXTURES = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ credential honesty


def test_every_credential_names_a_source_that_exists():
    """The regression guard for the defect that prompted this work.

    `keys` used to advertise NASA/EIA/FAA keys that nothing in the codebase read, sending the
    user off to register for something with no effect. Every credential must name a real,
    registered source.
    """
    for cred in CREDENTIALS:
        assert cred.consumed_by, f"{cred.name} declares no consumer"
        assert cred.consumed_by in SOURCES, (
            f"{cred.name} claims to be consumed by {cred.consumed_by!r}, "
            f"which is not a registered source"
        )


def test_env_example_matches_the_declared_credentials():
    """`.env.example` is what the user actually copies - it must not drift from the code.

    A stale name there sends them to configure something nothing reads; a missing one hides a
    credential they could have used.
    """
    example = Path(__file__).resolve().parents[1] / ".env.example"
    declared = {c.name for c in CREDENTIALS}
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert documented == declared, (
        f"only in .env.example: {sorted(documented - declared)}; "
        f"only in CREDENTIALS: {sorted(declared - documented)}"
    )


def test_no_credential_is_required_to_run(tmp_path):
    creds = Credentials(env_file=tmp_path / "absent", environ={})
    assert all(not present for _, present in creds.status())


def test_paired_credentials_still_group(tmp_path):
    half = Credentials(env_file=tmp_path / "x", environ={"FAA_NOTAM_CLIENT_ID": "a"})
    assert half.group_ready("FAA_NOTAM_CLIENT_ID") is False


# ------------------------------------------------------------------------ SCD2 dimension


def fix(hex_id="abc123", t=0, reg="N1", typ="B738"):
    return {"hex": hex_id, "snapshot_time": t, "registration": reg, "aircraft_type": typ}


def test_dimension_is_built_from_surveillance_alone():
    """ADS-B carries registration and type inline - no registry call is needed."""
    rows = aircraft_dimension([fix(t=10), fix(t=20)])
    assert len(rows) == 1
    assert rows[0]["registration"] == "N1"
    assert rows[0]["is_current"] is True
    assert rows[0]["valid_to"] is None


def test_unchanged_identity_extends_rather_than_duplicating():
    first = aircraft_dimension([fix(t=10)])
    second = aircraft_dimension([fix(t=20)], existing=first)
    assert len(second) == 1
    assert second[0]["last_seen"] == 20
    assert second[0]["valid_from"] == 10


def test_reassigned_tail_opens_a_new_version_and_closes_the_old():
    """A hex can be reassigned to a different airframe.

    Overwriting in place would silently re-attribute every historical leg to whichever tail
    holds that hex today - the quiet corruption this dimension exists to prevent.
    """
    first = aircraft_dimension([fix(t=10, reg="N1")])
    second = aircraft_dimension([fix(t=100, reg="N2")], existing=first)

    assert len(second) == 2
    closed = next(r for r in second if not r["is_current"])
    current = next(r for r in second if r["is_current"])

    assert closed["registration"] == "N1"
    assert closed["valid_to"] == 100
    assert current["registration"] == "N2"
    assert current["valid_from"] == 100
    assert current["valid_to"] is None


def test_exactly_one_current_row_per_hex():
    rows = aircraft_dimension([fix(t=10, reg="N1")])
    rows = aircraft_dimension([fix(t=50, reg="N2")], existing=rows)
    rows = aircraft_dimension([fix(t=90, reg="N3")], existing=rows)
    assert len(rows) == 3
    assert sum(1 for r in rows if r["is_current"]) == 1


def test_aircraft_not_seen_this_run_are_preserved():
    existing = aircraft_dimension([fix("aaa", t=10)])
    rows = aircraft_dimension([fix("bbb", t=20)], existing=existing)
    assert {r["hex"] for r in rows} == {"aaa", "bbb"}


def test_enrichment_supplies_operator_which_adsb_never_carries():
    rows = aircraft_dimension(
        [fix(t=10)],
        enrichment={"abc123": {"operator": "Delta Air Lines", "operator_icao": "DAL"}},
    )
    assert rows[0]["operator"] == "Delta Air Lines"
    assert rows[0]["operator_icao"] == "DAL"


def test_fixes_without_any_identity_are_ignored():
    assert aircraft_dimension([{"hex": "abc", "snapshot_time": 1}]) == []
    assert aircraft_dimension([]) == []


def test_keys_are_unique():
    rows = aircraft_dimension([fix(t=10, reg="N1")])
    rows = aircraft_dimension([fix(t=50, reg="N2")], existing=rows)
    keys = [(r["hex"], r["valid_from"]) for r in rows]
    assert len(set(keys)) == len(keys)


# ----------------------------------------------------------------------------- registry


def test_registry_parses_the_single_quoted_dialect():
    """This dataset quotes with ' - the default quotechar makes a stray " eat the file."""
    payload = {"csv": (FIXTURES / "registry.csv").read_text(encoding="utf-8")}
    rows = list(RegistrySource().transform(payload))
    assert rows
    # Compare against the lowercased form: islower() is False for all-digit hexes ("000001").
    assert all(r["icao24"] and r["icao24"] == r["icao24"].lower() for r in rows)
    assert all(not (r["registration"] or "").startswith("'") for r in rows)


def test_registry_never_coalesces_operator_name_with_icao_code():
    """Coalescing splits one carrier into two ("SWA" and "Southwest Airlines")."""
    payload = {
        "csv": "'icao24','registration','typecode','operator','operatorIcao'\n"
        "'abc123','N1','B738',,'SWA'\n"
    }
    (row,) = RegistrySource().transform(payload)
    assert row["operator"] is None, "the display name must not be filled from the code"
    assert row["operator_icao"] == "SWA"


def test_registry_skips_rows_with_no_identity():
    payload = {
        "csv": "'icao24','registration','typecode','operator','operatorIcao'\n"
        "'000000',,,,\n'abc123','N1','B738',,\n"
    }
    rows = list(RegistrySource().transform(payload))
    assert [r["icao24"] for r in rows] == ["abc123"]


# -------------------------------------------------------------- credentialed, dormant


@pytest.mark.parametrize(
    "source,needed",
    [
        (NotamSource(["KJFK"]), "FAA_NOTAM"),
        (FuelPriceSource(), "EIA_API_KEY"),
    ],
)
def test_unconfigured_sources_are_inert_not_broken(source, needed):
    """No credential is ever required to run: extract yields nothing and raises nothing."""
    assert source.configured is False
    assert list(source.extract(session=None)) == []
    assert any(c.name.startswith(needed.split("_API")[0]) for c in CREDENTIALS)


def test_configured_flag_requires_both_notam_halves():
    assert NotamSource(["KJFK"], client_id="a").configured is False
    assert NotamSource(["KJFK"], client_id="a", client_secret="b").configured is True


def test_notam_transform_reads_the_nested_payload():
    payload = {
        "_location": "KJFK",
        "items": [
            {
                "properties": {
                    "coreNOTAMData": {
                        "notam": {
                            "id": "NOTAM_1_1",
                            "number": "07/123",
                            "icaoLocation": "KJFK",
                            "classification": "DOM",
                            "effectiveStart": "2026-07-29T00:00:00Z",
                            "text": "RWY 04L/22R CLSD",
                        },
                        "notamTranslation": [{"formattedText": "RUNWAY 04L/22R CLOSED"}],
                    }
                }
            }
        ],
    }
    (row,) = NotamSource(["KJFK"]).transform(payload)
    assert row["notam_id"] == "NOTAM_1_1"
    assert row["icao_location"] == "KJFK"
    assert row["formatted_text"] == "RUNWAY 04L/22R CLOSED"


def test_notam_rows_without_an_id_are_skipped():
    payload = {"items": [{"properties": {"coreNOTAMData": {"notam": {}}}}]}
    assert list(NotamSource(["KJFK"]).transform(payload)) == []


def test_fuel_transform_unwraps_the_eia_envelope():
    payload = {
        "response": {
            "data": [
                {
                    "period": "2026-07-28",
                    "series": "EER_EPJK_PF4_RGC_DPG",
                    "product": "EPJK",
                    "product-name": "Kerosene-Type Jet Fuel",
                    "area-name": "GULF COAST",
                    "value": 2.31,
                    "units": "$/GAL",
                }
            ]
        }
    }
    (row,) = FuelPriceSource().transform(payload)
    assert row["period"] == "2026-07-28"
    assert row["price"] == 2.31
    assert row["units"] == "$/GAL"


def test_fuel_handles_an_empty_envelope():
    assert list(FuelPriceSource().transform({})) == []
    assert list(FuelPriceSource().transform({"response": {"data": []}})) == []
