#!/usr/bin/env python3
"""Atlas End-of-Day Settlement.

Runs after ASX market close to:
1. Refresh closing prices for all held positions
2. Check and execute stop-loss / take-profit exits
3. Update MAE/MFE excursions for all positions
4. Record daily equity snapshot
5. Update dashboard with closing data
6. Generate EOD summary report

Usage:
    python scripts/eod_settlement.py [--dry-run]
"""
import sys
import os
import json
import logging
import argparse
import sqlite3
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

BRISBANE = ZoneInfo("Australia/Brisbane")

# Setup
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT as PROJECT
SNAPSHOT_LOG = PROJECT / "logs" / "portfolio_snapshots.jsonl"
os.chdir(PROJECT)

from utils.logging_config import setup_logging
log = setup_logging("eod_settlement", extra_log_file="eod_settlement")

# FIX-PMEQ-AUDIT-004: all 3 tracked markets must get a market_equity_history row
# each EOD, even if some have zero positions. Prevents next-day HWM inflation to
# global broker equity when all positions in a market closed the same day.
_TRACKED_MARKETS_FOR_ATTRIBUTION: tuple[str, ...] = ("sp500", "sector_etfs", "commodity_etfs")


def _health_log(level, message, detail=None):
    """Write to system_log table. Non-fatal."""
    try:
        from monitor.health_writer import log_error, log_warning, log_info
        fn = {"error": log_error, "warning": log_warning}.get(level, log_info)
        fn("eod_settlement", message, detail)
    except (ImportError, OSError, AttributeError, RuntimeError) as e:  # health_writer import/write
        log.warning("Health-log write failed (non-fatal): %s", e)


def load_config(market_id: str = "sp500") -> dict:
    """Load active config for the given market (consults overrides)."""
    from utils.config import get_active_config
    return get_active_config(market_id)


def fetch_closing_prices(tickers, market_id=None):
    """Fetch latest OHLC prices for held tickers via Tiingo IEX.

    Returns three dicts:
      prices  - closing prices  (used for equity, MAE/MFE, report)
      lows    - intraday lows   (used for stop-loss checks)
      highs   - intraday highs  (used for take-profit checks)

    Using intraday lows/highs ensures a stop or TP that was breached
    during the session is caught even if the close recovered above/below
    the trigger level — matching the backtest engine behaviour.

    Data source: Tiingo IEX only (no yfinance/Alpaca fallback).
    """
    from data.tiingo import get_tiingo_client

    log.info(f"Fetching OHLC prices from Tiingo for {len(tickers)} tickers")
    prices = {}  # close prices
    lows   = {}  # intraday lows  (stop-loss)
    highs  = {}  # intraday highs (take-profit)

    tiingo = get_tiingo_client()
    if tiingo is None:
        log.error("Tiingo client unavailable — check TIINGO_API_TOKEN in ~/.atlas-secrets.json")
        return prices, lows, highs

    quotes = tiingo.get_quotes(tickers)

    for ticker in tickers:
        q = quotes.get(ticker)
        if q and q.get("price", 0) > 0:
            prices[ticker] = q["price"]
            lows[ticker]   = q.get("low", q["price"]) or q["price"]
            highs[ticker]  = q.get("high", q["price"]) or q["price"]
        else:
            log.warning(f"No Tiingo data for {ticker}")

    log.info(f"Got OHLC prices for {len(prices)}/{len(tickers)} tickers (Tiingo)")
    return prices, lows, highs



