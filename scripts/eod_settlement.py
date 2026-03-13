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
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

BRISBANE = ZoneInfo("Australia/Brisbane")

# Setup
PROJECT = Path(__file__).resolve().parent.parent
SNAPSHOT_LOG = PROJECT / "logs" / "portfolio_snapshots.jsonl"
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

from utils.logging_config import setup_logging
log = setup_logging("eod_settlement", extra_log_file="eod_settlement")


def load_config(market_id="asx"):
    config_path = PROJECT / "config" / "active" / f"{market_id}.json"
    with open(config_path) as f:
        return json.load(f)


def fetch_closing_prices(tickers, market_id=None):
    """Fetch latest OHLC prices for held tickers.

    Returns three dicts:
      prices  - closing prices  (used for equity, MAE/MFE, report)
      lows    - intraday lows   (used for stop-loss checks)
      highs   - intraday highs  (used for take-profit checks)

    Using intraday lows/highs ensures a stop or TP that was breached
    during the session is caught even if the close recovered above/below
    the trigger level — matching the backtest engine behaviour.
    """
    import pandas as pd
    from data.ingest import download_universe

    log.info(f"Fetching OHLC prices for {len(tickers)} tickers")
    prices = {}  # close prices
    lows   = {}  # intraday lows  (stop-loss)
    highs  = {}  # intraday highs (take-profit)

    # Download fresh data and use returned DataFrames directly.
    # use_cache=True ensures the cache is also updated for other consumers.
    downloaded = download_universe(tickers, use_cache=True, market_id=market_id)

    for ticker in tickers:
        df = downloaded.get(ticker)
        if df is not None and not df.empty and "close" in df.columns:
            data_age = (pd.Timestamp.now() - df.index[-1]).days
            if data_age > 2:
                log.warning(f"STALE DATA: {ticker} latest data is {data_age} days old ({df.index[-1].date()})")
            last = df.iloc[-1]
            prices[ticker] = float(last["close"])
            lows[ticker]   = float(last["low"])  if "low"  in df.columns else prices[ticker]
            highs[ticker]  = float(last["high"]) if "high" in df.columns else prices[ticker]
        else:
            log.warning(f"No data returned for {ticker}")

    log.info(f"Got OHLC prices for {len(prices)}/{len(tickers)} tickers")
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
                result = portfolio.execute_exit(pos.ticker, exit_price, trade_date, "stop_loss")
                if result:
                    exits.append(result)
            else:
                exits.append({"ticker": pos.ticker, "type": "stop_loss",
                              "intraday_low": intraday_low, "stop_price": pos.stop_price,
                              "exit_price": exit_price, "dry_run": True})
    return exits


def check_take_profits(portfolio, prices, highs, trade_date, dry_run):
    """Check and execute take-profit exits using intraday highs.

    Uses the day's intraday high (not just the close) to detect TP hits,
    consistent with the backtest engine. Exit is recorded at the take-profit
    price (simulating a limit order fill at the TP level).
    """
    exits = []
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
                result = portfolio.execute_exit(pos.ticker, exit_price, trade_date, "take_profit")
                if result:
                    exits.append(result)
            else:
                exits.append({"ticker": pos.ticker, "type": "take_profit",
                              "intraday_high": intraday_high, "take_profit": pos.take_profit,
                              "exit_price": exit_price, "dry_run": True})
    return exits


def generate_eod_report(portfolio, prices, trade_date, stop_exits, tp_exits):
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
    lines.append(f"  ATLAS-ASX END-OF-DAY REPORT -- {trade_date}")
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
        total_realized = sum(t["pnl"] for t in today_closed)
        for t in today_closed:
            lines.append(f"   {t['ticker']} {t['strategy']}: ${t['pnl']:+.2f} ({t['pnl_pct']:+.1f}%) [{t['exit_reason']}]")
        lines.append(f"   Total realized: ${total_realized:+.2f}")
        lines.append("")

    try:
        from markets import get_market
        _settle_tz = get_market(market_id).operator_tz() if 'market_id' in dir() else BRISBANE
    except Exception:
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
    except Exception as exc:
        log.warning("Portfolio snapshot failed (non-fatal): %s", exc)


