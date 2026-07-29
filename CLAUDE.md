# aerospacefunnel

Aviation data platform: live public feeds → layered Parquet → DuckDB marts.
Repo: https://github.com/moreno1a23-coder/aerospacefunnel

## Layout

- `sources/` — one module per upstream, each owning **both** `extract` (network) and
  `transform` (payload → rows). Contract: `name`, `table`, `keys`, `extract`, `transform`.
- `storage.py` — bronze (immutable gzip JSON) + silver/gold (atomic, deduped Parquet).
- `pipeline.py` — `run()` for live pulls, `replay()` to rebuild from bronze offline.
- `derive.py` — flight-leg segmentation from position fixes.
- `marts.py` — analytics views; each declares `depends_on` so it is skipped, not broken.
- `metadata.py` — SQLite: `pipeline_run`, `lineage`, `dq_result`. **Deliberately not DuckDB**
  (tiny frequent transactional writes).
- `throttle.py` — token bucket persisted in SQLite.

## Working here

```bash
.venv/bin/python -m pytest      # 115 tests, fully offline
.venv/bin/ruff check .
```

## Things that will bite you

- **Launch Library 2 is 15 req/hr per IP with no free key.** Always use `--dev` (the
  unlimited `lldev` mirror) while iterating. A 429 is retried with backoff, so a careless
  loop stalls rather than failing fast.
- **Tests must stay offline.** `tests/fixtures/` holds real captured responses. Change a
  transform → refresh the fixture once from a live call; never add a network call to the suite.
- **`alt_baro` is the string `"ground"`** for on-ground aircraft (116/716 in a live sample).
  It is the only ground signal in the feed and `derive.py` depends on it.
- **DuckDB `to_timestamp()` returns TIMESTAMPTZ**, which needs `pytz` to convert on fetch and
  lets the session timezone shift date grouping. Every mart casts `::TIMESTAMP`. Keep doing that.
- **PyArrow materialises `dt`/`hh` from the directory path** on read. `storage.write_partition`
  strips them before merging; if you skip that they get baked into the file and collide.
- **Never assign an airport to an unobserved endpoint.** `derive.py` returns NULL when the
  aircraft was not seen on the ground within the radius. Guessing would corrupt every
  utilisation and punctuality figure downstream. `complete` is the flag that matters.
- **Adding a table means three edits**: register the source in `sources/__init__.py`, add it
  to `TABLE_LAYERS`, and add expectations to `quality.SUITES`.

## Honesty constraints baked into the design

No free source publishes schedules, so `mart_punctuality_proxy` is a proxy against a rolling
median — do not rename it to imply schedule adherence. Hub-radius polling means most legs are
partial; that is represented, not smoothed over.
