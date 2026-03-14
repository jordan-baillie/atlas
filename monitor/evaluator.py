"""Position Monitor — Condition Evaluator.

Evaluates position conditions against live data feeds:
  - yfinance: prices, moving averages (any ticker)
  - FRED: economic indicators (rig counts, rates, etc.)
  - manual_toggle: user-controlled, not auto-evaluated

Run daily after market close to update condition statuses,
recalculate health scores, and fire alerts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT = Path(__file__).resolve().parent.parent

# ── Alpaca client (lazy singleton) ────────────────────────────────────────────

def _get_alpaca_client():
    """Return the shared AlpacaMarketData singleton, or None if unavailable."""
    try:
        from brokers.alpaca.market_data import get_alpaca_data_client
        return get_alpaca_data_client()
    except Exception:
        return None


def _is_us_equity(ticker: str) -> bool:
    """Return True for plain US equity symbols (no .AX/.HK suffix, no ^ prefix)."""
    return (
        not ticker.endswith(".AX")
        and not ticker.endswith(".HK")
        and not ticker.startswith("^")
    )


# FRED series ID mapping for common shorthand
FRED_SERIES_MAP = {
    "RIGS": "RIGS",           # Baker Hughes US rig count (if available)
    "RIGCOUNT": "RIGS",
    "T10Y2Y": "T10Y2Y",
    "VIXCLS": "VIXCLS",
    "FEDFUNDS": "FEDFUNDS",
    "ICSA": "ICSA",
}


def _fetch_yfinance_price(ticker: str) -> Optional[float]:
    """Get latest closing price.

    For US equities (no .AX/.HK suffix, no ^ prefix): tries Alpaca
    snapshot first (lower latency, no rate limits) then falls back to
    yfinance.  For all other tickers uses yfinance directly.
    """
    # Try Alpaca for US equities
    if _is_us_equity(ticker):
        try:
            alpaca = _get_alpaca_client()
            if alpaca is not None:
                snap = alpaca.get_snapshot(ticker)
                if snap and snap.get("price", 0) > 0:
                    logger.debug("Alpaca price for %s: %.4f", ticker, snap["price"])
                    return float(snap["price"])
        except Exception as e:
            logger.debug("Alpaca price fetch failed for %s: %s", ticker, e)

    # Fallback: yfinance
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        hist = t.history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.warning("yfinance price fetch failed for %s: %s", ticker, e)
        return None


def _fetch_yfinance_ma(ticker: str, period: int) -> Optional[float]:
    """Get N-day simple moving average.

    For US equities: tries Alpaca historical daily bars first (avoids
    yfinance rate limits), falls back to yfinance.  For non-US tickers
    uses yfinance directly.

    ``period + 30`` calendar days are requested to ensure enough trading
    days remain after excluding weekends and holidays.
    """
    lookback_days = period + 60  # extra buffer for weekends/holidays

    # Try Alpaca historical bars for US equities
    if _is_us_equity(ticker):
        try:
            from brokers.alpaca.market_data import get_historical_bars
            from datetime import datetime, timedelta
            end_dt = datetime.now()
            start_dt = end_dt - timedelta(days=lookback_days)
            result = get_historical_bars(
                ticker,
                start=start_dt.strftime("%Y-%m-%d"),
                end=end_dt.strftime("%Y-%m-%d"),
            )
            df = result.get(ticker)
            if df is not None and len(df) >= period:
                ma = df["close"].iloc[-period:].mean()
                logger.debug("Alpaca MA(%d) for %s: %.4f from %d bars",
                             period, ticker, ma, len(df))
                return round(float(ma), 4)
        except Exception as e:
            logger.debug("Alpaca MA fetch failed for %s: %s", ticker, e)

    # Fallback: yfinance
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        # Need period+20 extra days for warmup
        days = period + 30
        hist = t.history(period=f"{days}d")
        if len(hist) < period:
            logger.warning("Insufficient data for %s MA(%d): got %d rows",
                           ticker, period, len(hist))
            return None
        ma = hist["Close"].rolling(period).mean().iloc[-1]
        return float(ma) if pd.notna(ma) else None
    except Exception as e:
        logger.warning("yfinance MA fetch failed for %s: %s", ticker, e)
        return None


def _fetch_fred_value(series_id: str) -> Optional[float]:
    """Get latest value from FRED."""
    try:
        import sys
        sys.path.insert(0, str(PROJECT))
        from data.fred import FREDClient
        fred = FREDClient()
        if not fred.available:
            logger.warning("FRED API key not configured")
            return None
        series = fred.fetch_series(series_id, max_age_hours=24)
        if series.empty:
            return None
        # Get last non-NaN value
        last = series.dropna().iloc[-1] if not series.dropna().empty else None
        return float(last) if last is not None else None
    except Exception as e:
        logger.warning("FRED fetch failed for %s: %s", series_id, e)
        return None


def evaluate_condition(condition) -> tuple:
    """Evaluate a single condition. Returns (status, current_value).

    status: 'passing', 'warning', 'failing', 'unknown'
    """
    from monitor.models import Condition
    c = condition
    now = datetime.now().isoformat(timespec="seconds")

    if c.type == "manual_toggle":
        # Manual toggles keep their current status (user-controlled)
        return c.status if c.status != "unknown" else "passing", None

    # Resolve data source
    source = c.source or ""
    value = None

    if source.startswith("FRED:"):
        fred_key = source.split(":", 1)[1].strip().upper()
        fred_id = FRED_SERIES_MAP.get(fred_key, fred_key)
        value = _fetch_fred_value(fred_id)
    elif c.type == "ma_position":
        # source is the ticker, threshold is the MA period
        ticker = source or ""
        if not ticker:
            return "unknown", None
        price = _fetch_yfinance_price(ticker)
        ma = _fetch_yfinance_ma(ticker, int(c.threshold))
        if price is None or ma is None:
            return "unknown", price
        # For ma_position: passing = price above MA (for longs)
        if price >= ma:
            return "passing", round(price, 2)
        # Warning zone: within 2% of MA
        if price >= ma * 0.98:
            return "warning", round(price, 2)
        return "failing", round(price, 2)
    else:
        # price_above, price_below, indicator_threshold — all need a value
        if source:
            value = _fetch_yfinance_price(source)

    if value is None:
        return "unknown", None

    # Evaluate based on condition type
    if c.type == "price_above":
        if c.warning_threshold and value <= c.warning_threshold and value > c.threshold:
            return "warning", round(value, 2)
        if value >= c.threshold:
            return "passing", round(value, 2)
        return "failing", round(value, 2)

    elif c.type == "price_below":
        if c.warning_threshold and value >= c.warning_threshold and value < c.threshold:
            return "warning", round(value, 2)
        if value <= c.threshold:
            return "passing", round(value, 2)
        return "failing", round(value, 2)

    elif c.type == "indicator_threshold":
        direction = c.direction or "above"
        if direction == "below":
            if value <= c.threshold:
                return "passing", round(value, 2)
            if c.warning_threshold and value <= c.warning_threshold:
                return "warning", round(value, 2)
            return "failing", round(value, 2)
        else:  # above
            if value >= c.threshold:
                return "passing", round(value, 2)
            if c.warning_threshold and value >= c.warning_threshold:
                return "warning", round(value, 2)
            return "failing", round(value, 2)

    return "unknown", value


def evaluate_position(position) -> tuple:
    """Evaluate all conditions for a position. Returns (position, alerts).

    Updates condition statuses, current price, P&L, and health score.
    Returns list of alert dicts for any condition status changes.
    """
    alerts = []
    now = datetime.now().isoformat(timespec="seconds")

    # Update current price
    price = _fetch_yfinance_price(position.ticker)
    if price is not None:
        position.current_price = round(price, 2)
        if position.entry_price > 0:
            if position.direction == "long":
                pnl_per_share = price - position.entry_price
            else:
                pnl_per_share = position.entry_price - price
            position.unrealized_pnl = round(pnl_per_share * (position.quantity or 1), 2)
            position.unrealized_pnl_pct = round(pnl_per_share / position.entry_price * 100, 2)

    old_score = position.health_score

    # Evaluate each condition
    for c in position.conditions:
        old_status = c.status
        new_status, value = evaluate_condition(c)
        c.status = new_status
        c.current_value = value
        c.last_checked = now

        # Track status changes for alerts
        if old_status != new_status and old_status != "unknown":
            alert = {
                "position_id": position.id,
                "ticker": position.ticker,
                "condition_id": c.id,
                "condition_label": c.label,
                "old_status": old_status,
                "new_status": new_status,
                "value": value,
                "timestamp": now,
            }
            alerts.append(alert)

    position.update_health()

    # Alert on health score crossing threshold
    if old_score >= 5 and position.health_score < 5:
        alerts.append({
            "position_id": position.id,
            "ticker": position.ticker,
            "type": "health_critical",
            "message": f"Health score dropped to {position.health_score}/10 — review thesis",
            "old_score": old_score,
            "new_score": position.health_score,
            "timestamp": now,
        })

    # Alert on invalidation price breach
    if position.current_price and position.invalidation_price:
        if (position.direction == "long" and position.current_price <= position.invalidation_price):
            alerts.append({
                "position_id": position.id,
                "ticker": position.ticker,
                "type": "invalidation_breach",
                "message": f"Price ${position.current_price:.2f} breached invalidation ${position.invalidation_price:.2f}",
                "timestamp": now,
            })
        elif (position.direction == "short" and position.current_price >= position.invalidation_price):
            alerts.append({
                "position_id": position.id,
                "ticker": position.ticker,
                "type": "invalidation_breach",
                "message": f"Price ${position.current_price:.2f} breached invalidation ${position.invalidation_price:.2f}",
                "timestamp": now,
            })

    return position, alerts


def _fetch_broker_positions() -> Dict[str, Dict]:
    """Try to get live position data from active broker.

    Returns dict of {ticker: {current_price, unrealized_pnl, unrealized_pnl_pct,
    entry_price, shares, market_value}} or empty dict on failure.
    """
    try:
        import sys
        sys.path.insert(0, str(PROJECT))
        from utils.config import get_active_config
        from brokers.registry import get_broker

        result = {}
        for market_id in ("sp500", "asx"):
            try:
                config = get_active_config(market_id)
                broker = get_broker(market_id, config)
                if not broker.connect():
                    continue
                for p in broker.get_positions():
                    if p.shares > 0:
                        result[p.ticker] = {
                            "current_price": p.current_price,
                            "entry_price": p.entry_price,
                            "shares": p.shares,
                            "market_value": p.market_value,
                            "unrealized_pnl": p.unrealized_pnl,
                            "unrealized_pnl_pct": p.unrealized_pnl_pct,
                        }
                broker.disconnect()
            except Exception as e:
                logger.debug("Broker %s unavailable: %s", market_id, e)
        return result
    except Exception as e:
        logger.debug("Broker data fetch failed: %s", e)
        return {}


def evaluate_all(send_telegram: bool = True) -> Dict:
    """Evaluate all open positions. Returns summary dict."""
    import sys
    sys.path.insert(0, str(PROJECT))
    from monitor.models import PositionStore

    store = PositionStore()
    positions = store.get_open_positions()

    if not positions:
        logger.info("No open positions to evaluate")
        return {"evaluated": 0, "alerts": 0}

    # Try to get live broker data for positions
    broker_data = _fetch_broker_positions()
    if broker_data:
        logger.info("Broker data available for %d positions", len(broker_data))

    all_alerts = []
    for pos in positions:
        # Overlay broker data if available (broker is source of truth)
        bdata = broker_data.get(pos.ticker)
        if bdata:
            pos.current_price = bdata["current_price"]
            pos.unrealized_pnl = bdata["unrealized_pnl"]
            pos.unrealized_pnl_pct = bdata["unrealized_pnl_pct"]
            if bdata.get("shares"):
                pos.quantity = bdata["shares"]
            logger.info("Using broker data for %s: $%.2f pnl=$%.2f",
                        pos.ticker, pos.current_price, pos.unrealized_pnl)

        pos, alerts = evaluate_position(pos)
        store.update_position(pos)
        for a in alerts:
            store.add_alert(a)
        all_alerts.extend(alerts)

    # Send Telegram alerts
    if send_telegram and all_alerts:
        _send_telegram_alerts(all_alerts)

    summary = store.get_summary()
    logger.info("Evaluated %d positions, %d alerts fired", len(positions), len(all_alerts))
    return {
        "evaluated": len(positions),
        "alerts": len(all_alerts),
        "alert_details": all_alerts,
        "summary": summary,
    }


def _send_telegram_alerts(alerts: List[Dict]):
    """Send alert summary to Telegram."""
    try:
        import sys
        sys.path.insert(0, str(PROJECT))
        from utils.telegram import send_message

        lines = ["🔔 <b>Position Monitor Alerts</b>", ""]

        for a in alerts[:10]:
            ticker = a.get("ticker", "?")
            if a.get("type") == "health_critical":
                lines.append(f"🔴 <b>{ticker}</b> — {a['message']}")
            elif a.get("type") == "invalidation_breach":
                lines.append(f"⛔ <b>{ticker}</b> — {a['message']}")
            else:
                label = a.get("condition_label", "")
                old = a.get("old_status", "?")
                new = a.get("new_status", "?")
                icon = {"failing": "🔴", "warning": "🟡", "passing": "🟢"}.get(new, "⚪")
                val = a.get("value")
                val_str = f" ({val})" if val is not None else ""
                lines.append(f"{icon} <b>{ticker}</b> {label}: {old} → {new}{val_str}")

        if len(alerts) > 10:
            lines.append(f"\n… +{len(alerts) - 10} more alerts")

        send_message("\n".join(lines))
    except Exception as e:
        logger.error("Failed to send Telegram alerts: %s", e)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    result = evaluate_all(send_telegram=False)
    print(f"Evaluated {result['evaluated']} positions, {result['alerts']} alerts")
