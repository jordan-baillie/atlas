#!/usr/bin/env python3
"""
Weekly recovery check for auto-excluded tickers.

Tries to re-fetch data for tickers that were auto-excluded (stale/delisted).
If data is now available, removes from auto-exclusion and alerts.

Usage:
    python3 scripts/stale_ticker_recovery.py [--market sp500]
    
Cron (weekly, Sunday 6 PM UTC):
    0 18 * * 0 cd /root/atlas && python3 scripts/stale_ticker_recovery.py >> logs/recovery.log 2>&1
"""
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.logging_config import setup_logging
logger = setup_logging("stale_recovery")


def attempt_recovery(market_id: str = "sp500") -> dict:
    """Try to re-ingest auto-excluded tickers.

    For each auto-excluded ticker:
    1. Attempt to download recent data
    2. If successful (non-empty, reasonably fresh), remove from exclusion
    3. Update recovery attempt counter
    4. Send summary alert

    Args:
        market_id: Market to check recoveries for.

    Returns:
        Dict with recovered, still_excluded, and errors lists.
    """
    from data.auto_exclusions import (
        get_exclusion_details,
        remove_exclusion,
        update_recovery_attempt,
    )
    from data.ingest import _fetch_ohlcv, _normalize_ticker, _save_cache, _last_trading_day

    details = get_exclusion_details()
    excluded = details.get("excluded", {})

    if not excluded:
        logger.info("No auto-excluded tickers to recover")
        return {"recovered": [], "still_excluded": [], "errors": []}

    # Filter to requested market
    market_tickers = {
        t: info for t, info in excluded.items()
        if info.get("market_id", "").lower() == market_id.lower()
    }

    if not market_tickers:
        logger.info("No auto-excluded tickers for market %s", market_id)
        return {"recovered": [], "still_excluded": [], "errors": []}

    logger.info(
        "Attempting recovery for %d auto-excluded tickers in %s: %s",
        len(market_tickers), market_id, list(market_tickers.keys()),
    )

    recovered = []
    still_excluded = []
    errors = []

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    expected_dt = _last_trading_day() - timedelta(days=2)  # Allow 2-day lag

    for ticker, info in market_tickers.items():
        try:
            update_recovery_attempt(ticker)

            # Try to fetch recent data
            normalized = _normalize_ticker(ticker, market_id)
            df = _fetch_ohlcv(normalized, start_date, end_date, market_id)

            if df is not None and not df.empty:
                latest = df.index.max()
                if hasattr(latest, "to_pydatetime"):
                    latest = latest.to_pydatetime()

                if latest >= expected_dt:
                    # Data is fresh again — recover!
                    _save_cache(normalized, df, market_id)
                    remove_exclusion(ticker)
                    recovered.append({
                        "ticker": ticker,
                        "latest_date": latest.strftime("%Y-%m-%d"),
                        "rows": len(df),
                        "excluded_since": info.get("excluded_at", "unknown"),
                    })
                    logger.info(
                        "RECOVERED %s: data now available through %s (%d rows)",
                        ticker, latest.strftime("%Y-%m-%d"), len(df),
                    )
                else:
                    still_excluded.append({
                        "ticker": ticker,
                        "latest_date": latest.strftime("%Y-%m-%d"),
                        "reason": f"data still stale (latest: {latest.strftime('%Y-%m-%d')})",
                        "attempts": info.get("recovery_attempts", 0) + 1,
                    })
                    logger.info(
                        "%s still stale: latest data %s (need >= %s)",
                        ticker, latest.strftime("%Y-%m-%d"),
                        expected_dt.strftime("%Y-%m-%d"),
                    )
            else:
                still_excluded.append({
                    "ticker": ticker,
                    "latest_date": None,
                    "reason": "no data returned",
                    "attempts": info.get("recovery_attempts", 0) + 1,
                })
                logger.info("%s: no data returned — still excluded", ticker)

        except Exception as e:
            errors.append({"ticker": ticker, "error": str(e)})
            logger.warning("Recovery attempt for %s failed: %s", ticker, e)

    result = {
        "recovered": recovered,
        "still_excluded": still_excluded,
        "errors": errors,
        "market_id": market_id,
        "checked_at": datetime.now().isoformat(),
    }

    # Send Telegram summary
    _send_recovery_summary(result)

    return result


def _send_recovery_summary(result: dict) -> None:
    """Send Telegram summary of recovery results."""
    try:
        from utils.telegram import send_message

        recovered = result["recovered"]
        still_excluded = result["still_excluded"]
        errors = result["errors"]
        market = result.get("market_id", "?").upper()

        if not recovered and not still_excluded and not errors:
            return  # Nothing to report

        lines = [f"📊 <b>STALE TICKER RECOVERY [{market}]</b>\n"]

        if recovered:
            lines.append(f"✅ <b>Recovered ({len(recovered)}):</b>")
            for r in recovered:
                lines.append(f"  • {r['ticker']} — data through {r['latest_date']}")
            lines.append("")

        if still_excluded:
            lines.append(f"❌ <b>Still excluded ({len(still_excluded)}):</b>")
            for s in still_excluded:
                lines.append(
                    f"  • {s['ticker']} — {s['reason']} "
                    f"(attempt #{s.get('attempts', '?')})"
                )
            lines.append("")

        if errors:
            lines.append(f"⚠️ <b>Errors ({len(errors)}):</b>")
            for e in errors:
                lines.append(f"  • {e['ticker']}: {e['error']}")

        send_message("\n".join(lines))
    except Exception as e:
        logger.warning("Could not send recovery summary: %s", e)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Recovery check for auto-excluded tickers")
    parser.add_argument("--market", default="sp500", help="Market to check (default: sp500)")
    args = parser.parse_args()

    print(f"[recovery] Checking auto-excluded tickers for {args.market}...")
    result = attempt_recovery(args.market)

    print(f"\n=== Recovery Summary [{args.market.upper()}] ===")
    print(f"  Recovered:      {len(result['recovered'])}")
    print(f"  Still excluded:  {len(result['still_excluded'])}")
    print(f"  Errors:          {len(result['errors'])}")

    if result["recovered"]:
        print("\n  Recovered tickers:")
        for r in result["recovered"]:
            print(f"    ✅ {r['ticker']} (data through {r['latest_date']})")

    if result["still_excluded"]:
        print("\n  Still excluded:")
        for s in result["still_excluded"]:
            print(f"    ❌ {s['ticker']} — {s['reason']}")

    sys.exit(0)
