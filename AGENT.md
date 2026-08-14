# AGENT.md

Agent-facing operating notes for this repo.

## Purpose

Turn manually-downloaded Zomato order-history CSV exports plus a hand-maintained
menu master into a queryable DuckDB warehouse (bronze/silver/menu/gold) for
`the-oven-vibe-dashboard` to consume. Read that repo's `docs/ANALYTICS_SPEC.md`
before changing gold tables — it's the contract for what the numbers mean and
how the dashboard uses them.

## Key commands

- `uv run python main.py` — run full pipeline once (bronze -> silver -> menu -> gold)
- `uv run python watch_pipeline.py` — auto-rebuild whenever a CSV or `menu.csv` changes under `data/`
- `duckdb warehouse.duckdb -c "..."` — inspect the warehouse directly (CLI, not the Python API — avoids the pandas dependency issue below)

## Constraints

- **Never rename or drop existing columns in `silver.orders` or
  `silver.order_items`** — the dashboard's `build.py` reads them by name.
  Additive only (see `customer_order_seq`/`is_first_order` for the pattern).
- `warehouse.duckdb` takes a process-level lock. If a VS Code DB-viewer
  extension or another `duckdb.connect()` has it open, pipeline runs fail
  with `IO Error: Could not set lock`. Close the other connection first.
  (The dashboard's `build.py` already works around this with a read-only +
  copy-on-lock fallback — new consumers should do the same.)
- `duckdb.connect(...).execute(...).fetchdf()` requires `pandas`, which
  isn't installed — use `.fetchall()` or the `duckdb` CLI instead.
- DuckDB's regex engine (RE2) has no lookahead — line-item splitting in
  `pipeline/silver.py` and item-price inference in `pipeline/gold.py` are
  done in Python, not SQL, for this reason.
- Don't move `warehouse.duckdb` out of the repo root without updating
  `the-oven-vibe-dashboard/build.py`'s `DEFAULT_DB` path.
- `gold.data_quality`'s `missing_week_gaps` check reads filenames under
  `data/order_history/*.csv` (`order_history_YYYYMMDD_YYYYMMDD.csv`) — it's a
  real gap (2026-06-08 to 2026-06-14 missing at time of writing), not a bug.
  Fix by downloading the missing zip, not by changing the check.
- Keep `CLAUDE.md`, this file, `README.md`, and memory updated after each
  completed task.
