# aerospacefunnel

Python ETL pipeline: pulls public aerospace data into a local SQLite warehouse.
Repo: https://github.com/moreno1a23-coder/aerospacefunnel

## Layout

- `src/aerospacefunnel/sources/` — one module per source; each owns **both** `extract`
  (network) and `transform` (payload → rows). Add new sources here.
- `load.py` — `SCHEMA`, the `KEYS` natural-key map, and the idempotent `upsert`.
- `pipeline.py` — wires any `Source` to the warehouse and writes `ingest_run` rows.
- `cli.py` — argparse entry point (`aerospacefunnel <launches|flights|stats>`).

## Working here

```bash
.venv/bin/python -m pytest      # 28 tests, fully offline
.venv/bin/ruff check .
```

The venv is at `.venv/` (`pip install -e ".[dev]"`). Output goes to `data/`, which is
gitignored along with `*.db`.

## Things that will bite you

- **Launch Library 2 rate-limits anonymous callers to ~15 requests/hour.** Don't loop
  `--max-pages` while iterating; use `tests/fixtures/` instead. A 429 is retried with
  backoff, which means a careless test run stalls rather than fails fast.
- **Tests must stay offline.** `tests/fixtures/` holds real captured responses. If you
  change a transform, update the fixture from a live call once — don't add a network
  call to the suite.
- **OpenSky state vectors are positional arrays, not objects.** `FIELDS` in
  `sources/flights.py` *is* the schema; the array has gained fields over time, so reads
  go through `_at()` with a length guard.
- **Adding a table means three edits**: `SCHEMA` and `KEYS` in `load.py`, plus registering
  the source in `sources/__init__.py`. Missing `KEYS` raises `ValueError` on load.
