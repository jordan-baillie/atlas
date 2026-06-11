"""Refresh benchmark OHLCV rows (SPY) — the only live consumer of the ohlcv table.

The original full-universe ingest pipeline was deleted in the 2026-06 restructure,
but two dashboard features still read ``ohlcv``:

  * the SPY benchmark overlay on the equity curve (dashboard_builder.py)
  * the data-freshness chip (health.py — MAX(date) across non-excluded tickers)

This module keeps exactly that need alive: it pulls the last ~10 calendar days of
SPY daily bars from yfinance and upserts them. Run daily from forward-paper.sh.

Usage:  python3 -m atlas.db.refresh_benchmark [TICKER ...]   (default: SPY)
"""
from __future__ import annotations

import logging
import sys
from datetime import date, timedelta

from atlas.db import get_db

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = ["SPY"]
LOOKBACK_DAYS = 10  # overlap window — upsert makes re-pulls idempotent


def refresh(tickers: list[str] | None = None, lookback_days: int = LOOKBACK_DAYS) -> int:
    """Upsert recent daily bars for *tickers* into ohlcv. Returns row count written."""
    import yfinance as yf

    tickers = tickers or DEFAULT_TICKERS
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    n = 0
    with get_db() as db:
        for t in tickers:
            try:
                df = yf.download(t, start=start, interval="1d",
                                 progress=False, auto_adjust=False)
            except Exception as e:
                logger.warning("refresh_benchmark: download failed for %s: %s", t, e)
                continue
            if df is None or df.empty:
                logger.warning("refresh_benchmark: no data for %s", t)
                continue
            if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                df.columns = df.columns.get_level_values(0)
            for idx, row in df.iterrows():
                d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                try:
                    db.execute(
                        "INSERT OR REPLACE INTO ohlcv"
                        " (ticker, date, open, high, low, close, adj_close, volume, universe, source)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (t, d, float(row["Open"]), float(row["High"]), float(row["Low"]),
                         float(row["Close"]),
                         float(row["Adj Close"]) if "Adj Close" in row else None,
                         int(row["Volume"]), "sp500", "yfinance"),
                    )
                    n += 1
                except Exception as e:
                    logger.warning("refresh_benchmark: upsert %s %s failed: %s", t, d, e)
        db.commit()
    logger.info("refresh_benchmark: wrote %d rows for %s", n, ",".join(tickers))
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    tickers = sys.argv[1:] or None
    refresh(tickers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
