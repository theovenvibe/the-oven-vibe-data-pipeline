# CLAUDE.md

Project instructions for Claude.

## Overview

Zomato order-history data pipeline for The Oven Vibe restaurant. Builds a
medallion-architecture DuckDB warehouse (bronze -> silver -> menu -> gold) from
weekly order-history CSV exports plus a hand-maintained menu master, and feeds
`../the-oven-vibe-dashboard` which reads `warehouse.duckdb` directly to render
a static dashboard per `../the-oven-vibe-dashboard/docs/ANALYTICS_SPEC.md`.

## Setup

```
uv sync
```

Data source: user manually downloads weekly order-history zips from the
Zomato partner dashboard (https://www.zomato.com/partners/onlineordering/orderHistory/)
and extracts the CSVs into `data/order_history/`. An earlier Camoufox/Playwright
scraper approach was tried and scrapped in favor of manual download.

`data/menu.csv` (item_name, category, subcategory, is_veg, size, list_price,
unit_cost, active) is hand-maintained by the restaurant owner, not generated —
seeded once with the 28 items observed in the data. `unit_cost` is left blank
for the owner to fill in; the dashboard's contribution-margin view only
activates once it's populated.

## Running the pipeline

```
uv run python main.py              # bronze -> silver -> menu -> gold, one-shot
uv run python watch_pipeline.py    # watches data/ (order_history CSVs + menu.csv), rebuilds on change
```

Each layer is also runnable standalone and is idempotent (drop+recreate):
`uv run python -m pipeline.bronze`, `pipeline.silver`, `pipeline.menu`, `pipeline.gold`,
`pipeline.direct` (needs `OVEN_VIBE_ADMIN_TOKEN` — see below).

## Architecture

- `pipeline/bronze.py` — loads all CSVs in `data/order_history/*.csv` as-is
  (all-VARCHAR) into `bronze.order_history_raw`, tagged with `_source_file`
  and `_loaded_at`.
- `pipeline/silver.py` — types + cleans into `silver.orders` (one row per
  Order ID, deduped by `_loaded_at`, plus `customer_order_seq` and
  `is_first_order` computed per customer) and `silver.order_items` (exploded
  line items — split in Python, not SQL, since DuckDB's RE2 regex engine has
  no lookahead support).
- `pipeline/menu.py` — loads `data/menu.csv` into `silver.menu_items` and
  prints how many `silver.order_items` rows fail to match by `item_name`
  (should always be 0 — if a new item appears in an order export, add it to
  menu.csv).
- `pipeline/gold.py` — business aggregates: `gold.weekly_sales`,
  `gold.item_performance`, `gold.customer_summary`, `gold.ops_quality`,
  `gold.item_prices`, `gold.data_quality`. Revenue figures only count
  `order_status = 'Delivered'` rows.
  - `gold.item_prices` (item_name, unit_price, confidence, sample_n, method):
    infers one price per item since bronze only has order-level totals, not
    line-item prices. `confidence` is the ANALYTICS_SPEC-facing category
    (`observed` / `derived` / `listed` / `unknown`); `method` is a plain-text
    audit trail of the arithmetic. Resolution order: mode of `bill_subtotal`
    over single-item qty=1 orders ("observed") -> iterative residual solve
    over multi-item orders with exactly one unknown-priced item ("derived") ->
    `menu.csv` `list_price` fallback ("listed") -> `NULL` ("unknown").
  - `gold.data_quality` (check_name, status, detail, value): pipeline
    trust-panel checks — status is `ok` / `warn` / `fail`. Includes the
    2026-06-08 to 2026-06-14 missing-week gap (a missing download, not a bug —
    `warn`, not `fail`), duplicate Order IDs, unparsed dates/quantities,
    orders with no line items, and items with unresolved price.
- `warehouse.duckdb` lives at repo root — this exact path is the contract
  with the dashboard repo's `build.py` (`DEFAULT_DB`), which opens it
  read-only with a lock-safe copy fallback. Don't move it without updating
  that consumer too.
- **Hard constraint:** never rename or drop existing columns in
  `silver.orders` or `silver.order_items` — the dashboard's `build.py` reads
  them by name. New fields are additive only.

## `pipeline/direct.py` — Phase 8, direct orders from the backend

Pulls confirmed direct orders from `../the-oven-vibe-backend`'s D1 via
`GET /admin/api/export/orders` on the deployed Worker and loads
`bronze.direct_orders_raw` / `silver.direct_orders` / `silver.direct_order_items`
(`source = 'direct'`). Needs `OVEN_VIBE_ADMIN_TOKEN` in the environment
(`.env.example` has the shape; `main.py` skips this stage quietly when it's
unset). `gold.py`'s `build_combined_weekly_sales` unions this with
`silver.orders` (Zomato) into `gold.combined_weekly_sales`, by week and
`source` — a UNION, not a JOIN, since the two systems key customer identity
differently and merging them would produce unverifiable false matches. See
`AGENT.md`'s "Phase 8" section for the full reasoning, including why this
isn't a true unattended nightly cron.

## Conventions

- Update this file, `AGENT.md`, `README.md`, and memory after completing
  each task — keep docs in sync with the current state, not just the code.
- New pipeline stages follow the `bronze.py`/`silver.py`/`gold.py` pattern:
  a `main()` that connects to `warehouse.duckdb`, drop+recreates its
  table(s), prints row counts.
