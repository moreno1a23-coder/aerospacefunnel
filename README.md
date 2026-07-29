# aerospacefunnel

**Live preview:** https://moreno1a23-coder.github.io/aerospacefunnel/ — a snapshot of real warehouse figures.

A commercial-grade aviation data platform built entirely on public feeds. It funnels live
surveillance, weather, hazards, disruption and reference data into a layered Parquet
warehouse queried with DuckDB.

```
  ingest            bronze                silver / gold        marts
  ──────            ──────                ─────────────        ─────
  poller ──► immutable .json.gz ──► typed Parquet ──► dims + facts ──► DuckDB views
             dt=/hh= partitions      atomic, deduped    SCD-ready       analytics SQL
             (replayable)            (idempotent)
```

## What it actually does

Every figure below came from a real run, not an estimate:

```
$ aerospacefunnel poll
KJFK  ok    736 aircraft via adsb.lol
KLAX  ok    436 aircraft via adsb.lol
KORD  ok    954 aircraft via adsb.lol

$ aerospacefunnel query "SELECT airport, reason, sample_avg_delay FROM mart_network_disruption"
BOS   low ceilings           1 hour and 46 minutes
SFO   low ceilings           41 minutes
```

## Scope — read this before trusting anything

This is a production-grade platform **over public data**. It is not a full airline data
platform, and engineering cannot make it one.

**It does:** live network surveillance, aerodrome/fleet reference, weather and en-route
hazards, disruption and delay feeds, derived flight legs, fleet utilisation, route
efficiency, emergency detection, launch-airspace conflict.

**It cannot, at any price from public sources:** passenger bookings, crew rostering,
maintenance records, fuel uplift, cargo manifests, revenue accounting. These are internal
systems with no public API.

**It cannot without paid feeds:** published schedules (OAG/Cirium). ADS-B yields *actuals
only*, so there is no true scheduled-vs-actual on-time performance — which is why the mart
is named `mart_punctuality_proxy` and compares each callsign against its own rolling median
instead of a timetable. Community ADS-B also has genuine coverage gaps (no mid-ocean ground
stations; satellite ADS-B is commercial).

**Partial observation is explicit.** Hub-radius polling sees an aircraft only inside the
bubble, so most legs are transits whose real origin was never observed. Those get NULL
endpoints and `complete = false`, never a guessed nearest airport. In a 7-minute sample,
1,691 legs were derived and exactly 1 was complete — the flags are load-bearing, not
decorative.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Python 3.11+. Runtime dependencies: `requests`, `duckdb`, `pyarrow`. Config uses stdlib
`tomllib`.

## Use

```bash
aerospacefunnel ingest airports        # reference data first - everything joins to it
aerospacefunnel poll                   # one surveillance sweep across all hubs
aerospacefunnel ingest metar           # weather
aerospacefunnel ingest disruption      # FAA ground delays and stops
aerospacefunnel ingest sigmet          # en-route hazards
aerospacefunnel legs                   # derive flight legs from accumulated fixes
aerospacefunnel ingest registry        # fleet registry (keyless) - adds operator
aerospacefunnel fleet                  # build the SCD2 aircraft dimension
aerospacefunnel warehouse              # build DuckDB views and marts
aerospacefunnel query "SELECT * FROM mart_traffic_density"

aerospacefunnel check                  # data-quality gates
aerospacefunnel stats                  # what is loaded, how runs went
aerospacefunnel keys --probe           # credential status
aerospacefunnel replay sigmet          # rebuild silver from bronze, no network
```

## Sources

All verified live on 2026-07-29. **Nothing below requires a credential.**

| Domain | Source | Notes |
|---|---|---|
| Surveillance | adsb.lol → airplanes.live → adsb.fi | keyless, automatic failover |
| Weather | NOAA Aviation Weather Center (METAR/TAF) | keyless, ~0.2s |
| Hazards | SIGMET / G-AIRMET | keyless, 135 active in one sample |
| Disruption | FAA NAS status | keyless, live ground delay programmes |
| Aerodromes | OurAirports | 47,975 airports loaded |
| Launch windows | Launch Library 2 | 15 req/hr; `--dev` mirror is unlimited |
| Orbital | CelesTrak GP | keyless |
| Space weather | NOAA SWPC | keyless |
| Fleet registry | OpenSky aircraft database | keyless, 571,950 airframes |

