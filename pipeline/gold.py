"""Build the gold layer: business-ready aggregates over silver.orders / silver.order_items."""

import csv
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"
ORDER_HISTORY_DIR = ROOT_DIR / "data" / "order_history"

# The owner's own price sheet, lifted from the Control Room report. It carries
# the Zomato list price ("do not change"), the current landing price after the
# offer ("SET THIS price"), the local price and the making cost -- all of them
# decided by the owner rather than reverse-engineered from bills. When an item
# is in here, this file wins over anything inferred below.
PRICE_SHEET_FILE = ROOT_DIR / "data" / "price_sheet.csv"

WEEK_FILE_RE = re.compile(r"order_history_(\d{8})_(\d{8})\.csv$")


def main():
    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")

    con.execute("DROP TABLE IF EXISTS gold.weekly_sales")
    con.execute("""
        CREATE TABLE gold.weekly_sales AS
        SELECT
            restaurant_id,
            restaurant_name,
            date_trunc('week', order_placed_at) AS week_start,
            count(*) AS order_count,
            count(*) FILTER (WHERE order_status = 'Delivered') AS delivered_count,
            count(*) FILTER (WHERE order_status != 'Delivered') AS cancelled_count,
            sum(total) FILTER (WHERE order_status = 'Delivered') AS total_revenue,
            avg(total) FILTER (WHERE order_status = 'Delivered') AS avg_order_value,
            avg(rating) AS avg_rating
        FROM silver.orders
        GROUP BY 1, 2, 3
        ORDER BY 3
    """)

    con.execute("DROP TABLE IF EXISTS gold.item_performance")
    con.execute("""
        CREATE TABLE gold.item_performance AS
        SELECT
            o.restaurant_id,
            o.restaurant_name,
            i.item_name,
            count(*) AS times_ordered,
            sum(i.quantity) AS total_qty
        FROM silver.order_items i
        JOIN silver.orders o USING (order_id)
        GROUP BY 1, 2, 3
        ORDER BY total_qty DESC
    """)

    con.execute("DROP TABLE IF EXISTS gold.customer_summary")
    con.execute("""
        CREATE TABLE gold.customer_summary AS
        SELECT
            restaurant_id,
            restaurant_name,
            customer_id,
            count(*) AS order_count,
            sum(total) FILTER (WHERE order_status = 'Delivered') AS total_spend,
            avg(total) FILTER (WHERE order_status = 'Delivered') AS avg_order_value,
            min(order_placed_at) AS first_order_at,
            max(order_placed_at) AS last_order_at,
            avg(rating) AS avg_rating_given
        FROM silver.orders
        GROUP BY 1, 2, 3
        ORDER BY total_spend DESC NULLS LAST
    """)

    con.execute("DROP TABLE IF EXISTS gold.ops_quality")
    con.execute("""
        CREATE TABLE gold.ops_quality AS
        SELECT
            restaurant_id,
            restaurant_name,
            date_trunc('week', order_placed_at) AS week_start,
            avg(kpt_duration_min) AS avg_kpt_duration_min,
            avg(rider_wait_min) AS avg_rider_wait_min,
            (count(*) FILTER (WHERE order_status != 'Delivered')) / count(*)::DOUBLE AS cancellation_rate,
            count(*) FILTER (WHERE customer_complaint_tag IS NOT NULL) AS complaint_count
        FROM silver.orders
        GROUP BY 1, 2, 3
        ORDER BY 3
    """)

    build_item_prices(con)
    build_data_quality(con)
    build_combined_weekly_sales(con)

    for table in (
        "weekly_sales",
        "item_performance",
        "customer_summary",
        "ops_quality",
        "item_prices",
        "data_quality",
        "combined_weekly_sales",
    ):
        n = con.execute(f"SELECT count(*) FROM gold.{table}").fetchone()[0]
        print(f"gold.{table}: {n} rows")

    con.close()


def _mode(values):
    """Most frequent value; rounds to 2dp first to absorb float noise."""
    rounded = [round(v, 2) for v in values]
    return Counter(rounded).most_common(1)[0][0]


# Prefixes the Zomato menu editor bolts onto a name to decorate it. They are
# marketing, not identity: the same pizza appears with and without them in the
# same export.
_DECORATION_RE = re.compile(r"^[^\w(]+")
_BADGE_RE = re.compile(r"^(most ordered|bestseller)\s+", re.I)


