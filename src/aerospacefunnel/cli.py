"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config as config_mod
from . import derive, metadata, pipeline, quality, storage, warehouse
from .credentials import CREDENTIALS, Credentials
from .http import build_session
from .sources import SOURCES, SurveillanceSource
from .sources.flights import TokenProvider
from .throttle import Throttle

log = logging.getLogger("aerospacefunnel")

METADATA_DB = "data/metadata.db"

# Throttle bucket per source; unlisted sources fall back to a default politeness budget.
THROTTLE_KEYS = {
    "metar": "aviationweather",
    "taf": "aviationweather",
    "sigmet": "aviationweather",
    "gairmet": "aviationweather",
    "disruption": "faa",
    "surveillance": "adsb",
}


def _resolve_hub_coords(cfg: config_mod.Config, session) -> dict[str, tuple[float, float]]:
    """Look up hub coordinates from the loaded airport dimension, else from the live API.

    Surveillance needs a lat/lon per hub. Preferring the warehouse avoids a network call on
    every poll once reference data has been ingested.
    """
    idents = [h.icao for h in cfg.hubs]
    coords: dict[str, tuple[float, float]] = {}

    for row in storage.read_table(cfg.storage_root, "silver", "dim_airport"):
        if row.get("ident") in idents and row.get("latitude") is not None:
            coords[row["ident"]] = (row["latitude"], row["longitude"])

    missing = [i for i in idents if i not in coords]
    if missing:
        # Fall back to the weather station endpoint, which returns lat/lon for an ICAO id.
        response = session.get(
            "https://aviationweather.gov/api/data/stationinfo",
            params={"ids": ",".join(missing), "format": "json"},
            timeout=45,
        )
        response.raise_for_status()
        for station in response.json() or []:
            if station.get("icaoId") and station.get("lat") is not None:
                coords[station["icaoId"]] = (station["lat"], station["lon"])

    return coords


def cmd_poll(args, cfg: config_mod.Config) -> int:
    """One surveillance sweep across every configured hub."""
    session = build_session()
    throttle = Throttle(METADATA_DB)
    try:
        coords = _resolve_hub_coords(cfg, session)
    except Exception as exc:  # noqa: BLE001
        print(f"could not resolve hub coordinates: {exc}", file=sys.stderr)
        return 1

    failures = 0
    for hub in cfg.hubs:
        if hub.icao not in coords:
            print(f"{hub.icao}: no coordinates available, skipped", file=sys.stderr)
            failures += 1
            continue

        lat, lon = coords[hub.icao]
        source = SurveillanceSource(hub.icao, lat, lon, hub.radius_nm, cfg.feed_order)
        result = pipeline.run(
            source,
            storage_root=cfg.storage_root,
            metadata_db=METADATA_DB,
            throttle=throttle,
            throttle_key="adsb",
            dry_run=args.dry_run,
            session=session,
        )
        feed = f" via {result.feed_used}" if result.feed_used else ""
        print(
            f"{hub.icao:<5} {result.status:<9} {result.rows_read:>5} aircraft"
            f"{feed}{'  ' + result.error if result.error else ''}"
        )
        if not result.ok and result.status != "throttled":
            failures += 1

    session.close()
    return 1 if failures else 0


