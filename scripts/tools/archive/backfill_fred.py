#!/usr/bin/env python3
"""One-time backfill of macro_indicators with full FRED history (2015-present)."""

import sys
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from db.atlas_db import init_db
from data.macro import backfill_macro_indicators

def main():
    # Ensure tables exist
    init_db()

    # Backfill from 2015 to present
    # backfill_macro_indicators downloads from 300 days before start for 200-DMA warmup
    df = backfill_macro_indicators(start_date="2015-01-01")

    if df.empty:
        print("ERROR: No data returned from backfill")
        sys.exit(1)

    print(f"\nBackfill complete: {len(df)} rows")
    print(f"Date range: {df.index.min().date()} to {df.index.max().date()}")

    # Show FRED column coverage
    fred_cols = ['credit_oas', 'yield_2y', 'dxy', 'fed_funds', 'unemployment_claims']
    for col in fred_cols:
        if col in df.columns:
            non_null = df[col].notna().sum()
            print(f"  {col}: {non_null}/{len(df)} non-null")
        else:
            print(f"  {col}: MISSING from DataFrame")

if __name__ == "__main__":
    main()