def _price_key(name):
    """A name reduced to what identifies the dish: base name plus size.

    Size is kept -- Classic French Fries [Small] and [Large] are two products
    at two prices, and collapsing them would price a small portion as a large
    one. Everything else (emoji, "Most Ordered", trailing bracketed
    descriptions like "(7\u201d Pizza + Small Fries)") is dropped.
    """
    n = _DECORATION_RE.sub("", name or "").strip()
    n = _BADGE_RE.sub("", n).strip()
    m = re.search(r"\[([^\]]*)\]", n)
    size = m.group(1).strip().lower() if m else ""
    base = re.sub(r"\s*\[[^\]]*\]\s*", " ", n)
    base = re.sub(r"\s*\([^)]*\)\s*$", "", base)
    return (re.sub(r"\s+", " ", base).strip().lower(), size)


def _read_price_sheet():
    """The owner's price sheet as {item_name: {column: number}}.

    Absent file is not an error -- a fresh clone has no sheet and falls back to
    inference, which is what this pipeline did for its whole life before the
    sheet existed. Blank cells are dropped rather than read as zero: an item
    with no price on the sheet is one the sheet does not cover, and zero is a
    price.
    """
    if not PRICE_SHEET_FILE.exists():
        return {}
    numeric = (
        "zomato_list_price", "zomato_set_price", "local_price",
        "local_offer_price", "making_cost", "zomato_receipt",
    )
    out = {}
    with PRICE_SHEET_FILE.open(newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("item_name") or "").strip()
            if not name:
                continue
            parsed = {}
            for col in numeric:
                raw = (row.get(col) or "").strip()
                if raw:
                    try:
                        parsed[col] = float(raw)
                    except ValueError:
                        pass
            out[name] = parsed
    return out


