"""Run the full bronze -> silver -> menu -> gold pipeline against warehouse.duckdb.

`ov sync` calls this. By default it pulls D1 as well as the Zomato exports,
and **fails loudly** if it cannot — a sync that quietly imports nothing is
worse than one that stops, because the numbers still look like numbers.
Between 19 and 29 August this printed a cheerful "skipping" line and exited 0
while `silver.direct_orders` sat at zero rows.

Set `OVEN_VIBE_SKIP_D1=1` to run Zomato-only on purpose — a fresh clone, or CI
with no Cloudflare session. That is an explicit choice now, not a default.
"""

import os
import sys

from pipeline import bronze, silver, menu, gold, direct, dough, d1_tables


def main():
    bronze.main()
    silver.main()
    menu.main()

    if os.environ.get("OVEN_VIBE_SKIP_D1") == "1":
        print("OVEN_VIBE_SKIP_D1=1 — running Zomato-only; direct orders and Dough will be stale.")
    else:
        # Phase 8 (backend PRD §11 row 8) — direct orders from D1 alongside the
        # Zomato silver tables. Phase 9 of the Dough plan — the ledger,
        # balances and referrals, so "how much Dough does this number have and
        # where did it come from" is answerable from this laptop.
        try:
            direct.main()
            dough.main()
            # Everything else in D1, mirrored raw into its own `d1` schema —
            # stock, campaigns, rejections, demand signals, kitchen hours.
            # A separate stream from the Zomato exports, deliberately not
            # joined to them.
            d1_tables.main()
        except SystemExit as e:
            print(f"\n✗ The D1 pull failed, so the warehouse is NOT up to date.\n\n{e}\n", file=sys.stderr)
            print("  Re-run with OVEN_VIBE_SKIP_D1=1 to rebuild from Zomato alone.", file=sys.stderr)
            # A bare `raise` would make Python print the same message a second
            # time on its way out. The message above is the useful one.
            raise SystemExit(1) from None

    gold.main()


if __name__ == "__main__":
    main()