def update_dashboard():
    """Regenerate dashboard data."""
    log.info("Updating dashboard...")
    result = subprocess.run(
        [sys.executable, "dashboard/generate_data.py"],
        capture_output=True, text=True, cwd=str(PROJECT), timeout=120
    )
    if result.returncode == 0:
        log.info("Dashboard updated successfully")
    else:
        log.warning(f"Dashboard update issue: {result.stderr[-300:] if result.stderr else 'unknown'}")
    return result.returncode == 0


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
    config = load_config(market_id=market_id) if 'market_id' in load_config.__code__.co_varnames else load_config()
    from brokers.live_portfolio import LivePortfolio
    portfolio = LivePortfolio(config, market_id=market_id)
    if not portfolio.connect():
        log.error("Could not connect to broker. Aborting EOD settlement.")
        print("ERROR: Broker connection failed. Settlement aborted.")
        return

    # Guard: LivePortfolio detects zeroed broker data (OpenD up but Futu
    # backend unreachable) and sets broker_data_valid = False.
    # Abort settlement to prevent recording corrupted equity points.
    if not portfolio.broker_data_valid:
        log.error(
            "Broker returned zeroed data (likely offline). "
            "Aborting settlement to prevent state corruption."
        )
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
        print("ERROR: Could not fetch closing prices. Settlement aborted.")
        return

    missing = [t for t in held_tickers if t not in prices]
    if missing:
        log.warning(f"Missing prices for: {missing}")

    # Update position excursions (MAE/MFE)
    log.info("Updating position excursions (MAE/MFE)...")
    portfolio.update_positions(prices)

    # Reconcile protective orders on broker (SL/TP)
    # Ensures all positions have broker-side stops even after restarts/missed syncs
    # Protective orders are broker-specific
    broker_type = type(portfolio._broker).__name__ if portfolio._broker else ""
    if False:  # IBKR removed — protective orders handled per-broker
        log.info("Reconciling protective orders on broker...")
        try:
            plan_path = PROJECT / "plans" / f"plan_{market_id}_{trade_date}.json"
            plan_entries = []
            if plan_path.exists():
                with open(plan_path) as f:
                    plan_entries = json.load(f).get("proposed_entries", [])
            # Also include enriched position data as fallback entries
            for pos in portfolio.positions:
                if pos.stop_price > 0 and not any(e.get("ticker") == pos.ticker for e in plan_entries):
                    plan_entries.append({
                        "ticker": pos.ticker,
                        "stop_price": pos.stop_price,
                        "take_profit": pos.take_profit,
                    })
            if hasattr(portfolio._broker, "sync_all_protective_orders"):
                sync_result = portfolio._broker.sync_all_protective_orders(plan_entries)
                counts = sync_result if "sl_placed" in sync_result else sync_result.get("counts", {})
                log.info("Protective order sync: SL placed=%d, TP placed=%d, already_protected=%d",
                         counts.get("sl_placed", counts.get("orders_placed", 0)),
                         counts.get("tp_placed", 0),
                         counts.get("already_protected", counts.get("sl_already_exists", 0)))
        except Exception as e:
            log.warning(f"Protective order sync failed (non-fatal): {e}")

    # Check stop-losses
    log.info("Checking stop-losses...")
    stop_exits = check_stop_losses(portfolio, prices, lows, trade_date, args.dry_run)

    # Check take-profits
    log.info("Checking take-profits...")
    tp_exits = check_take_profits(portfolio, prices, highs, trade_date, args.dry_run)

    # Check daily drawdown
    halted, dd = portfolio.check_daily_drawdown(prices)
    if halted:
        log.warning(f"Daily drawdown limit breached: {dd:.2%}")

    # Record daily equity
    log.info("Recording daily equity snapshot...")
    portfolio.record_equity(trade_date, prices)
    portfolio.save_state()

    # Generate EOD report
    report = generate_eod_report(portfolio, prices, trade_date, stop_exits, tp_exits)
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
    realized_today = round(sum(t.get("pnl", 0) for t in today_closed), 2)

    summary = {
        "trade_date": trade_date,
        "market_id": market_id,
        "equity": eq,
        "broker_equity": portfolio.broker_equity,
        "cash": portfolio.cash,
        "daily_pnl": daily_pnl,
        "daily_pnl_pct": round(daily_pnl / prev_equity * 100, 2) if prev_equity > 0 else 0,
        "total_pnl": total_pnl,
        "total_pnl_pct": round(total_pnl / portfolio.starting_equity * 100, 2) if portfolio.starting_equity > 0 else 0,
        "positions": len(portfolio.positions),
        "position_details": position_snapshot,
        "stop_exits": len(stop_exits),
        "tp_exits": len(tp_exits),
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

    # Disconnect broker to free clientId for position monitor
    portfolio.disconnect()


def run_position_monitor():
    """Evaluate manual position conditions after EOD settlement."""
    try:
        from monitor.evaluator import evaluate_all
        result = evaluate_all(send_telegram=True)
        log.info(f"Position monitor: evaluated {result['evaluated']} positions, "
                 f"{result['alerts']} alerts fired")
    except Exception as e:
        log.error(f"Position monitor evaluation failed: {e}")


if __name__ == "__main__":
    main()
    # Run position monitor after EOD settlement
    # (main() broker connection is closed at end of main, so clientId is free)
    run_position_monitor()