def cmd_ingest(args, cfg: config_mod.Config) -> int:
    """Run one named source."""
    factory = SOURCES.get(args.source)
    if factory is None:
        print(f"unknown source: {args.source}", file=sys.stderr)
        return 2

    creds = Credentials()
    stations = [h.icao for h in cfg.hubs]

    if args.source in ("metar", "taf"):
        source = factory(stations)
    elif args.source in ("launches", "launch_updates"):
        source = factory(
            limit=args.limit,
            max_pages=args.max_pages,
            dev=args.dev,
            token=creds.get("LL2_TOKEN"),
        )
    elif args.source == "opensky":
        provider = None
        if creds.group_ready("OPENSKY_CLIENT_ID"):
            provider = TokenProvider(
                creds.get("OPENSKY_CLIENT_ID"), creds.get("OPENSKY_CLIENT_SECRET")
            )
        source = factory(token_provider=provider)
    elif args.source == "orbital":
        source = factory(group=args.group)
    elif args.source == "notam":
        source = factory(
            stations,
            client_id=creds.get("FAA_NOTAM_CLIENT_ID"),
            client_secret=creds.get("FAA_NOTAM_CLIENT_SECRET"),
        )
    elif args.source == "fuel":
        source = factory(api_key=creds.get("EIA_API_KEY"))
    else:
        source = factory()

    # A source needing credentials it does not have is skipped, not failed: the platform
    # must run end to end with an empty .env.
    if hasattr(source, "configured") and not source.configured:
        needed = [c.name for c in CREDENTIALS if c.consumed_by == args.source]
        print(f"{args.source}: skipped - needs {', '.join(needed)} (see `aerospacefunnel keys`)")
        return 0

    throttle_key = getattr(source, "throttle_key", THROTTLE_KEYS.get(args.source, args.source))
    if args.source == "opensky":
        throttle_key = "opensky_auth" if getattr(source, "authenticated", False) else "opensky_anon"

    result = pipeline.run(
        source,
        storage_root=cfg.storage_root,
        metadata_db=METADATA_DB,
        throttle=Throttle(METADATA_DB),
        throttle_key=throttle_key,
        # Reference dimensions change slowly; a daily partition avoids 24 copies a day.
        hourly=args.source not in ("airports", "runways", "registry", "fuel"),
        dry_run=args.dry_run,
    )
    verb = "would write" if args.dry_run else "wrote"
    print(
        f"{result.source}: {result.status} - {result.pages} page(s), "
        f"{result.rows_read} read, {verb} {result.rows_written} "
        f"({result.bytes_raw / 1024:.0f} KB raw)"
    )
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_legs(args, cfg: config_mod.Config) -> int:
    """Derive flight legs from accumulated position fixes."""
    positions = storage.read_table(cfg.storage_root, "silver", "fct_position")
    if not positions:
        print("no positions yet - run `poll` first")
        return 1

    airports = [
        derive.Airport(r["ident"], r["latitude"], r["longitude"])
        for r in storage.read_table(cfg.storage_root, "silver", "dim_airport")
        if r.get("latitude") is not None and r.get("longitude") is not None
    ]

    legs = derive.flight_legs(positions, airports)
    if not legs:
        print(f"{len(positions)} fixes produced no legs (need longer observation)")
        return 0

    from datetime import UTC, datetime

    written = 0
    for leg in legs:
        _, _ = storage.write_partition(
            cfg.storage_root,
            "gold",
            "fct_flight_leg",
            [leg],
            ts=datetime.fromtimestamp(leg["start_time"], tz=UTC),
            keys=("leg_id",),
            hourly=False,
        )
        written += 1

    complete = sum(1 for leg in legs if leg["complete"])
    print(
        f"derived {written} legs from {len(positions)} fixes "
        f"({complete} with both endpoints observed, "
        f"{written - complete} partial - aircraft transiting the hub radius)"
    )
    return 0


def cmd_fleet(args, cfg: config_mod.Config) -> int:
    """Build the SCD2 aircraft dimension from observed positions plus registry enrichment."""
    from datetime import UTC, datetime

    positions = storage.read_table(cfg.storage_root, "silver", "fct_position")
    if not positions:
        print("no positions yet - run `poll` first")
        return 1

    # Registry supplies operator, which ADS-B never transmits.
    enrichment = {
        r["icao24"]: r
        for r in storage.read_table(cfg.storage_root, "silver", "dim_aircraft_registry")
        if r.get("icao24")
    }
    existing = storage.read_table(cfg.storage_root, "gold", "dim_aircraft")

    rows = derive.aircraft_dimension(positions, existing, enrichment)
    if not rows:
        print(f"{len(positions)} fixes carried no identity information")
        return 0

    storage.write_partition(
        cfg.storage_root,
        "gold",
        "dim_aircraft",
        rows,
        ts=datetime.now(UTC),
        keys=("hex", "valid_from"),
        hourly=False,
    )
    current = sum(1 for r in rows if r.get("is_current"))
    with_operator = sum(1 for r in rows if r.get("operator"))
    print(
        f"{len(rows)} identity versions for {current} airframes "
        f"({len(rows) - current} superseded, {with_operator} with operator"
        f"{'' if enrichment else ' - run `ingest registry` to add operators'})"
    )
    return 0


