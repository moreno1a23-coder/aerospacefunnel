# aerospacefunnel

An ETL pipeline that funnels public aerospace data into a local SQLite warehouse.

Two sources ship today, both free and keyless:

| Source | Upstream | What lands |
|---|---|---|
| `launches` | [Launch Library 2](https://ll.thespacedevs.com/2.2.0/swagger/) | Orbital/suborbital launch records — provider, vehicle, pad, orbit, status |
| `flights` | [OpenSky Network](https://openskynetwork.github.io/opensky-api/rest.html) | A snapshot of live ADS-B state vectors inside a bounding box |

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Requires Python 3.11+. The only runtime dependency is `requests`.

## Use

```bash
# Pull the 100 most recent launches
aerospacefunnel launches --limit 100

# Walk several pages
aerospacefunnel launches --limit 100 --max-pages 5

# Snapshot every aircraft over the Alps (lat_min,lon_min,lat_max,lon_max)
aerospacefunnel flights --bbox 45,5,47,8

# See what's in the warehouse and how the last runs went
aerospacefunnel stats
```

Useful flags: `--db PATH` (default `data/aerospace.db`), `--dry-run` to
extract and transform without writing, `--raw-dir DIR` to also archive the untouched
API payloads, and `-v` for per-page logging.

## How it works

```
  extract          transform            load
  ───────          ─────────            ────
  paged HTTP  ──►  normalise rows  ──►  SQLite upsert
  w/ retries       (fixture-tested)     (idempotent)
                                            │
                                            ▼
                                        ingest_run
                                        bookkeeping
```

Each source in `src/aerospacefunnel/sources/` owns both its `extract` (network) and its
`transform` (payload → rows). Keeping the pair together means a payload shape is
understood in exactly one file, and the transform stays testable against a saved
fixture with no network involved. `pipeline.py` wires any source to the warehouse;
`load.py` owns the schema.

Three design points worth knowing:

- **Loads are idempotent.** Every table has a natural key (`launch.id`;
  `flight_state.(icao24, snapshot_time)`) and writes go through `INSERT … ON CONFLICT DO
  UPDATE`. Re-running a pull refreshes rows in place, so an overlapping cron is harmless.
- **Upstream failures are data, not exceptions.** A run that dies mid-pull rolls back its
  partial writes and records `status='error'` with the exception in `ingest_run`. The CLI
  exits non-zero, but the pipeline never raises into a scheduler.
- **Schema drift is survivable.** Row keys are intersected with the real table columns
  before they reach SQL, so a new upstream field is ignored rather than fatal — and can't
  inject SQL. OpenSky's positional state vectors are read by index with a length guard,
  since the array has grown over time.

## Schema

```
launch(id PK, slug, name, status, status_abbrev, net, window_start, window_end,
       provider, mission, mission_type, pad, location, orbit, launcher,
       image_url, last_updated, ingested_at)

flight_state(icao24, snapshot_time, PK(icao24, snapshot_time), callsign,
             origin_country, time_position, last_contact, longitude, latitude,
             baro_altitude, geo_altitude, on_ground, velocity, true_track,
             vertical_rate, squawk, spi, position_source, sensor_count, ingested_at)

ingest_run(id PK, source, started_at, finished_at, status, pages,
           rows_read, rows_loaded, error)
```

Query it like any SQLite database:

```sql
SELECT provider, COUNT(*) FROM launch
WHERE net >= '2025-01-01' GROUP BY 1 ORDER BY 2 DESC;
```

## Rate limits

Launch Library 2 allows roughly **15 requests/hour** to anonymous callers; OpenSky
throttles anonymous polling too. The shared session in `http.py` retries `429` and `5xx`
with exponential backoff and honours `Retry-After`, but it can't manufacture quota —
keep `--max-pages` modest and don't poll `flights` faster than about once a minute.

## Tests

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
```

The suite is offline. `tests/fixtures/` holds real captured responses from both APIs, so
transforms are tested against the shapes the services actually return rather than
invented ones.

## Adding a source

Implement the `Source` protocol in `sources/base.py` — a `name`, a `table`, an `extract`
that yields raw payloads, and a `transform` that yields rows. Add the table to `SCHEMA`
and its natural key to `KEYS` in `load.py`, register the class in `sources/__init__.py`,
and give the CLI a subcommand. Save a real response into `tests/fixtures/` while you're
there.

## License

MIT — see [LICENSE](LICENSE).