def build_item_prices(con):
    """Infer a single unit price per item.

    0. sheet: the owner's price sheet (data/price_sheet.csv), where it covers
       the item. Its `zomato_list_price` is the list price the whole discount
       calculation is built on, and it is a decision rather than an inference,
       so nothing below is allowed to overrule it. `zomato_set_price` -- the
       post-offer landing price -- is carried alongside for the dashboard, not
       used as the list price: allocating an order's receipts against an
       already-discounted price would take the discount off twice.
    1. observed: mode of bill_subtotal over orders containing exactly that
       item at quantity 1 (single-item, single-unit orders).
    2. derived: for orders where exactly one item's price is still unknown
       and every other item's price is known, solve
       price = (bill_subtotal - sum(known_price * qty)) / qty, take the mode
       across such orders. Iterate until nothing new resolves.
    3. listed: fall back to silver.menu_items.list_price if set.
    4. unknown: leave unit_price NULL.
    """
    all_items = [
        r[0] for r in con.execute(
            "SELECT DISTINCT item_name FROM silver.order_items"
        ).fetchall()
    ]

    orders_by_id = defaultdict(list)
    for order_id, item_name, quantity in con.execute("""
        SELECT order_id, item_name, quantity
        FROM silver.order_items
        WHERE quantity IS NOT NULL
    """).fetchall():
        orders_by_id[order_id].append((item_name, quantity))

    bill_subtotal = dict(con.execute("""
        SELECT order_id, bill_subtotal FROM silver.orders
        WHERE bill_subtotal IS NOT NULL
    """).fetchall())

    # method: which arithmetic produced the price (audit trail).
    # confidence: the ANALYTICS_SPEC-facing category ("observed"/"derived"/
    # "listed"/"unknown") the dashboard keys its UI trust language off of.
    prices = {}  # item_name -> (unit_price, confidence, sample_n, method)

    sheet = _read_price_sheet()
    # The Zomato export decorates its own item names -- a flame or a star in
    # front of a bestseller, "Most Ordered" bolted on, the size suffix present
    # on one row and missing on another. The sheet uses the catalogue names.
    # Matching on a normalised form is what makes 15 of 34 items become 30.
    sheet_by_key = {}
    sheet_by_base = defaultdict(list)
    for name, row in sheet.items():
        key = _price_key(name)
        sheet_by_key.setdefault(key, (name, row))
        sheet_by_base[key[0]].append((name, row))
    for item_name in all_items:
        key = _price_key(item_name)
        hit = sheet_by_key.get(key)
        if hit is None:
            # The export sometimes drops the size suffix entirely -- plain
            # "Herb Paneer Delight Pizza" is the same pizza as the sheet's
            # "[Regular, 7 inches]" row, because there is only one. Fall back
            # to the base name ONLY when the sheet has a single size for it:
            # fries come in Small and Large at different prices, and guessing
            # which one a sizeless row meant would price half of them wrong.
            candidates = sheet_by_base.get(key[0], [])
            hit = candidates[0] if len(candidates) == 1 else None
        if not hit:
            continue
        source_name, row = hit
        listed = row.get("zomato_list_price")
        if listed is None:
            continue
        prices[item_name] = (
            listed, "sheet", None,
            f"data/price_sheet.csv zomato_list_price, matched to '{source_name}'",
        )

    single_item_values = defaultdict(list)
    for order_id, items in orders_by_id.items():
        if len(items) == 1 and items[0][1] == 1 and order_id in bill_subtotal:
            single_item_values[items[0][0]].append(bill_subtotal[order_id])

    for item_name, values in single_item_values.items():
        if item_name in prices:
            continue  # the sheet already decided this one
        prices[item_name] = (
            _mode(values), "observed", len(values),
            "mode of bill_subtotal, single-item qty=1 orders",
        )

    while True:
        candidates = defaultdict(list)
        for order_id, items in orders_by_id.items():
            if order_id not in bill_subtotal:
                continue
            unknown = [it for it in items if it[0] not in prices]
            if len(unknown) != 1:
                continue
            unknown_name, unknown_qty = unknown[0]
            if unknown_qty in (None, 0):
                continue
            known_sum = sum(
                prices[name][0] * qty for name, qty in items if name in prices
            )
            candidates[unknown_name].append(
                (bill_subtotal[order_id] - known_sum) / unknown_qty
            )

        new_resolutions = {
            name: vals for name, vals in candidates.items() if name not in prices
        }
        if not new_resolutions:
            break
        for item_name, vals in new_resolutions.items():
            prices[item_name] = (
                _mode(vals), "derived", len(vals),
                "residual solve: (bill_subtotal - known items) / qty, mode across orders",
            )

    menu_list_prices = dict(con.execute("""
        SELECT item_name, list_price FROM silver.menu_items
        WHERE list_price IS NOT NULL
    """).fetchall())

    for item_name in all_items:
        if item_name in prices:
            continue
        if item_name in menu_list_prices:
            prices[item_name] = (
                menu_list_prices[item_name], "listed", None, "menu.csv list_price",
            )
        else:
            prices[item_name] = (None, "unknown", None, "no observation or menu price available")

    con.execute("DROP TABLE IF EXISTS gold.item_prices")
    con.execute("""
        CREATE TABLE gold.item_prices (
            item_name VARCHAR,
            unit_price DOUBLE,
            confidence VARCHAR,
            sample_n INTEGER,
            method VARCHAR
        )
    """)
    con.executemany(
        "INSERT INTO gold.item_prices VALUES (?, ?, ?, ?, ?)",
        [(name, *vals) for name, vals in prices.items()],
    )


def build_combined_weekly_sales(con):
    """Phase 8 (backend PRD §11 row 8) — direct D1 orders and Zomato orders,
    side by side by week, tagged by `source`. Deliberately a UNION, never a
    JOIN: the two systems key customers differently (this repo's Zomato
    export by a frequently-masked `Customer Phone`; the backend by a
    normalised 10-digit phone it owns end to end) and blending them into one
    customer identity here would produce false merges with no way to verify
    them — see pipeline/direct.py's own note. This table answers "how much
    business came through which channel," not "who is the same person on
    both."

    `silver.direct_orders` only exists once `pipeline.direct` has run at
    least once (it needs `OVEN_VIBE_ADMIN_TOKEN`); when it's absent this
    still produces a valid Zomato-only table so `gold.py` never breaks on an
    older warehouse or a token-less run — same degrade-gracefully contract
    the dashboard repo's `analytics.py` already follows for gold.item_prices
    etc.
    """
    has_direct = con.execute("""
        SELECT count(*) FROM information_schema.tables
        WHERE table_schema = 'silver' AND table_name = 'direct_orders'
    """).fetchone()[0] > 0

    con.execute("DROP TABLE IF EXISTS gold.combined_weekly_sales")
    zomato_select = """
        SELECT
            date_trunc('week', order_placed_at) AS week_start,
            'zomato' AS source,
            count(*) AS order_count,
            count(*) FILTER (WHERE order_status = 'Delivered') AS confirmed_count,
            sum(total) FILTER (WHERE order_status = 'Delivered') AS revenue
        FROM silver.orders
        GROUP BY 1, 2
    """
    if has_direct:
        con.execute(f"""
            CREATE TABLE gold.combined_weekly_sales AS
            {zomato_select}
            UNION ALL
            SELECT
                date_trunc('week', order_placed_at) AS week_start,
                'direct' AS source,
                count(*) AS order_count,
                count(*) AS confirmed_count,  -- silver.direct_orders is confirmed-only by construction (PRD §7.3)
                sum(total) AS revenue
            FROM silver.direct_orders
            GROUP BY 1, 2
            ORDER BY 1, 2
        """)
    else:
        con.execute(f"""
            CREATE TABLE gold.combined_weekly_sales AS
            {zomato_select}
            ORDER BY 1, 2
        """)


