"""Build the silver layer: typed, cleaned, deduped orders + exploded line items."""

import re
from pathlib import Path

import duckdb

# Split "1 x Item A, 2 x Item B" into ["1 x Item A", "2 x Item B"] without
# breaking on commas inside item names. DuckDB's RE2 engine has no lookahead
# support, so this split happens in Python before loading.
ITEM_SPLIT_RE = re.compile(r",\s*(?=\d+\s*x\s)")
ITEM_QTY_RE = re.compile(r"^(\d+)\s*x\s*(.*)$")

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"


def main():
    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")

    con.execute("DROP TABLE IF EXISTS silver.orders")
    con.execute("""
        CREATE TABLE silver.orders AS
        SELECT
            * EXCLUDE (_rn),
            row_number() OVER (
                PARTITION BY customer_id ORDER BY order_placed_at
            ) AS customer_order_seq,
            row_number() OVER (
                PARTITION BY customer_id ORDER BY order_placed_at
            ) = 1 AS is_first_order
        FROM (
            SELECT
                "Restaurant ID" AS restaurant_id,
                "Restaurant name" AS restaurant_name,
                "Subzone" AS subzone,
                "City" AS city,
                "Order ID" AS order_id,
                strptime("Order Placed At", '%I:%M %p, %B %d %Y') AS order_placed_at,
                trim("Order Status") AS order_status,
                "Delivery" AS delivery_type,
                try_cast(regexp_replace("Distance", '[^0-9.]', '', 'g') AS DOUBLE) AS distance_km,
                nullif("Instructions", '') AS instructions,
                nullif("Discount construct", '') AS discount_construct,
                try_cast("Bill subtotal" AS DOUBLE) AS bill_subtotal,
                try_cast("Packaging charges" AS DOUBLE) AS packaging_charges,
                try_cast("Restaurant discount (Promo)" AS DOUBLE) AS restaurant_discount_promo,
                try_cast("Restaurant discount (Flat offs, Freebies & others)" AS DOUBLE) AS restaurant_discount_other,
                try_cast("Gold discount" AS DOUBLE) AS gold_discount,
                try_cast("Brand pack discount" AS DOUBLE) AS brand_pack_discount,
                try_cast("Total" AS DOUBLE) AS total,
                try_cast("Rating" AS DOUBLE) AS rating,
                nullif("Review", '') AS review,
                nullif("Cancellation / Rejection reason", '') AS cancellation_reason,
                try_cast("Restaurant compensation (Cancellation)" AS DOUBLE) AS restaurant_compensation,
                try_cast("Restaurant penalty (Rejection)" AS DOUBLE) AS restaurant_penalty,
                try_cast("KPT duration (minutes)" AS DOUBLE) AS kpt_duration_min,
                try_cast("Rider wait time (minutes)" AS DOUBLE) AS rider_wait_min,
                "Order Ready Marked" AS order_ready_marked,
                nullif("Customer complaint tag", '') AS customer_complaint_tag,
                "Customer ID" AS customer_id,
                nullif("Customer Phone", '') AS customer_phone,
                _source_file,
                _loaded_at,
                row_number() OVER (
                    PARTITION BY "Order ID" ORDER BY _loaded_at DESC
                ) AS _rn
            FROM bronze.order_history_raw
        )
        WHERE _rn = 1
    """)

    con.execute("DROP TABLE IF EXISTS silver.order_items")
    con.execute("""
        CREATE TABLE silver.order_items (
            order_id VARCHAR,
            item_seq INTEGER,
            quantity INTEGER,
            item_name VARCHAR
        )
    """)

    raw_orders = con.execute(
        'SELECT "Order ID", "Items in order" FROM bronze.order_history_raw'
    ).fetchall()

    item_rows = []
    for order_id, items_str in raw_orders:
        if not items_str:
            continue
        for seq, raw_item in enumerate(ITEM_SPLIT_RE.split(items_str), start=1):
            m = ITEM_QTY_RE.match(raw_item.strip())
            quantity = int(m.group(1)) if m else None
            item_name = m.group(2).strip() if m else raw_item.strip()
            item_rows.append((order_id, seq, quantity, item_name))

    con.executemany(
        "INSERT INTO silver.order_items VALUES (?, ?, ?, ?)", item_rows
    )

    orders_count = con.execute("SELECT count(*) FROM silver.orders").fetchone()[0]
    items_count = con.execute("SELECT count(*) FROM silver.order_items").fetchone()[0]
    print(f"silver.orders: {orders_count} rows")
    print(f"silver.order_items: {items_count} rows")
    con.close()


if __name__ == "__main__":
    main()
