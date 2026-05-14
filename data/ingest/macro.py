"""Macro data refresh for Atlas ingestion pipeline.

Wraps data.macro download + DB write behind a single callable used by cron.
No intra-ingest dependencies at load time.
"""
import logging

logger = logging.getLogger(__name__)


def refresh_macro_data(cache_max_age_hours: int = 24) -> bool:
    """Refresh macro regime data (gold, copper, VIX, yield curve).

    Downloads macro data from yfinance and FRED and caches it for use by
    the macro regime calculator (data.macro).  Called by cron scripts
    during the daily data refresh cycle so that macro signals are current
    before the trading session begins.

    The function is a no-op (returns False with a warning) when data.macro
    is not yet installed -- this keeps the cron pipeline safe during deploys.

    Args:
        cache_max_age_hours: Skip network refresh when cached data is younger
                             than this many hours.  Defaults to 24 (daily).

    Returns:
        True if the refresh succeeded and returned non-empty data.
        False on any failure (import error, network error, empty result).
    """
    try:
        from data.macro import download_macro_data
    except ImportError as e:
        logger.warning(
            "data.macro not available -- macro data refresh skipped "
            f"(install data/macro.py to enable): {e}"
        )
        return False

    logger.info("Refreshing macro regime data (gold, copper, VIX, yields)...")
    try:
        df = download_macro_data(cache_max_age_hours=cache_max_age_hours)
        if df is None or (hasattr(df, "empty") and df.empty):
            logger.warning("Macro data refresh returned empty result -- check data sources")
            return False
        logger.info(
            f"Macro data refreshed successfully: {len(df)} rows, "
            f"columns={list(df.columns)}, "
            f"range=[{df.index.min().date()}, {df.index.max().date()}]"
        )
    except Exception as e:
        logger.error(f"Macro data refresh failed: {e}", exc_info=True)
        return False

    # Also persist to macro_indicators SQLite table (includes FRED data)
    try:
        from data.macro import fetch_macro_data
        db_df = fetch_macro_data(
            write_to_db=True,
            use_cache=True,
            # Propagate the caller's cache-age preference to FRED so that
            # refresh_macro_data(cache_max_age_hours=0) also forces a fresh
            # FRED API fetch (not just yfinance).
            fred_max_age_hours=cache_max_age_hours,
        )
        if db_df is not None and not db_df.empty:
            logger.info(
                f"Macro indicators written to DB: {len(db_df)} rows "
                f"[{db_df.index.min().date()}, {db_df.index.max().date()}]"
            )
        else:
            logger.warning("fetch_macro_data(write_to_db=True) returned empty -- FRED data may be missing")
    except Exception as e:
        logger.warning(f"Macro DB write failed (yfinance cache still updated): {e}")

    return True
