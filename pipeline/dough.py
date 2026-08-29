"""Pull the Dough ledger, balances and referrals from D1 into the warehouse.

Phase 9 of the Dough plan (`../the-oven-vibe-backend/docs/superpowers/plans/
2026-08-19-dough-and-referrals.md`). It exists so the owner can answer a
customer's question from his own laptop — *"how much Dough does 6371197255 have
and where did it come from?"* — without a Cloudflare login.

Source: `GET /admin/api/export/dough`, admin-token gated because it carries
phone numbers. A full snapshot each run, and these tables are dropped and
recreated, matching the convention every other module here follows: re-pulling
the same snapshot twice is harmless.

**The ledger is the truth; the balance column is a cache.** The backend writes
both in one transaction, but if they ever disagree the ledger wins — which is
why `silver.dough_balances_derived` recomputes from the ledger and
`gold.dough_balance_check` shows any row where the two differ. A loyalty scheme
whose balance nobody can audit is a scheme you cannot defend to a customer.
"""

import json
from pathlib import Path

import duckdb

from pipeline import access

ROOT_DIR = Path(__file__).parent.parent
DB_FILE = ROOT_DIR / "warehouse.duckdb"


def _fetch_snapshot() -> dict:
    """The Dough ledger snapshot. Same two locks as direct.py."""
    return access.fetch_admin_json("/admin/api/export/dough")


def main() -> None:
    payload = _fetch_snapshot()
    con = duckdb.connect(str(DB_FILE))
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("CREATE SCHEMA IF NOT EXISTS silver")
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")

    # Bronze: exactly what the Worker sent, one JSON blob per row, so a change
    # in the API shape can never silently drop a column on the way in.
    for name in ("ledger", "balances", "referrals"):
        rows = [(json.dumps(r),) for r in payload.get(name, [])]
        con.execute(f"CREATE OR REPLACE TABLE bronze.dough_{name} (raw JSON)")
        if rows:
            con.executemany(f"INSERT INTO bronze.dough_{name} VALUES (?)", rows)

    con.execute("""
        CREATE OR REPLACE TABLE silver.dough_ledger AS
        SELECT
            try_cast(raw->>'id' AS INTEGER)          AS entry_id,
            try_cast(raw->>'customer_id' AS INTEGER) AS customer_id,
            raw->>'customer_phone'                   AS customer_phone,
            try_cast(raw->>'delta' AS INTEGER)       AS delta,
            raw->>'kind'                             AS kind,
            try_cast(raw->>'order_id' AS INTEGER)    AS order_id,
            raw->>'note'                             AS note,
            try_cast(raw->>'created_at' AS TIMESTAMP) AS created_at
        FROM bronze.dough_ledger
    """)

    con.execute("""
        CREATE OR REPLACE TABLE silver.dough_balances AS
        SELECT
            try_cast(raw->>'customer_id' AS INTEGER)      AS customer_id,
            raw->>'phone'                                 AS phone,
            raw->>'name'                                  AS name,
            try_cast(raw->>'dough_balance' AS INTEGER)    AS balance_cached,
            try_cast(raw->>'dough_expires_at' AS TIMESTAMP) AS expires_at,
            raw->>'referral_code'                         AS referral_code
        FROM bronze.dough_balances
    """)

    con.execute("""
        CREATE OR REPLACE TABLE silver.referrals AS
        SELECT
            try_cast(raw->>'id' AS INTEGER)                        AS referral_id,
            try_cast(raw->>'referrer_customer_id' AS INTEGER)      AS referrer_customer_id,
            raw->>'referrer_phone'                                 AS referrer_phone,
            raw->>'invitee_phone'                                  AS invitee_phone,
            try_cast(raw->>'order_id' AS INTEGER)                  AS order_id,
            raw->>'status'                                         AS status,
            try_cast(raw->>'same_device' AS INTEGER) = 1           AS same_device,
            try_cast(raw->>'created_at' AS TIMESTAMP)              AS created_at,
            try_cast(raw->>'rewarded_at' AS TIMESTAMP)             AS rewarded_at
        FROM bronze.dough_referrals
    """)

    # The ledger is the truth, so the balance is recomputed here rather than
    # trusted. Reminders carry delta = 0 and fall out of the sum on their own.
    con.execute("""
        CREATE OR REPLACE TABLE silver.dough_balances_derived AS
        SELECT customer_id, customer_phone, SUM(delta) AS balance_from_ledger
        FROM silver.dough_ledger GROUP BY customer_id, customer_phone
    """)

    # Any row here is a bug worth knowing about before a customer finds it.
    con.execute("""
        CREATE OR REPLACE VIEW gold.dough_balance_check AS
        SELECT b.customer_id, b.phone, b.name,
               b.balance_cached, d.balance_from_ledger,
               b.balance_cached - d.balance_from_ledger AS difference
        FROM silver.dough_balances b
        LEFT JOIN silver.dough_balances_derived d USING (customer_id)
        WHERE coalesce(b.balance_cached, 0) <> coalesce(d.balance_from_ledger, 0)
    """)

    # The everyday question, ready to query: one row per customer.
    con.execute("""
        CREATE OR REPLACE VIEW gold.dough_customers AS
        SELECT b.phone, b.name, b.balance_cached AS balance, b.expires_at, b.referral_code,
               coalesce(sum(CASE WHEN l.delta > 0 THEN l.delta END), 0) AS earned_all_time,
               coalesce(-sum(CASE WHEN l.kind = 'spend' THEN l.delta END), 0) AS spent_all_time,
               count(CASE WHEN l.kind = 'referral' THEN 1 END) AS referral_entries
        FROM silver.dough_balances b
        LEFT JOIN silver.dough_ledger l USING (customer_id)
        GROUP BY 1, 2, 3, 4, 5
    """)

    counts = {
        name: con.execute(f"SELECT count(*) FROM silver.{table}").fetchone()[0]
        for name, table in (
            ("ledger entries", "dough_ledger"),
            ("customers with a balance or code", "dough_balances"),
            ("referrals", "referrals"),
        )
    }
    for label, n in counts.items():
        print(f"{label}: {n}")
    mismatches = con.execute("SELECT count(*) FROM gold.dough_balance_check").fetchone()[0]
    print(
        "balance check: OK — cache agrees with the ledger"
        if mismatches == 0
        else f"balance check: {mismatches} customer(s) where the cache and the ledger DISAGREE"
    )
    con.close()


if __name__ == "__main__":
    main()
