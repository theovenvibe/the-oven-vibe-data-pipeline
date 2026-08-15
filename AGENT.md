# AGENT.md

Agent-facing operating notes for this repo.

## Purpose

Turn manually-downloaded Zomato order-history CSV exports plus a hand-maintained
menu master into a queryable DuckDB warehouse (bronze/silver/menu/gold) for
`the-oven-vibe-dashboard` to consume. Read that repo's `docs/ANALYTICS_SPEC.md`
before changing gold tables — it's the contract for what the numbers mean and
how the dashboard uses them.

## Key commands

- `uv run python main.py` — run full pipeline once (bronze -> silver -> menu -> gold, plus `pipeline.direct` if `OVEN_VIBE_ADMIN_TOKEN` is set)
- `uv run python watch_pipeline.py` — auto-rebuild whenever a CSV or `menu.csv` changes under `data/`
- `duckdb warehouse.duckdb -c "..."` — inspect the warehouse directly (CLI, not the Python API — avoids the pandas dependency issue below)

## Phase 8 — direct orders from the backend (added 2026-08-16)

`pipeline/direct.py` pulls confirmed direct orders from
`../the-oven-vibe-backend`'s D1 database via its deployed Worker
(`GET /admin/api/export/orders`) and loads them into
`bronze.direct_orders_raw` / `silver.direct_orders` /
`silver.direct_order_items`, tagged `source = 'direct'`. `main.py` runs it
automatically whenever `OVEN_VIBE_ADMIN_TOKEN` is set in the environment
(copy `.env.example` to `.env`, value in
`~/workbench/the-oven-vibe/CREDENTIALS.local.md`); skipped with a message,
not a failure, when absent — a fresh clone or a CI run with no secret still
produces the Zomato-only warehouse this pipeline has always built.

`gold.combined_weekly_sales` (built by `gold.build_combined_weekly_sales`)
unions Zomato's `silver.orders` and `silver.direct_orders` by week,
**deliberately a UNION, never a JOIN** — this repo's Zomato export keys
customers by a frequently-masked `Customer Phone`; the backend keys by a
normalised 10-digit phone it owns end to end. Blending the two into one
customer identity would produce unverifiable false merges, so direct and
Zomato orders stay side by side (`source` column), not merged into one
customer or one order stream. `the-oven-vibe-dashboard` renders this as
"Direct vs Zomato" on the Plan tab and in `weekly_brief.md`.

No true unattended nightly cron was built (considered: a GitHub Actions
schedule in this public repo). Rejected because the Zomato half of this
warehouse depends on manually-downloaded CSVs that live outside git by
design (`data/` is gitignored) — a CI runner would have no Zomato data to
rebuild against, so full automation would mean restructuring this
project's local-first architecture, out of scope for what was asked.
Instead, the direct-orders pull runs automatically as part of every
`main.py`/`ov sync` run, which already happens on the owner's normal weekly
ingest rhythm — direct orders are never more than one sync away from
current.

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
