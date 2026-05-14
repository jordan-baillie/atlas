"""CLI entry point for Atlas data ingestion.

Usage:
    python3 -m data.ingest --universe sector_etfs
    python3 -m data.ingest --universe all_etfs
    python3 -m data.ingest --universe all_etfs --start 2019-01-01
    python3 -m data.ingest --universe gold_etfs --force
"""
import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from data.ingest import ingest_universe, ingest_all_etf_universes

parser = argparse.ArgumentParser(
    description="Atlas data ingestion CLI",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Examples:
  python3 -m data.ingest --universe sector_etfs
  python3 -m data.ingest --universe all_etfs
  python3 -m data.ingest --universe all_etfs --start 2019-01-01
  python3 -m data.ingest --universe gold_etfs --force
    """,
)
parser.add_argument(
    "--universe",
    required=True,
    help=(
        "Universe to ingest: one of the 6 named universes from definitions.py, "
        "or 'all_etfs' to ingest all 5 static ETF universes."
    ),
)
parser.add_argument("--start", dest="start_date", default=None, help="Start date YYYY-MM-DD (default: 7 years ago)")
parser.add_argument("--end", dest="end_date", default=None, help="End date YYYY-MM-DD (default: today)")
parser.add_argument("--force", action="store_true", default=False, help="Bypass parquet cache and re-fetch from API")

args = parser.parse_args()

if args.universe == "all_etfs":
    result = ingest_all_etf_universes(
        start_date=args.start_date,
        end_date=args.end_date,
        force=args.force,
    )
    print("\n=== Summary ===")
    print(f"Universes ingested : {result['universes_ingested']}")
    print(f"Total rows written : {result['total_rows_written']}")
    print(f"Total failed       : {result['total_tickers_failed']}")
else:
    result = ingest_universe(
        args.universe,
        start_date=args.start_date,
        end_date=args.end_date,
        force=args.force,
    )
    print("\n=== Summary ===")
    print(f"Universe       : {result['universe']}")
    print(f"Tickers fetched: {len(result['tickers_fetched'])}")
    print(f"Tickers failed : {result['tickers_failed']}")
    print(f"Rows written   : {result['rows_written']}")

sys.exit(0 if not result.get("tickers_failed") else 1)
