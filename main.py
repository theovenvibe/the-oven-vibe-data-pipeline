"""Run the full bronze -> silver -> menu -> gold pipeline against warehouse.duckdb."""

import os

from pipeline import bronze, silver, menu, gold, direct


def main():
    bronze.main()
    silver.main()
    menu.main()
    # Phase 8 (backend PRD §11 row 8) — direct orders from D1, alongside the
    # Zomato silver tables. Skipped, not failed, when no admin token is
    # configured (e.g. a fresh clone, or CI without the secret set) so the
    # Zomato-only pipeline this repo has always run keeps working unchanged.
    if os.environ.get("OVEN_VIBE_ADMIN_TOKEN"):
        direct.main()
    else:
        print("skipping pipeline.direct: OVEN_VIBE_ADMIN_TOKEN not set (see .env.example)")
    gold.main()


if __name__ == "__main__":
    main()
