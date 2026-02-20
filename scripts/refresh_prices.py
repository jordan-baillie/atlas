#!/usr/bin/env python3
"""Lightweight price refresh for open positions.

Fetches latest prices for open positions only (max 5 tickers)
and updates the dashboard. Designed to run frequently during
ASX trading hours via crontab.
"""
import json
import sys
import logging
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

BRISBANE = ZoneInfo("Australia/Brisbane")
PROJECT = Path("/a0/usr/projects/atlas-asx")

# Logging
log_dir = PROJECT / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "price_refresh.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("price_refresh")


def get_open_tickers():
    """Get tickers of open positions from portfolio state."""
    state_path = PROJECT / "paper_engine" / "portfolio_state.json"
    if not state_path.exists():
        return []
    with open(state_path) as f:
        state = json.load(f)
    return [p["ticker"] for p in state.get("positions", [])]


def refresh_prices(tickers):
    """Download latest prices and update parquet cache."""
    if not tickers:
        log.info("No open positions - nothing to refresh")
        return {}

    cache_dir = PROJECT / "data" / "cache"
    prices = {}

    for ticker in tickers:
        try:
            # Download just today + yesterday for latest price
            data = yf.download(ticker, period="5d", progress=False, auto_adjust=True)
            if data.empty:
                log.warning(f"{ticker}: no data returned")
                continue

            # Flatten multi-level columns if present
            if hasattr(data.columns, "levels") and data.columns.nlevels > 1:
                data.columns = [c[0].lower() for c in data.columns]
            else:
                data.columns = [c.lower() for c in data.columns]

            latest_price = float(data["close"].iloc[-1])
            prices[ticker] = latest_price

            # Update the parquet cache with latest data
            parquet_path = cache_dir / (ticker.replace(".", "_") + ".parquet")
            if parquet_path.exists():
                existing = pd.read_parquet(parquet_path)
                # Merge: update existing rows and add new ones
                data.index = pd.to_datetime(data.index)
                existing.index = pd.to_datetime(existing.index)
                # Ensure matching column names
                data.columns = [c.lower() for c in data.columns]
                existing.columns = [c.lower() for c in existing.columns]
                # Combine: new data overwrites existing for same dates
                combined = pd.concat([existing, data])
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.sort_index(inplace=True)
                combined.to_parquet(parquet_path)
            else:
                data.to_parquet(parquet_path)

            log.info(f"{ticker}: ${latest_price:.4f}")

        except Exception as e:
            log.error(f"{ticker}: refresh failed - {e}")

    return prices


def refresh_dashboard():
    """Regenerate dashboard data JSON."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "dashboard/generate_data.py"],
        capture_output=True, text=True, cwd=str(PROJECT),
        timeout=60
    )
    if result.returncode == 0:
        log.info("Dashboard data refreshed")
    else:
        log.error(f"Dashboard refresh failed: {result.stderr[-300:]}")


def main():
    now = datetime.now(BRISBANE)
    log.info(f"Price refresh started at {now.strftime(' %H:%M:%S AEST')}")

    # Only refresh during ASX hours (roughly 10 AM - 4:30 PM AEST)
    hour = now.hour
    if hour < 9 or hour >= 17:
        log.info(f"Outside market hours ({hour}:xx AEST) - skipping")
        return

    if now.weekday() >= 5:
        log.info("Weekend - skipping")
        return

    tickers = get_open_tickers()
    log.info(f"Refreshing prices for {len(tickers)} open positions: {tickers}")

    prices = refresh_prices(tickers)

    if prices:
        refresh_dashboard()
        log.info(f"Updated {len(prices)} prices")
    else:
        log.info("No prices updated")


if __name__ == "__main__":
    main()
