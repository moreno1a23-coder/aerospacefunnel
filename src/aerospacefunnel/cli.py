"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import sys

from .load import connect
from .pipeline import run as run_pipeline
from .sources import FlightsSource, LaunchesSource

DEFAULT_DB = "data/aerospace.db"


def _bbox(value: str) -> tuple[float, float, float, float]:
    parts = value.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be lat_min,lon_min,lat_max,lon_max")
    try:
        lamin, lomin, lamax, lomax = (float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError("bbox values must be numbers") from None
    if not (-90 <= lamin < lamax <= 90) or not (-180 <= lomin < lomax <= 180):
        raise argparse.ArgumentTypeError("bbox must satisfy min < max and be in lat/lon range")
    return lamin, lomin, lamax, lomax


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aerospacefunnel", description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")
    parser.add_argument("--raw-dir", help="also archive untouched API payloads here")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    launches = sub.add_parser("launches", help="pull launch records from Launch Library 2")
    launches.add_argument("--limit", type=int, default=100, help="rows per page (max 100)")
    launches.add_argument("--max-pages", type=int, default=1, help="pages to walk")
    launches.add_argument("--search", help="filter by free-text search")
    launches.add_argument("--dry-run", action="store_true", help="extract+transform, skip load")

    flights = sub.add_parser("flights", help="snapshot live aircraft from OpenSky")
    flights.add_argument("--bbox", type=_bbox, help="lat_min,lon_min,lat_max,lon_max")
    flights.add_argument("--dry-run", action="store_true", help="extract+transform, skip load")

    sub.add_parser("stats", help="summarise what is in the warehouse")

    return parser


def _stats(db: str) -> int:
    with connect(db) as conn:
        for table in ("launch", "flight_state"):
            count = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            print(f"{table:<14} {count:>8,} rows")

        print("\nrecent runs:")
        rows = conn.execute(
            "SELECT source, started_at, status, pages, rows_read, rows_loaded, error "
            "FROM ingest_run ORDER BY id DESC LIMIT 10"
        ).fetchall()
        if not rows:
            print("  (none yet)")
        for r in rows:
            line = (
                f"  {r['started_at']}  {r['source']:<9} {r['status']:<6} "
                f"pages={r['pages']} read={r['rows_read']} loaded={r['rows_loaded']}"
            )
            if r["error"]:
                line += f"  {r['error'][:60]}"
            print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
    )

    if args.command == "stats":
        return _stats(args.db)

    if args.command == "launches":
        source = LaunchesSource(limit=args.limit, max_pages=args.max_pages, search=args.search)
    else:
        source = FlightsSource(bbox=args.bbox)

    result = run_pipeline(source, args.db, raw_dir=args.raw_dir, dry_run=args.dry_run)
    verb = "would load" if args.dry_run else "loaded"
    print(
        f"{result.source}: {result.status} — {result.pages} page(s), "
        f"{result.rows_read} read, {verb} {result.rows_loaded}"
    )
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    return 0 if result.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
