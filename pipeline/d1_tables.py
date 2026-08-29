"""Mirror the rest of D1 into the warehouse, as-is.

`pipeline/direct.py` and `pipeline/dough.py` pull *shaped* data — orders and
the Dough ledger, joined and renamed for the questions the dashboard already
asks. This pulls everything else raw: stock batches and moves, campaigns and
claims, rejected orders, demand signals, kitchen open/close history, the
settings table.

**These are the direct/offline tables and they are their own stream.** The
Zomato exports are a separate business channel that happens to land in the
same DuckDB file; nothing here is joined to them, and nothing here should be.
Everything this module writes goes into its own `d1` schema, so
`d1.stock_moves` is unambiguously "the live table in Cloudflare" and never
gets confused with a silver table derived from a CSV.

Why raw rather than modelled: before today none of it existed locally at all,
so the useful first step is having it. Shaping a table into silver is worth
doing when a real question needs it — modelling twenty tables nobody has
queried yet is work spent ahead of knowing what it is for.

Drop and recreate on every run, matching bronze.py/silver.py/gold.py's own
idempotent convention. The whole payload is a few hundred rows.

Types are inferred by DuckDB from the JSON itself (`read_json_auto`) rather
than everything being forced to VARCHAR. A count that arrives as a string is a
count nobody can average, and these tables exist to be queried.
"""

import json
import tempfile
from pathlib import Path

import duckdb

from pipeline import access

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"


def main() -> None:
    payload = access.fetch_admin_json("/admin/api/export/tables")
    tables = payload["tables"]
    generated_at = payload["generated_at"]

    # The Worker reports a table it could not read rather than dropping it
    # silently. Surface that here too — a table quietly missing from the
    # warehouse is exactly the failure this whole day was about.
    failed = payload.get("failed") or {}
    for name, message in failed.items():
        print(f"  ! d1.{name} could not be exported: {message}")

    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS d1")

    empty = []
    for name, table in sorted(tables.items()):
        columns = table["columns"]
        rows = table["rows"]

        con.execute(f"DROP TABLE IF EXISTS d1.{name}")

        if not columns:
            # An empty D1 table sends no rows, so there is nothing to read
            # column names from. Recording it as a zero-column table would be
            # a lie about the schema; leaving the old copy in place would be
            # worse. It is dropped and named in the summary instead.
            empty.append(name)
            continue

        # Hand DuckDB the rows as JSON and let it infer the types. Forcing
        # everything to VARCHAR would turn every count and price into a string
        # nobody can average, and these tables exist to be queried.
        records = [dict(zip(columns, row), synced_at=generated_at) for row in rows]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(records, fh)
            tmp = fh.name
        try:
            con.execute(f"CREATE TABLE d1.{name} AS SELECT * FROM read_json_auto('{tmp}')")
        finally:
            Path(tmp).unlink(missing_ok=True)
        print(f"  d1.{name}: {len(rows)} rows")

    if empty:
        print(f"  empty in D1, not created: {', '.join(empty)}")

    con.close()


if __name__ == "__main__":
    main()
