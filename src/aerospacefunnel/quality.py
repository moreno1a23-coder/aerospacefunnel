"""Data-quality gates.

Marts are only as trustworthy as the partitions beneath them, so checks run between load and
publish. A failed `error`-severity check blocks the mart refresh; publishing a confidently
wrong number is worse than publishing none.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

Rows = Sequence[dict[str, Any]]


@dataclass(frozen=True)
class CheckResult:
    table: str
    name: str
    passed: bool
    observed: str
    expected: str
    severity: str = "error"


@dataclass(frozen=True)
class Expectation:
    name: str
    check: Callable[[Rows], tuple[bool, str, str]]
    severity: str = "error"


def not_empty() -> Expectation:
    def check(rows: Rows) -> tuple[bool, str, str]:
        return bool(rows), f"{len(rows)} rows", ">0 rows"

    return Expectation("not_empty", check)


def row_count_between(low: int, high: int, severity: str = "warn") -> Expectation:
    def check(rows: Rows) -> tuple[bool, str, str]:
        return low <= len(rows) <= high, f"{len(rows)} rows", f"{low}..{high} rows"

    return Expectation(f"row_count_between_{low}_{high}", check, severity)


def not_null(column: str, max_null_fraction: float = 0.0) -> Expectation:
    def check(rows: Rows) -> tuple[bool, str, str]:
        if not rows:
            return True, "no rows", "n/a"
        nulls = sum(1 for r in rows if r.get(column) is None)
        fraction = nulls / len(rows)
        return (
            fraction <= max_null_fraction,
            f"{fraction:.1%} null ({nulls}/{len(rows)})",
            f"<={max_null_fraction:.1%} null",
        )

    return Expectation(f"not_null[{column}]", check)


def in_range(column: str, low: float, high: float, severity: str = "error") -> Expectation:
    def check(rows: Rows) -> tuple[bool, str, str]:
        bad = [
            r[column]
            for r in rows
            if isinstance(r.get(column), int | float) and not (low <= r[column] <= high)
        ]
        sample = f" e.g. {bad[0]}" if bad else ""
        return not bad, f"{len(bad)} out of range{sample}", f"{low}..{high}"

    return Expectation(f"in_range[{column}]", check, severity)


def unique(keys: Sequence[str]) -> Expectation:
    def check(rows: Rows) -> tuple[bool, str, str]:
        seen = {tuple(r.get(k) for k in keys) for r in rows}
        return (
            len(seen) == len(rows),
            f"{len(rows) - len(seen)} duplicates",
            f"unique on {'+'.join(keys)}",
        )

    return Expectation(f"unique[{'+'.join(keys)}]", check)


def fresh_within(column: str, max_age_seconds: int, now: float | None = None) -> Expectation:
    """The newest timestamp must be recent - a silently stalled feed looks like calm skies."""

    def check(rows: Rows) -> tuple[bool, str, str]:
        stamps = [r[column] for r in rows if isinstance(r.get(column), int | float)]
        if not stamps:
            return False, "no timestamps", f"<={max_age_seconds}s old"
        age = (time.time() if now is None else now) - max(stamps)
        return age <= max_age_seconds, f"{age:.0f}s old", f"<={max_age_seconds}s old"

    return Expectation(f"fresh_within[{column}]", check)


def referential(column: str, valid: set[str], severity: str = "warn") -> Expectation:
    """Non-null values in `column` must exist in the reference set."""

    def check(rows: Rows) -> tuple[bool, str, str]:
        missing = {r[column] for r in rows if r.get(column) and r[column] not in valid}
        sample = f" e.g. {sorted(missing)[:3]}" if missing else ""
        return not missing, f"{len(missing)} unknown{sample}", "all present in reference"

    return Expectation(f"referential[{column}]", check, severity)


# Per-table expectations. Ranges are physical bounds, not preferences: an aircraft below
# -1,500 ft or above 60,000 ft is a decoding fault, not a remarkable flight.
SUITES: dict[str, list[Expectation]] = {
    "fct_position": [
        not_empty(),
        unique(["hex", "snapshot_time"]),
        not_null("hex"),
        not_null("snapshot_time"),
        not_null("latitude", max_null_fraction=0.02),
        in_range("latitude", -90, 90),
        in_range("longitude", -180, 180),
        in_range("alt_baro", -1500, 60000),
        in_range("ground_speed", 0, 1200),
        in_range("track", 0, 360),
    ],
    "fct_weather_obs": [
        not_empty(),
        unique(["station", "obs_time"]),
        not_null("station"),
        in_range("wind_speed_kt", 0, 250),
        in_range("visibility_sm", 0, 100),
        in_range("temp_c", -90, 60),
    ],
    "fct_flight_leg": [
        unique(["leg_id"]),
        not_null("hex"),
        in_range("duration_s", 0, 86400),
        in_range("track_efficiency", 0.5, 50),
    ],
    "dim_airport": [
        not_empty(),
        unique(["ident"]),
        in_range("latitude", -90, 90),
        in_range("longitude", -180, 180),
    ],
    "fct_disruption": [unique(["airport", "delay_type", "observed_at"]), not_null("airport")],
    "fct_hazard": [unique(["hazard_id"]), not_null("hazard_id")],
    "fct_notam": [unique(["notam_id"]), not_null("notam_id"), not_null("location")],
    "fct_fuel_price": [
        unique(["period", "series"]),
        not_null("period"),
        # A jet fuel spot price outside this band means the series or units changed.
        in_range("price", 0, 25),
    ],
    "dim_aircraft_registry": [unique(["icao24"]), not_null("icao24")],
    "dim_aircraft": [
        unique(["hex", "valid_from"]),
        not_null("hex"),
        not_null("valid_from"),
    ],
}


def run_suite(table: str, rows: Rows, extra: Sequence[Expectation] = ()) -> list[CheckResult]:
    """Run every expectation for a table. Unknown tables simply have no suite yet."""
    results = []
    for exp in [*SUITES.get(table, []), *extra]:
        try:
            passed, observed, expected = exp.check(rows)
        except Exception as err:  # a broken check must not masquerade as clean data
            passed, observed, expected = False, f"check raised {type(err).__name__}: {err}", "-"
        results.append(CheckResult(table, exp.name, passed, observed, expected, exp.severity))
    return results


def blocking_failures(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [r for r in results if not r.passed and r.severity == "error"]
