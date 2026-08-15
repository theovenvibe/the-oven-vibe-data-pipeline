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