Two sources stay dormant until you add a free key: `notam` (FAA) and `fuel` (EIA jet
fuel spot prices). Without credentials they skip cleanly and exit 0 — they never fail a
run. Every credential listed by `aerospacefunnel keys` names the source that consumes it;
a key nothing reads is a bug, and a test enforces that.

Optional credentials only *improve* things — see `.env.example`. OpenSky OAuth2 lifts
400 credits/day to 4,000 and 10s to 5s resolution. **Launch Library 2 has no free key**:
15 req/hr per IP is the ceiling and keys are Patreon-only, which is why development uses
the unlimited `lldev` mirror.

## Design decisions worth knowing

- **Bronze is immutable and replayable.** Payloads are archived before parsing, so a
  transform bug is fixed by reprocessing archived bytes — never by re-fetching from a
  rate-limited upstream. `aerospacefunnel replay <source>` does this with no network.
- **Writes are atomic and idempotent.** A partition is one `data.parquet` written to a temp
  file, fsynced, then `os.replace`d. Re-running a load merges and deduplicates on the
  natural key rather than appending duplicates.
- **Rate limiting survives process exit.** LL2's 15/hr is per IP, so an in-process limiter
  cannot enforce it across cron invocations. The token bucket lives in SQLite and refuses
  *before* the request instead of earning a 429.
- **Upstream failure is data, not an exception.** A failed run records `status='error'` in
  `pipeline_run` with the exception; the pipeline never raises into a scheduler.
- **Quality gates block publication.** Range, uniqueness, null-rate and freshness checks run
  between load and mart refresh. Publishing a confidently wrong number is worse than none.
- **Antimeridian hazards are handled.** Pacific FIRs report longitudes past 180 (a live
  sample carried 183.8). Naively wrapping and taking min/max would produce a box covering
  nearly the globe, silently matching every flight; crossings are detected and flagged.
- **A hex can be reassigned to another airframe**, so `dim_aircraft` is slowly-changing
  (type 2): an identity change closes the old version and opens a new one. Overwriting
  would silently re-attribute every historical leg to whichever tail holds that hex today.
- **Operator name and ICAO code are kept apart.** Coalescing them split one carrier into
  two (`SWA` *and* `Southwest Airlines`). `operator_icao` is the controlled vocabulary and
  the grouping key; `operator` is a display label and is not safe to group on.
- **`alt_baro: "ground"`** is a string sentinel, not a null — 116 of 716 aircraft in one
  sample. It is the only on-ground signal the feed carries, and leg segmentation needs it.

## Storage

Measured, not estimated: zstd compresses a real ADS-B payload **8.2x** (497 B → 61 B/row),
and columnar Parquet does better because `hex`/`type`/`squawk` dictionary-encode. Planning
figure ~20 B/row.

| Coverage | Raw tier | Steady state (all tiers) |
|---|---|---|
| Hub-focused @60s | ~20 MB/day | **< 5 GB** |
| US-wide @10s | ~1 GB/day | ~32 GB |
| Global @10s | ~2.6 GB/day | ~80 GB |

Storage is not the constraint at hub scope — bandwidth and feed politeness are. adsb.lol,
airplanes.live and adsb.fi are donated community infrastructure; poll them accordingly.

## Configuration

`config/platform.toml`. Adding a hub is a config edit, never a code change:

```toml
[surveillance]
cadence_seconds = 60

[[hubs]]
icao = "KJFK"
radius_nm = 250
```

## Scheduling

systemd user timers in `systemd/` — see `systemd/README.md`. Remember
`loginctl enable-linger $USER`, or the timers stop at logout.

## Tests

```bash
.venv/bin/python -m pytest      # 136 tests, fully offline
.venv/bin/ruff check .
```

`tests/fixtures/` holds real captured responses from every upstream, so transforms are
tested against the shapes these services actually return. The suite never makes a network
call — including the failover and OAuth2 paths, which use stand-in sessions.

## Adding a source

Implement `name`, `table`, `keys`, `extract` and `transform`. Register it in
`sources/__init__.py` and add the table to `TABLE_LAYERS`. Add expectations to
`quality.SUITES`, and save a real response into `tests/fixtures/`.

## License

MIT — see [LICENSE](LICENSE).
