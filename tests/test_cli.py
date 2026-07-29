from __future__ import annotations

import pytest

from aerospacefunnel.cli import _bbox, build_parser, main


def test_bbox_parses_four_floats():
    assert _bbox("45,5,47,8") == (45.0, 5.0, 47.0, 8.0)


@pytest.mark.parametrize("bad", ["45,5,47", "a,b,c,d", "47,5,45,8", "45,5,47,200"])
def test_bbox_rejects_malformed_input(bad):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        _bbox(bad)


def test_stats_runs_against_an_empty_warehouse(db, capsys):
    assert main(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "launch" in out
    assert "(none yet)" in out


def test_a_command_is_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
