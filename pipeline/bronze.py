"""Build the bronze layer: raw Zomato order-history CSVs loaded as-is into DuckDB.

Bronze = source data with source metadata (filename, load time) added, no
transformation or type-casting beyond what DuckDB's CSV sniffer infers.
"""

from pathlib import Path

import duckdb

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"
ORDER_HISTORY_DIR = ROOT_DIR / "data" / "order_history"


def main():
    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")

    con.execute("DROP TABLE IF EXISTS bronze.order_history_raw")
    con.execute(f"""
        CREATE TABLE bronze.order_history_raw AS
        SELECT
            * RENAME (filename AS _source_file),
            current_timestamp AS _loaded_at
        FROM read_csv(
            '{ORDER_HISTORY_DIR}/*.csv',
            all_varchar = true,
            filename = true,
            union_by_name = true,
            header = true
        )
    """)

    count = con.execute("SELECT count(*) FROM bronze.order_history_raw").fetchone()[0]
    files = con.execute("SELECT count(DISTINCT _source_file) FROM bronze.order_history_raw").fetchone()[0]
    print(f"bronze.order_history_raw: {count} rows from {files} files")
    con.close()


if __name__ == "__main__":
    main()