def check_stop_losses(portfolio, prices, lows, trade_date, dry_run):
    """Check and execute stop-loss exits using intraday lows.

    Uses the day's intraday low (not just the close) to detect stop breaches,
    consistent with the backtest engine. Exit is recorded at the stop price
    (simulating a stop-market order), not at the intraday low.

    Positions with a stop_order_id (exchange stop placed) are skipped —
    the broker handles those, and reconcile_stops() syncs them to live state.
    """
    exits = []
    _fill_fallback_count = 0
    for pos in list(portfolio.positions):
        if pos.ticker not in prices:
            continue
        # Skip positions protected by exchange stop orders
        if getattr(pos, "stop_order_id", ""):
            log.info(f"  {pos.ticker}: exchange stop active (order {pos.stop_order_id}), skipping local check")
            continue
        intraday_low = lows.get(pos.ticker, prices[pos.ticker])
        close_price  = prices[pos.ticker]
        if intraday_low <= pos.stop_price:
            exit_price = pos.stop_price  # simulate stop-market fill at stop level
            log.warning(
                f"STOP HIT: {pos.ticker} intraday low ${intraday_low:.4f} "
                f"<= stop ${pos.stop_price:.4f} (close ${close_price:.4f}) "
                f"-> exit at ${exit_price:.4f}"
            )
            if not dry_run:
                # ── Submit broker sell order ──────────────────────
                # The broker is the source of truth for what we hold.
                # We MUST close at broker BEFORE updating internal state,
                # otherwise we get orphaned positions (broker still holds
                # shares but Atlas thinks the trade is closed).
                broker = getattr(portfolio, '_broker', None)
                broker_sell_ok = False
                actual_exit_price = exit_price  # default to stop price

                if broker:
                    try:
                        # Cancel existing protective orders first (SL/TP/trailing)
                        # so they don't hold shares and cause "insufficient qty"
                        try:
                            from brokers.live_executor import LiveExecutor
                            _temp_exec = LiveExecutor.__new__(LiveExecutor)
                            _temp_exec._broker = broker
                            _temp_exec._connected = True
                            _temp_exec._cancel_open_orders_for_ticker(pos.ticker)
                            import time
                            time.sleep(1.0)  # let Alpaca settle after cancel
                        except (ImportError, OSError, RuntimeError, ConnectionError) as _cancel_err:  # broker cancel call
                            log.warning("Failed to cancel protective orders for %s: %s", pos.ticker, _cancel_err)

                        from brokers.base import OrderSide, OrderType
                        log.info(f"STOP HIT for {pos.ticker} — submitting market sell to broker ({pos.shares} shares)")
                        sell_result = broker.place_order(
                            ticker=pos.ticker,
                            side=OrderSide.SELL,
                            qty=pos.shares,
                            price=0.0,
                            order_type=OrderType.MARKET,
                            remark="eod_stop_loss",
                        )
                        if sell_result.success:
                            broker_sell_ok = True
                            # Priority 1: broker_orders canonical fill (B.3 oracle)
                            _p1_fill = None
                            try:
                                from db import atlas_db as _adb
                                _p1_fill = _adb.get_fill_price(sell_result.order_id)
                            except (ImportError, AttributeError, sqlite3.OperationalError) as _p1_exc:
                                log.debug("broker_orders fill price lookup failed (non-fatal): %s", _p1_exc)
                            if _p1_fill and _p1_fill > 0:
                                actual_exit_price = _p1_fill
                                log.info(f"[fill-price P1] {pos.ticker}: broker_orders fill ${_p1_fill:.4f} order_id={sell_result.order_id}")
                            elif sell_result.fill_price and sell_result.fill_price > 0:
                                # Priority 2: immediate broker API fill price
                                actual_exit_price = sell_result.fill_price
                                log.info(f"Broker sell confirmed for {pos.ticker}: order_id={sell_result.order_id}, price=${actual_exit_price:.4f}")
                            else:
                                # Priority 3 (degraded): stop_price as fill estimate
                                _fill_fallback_count += 1
                                log.warning(
                                    f"[fill-price] eod_settlement using stop_price as fill price "
                                    f"(broker_orders empty for order_id={sell_result.order_id}) ticker={pos.ticker}"
                                )
                        else:
                            log.error(f"Broker sell FAILED for {pos.ticker}: {sell_result.message} — NOT marking as closed internally")
                    except Exception as _broker_err:  # noqa: BLE001 — broker sell can raise any SDK exception
                        log.error("Broker sell exception for %s: %s — NOT marking as closed internally", pos.ticker, _broker_err)
                else:
                    # No broker connected (backtest/offline mode) — proceed with internal-only exit
                    broker_sell_ok = True
                    log.info(f"No broker connected — recording internal exit only for {pos.ticker}")

                if broker_sell_ok:
                    result = portfolio.execute_exit(pos.ticker, actual_exit_price, trade_date, "stop_loss")
                    if result:
                        exits.append(result)
                        # SQLite dual-write (non-fatal — ledger is source of truth)
                        try:
                            from db import atlas_db
                            try:
                                from regime.model import RegimeModel
                                _eod_regime = RegimeModel().classify_current().state.value
                            except (ImportError, AttributeError, ValueError, RuntimeError) as _re:  # regime model
                                log.debug(
                                    "RegimeModel classification failed (non-fatal, using None): %s",
                                    _re,
                                )
                                _eod_regime = None
                            atlas_db.record_trade_exit(
                                ticker=pos.ticker,
                                strategy=getattr(pos, "strategy", ""),
                                exit_price=actual_exit_price,
                                exit_reason="stop_loss",
                                regime_at_exit=_eod_regime,
                            )
                        except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError, AttributeError) as _e:  # DB write
                            log.warning("SQLite trade exit dual-write failed: %s", _e)
            else:
                exits.append({"ticker": pos.ticker, "type": "stop_loss",
                              "intraday_low": intraday_low, "stop_price": pos.stop_price,
                              "exit_price": exit_price, "dry_run": True})
    return exits, _fill_fallback_count


