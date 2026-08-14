"""Run the full bronze -> silver -> menu -> gold pipeline against warehouse.duckdb."""

from pipeline import bronze, silver, menu, gold


def main():
    bronze.main()
    silver.main()
    menu.main()
    gold.main()


if __name__ == "__main__":
    main()
