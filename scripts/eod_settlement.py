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
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

# Logging
log_dir = PROJECT / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "eod_settlement.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("eod")


def load_config(market_id="asx"):
    config_path = PROJECT / "config" / "active" / f"{market_id}.json"
    with open(config_path) as f:
        return json.load(f)


def fetch_closing_prices(tickers):
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
    download_universe(tickers, use_cache=False)  # Force fresh download

    for ticker in tickers:
        cache_key = ticker.replace(".", "_")
        cache_path = PROJECT / "data" / "cache" / f"{cache_key}.parquet"
        if cache_path.exists():
            try:
                df = pd.read_parquet(cache_path)
                if not df.empty and "close" in df.columns:
                    last = df.iloc[-1]
                    prices[ticker] = float(last["close"])
                    lows[ticker]   = float(last["low"])  if "low"  in df.columns else prices[ticker]
                    highs[ticker]  = float(last["high"]) if "high" in df.columns else prices[ticker]
            except Exception as e:
                log.warning(f"Failed to read price for {ticker}: {e}")

    log.info(f"Got OHLC prices for {len(prices)}/{len(tickers)} tickers")
    return prices, lows, highs



def check_stop_losses(portfolio, prices, lows, trade_date, dry_run):
    """Check and execute stop-loss exits using intraday lows.

    Uses the day's intraday low (not just the close) to detect stop breaches,
    consistent with the backtest engine. Exit is recorded at the stop price
    (simulating a stop-market order), not at the intraday low.
    """
    exits = []
    for pos in list(portfolio.positions):
        if pos.ticker not in prices:
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

    lines.append(f"Settlement completed at {datetime.now(BRISBANE).strftime('%I:%M %p AEST')}")
    lines.append("=" * 55)

    return "\n".join(lines)


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
    args = parser.parse_args()

    now = datetime.now(BRISBANE)
    trade_date = now.strftime("%Y-%m-%d")

    log.info("=" * 60)
    log.info(f"END-OF-DAY SETTLEMENT -- {trade_date}")
    log.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    log.info("=" * 60)

    # Check if trading day
    if now.weekday() >= 5:
        log.info(f"Today is {now.strftime('%A')} - not a trading day, skipping EOD.")
        print(f"Not a trading day ({now.strftime('%A')}). No settlement needed.")
        return

    # Load config and portfolio
    config = load_config()
    from paper_engine.engine import PaperPortfolio
    portfolio = PaperPortfolio(config)

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
    prices, lows, highs = fetch_closing_prices(held_tickers)

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

    # Summary stats for automation
    summary = {
        "trade_date": trade_date,
        "equity": portfolio.equity(prices),
        "positions": len(portfolio.positions),
        "stop_exits": len(stop_exits),
        "tp_exits": len(tp_exits),
        "halted": portfolio.halted,
        "report_path": str(report_path)
    }
    summary_path = PROJECT / "logs" / f"eod_summary_{trade_date}.json"
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    log.info(f"EOD summary saved: {summary_path}")


if __name__ == "__main__":
    main()