def check_take_profits(portfolio, prices, highs, trade_date, dry_run):
    """Check and execute take-profit exits using intraday highs.

    Uses the day's intraday high (not just the close) to detect TP hits,
    consistent with the backtest engine. Exit is recorded at the take-profit
    price (simulating a limit order fill at the TP level).
    """
    exits = []
    _fill_fallback_count = 0
    for pos in list(portfolio.positions):
        if not pos.take_profit or pos.ticker not in prices:
            continue
        intraday_high = highs.get(pos.ticker, prices[pos.ticker])
        close_price   = prices[pos.ticker]
        if intraday_high >= pos.take_profit:
            exit_price = pos.take_profit  # simulate limit fill at TP level
            log.info(
                f"TP HIT: {pos.ticker} intraday high ${intraday_high:.4f} "
                f">= target ${pos.take_profit:.4f} (close ${close_price:.4f}) "
                f"-> exit at ${exit_price:.4f}"
            )
            if not dry_run:
                # ── Submit broker sell order ──────────────────────
                # The broker is the source of truth for what we hold.
                # We MUST close at broker BEFORE updating internal state,
                # otherwise we get orphaned positions (broker still holds
                # shares but Atlas thinks the trade is closed).
                broker = getattr(portfolio, '_broker', None)
                broker_sell_ok = False
                actual_exit_price = exit_price  # default to take-profit price

                if broker:
                    try:
                        # Cancel existing protective orders first (SL/TP/trailing)
                        # so they don't hold shares and cause "insufficient qty"
                        try:
                            from brokers.live_executor import LiveExecutor
                            _temp_exec = LiveExecutor.__new__(LiveExecutor)
                            _temp_exec._broker = broker
                            _temp_exec._connected = True
                            _temp_exec._cancel_open_orders_for_ticker(pos.ticker)
                            import time
                            time.sleep(1.0)  # let Alpaca settle after cancel
                        except (ImportError, OSError, RuntimeError, ConnectionError) as _cancel_err:  # broker cancel call
                            log.warning("Failed to cancel protective orders for %s: %s", pos.ticker, _cancel_err)

                        from brokers.base import OrderSide, OrderType
                        log.info(f"TP HIT for {pos.ticker} — submitting sell to broker ({pos.shares} shares)")
                        sell_result = broker.place_order(
                            ticker=pos.ticker,
                            side=OrderSide.SELL,
                            qty=pos.shares,
                            price=0.0,
                            order_type=OrderType.MARKET,
                            remark="eod_take_profit",
                        )
                        if sell_result.success:
                            broker_sell_ok = True
                            # Priority 1: broker_orders canonical fill (B.3 oracle)
                            _p1_fill = None
                            try:
                                from db import atlas_db as _adb
                                _p1_fill = _adb.get_fill_price(sell_result.order_id)
                            except (ImportError, AttributeError, sqlite3.OperationalError) as _p1_exc:
                                log.debug("broker_orders fill price lookup failed (non-fatal): %s", _p1_exc)
                            if _p1_fill and _p1_fill > 0:
                                actual_exit_price = _p1_fill
                                log.info(f"[fill-price P1] {pos.ticker}: broker_orders fill ${_p1_fill:.4f} order_id={sell_result.order_id}")
                            elif sell_result.fill_price and sell_result.fill_price > 0:
                                # Priority 2: immediate broker API fill price
                                actual_exit_price = sell_result.fill_price
                                log.info(f"Broker sell confirmed for {pos.ticker}: order_id={sell_result.order_id}, price=${actual_exit_price:.4f}")
                            else:
                                # Priority 3 (degraded): take_profit price as fill estimate
                                _fill_fallback_count += 1
                                log.warning(
                                    f"[fill-price] eod_settlement using take_profit as fill price "
                                    f"(broker_orders empty for order_id={sell_result.order_id}) ticker={pos.ticker}"
                                )
                        else:
                            log.error(f"Broker sell FAILED for {pos.ticker}: {sell_result.message} — NOT marking as closed internally")
                    except Exception as _broker_err:  # noqa: BLE001 — broker sell can raise any SDK exception
                        log.error("Broker sell exception for %s: %s — NOT marking as closed internally", pos.ticker, _broker_err)
                else:
                    # No broker connected (backtest/offline mode) — proceed with internal-only exit
                    broker_sell_ok = True
                    log.info(f"No broker connected — recording internal exit only for {pos.ticker}")

                if broker_sell_ok:
                    result = portfolio.execute_exit(pos.ticker, actual_exit_price, trade_date, "take_profit")
                    if result:
                        exits.append(result)
                        # SQLite dual-write (non-fatal — ledger is source of truth)
                        try:
                            from db import atlas_db
                            try:
                                from regime.model import RegimeModel
                                _eod_regime = RegimeModel().classify_current().state.value
                            except (ImportError, AttributeError, ValueError, RuntimeError) as _re:  # regime model
                                log.debug(
                                    "RegimeModel classification failed (non-fatal, using None): %s",
                                    _re,
                                )
                                _eod_regime = None
                            atlas_db.record_trade_exit(
                                ticker=pos.ticker,
                                strategy=getattr(pos, "strategy", ""),
                                exit_price=actual_exit_price,
                                exit_reason="take_profit",
                                regime_at_exit=_eod_regime,
                            )
                        except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError, AttributeError) as _e:  # DB write
                            log.warning("SQLite trade exit dual-write failed: %s", _e)
            else:
                exits.append({"ticker": pos.ticker, "type": "take_profit",
                              "intraday_high": intraday_high, "take_profit": pos.take_profit,
                              "exit_price": exit_price, "dry_run": True})
    return exits, _fill_fallback_count