def cmd_check(args, cfg: config_mod.Config) -> int:
    """Run data-quality expectations over the loaded tables."""
    from .sources import TABLE_LAYERS

    failures = 0
    with metadata.connect(METADATA_DB) as conn:
        for table, layer in TABLE_LAYERS.items():
            rows = storage.read_table(cfg.storage_root, layer, table)
            if not rows:
                continue
            results = quality.run_suite(table, rows)
            if not results:
                continue

            blocking = quality.blocking_failures(results)
            failures += len(blocking)
            mark = "FAIL" if blocking else "ok"
            print(
                f"{table:<22} {mark:<5} {len(rows):>7,} rows  "
                f"{sum(1 for r in results if r.passed)}/{len(results)} checks passed"
            )
            for r in results:
                if not r.passed:
                    print(f"    [{r.severity}] {r.name}: {r.observed} (expected {r.expected})")
                metadata.record_dq(
                    conn,
                    run_id=None,
                    table_name=table,
                    check_name=r.name,
                    passed=r.passed,
                    observed=r.observed,
                    expected=r.expected,
                    severity=r.severity,
                )

    if failures:
        print(f"\n{failures} blocking failure(s) - marts should not be published", file=sys.stderr)
    return 1 if failures else 0


def cmd_warehouse(args, cfg: config_mod.Config) -> int:
    built = warehouse.refresh(cfg.storage_root, cfg.warehouse)
    print(f"tables: {len(built['tables'])}  marts: {len(built['marts'])}")
    for t in built["tables"]:
        print(f"  table  {t}")
    for m in built["marts"]:
        print(f"  mart   {m}")
    if built["skipped"]:
        print(f"  skipped (no source data): {', '.join(built['skipped'])}")
    return 0


def cmd_query(args, cfg: config_mod.Config) -> int:
    with warehouse.connect(cfg.warehouse) as conn:
        warehouse.build(conn, cfg.storage_root)
        try:
            result = conn.execute(args.sql)
        except Exception as exc:  # noqa: BLE001
            print(f"query failed: {exc}", file=sys.stderr)
            return 1
        columns = [d[0] for d in result.description]
        rows = result.fetchmany(args.limit)
    if not rows:
        print("(no rows)")
        return 0
    widths = [
        max(len(str(c)), max((len(str(r[i])) for r in rows), default=0))
        for i, c in enumerate(columns)
    ]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
    return 0


def cmd_keys(args, cfg: config_mod.Config) -> int:
    """Show which credentials are present, and probe them if asked.

    Missing credentials are never an error: every source has an anonymous path.
    """
    creds = Credentials()
    present = 0
    print(f"{'credential':<27} {'status':<9} {'used by':<10} purpose")
    print("-" * 100)
    for cred in CREDENTIALS:
        has = creds.has(cred.name)
        present += has
        if has and not creds.group_ready(cred.name):
            status = "PARTIAL"
        elif has:
            status = "set"
        else:
            status = "-"
        print(f"{cred.name:<27} {status:<9} {cred.consumed_by:<10} {cred.purpose}")
        if not has:
            print(f"{'':<27} {'':<9} {'':<10} get one: {cred.signup_url}")

    print(
        f"\n{present}/{len(CREDENTIALS)} set. None are required - all sources have an "
        "anonymous path."
    )

    if args.probe:
        print("\nprobing...")
        session = build_session()
        if creds.group_ready("OPENSKY_CLIENT_ID"):
            provider = TokenProvider(
                creds.get("OPENSKY_CLIENT_ID"), creds.get("OPENSKY_CLIENT_SECRET")
            )
            try:
                provider.token(session)
                print("  OPENSKY           OK - token acquired")
            except Exception as exc:  # noqa: BLE001
                print(f"  OPENSKY           FAILED - {exc}")
        else:
            print("  OPENSKY           skipped (needs both id and secret)")
        session.close()
    return 0


