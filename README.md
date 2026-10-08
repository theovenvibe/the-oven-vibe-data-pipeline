## 9 October 2026 — ingredient stock archive

Ingredient counting is retired in the live backend. Exported stock_batches and
stock_moves remain historical snapshots; do not treat them as current balances,
sales-channel counts or ongoing waste metrics. Preserve existing columns and
archive ingestion. stock_waitlist remains active for manual menu availability.
Direct sales, Dough and platform exports remain independent data sources.
See [current policy](https://github.com/theovenvibe/the-oven-vibe-backend/blob/develop/docs/STOCK_RETIREMENT.md) and [release evidence](https://github.com/theovenvibe/the-oven-vibe-backend/blob/develop/docs/STOCK_RETIREMENT_RELEASE.md).

# the-oven-vibe-data-pipeline

Zomato order-history data pipeline for The Oven Vibe restaurant. Loads
weekly order-history CSV exports and a hand-maintained menu master into a
DuckDB warehouse through a bronze -> silver -> menu -> gold medallion
architecture, consumed by `../the-oven-vibe-dashboard`.

## Usage

```
uv sync
uv run python main.py              # run pipeline once
uv run python watch_pipeline.py    # auto-rebuild on changes under data/
```

Optionally also pulls confirmed direct orders from `../the-oven-vibe-backend`'s
D1 database (Phase 8) — copy `.env.example` to `.env`, fill in
`OVEN_VIBE_ADMIN_TOKEN`, and `source ./.env` (or export it) before running.
Skipped quietly when unset.

See `CLAUDE.md` / `AGENT.md` for architecture details and operating notes.
