
#!/usr/bin/env python3
"""Refresh ALL parquet files to latest data."""
import yfinance as yf
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import time
import sys

PROJ = Path('/a0/usr/projects/atlas-asx')
CACHE = PROJ / 'data' / 'cache'

# Get all parquet files
files = sorted(CACHE.glob('*.parquet'))
print(f"Found {len(files)} parquet files to refresh")
print(f"Start time: {datetime.now().isoformat()}")
print("="*60)

updated = 0
failed = 0
already_current = 0

for i, pf in enumerate(files):
    ticker = pf.stem.replace('_AX', '.AX')

    try:
        # Read existing data
        existing = pd.read_parquet(pf)
        existing.index = pd.to_datetime(existing.index)
        last_date = existing.index.max().date()
        today = datetime.now().date()

        # If already current (within 1 trading day), skip
        if (today - last_date).days <= 1:
            already_current += 1
            continue

        # Download from last date to now
        start = (last_date - timedelta(days=5)).strftime('%Y-%m-%d')
        data = yf.download(ticker, start=start, progress=False, auto_adjust=True)

        if data.empty:
            print(f"  [{i+1}/{len(files)}] {ticker}: no new data")
            failed += 1
            continue

        # Handle MultiIndex columns from yfinance
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data.columns = [c.lower() for c in data.columns]
        data.index = pd.to_datetime(data.index)

        # Merge with existing
        existing.columns = [c.lower() for c in existing.columns]
        combined = pd.concat([existing, data])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined = combined.sort_index()

        # Save
        combined.to_parquet(pf)
        new_last = combined.index.max().date()
        print(f"  [{i+1}/{len(files)}] {ticker}: {last_date} -> {new_last} ({len(combined)} rows)")
        updated += 1

        # Rate limiting
        if (i + 1) % 10 == 0:
            time.sleep(1)

    except Exception as e:
        print(f"  [{i+1}/{len(files)}] {ticker}: ERROR - {e}")
        failed += 1

print("="*60)
print(f"Done at {datetime.now().isoformat()}")
print(f"Updated: {updated}, Already current: {already_current}, Failed: {failed}")
