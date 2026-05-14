"""SQLite batch writer for Atlas OHLCV data.

Handles writing DataFrames to the ohlcv table and verifying SQLite integrity.
Depends on: data.ingest.cache (_load_cache) -- lazy import to avoid cycles.
"""
import logging
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Batch write
# ---------------------------------------------------------------------------

def _sqlite_batch_write(
    ticker: str,
    df: "pd.DataFrame",
    universe_name: str,
    source: str = "yfinance",
) -> int:
    """Write a DataFrame of OHLCV rows to the SQLite ohlcv table.

    Uses INSERT OR REPLACE so the universe column reflects the *calling*
    universe, regardless of what prior ingests wrote.  Returns row count.
    """
    try:
        from db.atlas_db import get_db as _get_db
        rows = []
        for t_row in df.itertuples():
            date_str = (
                t_row.Index.strftime("%Y-%m-%d")
                if hasattr(t_row.Index, "strftime")
                else str(t_row.Index)[:10]
            )
            rows.append((
                ticker,
                date_str,
                float(getattr(t_row, "open", 0) or 0),
                float(getattr(t_row, "high", 0) or 0),
                float(getattr(t_row, "low", 0) or 0),
                float(getattr(t_row, "close", 0) or 0),
                None,  # adj_close -- not stored in canonical format
                int(getattr(t_row, "volume", 0) or 0),
                universe_name,
                source,
            ))
        if rows:
            with _get_db() as db:
                db.executemany(
                    """
                    INSERT OR REPLACE INTO ohlcv
                        (ticker, date, open, high, low, close, adj_close,
                         volume, universe, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            logger.debug(
                "SQLite batch write: %d rows for %s (universe=%s)",
                len(rows), ticker, universe_name,
            )
        return len(rows)
    except Exception as exc:
        logger.warning(
            "SQLite batch write failed for %s (universe=%s): %s",
            ticker, universe_name, exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Integrity verification
# ---------------------------------------------------------------------------

def verify_sqlite_integrity(
    market_id: str,
    tickers: List[str],
    backfill: bool = True,
) -> dict:
    """Verify SQLite ohlcv has data for all tickers and optionally backfill gaps.

    Args:
        market_id: Universe/market name (used as the 'universe' column value).
        tickers: List of tickers that should have data.
        backfill: If True, backfill missing/stale data from parquet cache.

    Returns:
        dict with keys: market, total, present, missing, backfilled, still_missing.
    """
    from db.atlas_db import get_db as _get_db
    from data.ingest.cache import _load_cache  # lazy import

    present = []
    missing = []

    with _get_db() as db:
        for ticker in tickers:
            count = db.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE ticker = ? AND universe = ?",
                (ticker, market_id.lower()),
            ).fetchone()[0]
            if count > 0:
                present.append(ticker)
            else:
                missing.append(ticker)

    backfilled = []
    still_missing = []

    if backfill and missing:
        logger.warning(
            "verify_sqlite_integrity(%s): %d tickers missing from SQLite, attempting backfill",
            market_id, len(missing),
        )
        for ticker in missing:
            cached = _load_cache(ticker, market_id)
            if cached is not None and not cached.empty:
                n = _sqlite_batch_write(ticker, cached, market_id.lower())
                if n > 0:
                    backfilled.append(ticker)
                    logger.info(
                        "Backfilled %s from parquet -> SQLite (%d rows, universe=%s)",
                        ticker, n, market_id,
                    )
                else:
                    still_missing.append(ticker)
            else:
                still_missing.append(ticker)

        if still_missing:
            logger.error(
                "verify_sqlite_integrity(%s): %d tickers still missing after backfill: %s",
                market_id, len(still_missing), still_missing,
            )
            try:
                from alerting import get_alert_manager
                alert = (
                    f"🚨 <b>DATA INTEGRITY FAILURE [{market_id.upper()}]</b>\n\n"
                    f"{len(still_missing)} tickers have NO data in SQLite "
                    f"even after backfill attempt:\n"
                    + "\n".join(f"  * {t}" for t in still_missing)
                    + "\n\nParquet cache also missing. Manual investigation required."
                )
                get_alert_manager().send(alert)
            except Exception:
                pass
    else:
        still_missing = list(missing)

    result = {
        "market": market_id,
        "total": len(tickers),
        "present": present,
        "missing": missing,
        "backfilled": backfilled,
        "still_missing": still_missing,
    }

    logger.info(
        "verify_sqlite_integrity(%s): %d/%d present, %d backfilled, %d still missing",
        market_id, len(present), len(tickers), len(backfilled), len(still_missing),
    )
    return result
