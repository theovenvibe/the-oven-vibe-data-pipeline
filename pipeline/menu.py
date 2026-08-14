"""Load the hand-maintained menu master (data/menu.csv) into silver.menu_items.

The owner edits this CSV directly (category, is_veg, size, prices, active).
Order items are matched to it by exact item_name; unmatched order items are
expected to be zero once the menu is kept in sync with what's actually sold.
"""

from pathlib import Path

import duckdb

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"
MENU_FILE = ROOT_DIR / "data" / "menu.csv"


def main():
    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")

    con.execute("DROP TABLE IF EXISTS silver.menu_items")
    con.execute(f"""
        CREATE TABLE silver.menu_items AS
        SELECT
            item_name,
            category,
            subcategory,
            is_veg,
            size,
            list_price,
            unit_cost,
            active
        FROM read_csv(
            '{MENU_FILE}',
            columns = {{
                'item_name': 'VARCHAR',
                'category': 'VARCHAR',
                'subcategory': 'VARCHAR',
                'is_veg': 'BOOLEAN',
                'size': 'VARCHAR',
                'list_price': 'DOUBLE',
                'unit_cost': 'DOUBLE',
                'active': 'BOOLEAN'
            }},
            header = true
        )
    """)

    unmatched = con.execute("""
        SELECT count(*)
        FROM silver.order_items oi
        LEFT JOIN silver.menu_items m ON oi.item_name = m.item_name
        WHERE m.item_name IS NULL
    """).fetchone()[0]

    menu_count = con.execute("SELECT count(*) FROM silver.menu_items").fetchone()[0]
    print(f"silver.menu_items: {menu_count} rows")
    print(f"unmatched order_items rows: {unmatched}")
    con.close()


if __name__ == "__main__":
    main()
