"""Pull confirmed direct orders from the-oven-vibe-backend's D1 database.

Phase 8 of the backend PRD (`../the-oven-vibe-backend/PRD.md` §11 row 8):
"nightly D1 pull into the DuckDB warehouse," closing the blind spot where
this warehouse only ever saw Zomato exports and never the site's own direct
WhatsApp/pickup orders (`../the-oven-vibe-backend/HANDOFF.md`'s Phase 8
brief).

Source: `GET /admin/api/export/orders` on the deployed Worker — a full
snapshot of every **confirmed** order (never pending/dropped: a click is not
an order, same rule this whole ecosystem follows), admin-token gated because
it's customer PII (phone, name, locality). Full snapshot every pull, not an
incremental cursor — see that endpoint's own comment in
`the-oven-vibe-backend/src/analytics-export.ts` for why that's the right
call at this data volume. This module drops and recreates its tables every
run, matching bronze.py/silver.py/gold.py's own idempotent convention, so
re-pulling the same snapshot twice is harmless.

Credentials: `OVEN_VIBE_WORKER_URL` (defaults to the live Worker) and
`OVEN_VIBE_ADMIN_TOKEN` (required, no default — same token documented in
`~/workbench/the-oven-vibe/CREDENTIALS.local.md`, which sits outside every
git repo). Read from the environment, never hard-coded or committed;
`.env.example` documents the shape and `.env` (gitignored) holds the real
value locally. The nightly GitHub Actions workflow
(`.github/workflows/nightly-sync.yml`) supplies the same two as repo secrets.

**Identity, deliberately not merged with Zomato's:** this backend keys
customers by phone (10-digit, normalised); Zomato's export has its own
`Customer ID`/`Customer Phone` that is frequently masked or absent
(`silver.orders.customer_phone` is often NULL). Blending the two into one
customer identity risks false merges with no way to verify them, so this
stays a separate, clearly `source`-tagged stream — every table this module
writes has a `source = 'direct'` column, and `gold.py`'s combined view unions
rather than joins. "Side by side, not blended" is Phase 8's own success
measure (PRD §12).
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import duckdb

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"

DEFAULT_WORKER_URL = "https://oven-vibe-backend.theovenvibe.workers.dev"


def _fetch_snapshot() -> dict:
    worker_url = os.environ.get("OVEN_VIBE_WORKER_URL", DEFAULT_WORKER_URL).rstrip("/")
    token = os.environ.get("OVEN_VIBE_ADMIN_TOKEN")
    if not token:
        raise SystemExit(
            "OVEN_VIBE_ADMIN_TOKEN is not set. Copy .env.example to .env and fill it in "
            "(see ~/workbench/the-oven-vibe/CREDENTIALS.local.md for the value), or export "
            "it in the shell/CI secret running this."
        )

    req = urllib.request.Request(
        f"{worker_url}/admin/api/export/orders",
        headers={
            "Authorization": f"Bearer {token}",
            # Cloudflare's bot protection (error 1010) blocks Python's
            # default urllib User-Agent on *.workers.dev; a normal-looking
            # one clears it. Not a security control on our side either way —
            # the real gate is the ADMIN_TOKEN check above.
            "User-Agent": "Mozilla/5.0 (compatible; oven-vibe-data-pipeline/1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"export pull failed: HTTP {e.code} {e.reason} — {e.read().decode(errors='replace')}")
    except urllib.error.URLError as e:
        raise SystemExit(f"export pull failed: could not reach {worker_url} ({e.reason})")

    if not payload.get("ok"):
        raise SystemExit(f"export pull failed: Worker returned {payload}")
    return payload


def main():
    payload = _fetch_snapshot()
    orders = payload["orders"]

    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")

    # Bronze: the raw snapshot, one row per order, items kept as a JSON
    # string — mirrors bronze.py's "as-is, no transformation" discipline.
    con.execute("DROP TABLE IF EXISTS bronze.direct_orders_raw")
    con.execute("""
        CREATE TABLE bronze.direct_orders_raw (
            order_id INTEGER,
            status VARCHAR,
            order_type VARCHAR,
            slot_at VARCHAR,
            distance_band VARCHAR,
            locality VARCHAR,
            food_total INTEGER,
            delivery_fee INTEGER,
            total INTEGER,
            created_at VARCHAR,
            confirmed_at VARCHAR,
            customer_phone VARCHAR,
            customer_name VARCHAR,
            items_json VARCHAR,
            _generated_at VARCHAR,
            _loaded_at TIMESTAMP
        )
    """)
    generated_at = payload.get("generated_at")
    # A quiet launch week (or a brand-new backend with zero confirmed orders
    # yet, as of this Phase 8 build) is a real, expected state — not an
    # error. executemany() rejects an empty parameter list outright, so
    # guard it rather than let a normal "nothing confirmed yet" pull crash
    # the whole main.py run.
    if orders:
        con.executemany(
            "INSERT INTO bronze.direct_orders_raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, current_timestamp)",
            [
                (
                    o["order_id"], o["status"], o["order_type"], o.get("slot_at"),
                    o.get("distance_band"), o.get("locality"),
                    o["food_total"], o["delivery_fee"], o["total"],
                    o["created_at"], o.get("confirmed_at"),
                    o["customer_phone"], o["customer_name"],
                    json.dumps(o["items"]), generated_at,
                )
                for o in orders
            ],
        )

    # Silver: typed, one row per order — column names chosen to line up with
    # silver.orders (order_id, order_placed_at, order_status, total,
    # customer_phone) so gold.py can UNION the two without a translation
    # layer, per this module's own "side by side, not blended" note above.
    con.execute("DROP TABLE IF EXISTS silver.direct_orders")
    con.execute("""
        CREATE TABLE silver.direct_orders AS
        SELECT
            order_id,
            'direct' AS source,
            status AS order_status,        -- always 'confirmed' — the export never sends pending/dropped
            order_type,
            try_cast(slot_at AS TIMESTAMP) AS slot_at,
            distance_band,
            locality,
            food_total,
            delivery_fee,
            total,
            try_cast(created_at AS TIMESTAMP) AS created_at,
            try_cast(confirmed_at AS TIMESTAMP) AS confirmed_at,
            try_cast(confirmed_at AS TIMESTAMP) AS order_placed_at,  -- the direct-order equivalent of Zomato's order_placed_at: when it became real (PRD §7.3)
            customer_phone,
            customer_name
        FROM bronze.direct_orders_raw
    """)

    con.execute("DROP TABLE IF EXISTS silver.direct_order_items")
    con.execute("""
        CREATE TABLE silver.direct_order_items AS
        SELECT
            order_id,
            'direct' AS source,
            unnest(from_json(items_json, '["JSON"]')) AS item_json
        FROM bronze.direct_orders_raw
    """)
    # Explode the per-item JSON into typed columns in a second pass — DuckDB's
    # from_json + unnest above gives one JSON value per item; pull fields out
    # of it here rather than fighting a single nested SQL expression.
    con.execute("""
        CREATE OR REPLACE TABLE silver.direct_order_items AS
        SELECT
            order_id,
            source,
            item_json->>'catalog_id' AS catalog_id,
            item_json->>'name' AS item_name,
            try_cast(item_json->>'qty' AS INTEGER) AS quantity,
            try_cast(item_json->>'unit_price' AS DOUBLE) AS unit_price
        FROM silver.direct_order_items
    """)

    n_orders = con.execute("SELECT count(*) FROM silver.direct_orders").fetchone()[0]
    n_items = con.execute("SELECT count(*) FROM silver.direct_order_items").fetchone()[0]
    print(f"silver.direct_orders: {n_orders} rows")
    print(f"silver.direct_order_items: {n_items} rows")
    con.close()


if __name__ == "__main__":
    main()