def _find_week_gaps():
    """Parse data/order_history/*.csv filenames for date-range gaps."""
    ranges = []
    for f in sorted(ORDER_HISTORY_DIR.glob("*.csv")):
        m = WEEK_FILE_RE.search(f.name)
        if not m:
            continue
        start = datetime.strptime(m.group(1), "%Y%m%d").date()
        end = datetime.strptime(m.group(2), "%Y%m%d").date()
        ranges.append((start, end))
    ranges.sort()

    gaps = []
    for (start, end), (next_start, _) in zip(ranges, ranges[1:]):
        if next_start > end + timedelta(days=1):
            gaps.append((end + timedelta(days=1), next_start - timedelta(days=1)))
    return gaps


def build_data_quality(con):
    checks = []

    gaps = _find_week_gaps()
    checks.append((
        "missing_week_gaps",
        "warn" if gaps else "ok",
        "Weeks with no source CSV: " + (
            ", ".join(f"{g[0]} to {g[1]}" for g in gaps) if gaps else "none"
        ),
        len(gaps),
    ))

    dup_order_ids = con.execute("""
        SELECT count(*) - count(DISTINCT "Order ID") FROM bronze.order_history_raw
    """).fetchone()[0]
    checks.append((
        "duplicate_order_ids_bronze",
        "fail" if dup_order_ids > 0 else "ok",
        f"{dup_order_ids} duplicate Order ID rows in bronze.order_history_raw",
        dup_order_ids,
    ))

    unparsed_dates = con.execute("""
        SELECT count(*) FROM bronze.order_history_raw
        WHERE nullif("Order Placed At", '') IS NOT NULL
          AND try_strptime("Order Placed At", '%I:%M %p, %B %d %Y') IS NULL
    """).fetchone()[0]
    checks.append((
        "unparsed_order_placed_at",
        "fail" if unparsed_dates > 0 else "ok",
        f"{unparsed_dates} bronze rows where Order Placed At failed to parse",
        unparsed_dates,
    ))

    unparsed_qty = con.execute("""
        SELECT count(*) FROM silver.order_items WHERE quantity IS NULL
    """).fetchone()[0]
    checks.append((
        "unparsed_item_quantity",
        "fail" if unparsed_qty > 0 else "ok",
        f"{unparsed_qty} silver.order_items rows where quantity failed to parse",
        unparsed_qty,
    ))

    empty_item_orders = con.execute("""
        SELECT count(*) FROM silver.orders o
        LEFT JOIN silver.order_items i ON o.order_id = i.order_id
        WHERE i.order_id IS NULL
    """).fetchone()[0]
    checks.append((
        "orders_with_no_items",
        "fail" if empty_item_orders > 0 else "ok",
        f"{empty_item_orders} orders in silver.orders with no matching line items",
        empty_item_orders,
    ))

    unknown_price_items = con.execute("""
        SELECT count(*) FROM gold.item_prices WHERE confidence = 'unknown'
    """).fetchone()[0]
    checks.append((
        "items_with_unknown_price",
        "warn" if unknown_price_items > 0 else "ok",
        f"{unknown_price_items} items in gold.item_prices with no resolvable price",
        unknown_price_items,
    ))

    con.execute("DROP TABLE IF EXISTS gold.data_quality")
    con.execute("""
        CREATE TABLE gold.data_quality (
            check_name VARCHAR,
            status VARCHAR,
            detail VARCHAR,
            value INTEGER
        )
    """)
    con.executemany(
        "INSERT INTO gold.data_quality VALUES (?, ?, ?, ?)", checks
    )


if __name__ == "__main__":
    main()
