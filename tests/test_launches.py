from __future__ import annotations

from aerospacefunnel.sources.launches import LaunchesSource, _flat


def test_transform_reads_every_column(launch_payload):
    rows = list(LaunchesSource().transform(launch_payload))
    assert rows, "fixture should contain at least one launch"

    row = rows[0]
    assert row["id"]
    assert row["name"]
    # The fixture's first record is Sputnik 1 — ordering is whatever the API returned.
    assert row["status"] == "Launch Successful"
    assert row["status_abbrev"] == "Success"
    assert row["net"].startswith("1957-10-04")
    assert row["provider"] == "Soviet Space Program"
    assert row["location"].startswith("Baikonur")


def test_flat_accepts_both_list_and_detailed_shapes():
    # mode=list gives a bare string; mode=detailed gives a nested object.
    assert _flat("Low Earth Orbit") == "Low Earth Orbit"
    assert _flat({"name": "Low Earth Orbit", "abbrev": "LEO"}) == "Low Earth Orbit"
    assert _flat({"name": "Low Earth Orbit", "abbrev": "LEO"}, "abbrev") == "LEO"
    assert _flat(None) is None
    assert _flat({}) is None


def test_transform_tolerates_empty_payload():
    assert list(LaunchesSource().transform({})) == []
    assert list(LaunchesSource().transform({"results": []})) == []


def test_page_size_is_capped_at_api_maximum():
    assert LaunchesSource(limit=5000).limit == 100