def generate_eod_report(portfolio, prices, trade_date, stop_exits, tp_exits, market: str = "asx"):
    """Generate formatted EOD report."""
    summary = portfolio.portfolio_summary(prices)
    eq = summary["equity"]
    starting = summary["starting_equity"]

    # Today's P&L from equity history
    prev_equity = starting
    if portfolio.equity_history:
        prev_equity = portfolio.equity_history[-1].get("equity", starting)
    daily_pnl = eq - prev_equity
    daily_pnl_pct = (daily_pnl / prev_equity * 100) if prev_equity > 0 else 0

    lines = []
    lines.append("=" * 55)
    lines.append(f"  ATLAS-{market.upper()} END-OF-DAY REPORT -- {trade_date}")
    lines.append("=" * 55)
    lines.append("")

    lines.append("PORTFOLIO OVERVIEW")
    lines.append(f"   Equity:      ${eq:>10,.2f}")
    lines.append(f"   Cash:        ${summary['cash']:>10,.2f}")
    lines.append(f"   Invested:    ${eq - summary['cash']:>10,.2f}")
    lines.append(f"   Positions:   {summary['num_open']}")
    lines.append("")

    arrow = "UP" if daily_pnl >= 0 else "DOWN"
    lines.append(f"DAILY P&L ({arrow})")
    lines.append(f"   Today:       ${daily_pnl:>+10,.2f} ({daily_pnl_pct:>+.2f}%)")
    lines.append(f"   Total:       ${summary['total_pnl']:>+10,.2f} ({summary['total_pnl_pct']:>+.2f}%)")
    lines.append("")

    if stop_exits:
        lines.append(f"STOP-LOSS EXITS ({len(stop_exits)})")
        for ex in stop_exits:
            if "pnl" in ex:
                lines.append(f"   {ex['ticker']}: ${ex['exit_price']:.2f} | PnL ${ex['pnl']:+.2f} ({ex['pnl_pct']:+.1f}%)")
            else:
                lines.append(f"   {ex['ticker']}: ${ex['current_price']:.4f} <= stop ${ex['stop_price']:.4f} [DRY RUN]")
        lines.append("")

    if tp_exits:
        lines.append(f"TAKE-PROFIT EXITS ({len(tp_exits)})")
        for ex in tp_exits:
            if "pnl" in ex:
                lines.append(f"   {ex['ticker']}: ${ex['exit_price']:.2f} | PnL ${ex['pnl']:+.2f} ({ex['pnl_pct']:+.1f}%)")
            else:
                lines.append(f"   {ex['ticker']}: ${ex['current_price']:.4f} >= target ${ex['take_profit']:.4f} [DRY RUN]")
        lines.append("")

    if summary["open_positions"]:
        lines.append(f"OPEN POSITIONS ({len(summary['open_positions'])})")
        lines.append(f"   {'Ticker':<8} {'Entry':>8} {'Close':>8} {'PnL$':>9} {'PnL%':>7} {'Stop':>8}")
        lines.append(f"   {'---':<8} {'---':>8} {'---':>8} {'---':>9} {'---':>7} {'---':>8}")
        for p in summary["open_positions"]:
            lines.append(f"   {p['ticker']:<8} ${p['entry_price']:>7.2f} ${p['current_price']:>7.2f} "
                        f"${p['unrealized_pnl']:>+8.2f} {p['unrealized_pnl_pct']:>+6.1f}% "
                        f"${p['stop_price']:>7.2f}")
        lines.append("")

    today_closed = [t for t in portfolio.closed_trades if t.get("exit_date") == trade_date]
    if today_closed:
        lines.append(f"TRADES CLOSED TODAY ({len(today_closed)})")
        total_realized = sum(t.get("pnl", 0) or 0 for t in today_closed)
        for t in today_closed:
            _pnl = t.get('pnl', 0) or 0
            _pnl_pct = t.get('pnl_pct', 0) or 0
            lines.append(f"   {t.get('ticker','?')} {t.get('strategy','?')}: ${_pnl:+.2f} ({_pnl_pct:+.1f}%) [{t.get('exit_reason','?')}]")
        lines.append(f"   Total realized: ${total_realized:+.2f}")
        lines.append("")

    try:
        from markets import get_market
        _settle_tz = get_market(market_id).operator_tz() if 'market_id' in dir() else BRISBANE
    except (ImportError, AttributeError, RuntimeError) as _tz_e:  # market module import/call
        log.debug("Could not detect operator timezone (using Brisbane fallback): %s", _tz_e)
        _settle_tz = BRISBANE
    _settle_now = datetime.now(_settle_tz)
    lines.append(f"Settlement completed at {_settle_now.strftime('%I:%M %p %Z')}")
    lines.append("=" * 55)

    return "\n".join(lines)


def record_daily_snapshot(portfolio, prices: dict, eq: float, daily_pnl: float, trade_date: str):
    """Append a daily portfolio snapshot to logs/portfolio_snapshots.jsonl.

    Uses atomic write pattern (tmp → append) to prevent JSONL corruption.
    Never raises — snapshot failure must not interrupt the settlement flow.
    """
    try:
        position_list = []
        for pos in portfolio.positions:
            cur_price = prices.get(pos.ticker, pos.entry_price)
            position_list.append({
                "ticker": pos.ticker,
                "shares": pos.shares,
                "entry_price": pos.entry_price,
                "current_price": cur_price,
                "unrealized_pnl": pos.unrealized_pnl(cur_price),
                "market_value": pos.current_value(cur_price),
            })

        snapshot = {
            "date": trade_date,
            "timestamp": datetime.now().isoformat(),
            "equity": eq,
            "cash": portfolio.cash,
            "market_value": round(eq - portfolio.cash, 2),
            "buying_power": portfolio.cash,  # cash is effective buying power for Atlas positions
            "num_positions": len(portfolio.positions),
            "positions": position_list,
            "daily_pnl": daily_pnl,
        }

        SNAPSHOT_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(snapshot, default=str) + "\n"

        # Atomic write: stage to tmp, append to log only when fully serialised
        tmp = SNAPSHOT_LOG.with_suffix(".snap.tmp")
        tmp.write_text(line, encoding="utf-8")
        with open(SNAPSHOT_LOG, "ab") as f:
            f.write(tmp.read_bytes())
        tmp.unlink(missing_ok=True)

        log.info(
            "Portfolio snapshot recorded: equity=$%.2f, positions=%d, daily_pnl=$%+.2f",
            eq, len(position_list), daily_pnl,
        )
    except Exception as exc:  # noqa: BLE001 — snapshot touches DB+broker, any failure is non-fatal
        log.warning("Portfolio snapshot failed (non-fatal): %s", exc)


def update_dashboard():
    """Dashboard data is now served via SQLite API endpoints.

    generate_data.py was retired in Phase 5 — this function is a no-op.
    The dashboard server reads directly from the SQLite database.
    """
    log.info("Dashboard update skipped — SQLite API endpoints serve live data (generate_data.py retired)")
    return True


def save_eod_report(report, trade_date):
    report_path = PROJECT / "logs" / f"eod_{trade_date}.txt"
    with open(report_path, "w") as f:
        f.write(report)
    log.info(f"EOD report saved: {report_path}")
    return report_path


