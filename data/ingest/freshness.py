"""Data freshness checks for Atlas OHLCV pipeline.

Verifies that downloaded data is current and auto-excludes stale tickers.
No intra-ingest dependencies at load time.
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Last trading day
# ---------------------------------------------------------------------------

def _last_trading_day(reference_date: Optional[datetime] = None) -> datetime:
    """Return the most recent COMPLETED NYSE trading day before *reference_date*.

    Uses ``pandas_market_calendars`` for full holiday awareness (MLK Day, Good
    Friday, Thanksgiving, etc.).  If the library is unavailable the function
    falls back to a simple weekend walk-back (no holiday handling).

    "Most recent completed" means: if *reference_date* itself is a trading day
    (e.g. Monday), we return the previous trading day (e.g. Friday) so that
    pre-market runs on a trading day treat the prior session's close as fresh.

    The returned datetime is always at midnight (00:00:00) so that date
    comparisons against DataFrame DatetimeIndex values are unambiguous.

    Args:
        reference_date: Date to anchor from (default: today).

    Returns:
        datetime at midnight of the last completed NYSE trading day.
    """
    if reference_date is None:
        reference_date = datetime.now()

    ref_date = reference_date.date() if hasattr(reference_date, "date") else reference_date

    # -- NYSE calendar path (holiday-aware) --
    try:
        import pandas_market_calendars as mcal  # already a project dependency

        nyse = mcal.get_calendar("NYSE")
        start = ref_date - timedelta(days=10)  # buffer for long holiday runs
        valid_days = nyse.valid_days(start_date=start, end_date=ref_date)
        if not valid_days.empty:
            last_valid = valid_days[-1].date()
            if last_valid == ref_date:
                # Today is a trading day -- return the *previous* session's date
                # so that pre-market checks treat yesterday's close as fresh.
                last_valid = valid_days[-2].date() if len(valid_days) >= 2 else last_valid
            return datetime(last_valid.year, last_valid.month, last_valid.day)
    except Exception:
        pass  # fall through to weekend-only fallback

    # -- Fallback: weekend walk-back (no holiday awareness) --
    d = reference_date
    # Step back one day first (we want the *previous* trading day, not today)
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # 5=Saturday, 6=Sunday
        d -= timedelta(days=1)
    # Normalise to midnight to avoid time-of-day comparison issues
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Freshness check
# ---------------------------------------------------------------------------

def check_data_freshness(
    data: Dict[str, "pd.DataFrame"],
    market_id: Optional[str] = None,
    max_lag_days: int = 1,
) -> dict:
    """Verify that downloaded data is fresh (not stale/cached from a prior day).

    Checks each ticker's most recent data date against the expected last
    trading day.  Returns a summary with overall pass/fail, stale ticker list,
    and the freshest/stalest dates found.

    Args:
        data:         Dict of ticker -> DataFrame (output of download_universe).
        market_id:    Market identifier for logging (informational only).
        max_lag_days: Maximum allowed lag in trading days.  Default 1 allows
                      for end-of-day data that arrives the morning after
                      (e.g. data as of yesterday is fresh when running pre-market).

    Returns:
        Dict with keys:
            is_fresh (bool):         True if all checked tickers meet freshness.
            stale_tickers (list):    Tickers whose data is too old.
            fresh_count (int):       Number of tickers with fresh data.
            stale_count (int):       Number of tickers with stale data.
            expected_date (str):     Expected minimum data date (YYYY-MM-DD).
            newest_date (str | None): Most recent data date across all tickers.
            oldest_date (str | None): Oldest most-recent-date across all tickers.
            message (str):           Human-readable summary.
    """
    import pandas as _pd

    if not data:
        return {
            "is_fresh": False,
            "stale_tickers": [],
            "fresh_count": 0,
            "stale_count": 0,
            "expected_date": "",
            "newest_date": None,
            "oldest_date": None,
            "message": "No data provided -- nothing to check",
        }

    # expected_dt is the oldest date we still consider "fresh":
    # max_lag_days=1 -> data from yesterday or today is acceptable
    # max_lag_days=0 -> only today's data is acceptable
    expected_dt = _last_trading_day() - timedelta(days=max_lag_days)
    expected_date = expected_dt.strftime("%Y-%m-%d")

    stale_tickers = []
    all_latest_dates = []
    checked = 0

    for ticker, df in data.items():
        if df is None or (hasattr(df, "empty") and df.empty):
            continue
        checked += 1
        try:
            latest = df.index.max()
            if hasattr(latest, "to_pydatetime"):
                latest = latest.to_pydatetime()
            elif not isinstance(latest, datetime):
                latest = _pd.Timestamp(latest).to_pydatetime()
            # Strip time component
            latest_date_str = latest.strftime("%Y-%m-%d")
            all_latest_dates.append(latest_date_str)
            if latest < expected_dt:
                stale_tickers.append(ticker)
        except Exception as e:
            logger.debug("Freshness check for %s failed: %s", ticker, e)

    if not all_latest_dates:
        return {
            "is_fresh": False,
            "stale_tickers": [],
            "fresh_count": 0,
            "stale_count": 0,
            "expected_date": expected_date,
            "newest_date": None,
            "oldest_date": None,
            "message": "Could not determine data dates from downloaded data",
        }

    newest_date = max(all_latest_dates)
    oldest_date = min(all_latest_dates)
    stale_count = len(stale_tickers)
    fresh_count = checked - stale_count
    is_fresh = stale_count == 0

    if is_fresh:
        message = (
            f"Data is FRESH: {fresh_count}/{checked} tickers at or after {expected_date}. "
            f"Newest: {newest_date}."
        )
    else:
        sample = stale_tickers[:5]
        more = f" (+{stale_count - 5} more)" if stale_count > 5 else ""
        message = (
            f"STALE DATA DETECTED: {stale_count}/{checked} tickers older than "
            f"{expected_date}. Stale: {sample}{more}. "
            f"Oldest latest: {oldest_date}."
        )

    logger.info("Data freshness check: %s", message)
    return {
        "is_fresh": is_fresh,
        "stale_tickers": stale_tickers,
        "fresh_count": fresh_count,
        "stale_count": stale_count,
        "expected_date": expected_date,
        "newest_date": newest_date,
        "oldest_date": oldest_date,
        "message": message,
    }


# ---------------------------------------------------------------------------
# Verify ingest freshness (with auto-exclusion)
# ---------------------------------------------------------------------------

def verify_ingest_freshness(
    data: Dict[str, "pd.DataFrame"],
    config: Optional[dict] = None,
    market_id: Optional[str] = None,
) -> bool:
    """Verify data freshness with smart auto-exclusion for stale tickers.

    Instead of binary halt/continue, applies graduated response:
    - ALL tickers stale -> halt (real data provider issue)
    - >5% of tickers stale -> halt (systemic problem)
    - 1-3 individual tickers stale -> auto-exclude them, alert, continue
    - 0 stale -> pass

    Auto-excluded tickers are:
    - Added to config/auto_excluded_tickers.json
    - Cache files quarantined
    - Telegram alert sent
    - Pipeline continues with remaining tickers

    Args:
        data:      Dict of ticker -> DataFrame (output of download_universe).
        config:    Active Atlas config dict.
        market_id: Market identifier for log/alert messages.

    Returns:
        True if data is fresh (possibly after auto-excluding stale tickers).
        False if stale data remains and halt_on_stale_data is False.

    Raises:
        RuntimeError: If stale data is systemic and halt_on_stale_data is True.
    """
    freshness = check_data_freshness(data, market_id=market_id)

    if freshness["is_fresh"]:
        logger.info(
            "Ingest freshness OK [%s]: %d tickers, newest=%s",
            market_id or "?",
            freshness["fresh_count"],
            freshness["newest_date"],
        )
        return True

    # Stale data detected -- apply graduated response
    market_label = market_id or "?"
    stale_tickers = freshness["stale_tickers"]
    stale_count = freshness["stale_count"]
    total_checked = freshness["fresh_count"] + stale_count
    expected = freshness["expected_date"]
    oldest = freshness["oldest_date"]
    stale_pct = (stale_count / total_checked * 100) if total_checked > 0 else 100

    # Determine if this is systemic or individual
    all_stale = stale_count == total_checked
    systemic = all_stale or (total_checked > 20 and stale_pct > 5)
    auto_excludable = not systemic and stale_count <= 10

    logger.warning(
        "STALE DATA [%s]: %d/%d stale (%.1f%%). systemic=%s, auto_excludable=%s",
        market_label, stale_count, total_checked, stale_pct,
        systemic, auto_excludable,
    )

    if auto_excludable:
        # Auto-exclude individual stale tickers and continue
        from data.auto_exclusions import add_exclusion, quarantine_cache

        excluded_details = []
        for ticker in stale_tickers:
            # Get last data date for the alert
            df = data.get(ticker)
            last_date = "unknown"
            if df is not None and not df.empty:
                try:
                    last_date = df.index.max().strftime("%Y-%m-%d")
                except Exception:
                    pass

            add_exclusion(
                ticker=ticker,
                market_id=market_label,
                reason=f"stale_data: last data {last_date}, expected >= {expected}",
                last_data_date=last_date,
            )
            quarantine_cache(ticker, market_label)
            excluded_details.append(f"{ticker} (last: {last_date})")

            # Remove from data dict so downstream gets clean data
            data.pop(ticker, None)

        logger.info(
            "Auto-excluded %d stale tickers from %s: %s",
            len(excluded_details), market_label, excluded_details,
        )

        # Send Telegram alert for auto-exclusions
        try:
            from alerting import get_alert_manager
            ticker_lines = "\n".join(f"  * {d}" for d in excluded_details)
            alert = (
                f"⚠️ <b>AUTO-EXCLUDED STALE TICKERS [{market_label.upper()}]</b>\n\n"
                f"Auto-excluded <b>{len(excluded_details)}</b> ticker(s):\n"
                f"{ticker_lines}\n\n"
                f"Expected data >= {expected}\n"
                f"Pipeline continuing with {freshness['fresh_count']} fresh tickers.\n\n"
                f"💡 These tickers will be retried weekly. "
                f"Check if delisted or renamed."
            )
            get_alert_manager().send(alert)
        except Exception as tg_exc:
            logger.warning("Could not send auto-exclusion Telegram alert: %s", tg_exc)

        return True  # Pipeline continues

    # Systemic stale data -- may need to halt
    logger.warning(
        "SYSTEMIC stale data [%s]: %d/%d (%.1f%%) stale. "
        "This suggests a data provider issue, not individual ticker problems.",
        market_label, stale_count, total_checked, stale_pct,
    )

    # Send Telegram alert for systemic issue
    try:
        from alerting import get_alert_manager
        stale_sample = stale_tickers[:10]
        halt = True  # safe default
        if config:
            halt = config.get("trading", {}).get(
                "live_safety", {}
            ).get("halt_on_stale_data", True)

        alert = (
            f"🛑 <b>SYSTEMIC STALE DATA [{market_label.upper()}]</b>\n\n"
            f"Stale tickers: <b>{stale_count}/{total_checked}</b> ({stale_pct:.1f}%)\n"
            f"Expected data >= {expected}\n"
            f"Oldest latest date: {oldest}\n"
            f"Sample: {stale_sample}\n\n"
        )
        if halt:
            alert += "🛑 Pipeline HALTED (halt_on_stale_data=true)\n"
            alert += "This looks like a data provider outage, not individual delistings."
        else:
            alert += "⚡ Continuing despite systemic stale data (halt_on_stale_data=false)"
        get_alert_manager().send(alert)
    except Exception as tg_exc:
        logger.warning("Could not send stale data Telegram alert: %s", tg_exc)

    # Decide whether to halt
    halt = True  # safe default
    if config:
        halt = config.get("trading", {}).get(
            "live_safety", {}
        ).get("halt_on_stale_data", True)

    if halt:
        raise RuntimeError(
            f"SYSTEMIC STALE DATA: {stale_count}/{total_checked} tickers ({stale_pct:.1f}%) "
            f"have data older than {expected}. This suggests a data provider issue. "
            "Set halt_on_stale_data=false in config to continue."
        )

    logger.warning("Continuing pipeline with systemic stale data (halt_on_stale_data=false)")
    return False