def cmd_stats(args, cfg: config_mod.Config) -> int:
    from .sources import TABLE_LAYERS

    print("table                    layer    rows      on-disk")
    print("-" * 56)
    total_bytes = 0
    for table, layer in TABLE_LAYERS.items():
        base = Path(cfg.storage_root) / layer / table
        files = list(base.rglob("data.parquet")) if base.exists() else []
        if not files:
            continue
        size = sum(f.stat().st_size for f in files)
        total_bytes += size
        rows = len(storage.read_table(cfg.storage_root, layer, table))
        print(f"{table:<24} {layer:<8} {rows:>8,}  {size / 1024:>8.0f} KB")

    bronze = Path(cfg.storage_root) / "bronze"
    bronze_bytes = (
        sum(f.stat().st_size for f in bronze.rglob("*.json.gz")) if bronze.exists() else 0
    )
    print(
        f"\nsilver+gold: {total_bytes / 1024 / 1024:.1f} MB   "
        f"bronze archive: {bronze_bytes / 1024 / 1024:.1f} MB"
    )

    with metadata.connect(METADATA_DB) as conn:
        rows = conn.execute(
            "SELECT source, started_at, status, rows_read, rows_written, feed_used, error "
            "FROM pipeline_run ORDER BY id DESC LIMIT ?",
            (args.runs,),
        ).fetchall()
    print("\nrecent runs:")
    if not rows:
        print("  (none yet)")
    for r in rows:
        line = (
            f"  {r['started_at']}  {r['source']:<14} {r['status']:<9} "
            f"read={r['rows_read']:<6} wrote={r['rows_written']:<6}"
        )
        if r["feed_used"]:
            line += f" via {r['feed_used']}"
        if r["error"]:
            line += f"  {r['error'][:50]}"
        print(line)
    return 0


def cmd_replay(args, cfg: config_mod.Config) -> int:
    """Rebuild silver from archived bronze, with no network access."""
    factory = SOURCES.get(args.source)
    if factory is None:
        print(f"unknown source: {args.source}", file=sys.stderr)
        return 2
    source = factory([h.icao for h in cfg.hubs]) if args.source in ("metar", "taf") else factory()
    result = pipeline.replay(source, storage_root=cfg.storage_root)
    print(
        f"replayed {result.pages} archived payload(s) -> {result.rows_written} rows "
        f"across {len(result.partitions)} partition(s)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aerospacefunnel", description=__doc__)
    parser.add_argument("--config", default=config_mod.DEFAULT_CONFIG, help="platform TOML")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="one surveillance sweep across all hubs")
    poll.add_argument("--dry-run", action="store_true")
    poll.set_defaults(func=cmd_poll)

    ingest = sub.add_parser("ingest", help="run one named source")
    ingest.add_argument("source", choices=sorted(SOURCES))
    ingest.add_argument("--limit", type=int, default=50)
    ingest.add_argument("--max-pages", type=int, default=1)
    ingest.add_argument("--group", default="last-30-days", help="CelesTrak group (orbital)")
    ingest.add_argument(
        "--dev", action="store_true", help="use the LL2 dev mirror (unlimited, stale data)"
    )
    ingest.add_argument("--dry-run", action="store_true")
    ingest.set_defaults(func=cmd_ingest)

    legs = sub.add_parser("legs", help="derive flight legs from position fixes")
    legs.set_defaults(func=cmd_legs)

    fleet = sub.add_parser("fleet", help="build the SCD2 aircraft dimension")
    fleet.set_defaults(func=cmd_fleet)

    check = sub.add_parser("check", help="run data-quality expectations")
    check.set_defaults(func=cmd_check)

    wh = sub.add_parser("warehouse", help="rebuild DuckDB views and marts")
    wh.set_defaults(func=cmd_warehouse)

    query = sub.add_parser("query", help="run SQL against the warehouse")
    query.add_argument("sql")
    query.add_argument("--limit", type=int, default=25)
    query.set_defaults(func=cmd_query)

    keys = sub.add_parser("keys", help="show credential status")
    keys.add_argument("--probe", action="store_true", help="verify credentials against upstream")
    keys.set_defaults(func=cmd_keys)

    stats = sub.add_parser("stats", help="what is in the warehouse and how runs went")
    stats.add_argument("--runs", type=int, default=10)
    stats.set_defaults(func=cmd_stats)

    replay = sub.add_parser("replay", help="rebuild silver from bronze, no network")
    replay.add_argument("source", choices=sorted(SOURCES))
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        cfg = config_mod.load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return args.func(args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