def main():
    parser = argparse.ArgumentParser(description="Atlas End-of-Day Settlement")
    parser.add_argument("--dry-run", action="store_true", help="Preview without executing exits")
    parser.add_argument("--market", "-m", default="asx", help="Market ID (default: asx)")
    args = parser.parse_args()

    market_id = args.market

    # Use exchange timezone for trade-date and weekend detection.
    # The operator may be in a different TZ (e.g. AEST Saturday = NYSE Friday),
    # so we must use the exchange's local date to decide if it's a trading day.
    try:
        from markets import get_market
        _market = get_market(market_id)
        _tz = _market.exchange_tz()
        _is_weekend = _market.is_weekend(datetime.now(_tz).weekday())
        _tz_label = datetime.now(_tz).strftime("%Z")
    except (ImportError, KeyError):
        # Fallback: use correct exchange timezone per market
        _tz = ZoneInfo("America/New_York") if market_id == "sp500" else BRISBANE
        _is_weekend = datetime.now(_tz).weekday() >= 5
        _tz_label = "AEST"

    now = datetime.now(_tz)
    trade_date = now.strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info(f"END-OF-DAY SETTLEMENT [{market_id.upper()}] -- {trade_date}")
    log.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    log.info("=" * 60)

    # Check if trading day
    if _is_weekend:
        log.info(f"Today is {now.strftime('%A')} - not a trading day for {market_id}, skipping EOD.")
        print(f"Not a trading day ({now.strftime('%A')}). No settlement needed.")
        return

    # Load config and portfolio from live broker
    config = load_config(market_id)

    # Skip non-live markets — they don't have broker connections
    # Paper mode is active even without live_enabled (targets Alpaca paper account)
    live_enabled = config.get("trading", {}).get("live_enabled", False)
    _mode_eod = config.get("trading", {}).get("mode", "live")
    _mode_label_eod = f"[{_mode_eod.upper()}]"
    if not (live_enabled or _mode_eod == "paper"):
        log.info("%s Market %s is not live-enabled (live_enabled=False). Skipping EOD settlement.", _mode_label_eod, market_id)
        print(f"Market {market_id} is not live-enabled. Skipping settlement.")
        _health_log("info", "EOD settlement skipped: market not live-enabled", {"market": market_id, "reason": "market_disabled"})
        return

    from brokers.live_portfolio import LivePortfolio
    portfolio = LivePortfolio(config, market_id=market_id)

    # Connect with retry (transient failures: DNS, rate-limit, maintenance windows)
    import time as _time
    _connect_delays = [5, 15, 45]  # exponential backoff
    _connected = False
    for _attempt, _delay in enumerate(_connect_delays, 1):
        if portfolio.connect():
            _connected = True
            break
        if _attempt < len(_connect_delays):
            log.warning("Broker connect attempt %d/%d failed for %s, retrying in %ds...",
                        _attempt, len(_connect_delays), market_id, _delay)
            _time.sleep(_delay)
        else:
            log.error("Broker connect failed after %d attempts for %s", len(_connect_delays), market_id)

    if not _connected:
        log.error("Could not connect to broker after retries. Aborting EOD settlement.")
        _health_log("error", "Broker connection failed after retries", {"market": market_id, "attempts": len(_connect_delays)})
        # Send Telegram alert
        try:
            from utils.telegram import send_message, tg_escape as _tge
            send_message(f"\U0001f534 <b>EOD Settlement Failed</b>\nMarket: {_tge(market_id)}\nBroker connection failed after {len(_connect_delays)} attempts.\nCheck logs/eod_settlement.log")
        except (ImportError, OSError, ConnectionError, RuntimeError) as e:  # Telegram non-fatal
            log.warning("Broker-failure Telegram alert could not be sent: %s", e)
        print("ERROR: Broker connection failed after retries. Settlement aborted.")
        return

    # Guard: LivePortfolio detects zeroed broker data (OpenD up but Futu
    # backend unreachable) and sets broker_data_valid = False.
    # Abort settlement to prevent recording corrupted equity points.
    if not portfolio.broker_data_valid:
        log.error(
            "Broker returned zeroed data (likely offline). "
            "Aborting settlement to prevent state corruption."
        )
        _health_log("error", "Broker returned zeroed data — settlement aborted", {"market": market_id})
        print("ERROR: Broker returned zeroed data (likely offline). "
              "Settlement aborted to protect state.")
        return

    if not portfolio.positions:
        log.info("No open positions. Recording equity and updating dashboard.")
        portfolio.record_equity(trade_date, {})
        update_dashboard()
        print("No open positions. EOD settlement complete (equity recorded, dashboard updated).")
        return

    # Get tickers from open positions
    held_tickers = [pos.ticker for pos in portfolio.positions]
    log.info(f"Held positions: {held_tickers}")

    # Fetch OHLC prices (close for equity/report, low for stops, high for TPs)
    prices, lows, highs = fetch_closing_prices(held_tickers, market_id=market_id)

    if not prices:
        log.error("Could not fetch any closing prices. Aborting settlement.")
        _health_log("error", "No closing prices fetched — settlement aborted", {"market": market_id})
        print("ERROR: Could not fetch closing prices. Settlement aborted.")
        return

    missing = [t for t in held_tickers if t not in prices]
    if missing:
        log.warning(f"Missing prices for: {missing}")

    # Update position excursions (MAE/MFE)
    log.info("Updating position excursions (MAE/MFE)...")
    portfolio.update_positions(prices)

    # Protective order sync removed: IBKR removed — per-broker sync handled by sync_protective_orders.py cron

    # ── Ledger-broker reconciliation ─────────────────────────
    # Catches LIMIT fills that happened after order submission but before
    # ledger was updated (e.g. limit order placed at premarket, filled at open).
    log.info("Reconciling trade ledger vs broker...")
    try:
        from scripts.reconcile_ledger import reconcile_ledger as _reconcile_ledger
        _ledger_result = _reconcile_ledger(market_id, dry_run=args.dry_run, broker=portfolio._broker)
        if _ledger_result.get("backfilled"):
            log.info("Ledger backfilled: %s", _ledger_result["backfilled"])
        if _ledger_result.get("closed_phantom"):
            log.info("Ledger phantoms closed: %s", _ledger_result["closed_phantom"])
    except Exception as _lr_err:  # noqa: BLE001 — reconciliation touches broker+DB+file ops
        log.warning("Ledger reconciliation failed (non-fatal): %s", _lr_err)

    # Reconcile broker-side fills (trailing stops, etc.)
    log.info("Reconciling broker-side fills...")
    broker_fills = portfolio.reconcile_broker_fills(trade_date)
    if broker_fills:
        for bf in broker_fills:
            log.info(f"  RECONCILED: {bf['ticker']} exited at ${bf['exit_price']:.2f} "
                     f"({bf['exit_reason']}) — PnL ${bf['pnl']:.2f}")
    else:
        log.info("  No unreconciled broker fills found.")

    # Check stop-losses
    log.info("Checking stop-losses...")
    stop_exits, _stop_fill_fallbacks = check_stop_losses(portfolio, prices, lows, trade_date, args.dry_run)

    # Check take-profits
    log.info("Checking take-profits...")
    tp_exits, _tp_fill_fallbacks = check_take_profits(portfolio, prices, highs, trade_date, args.dry_run)

    _total_fill_fallbacks = _stop_fill_fallbacks + _tp_fill_fallbacks
    if _total_fill_fallbacks:
        log.warning(
            "EOD: %d fill-price fallback(s) — stop/TP price used instead of "
            "broker-confirmed fill (run sync_broker_orders.py daily to minimise)",
            _total_fill_fallbacks,
        )

    # Check daily drawdown
    halted, dd = portfolio.check_daily_drawdown(prices)
    if halted:
        log.warning(f"Daily drawdown limit breached: {dd:.2%}")

    # Record daily equity
    log.info("Recording daily equity snapshot...")
    portfolio.record_equity(trade_date, prices)
    portfolio.save_state()

    # Generate EOD report
    report = generate_eod_report(portfolio, prices, trade_date, stop_exits, tp_exits, market=market_id)
    report_path = save_eod_report(report, trade_date)

    # Update dashboard
    update_dashboard()

    # Print report
    print(report)
    print(f"\nFull report saved to: {report_path}")

    # Summary stats for automation and future analysis
    eq = portfolio.equity(prices)
    prev_equity = portfolio.starting_equity
    if portfolio.equity_history and len(portfolio.equity_history) > 1:
        prev_equity = portfolio.equity_history[-2].get("equity", portfolio.starting_equity)
    elif portfolio.equity_history:
        prev_equity = portfolio.equity_history[-1].get("equity", portfolio.starting_equity)
    daily_pnl = round(eq - prev_equity, 2)
    total_pnl = round(eq - portfolio.starting_equity, 2)

    # Per-position snapshot
    position_snapshot = []
    for pos in portfolio.positions:
        if pos.ticker in prices:
            p = prices[pos.ticker]
            position_snapshot.append({
                "ticker": pos.ticker,
                "strategy": pos.strategy,
                "entry_date": pos.entry_date,
                "entry_price": pos.entry_price,
                "close_price": p,
                "shares": pos.shares,
                "unrealized_pnl": pos.unrealized_pnl(p),
                "unrealized_pnl_pct": pos.unrealized_pnl_pct(p),
                "stop_price": pos.stop_price,
                "take_profit": pos.take_profit,
                "mae_pct": round(pos.mae * 100, 2),
                "mfe_pct": round(pos.mfe * 100, 2),
                "holding_days": pos.holding_days(trade_date),
                "sector": pos.sector,
            })

    # Today's closed trades
    today_closed = [t for t in portfolio.closed_trades if t.get("exit_date") == trade_date]

    # Include reconciled fills from previous days that weren't recorded
    reconciled_closed = [t for t in portfolio.closed_trades if t.get("reconciled")]
    for rc in reconciled_closed:
        if rc not in today_closed:
            today_closed.append(rc)

    realized_today = round(sum(t.get("pnl", 0) for t in today_closed), 2)

    summary = {
        "trade_date": trade_date,
        "market_id": market_id,
        "equity": eq,
        "broker_equity": portfolio.broker_equity(),
        "cash": portfolio.cash,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": round(daily_pnl / prev_equity * 100, 2) if prev_equity > 0 else 0,
        "total_pnl": total_pnl,
        "total_pnl_pct": round(total_pnl / portfolio.starting_equity * 100, 2) if portfolio.starting_equity > 0 else 0,
        "positions": len(portfolio.positions),
        "position_details": position_snapshot,
        "stop_exits": len(stop_exits),
        "tp_exits": len(tp_exits),
        "fill_price_fallbacks": _total_fill_fallbacks,
        "signal_exits": len([t for t in today_closed if t.get("exit_reason") == "signal_exit"]),
        "realized_pnl_today": realized_today,
        "closed_trades_today": today_closed,
        "halted": portfolio.halted,
        "report_path": str(report_path),
    }
    summary_path = PROJECT / "logs" / f"eod_summary_{trade_date}.json"
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    log.info(f"EOD summary saved: {summary_path}")

    # Record daily portfolio snapshot to logs/portfolio_snapshots.jsonl
    record_daily_snapshot(portfolio, prices, eq, daily_pnl, trade_date)

    # SQLite primary write: equity curve + portfolio/position snapshots
    try:
        from db import atlas_db
        positions_value = round(eq - portfolio.cash, 2)
        daily_pnl_pct = round(daily_pnl / prev_equity * 100, 2) if prev_equity > 0 else 0
        total_pnl_pct = round(total_pnl / portfolio.starting_equity * 100, 2) if portfolio.starting_equity > 0 else 0
        atlas_db.record_equity(
            date=trade_date,
            market_id=market_id,
            equity=eq,
            cash=portfolio.cash,
            positions_value=positions_value,
            day_pnl=daily_pnl,
            regime_state=None,
            broker_equity=portfolio.broker_equity(),
            daily_pnl_pct=daily_pnl_pct,
            total_pnl=total_pnl,
            total_pnl_pct=total_pnl_pct,
            positions_count=len(portfolio.positions),
            realized_pnl=realized_today,
        )
        _snap_ts = datetime.now().isoformat()
        atlas_db.record_snapshot(
            timestamp=_snap_ts,
            total_equity=eq,
            cash=portfolio.cash,
            positions=position_snapshot,
            regime_state=None,
            market_id=market_id,
        )
        # Broker-level aggregate snapshot (market_id='ALL'): authoritative total equity.
        # Written once per market EOD so the latest ALL row is from the last market to
        # settle.  Readers wanting portfolio total should use get_latest_snapshot('ALL').
        _broker_eq = portfolio.broker_equity()
        _broker_cash = portfolio.cash  # same account cash for all markets
        atlas_db.record_all_markets_snapshot(
            timestamp=_snap_ts,
            broker_equity=_broker_eq,
            broker_cash=_broker_cash,
        )
        # Per-position snapshots for historical tracking
        atlas_db.record_position_snapshots(
            date=trade_date,
            market_id=market_id,
            positions=position_snapshot,
        )
        log.info("SQLite EOD data written: equity_curve + portfolio_snapshots + position_snapshots")
    except (ImportError, sqlite3.OperationalError, sqlite3.DatabaseError, AttributeError) as _e:  # DB write
        log.warning("SQLite EOD write failed (non-fatal): %s", _e)

    # RCA #4D: per-market virtual equity attribution
    # FIX-PMEQ-AUDIT-004: pre-populate ALL tracked markets so zero-position markets
    # still get a row in market_equity_history each EOD. Without this, the next
    # morning _get_per_market_equity returns None → falls back to global broker
    # equity for HWM comparison → a small global drawdown could trip a per-market
    # HALT (false positive).
    try:
        from portfolio.market_equity_attribution import attribute_equity_pro_rata
        from portfolio.per_market_cash_flow import compute_realized_cash_flow_since
        from universe.membership import derive_universe
        from db.atlas_db import get_db
        from datetime import timezone

        # Pre-populate so every tracked market is a key (empty list = zero positions).
        # attribute_equity_pro_rata will produce a row with position_mv=0 /
        # cash_attributed=0 for zero-position markets; the carry-forward block below
        # then overwrites that row with the correct cash baseline.
        _positions_by_market: dict[str, list[dict]] = {
            m: [] for m in _TRACKED_MARKETS_FOR_ATTRIBUTION
        }
        for _bp in portfolio._broker.get_positions():
            _m = derive_universe(_bp.ticker)
            if _m is None:
                continue
            _positions_by_market.setdefault(_m, []).append({
                "ticker": _bp.ticker,
                "market_value": _bp.market_value,
            })

        _broker_eq = portfolio.broker_equity()
        _broker_cash = portfolio.cash
        _attribution = attribute_equity_pro_rata(
            broker_equity=_broker_eq,
            broker_cash=_broker_cash,
            positions_by_market=_positions_by_market,
        )

        # ── Carry-forward for zero-position markets ───────────────────────────
        # When a market has no positions, attribute_equity_pro_rata gives it
        # allocated_equity=0 (pro-rata share = 0/total_mv = 0).  But _get_per_market_equity
        # uses the snapshot's cash_attributed as the cash baseline.  If cash_attributed=0,
        # the per-market equity formula returns 0 → HWM comparison is meaningless →
        # false HALT risk.  Fix: carry forward the previous snapshot's cash_attributed
        # plus any realized cash flows since that snapshot (from exits today).
        _zero_position_markets = [
            m for m in _TRACKED_MARKETS_FOR_ATTRIBUTION
            if not _positions_by_market.get(m)
        ]
        if _zero_position_markets:
            log.info(
                "FIX-PMEQ-AUDIT-004: zero-position markets detected: %s — "
                "carrying forward cash_attributed from previous snapshot",
                _zero_position_markets,
            )
            with get_db() as _db_carry:
                for _zm in _zero_position_markets:
                    _prev = _db_carry.execute(
                        "SELECT cash_attributed, snapshot_time "
                        "FROM market_equity_history "
                        "WHERE market_id = ? ORDER BY date DESC, created_at DESC LIMIT 1",
                        (_zm,),
                    ).fetchone()
                    if _prev is None:
                        # No prior snapshot — give equal share of broker_cash
                        # (rare: only on very first EOD run for this market).
                        _carry = round(_broker_cash / max(1, len(_TRACKED_MARKETS_FOR_ATTRIBUTION)), 2)
                        log.warning(
                            "FIX-PMEQ-AUDIT-004: no prior snapshot for %s — "
                            "using equal cash share $%.2f",
                            _zm, _carry,
                        )
                    else:
                        _prev_cash = float(_prev["cash_attributed"] or 0.0)
                        _prev_snap_time_str = _prev["snapshot_time"]
                        _flow = 0.0
                        try:
                            if _prev_snap_time_str:
                                _ts = _prev_snap_time_str.replace("Z", "+00:00")
                                _prev_snap_time = datetime.fromisoformat(_ts)
                                if _prev_snap_time.tzinfo is None:
                                    _prev_snap_time = _prev_snap_time.replace(
                                        tzinfo=timezone.utc
                                    )
                                _market_symbols = {
                                    m: set() for m in _TRACKED_MARKETS_FOR_ATTRIBUTION
                                }
                                _flows, _degraded = compute_realized_cash_flow_since(
                                    portfolio._broker, _prev_snap_time, _market_symbols
                                )
                                if _degraded:
                                    log.warning(
                                        "FIX-PMEQ-AUDIT-004: activities API degraded "
                                        "for %s carry-forward — using prev cash only",
                                        _zm,
                                    )
                                else:
                                    _flow = _flows.get(_zm, 0.0)
                        except Exception as _cf_exc:
                            log.warning(
                                "FIX-PMEQ-AUDIT-004: carry-forward flow lookup "
                                "failed for %s: %s",
                                _zm, _cf_exc,
                            )
                        _carry = round(_prev_cash + _flow, 2)
                        log.info(
                            "FIX-PMEQ-AUDIT-004: carry-forward for %s: "
                            "prev_cash=$%.2f + flow=$%.2f → cash_attributed=$%.2f",
                            _zm, _prev_cash, _flow, _carry,
                        )
                    # Override the zero row with the correct cash baseline.
                    _attribution[_zm] = {
                        "position_mv": 0.0,
                        "cash_attributed": _carry,
                        "allocated_equity": _carry,
                    }

        _today = trade_date
        _snap_iso = datetime.now(timezone.utc).isoformat()
        with get_db() as _conn:
            for _mid, _vals in _attribution.items():
                _conn.execute(
                    """INSERT OR REPLACE INTO market_equity_history
                       (date, market_id, allocated_equity, position_mv, cash_attributed,
                        broker_equity, broker_cash, snapshot_time)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _today, _mid, _vals["allocated_equity"], _vals["position_mv"],
                        _vals["cash_attributed"], _broker_eq, _broker_cash, _snap_iso,
                    ),
                )
            _conn.commit()
        log.info("Market equity attribution: %s", _attribution)
    except Exception as _eq_exc:  # noqa: BLE001 — equity attribution touches DB+market ops
        log.warning("Per-market equity attribution failed (non-fatal): %s", _eq_exc)

    _health_log("info", "EOD settlement completed", {
        "market": market_id,
        "trade_date": trade_date,
        "equity": eq,
        "daily_pnl": daily_pnl,
        "positions": len(portfolio.positions),
        "stop_exits": len(stop_exits),
        "tp_exits": len(tp_exits),
    })

    # Disconnect broker to free clientId for position monitor
    portfolio.disconnect()


# State file for per-day Telegram dedup: prevents all 3 market EOD
# invocations (sp500 / commodity_etfs / sector_etfs at 08:00 UTC) from
# sending the same position-alert Telegram three times.  evaluate_all
# is market-agnostic — it checks ALL open positions — so once is enough.
_EOD_MONITOR_STATE_FILE = PROJECT / "data" / "eod_position_monitor_state.json"


def _eod_monitor_already_sent_today() -> bool:
    """Return True if a position-monitor Telegram was already sent today."""
    try:
        if _EOD_MONITOR_STATE_FILE.exists():
            data = json.loads(_EOD_MONITOR_STATE_FILE.read_text())
            from datetime import date
            return data.get("last_sent_date") == str(date.today())
    except (json.JSONDecodeError, OSError, AttributeError, KeyError) as _mon_err:  # state file read
        log.debug("_eod_monitor_already_sent_today: state read failed (non-fatal): %s", _mon_err)
    return False


def _eod_monitor_mark_sent() -> None:
    """Record that the position-monitor Telegram was sent today."""
    try:
        from datetime import date
        _EOD_MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _EOD_MONITOR_STATE_FILE.write_text(
            json.dumps({"last_sent_date": str(date.today())}),
        )
    except (OSError, json.JSONDecodeError) as exc:  # state file write
        log.warning("_eod_monitor_mark_sent: state write failed (non-fatal): %s", exc)


def run_position_monitor():
    """Evaluate manual position conditions after EOD settlement.

    Deduplication: evaluate_all checks ALL positions regardless of which
    market triggered this EOD run.  The 3 concurrent market cron entries
    (sp500 / commodity_etfs / sector_etfs) all call this function at 08:00
    UTC.  We use a daily cooldown so only the first run sends Telegram — the
    others still evaluate (for logging) but suppress the send.
    """
    try:
        from monitor.evaluator import evaluate_all
        already_sent = _eod_monitor_already_sent_today()
        if already_sent:
            log.info("Position monitor: Telegram already sent today — running evaluate_all silent")
        result = evaluate_all(send_telegram=(not already_sent))
        if not already_sent and result.get("alerts", 0) > 0:
            _eod_monitor_mark_sent()
        log.info(f"Position monitor: evaluated {result['evaluated']} positions, "
                 f"{result['alerts']} alerts fired (telegram={'suppressed' if already_sent else 'enabled'})")
    except Exception as e:  # noqa: BLE001 — position monitor evaluation wraps broker+DB ops
        log.error("Position monitor evaluation failed: %s", e)


if __name__ == "__main__":
    try:
        main()
        # Run position monitor after EOD settlement
        # (main() broker connection is closed at end of main, so clientId is free)
        run_position_monitor()
    except Exception as exc:  # noqa: BLE001 — top-level crash guard; must catch all
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message, tg_escape as _tge
            send_message(
                f"🚨 <b>eod_settlement CRASHED</b>\n\n"
                f"<pre>{_tge(type(exc).__name__)}: {_tge(str(exc)[:500])}</pre>\n\n"
                f"Check logs/eod_settlement.log"
            )
        except (ImportError, OSError, ConnectionError, RuntimeError) as e:  # Telegram in crash guard
            log.warning("Crash-alert Telegram notification failed: %s", e)
        raise
