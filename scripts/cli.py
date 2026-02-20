#!/usr/bin/env python3
"""Atlas-ASX CLI - Main entry point for all operations."""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np

from utils.config import get_active_config, save_config_version, list_versions
from utils.helpers import format_aud, format_pct
from data.ingest import download_ticker, download_universe, get_asx200_tickers, cache_stats
from universe.builder import build_universe, load_universe, get_universe_tickers
from strategies.momentum_breakout import MomentumBreakout
from strategies.mean_reversion import MeanReversion
from strategies.sector_rotation import SectorRotation
from strategies.trend_following import TrendFollowing
from strategies.short_term_mr import ShortTermMR
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.mtf_momentum import MTFMomentum
from strategies.dividend_capture import DividendCapture
from backtest.engine import BacktestEngine
from paper_engine.engine import PaperPortfolio, TradePlanGenerator
from journal.logger import DecisionJournal, TradeLedger, MistakeLog, WeeklySummary
from utils.signal_enrichment import enrich_signals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / "logs" / "atlas.log", mode="a"),
    ],
)
logger = logging.getLogger("atlas")


def load_data(tickers, config):
    """Load OHLCV data for tickers from cache."""
    cache_dir = PROJECT_ROOT / config["data"]["cache_dir"]
    data = {}
    for ticker in tickers:
        fname = ticker.replace(".", "_") + ".parquet"
        path = cache_dir / fname
        if path.exists():
            data[ticker] = pd.read_parquet(path)
        else:
            logger.warning("No cached data for %s", ticker)
    return data


def get_latest_prices(data):
    """Get latest close price for each ticker."""
    return {t: float(df["close"].iloc[-1]) for t, df in data.items() if len(df) > 0}


def get_strategies(config):
    """Instantiate enabled strategies."""
    strats = []
    sc = config["strategies"]
    if sc["momentum_breakout"]["enabled"]:
        strats.append(MomentumBreakout(config))
    if sc["mean_reversion"]["enabled"]:
        strats.append(MeanReversion(config))
    if sc["trend_following"]["enabled"]:
        strats.append(TrendFollowing(config))
    if sc.get("sector_rotation", {}).get("enabled", False):
        strats.append(SectorRotation(config))
    if sc.get("short_term_mr", {}).get("enabled", False):
        strats.append(ShortTermMR(config))
    if sc.get("bb_squeeze", {}).get("enabled", False):
        strats.append(BBSqueeze(config))
    if sc.get("opening_gap", {}).get("enabled", False):
        strats.append(OpeningGap(config))
    if sc.get("mtf_momentum", {}).get("enabled", False):
        strats.append(MTFMomentum(config))
    if config["strategies"].get("dividend_capture", {}).get("enabled"):
        strats.append(DividendCapture(config))
    return strats


def get_tickers():
    """Get universe tickers or fallback."""
    try:
        return get_universe_tickers()
    except Exception:
        return get_asx200_tickers()[:20]


# ===================================================================
# COMMANDS
# ===================================================================

def cmd_ingest(args):
    config = get_active_config()
    tickers = get_tickers()
    years = config["data"]["history_years"]
    end = datetime.now()
    start = end - timedelta(days=years * 365)
    logger.info("Downloading %d tickers from %s to %s", len(tickers), start.date(), end.date())
    results = download_universe(tickers, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    stats = cache_stats()
    print("\nIngestion complete")
    print("  Tickers downloaded: %d" % len(results))
    print("  Cache: %d files, %.1f MB" % (stats.get("total_files", 0), stats.get("total_size_mb", 0)))


def cmd_universe(args):
    config = get_active_config()
    print("Building universe with filters:")
    print("  Method: %s" % config["universe"]["method"])
    print("  Top N: %d" % config["universe"]["top_n"])
    print("  Min daily value: %s" % format_aud(config["universe"]["min_median_daily_value"]))
    print("  Min price: %s" % format_aud(config["universe"]["min_price"]))
    print("  Min market cap: %s" % format_aud(config["universe"]["min_market_cap"]))
    print()
    universe = build_universe(config)
    print("\nUniverse built: %d tickers" % len(universe))
    for i, t in enumerate(universe):
        print("  %3d. %s" % (i + 1, t))


def cmd_backtest(args):
    config = get_active_config()
    tickers = get_tickers()
    data = load_data(tickers, config)
    if not data:
        print("ERROR: No data available. Run 'ingest' first.")
        return
    print("Running walk-forward backtest on %d tickers..." % len(data))
    print("  Train window: %d days" % config["backtest"]["train_window_days"])
    print("  Test window: %d days" % config["backtest"]["test_window_days"])
    print("  Step: %d days" % config["backtest"]["step_days"])
    print()
    strategies = get_strategies(config)
    engine = BacktestEngine(config)
    result = engine.run_walkforward(data, strategies)
    metrics = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
    trades = result.trades if hasattr(result, "trades") else result.get("trades", [])
    bench = result.benchmark_metrics if hasattr(result, "benchmark_metrics") else result.get("benchmark_metrics", {})
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print("\nStrategy Performance:")
    print("   CAGR:           %+.2f%%" % (metrics.get("cagr", 0) * 100))
    print("   Max Drawdown:   %.2f%%" % (metrics.get("max_drawdown", 0) * 100))
    print("   Sharpe Ratio:   %.3f" % metrics.get("sharpe", 0))
    print("   Sortino Ratio:  %.3f" % metrics.get("sortino", 0))
    print("   Win Rate:       %.1f%%" % (metrics.get("win_rate", 0) * 100))
    print("   Profit Factor:  %.2f" % metrics.get("profit_factor", 0))
    print("   Total Trades:   %d" % metrics.get("total_trades", len(trades)))
    print("   Avg Trade:      %s" % format_aud(metrics.get("avg_trade", 0)))
    if bench:
        print("\nBenchmark (Buy & Hold):")
        print("   CAGR:           %+.2f%%" % (bench.get("cagr", 0) * 100))
        print("   Max Drawdown:   %.2f%%" % (bench.get("max_drawdown", 0) * 100))
        print("   Sharpe Ratio:   %.3f" % bench.get("sharpe", 0))
    results_path = PROJECT_ROOT / "backtest" / "results"
    results_path.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_data = {
        "timestamp": ts, "config_version": config["version"],
        "tickers_count": len(data), "metrics": metrics,
        "benchmark_metrics": bench, "total_trades": len(trades),
    }
    with open(results_path / ("backtest_%s.json" % ts), "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    print("\nResults saved to backtest/results/backtest_%s.json" % ts)


def cmd_plan(args):
    config = get_active_config()
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    tickers = get_tickers()
    data = load_data(tickers, config)
    if not data:
        print("ERROR: No data available. Run 'ingest' first.")
        return
    prices = get_latest_prices(data)
    portfolio = PaperPortfolio(config)
    plan_gen = TradePlanGenerator(portfolio, config)
    decision_journal = DecisionJournal()
    halted, dd = portfolio.check_daily_drawdown(prices)
    if halted:
        print("TRADING HALTED: Daily drawdown %.2f%% >= %.2f%%" % (dd * 100, config["risk"]["max_daily_drawdown_pct"] * 100))
        return
    strategies = get_strategies(config)
    all_signals = []
    exit_recommendations = []
    existing_positions = [p.to_dict() for p in portfolio.positions]
    for strat in strategies:
        try:
            signals = strat.generate_signals(data, portfolio.equity(prices), existing_positions)
            for sig in signals:
                all_signals.append(sig)
                decision_journal.record_signal(sig, "proposed", config_version=config["version"])
            exits = strat.check_exits(data, existing_positions)
            exit_recommendations.extend(exits)
        except Exception as e:
            logger.error("Strategy %s error: %s", strat.name, e)
    # Phase 7: Enrich signals with breadth, RS, and earnings blackout
    logger.info("Enriching %d raw signals with Phase 7 features...", len(all_signals))
    all_signals = enrich_signals(all_signals, data, config, trade_date)
    logger.info("%d signals after enrichment", len(all_signals))
    all_signals.sort(key=lambda s: s.confidence, reverse=True)
    plan = plan_gen.generate_plan(all_signals, exit_recommendations, prices, trade_date)
    print(plan_gen.format_plan_text(plan))
    print("\nPlan saved to paper_engine/plans/plan_%s.json" % trade_date)


def cmd_paper_run(args):
    config = get_active_config()
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    portfolio = PaperPortfolio(config)
    plan_gen = TradePlanGenerator(portfolio, config)
    ledger = TradeLedger()
    mistake_log = MistakeLog()
    plan = plan_gen.load_plan(trade_date)
    if not plan:
        print("ERROR: No plan found for %s. Run 'plan' first." % trade_date)
        return
    if plan["status"] != "APPROVED":
        print("Plan status is '%s'. Need APPROVED to execute." % plan["status"])
        return
    tickers = get_tickers()
    data = load_data(tickers, config)
    prices = get_latest_prices(data)
    print("\nExecuting approved plan for %s..." % trade_date)
    for exit_rec in plan.get("proposed_exits", []):
        ticker = exit_rec.get("ticker")
        if ticker and ticker in prices:
            result = portfolio.execute_exit(ticker, prices[ticker], trade_date, exit_rec.get("reason", "planned_exit"))
            if result:
                ledger.record_exit(result)
                print("  EXIT %s: PnL %s (%.1f%%)" % (ticker, format_aud(result["pnl"]), result["pnl_pct"]))
                if result["pnl"] < 0:
                    categories = mistake_log.auto_categorize(result)
                    for cat in categories:
                        mistake_log.record_mistake(result, cat["category"], cat["description"], cat["impact"])
    for entry in plan.get("proposed_entries", []):
        ticker = entry.get("ticker")
        if ticker and ticker in prices:
            class SignalProxy:
                pass
            sig = SignalProxy()
            sig.ticker = ticker
            sig.strategy = entry["strategy"]
            sig.entry_price = entry["entry_price"]
            sig.stop_price = entry["stop_price"]
            sig.take_profit = entry.get("take_profit")
            sig.position_size = entry["position_size"]
            sig.confidence = entry["confidence"]
            sig.rationale = entry["rationale"]
            sig.sector = entry.get("sector", "Unknown")
            result = portfolio.execute_entry(sig, prices[ticker], trade_date)
            ledger.record_entry(result)
            print("  ENTRY %s: %d@%s cost=%s" % (ticker, result["shares"], format_aud(result["fill_price"]), format_aud(result["total_cost"])))
    portfolio.update_positions(prices)
    portfolio.record_equity(trade_date, prices)
    summary = portfolio.portfolio_summary(prices)
    print("\nPortfolio after execution:")
    print("   Equity: %s" % format_aud(summary["equity"]))
    print("   Cash: %s" % format_aud(summary["cash"]))
    print("   PnL: %s (%.1f%%)" % (format_aud(summary["total_pnl"]), summary["total_pnl_pct"]))
    print("   Open positions: %d" % summary["num_open"])


def cmd_approve(args):
    config = get_active_config()
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    portfolio = PaperPortfolio(config)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.approve_plan(trade_date)
    if plan:
        print("Plan for %s APPROVED" % trade_date)
    else:
        print("No plan found for %s" % trade_date)


def cmd_status(args):
    config = get_active_config()
    tickers = get_tickers()
    data = load_data(tickers, config) if tickers else {}
    prices = get_latest_prices(data) if data else {}
    portfolio = PaperPortfolio(config)
    summary = portfolio.portfolio_summary(prices)
    print("\n" + "=" * 50)
    print("  ATLAS-ASX STATUS")
    print("=" * 50)
    print("\nConfig: %s" % config["version"])
    print("Date: %s" % datetime.now().strftime("%Y-%m-%d %H:%M"))
    print("\nPORTFOLIO:")
    print("   Equity:    %s" % format_aud(summary["equity"]))
    print("   Cash:      %s" % format_aud(summary["cash"]))
    print("   Total PnL: %s (%.1f%%)" % (format_aud(summary["total_pnl"]), summary["total_pnl_pct"]))
    print("   Positions: %d open, %d closed" % (summary["num_open"], summary["num_closed_trades"]))
    print("   Halted:    %s" % ("YES" if summary["halted"] else "No"))
    if summary["open_positions"]:
        print("\nOPEN POSITIONS:")
        for p in summary["open_positions"]:
            print("   %s  entry=$%.2f  now=$%.2f  PnL=%s  stop=$%.2f" % (
                p["ticker"].ljust(8), p["entry_price"], p["current_price"],
                format_aud(p["unrealized_pnl"]), p["stop_price"]))
    stats = cache_stats()
    print("\nDATA:")
    print("   Cache: %d files, %.1f MB" % (stats.get("total_files", 0), stats.get("total_size_mb", 0)))
    try:
        uni = get_universe_tickers()
        print("   Universe: %d tickers" % len(uni))
    except Exception:
        print("   Universe: Not built yet")


def cmd_ledger(args):
    ledger = TradeLedger()
    days = args.days or 30
    perf = ledger.performance_summary(days=days)
    print("\nTRADE LEDGER (last %d days)" % days)
    print("=" * 50)
    if perf.get("total_trades", 0) == 0:
        print("No closed trades in this period.")
        return
    print("   Total trades:   %d" % perf["total_trades"])
    print("   Win rate:       %s%%" % perf["win_rate"])
    print("   Total PnL:      %s" % format_aud(perf["total_pnl"]))
    print("   Profit Factor:  %s" % perf["profit_factor"])


def cmd_review(args):
    from scripts.anneal import run_annealing_cycle
    run_annealing_cycle()


def main():
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas-ASX Daily Swing Trading Lab")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    subparsers.add_parser("ingest", help="Download/update market data")
    subparsers.add_parser("universe", help="Build trading universe")
    subparsers.add_parser("backtest", help="Run walk-forward backtest")
    p = subparsers.add_parser("plan", help="Generate daily trade plan")
    p.add_argument("--date", type=str, default=None)
    p = subparsers.add_parser("approve", help="Approve a pending trade plan")
    p.add_argument("--date", type=str, default=None)
    p = subparsers.add_parser("paper-run", help="Execute approved trade plan")
    p.add_argument("--date", type=str, default=None)
    subparsers.add_parser("status", help="Show portfolio status")
    p = subparsers.add_parser("ledger", help="Show trade ledger")
    p.add_argument("--days", type=int, default=30)
    subparsers.add_parser("review", help="Run self-annealing review")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    (PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    commands = {
        "ingest": cmd_ingest, "universe": cmd_universe, "backtest": cmd_backtest,
        "plan": cmd_plan, "approve": cmd_approve, "paper-run": cmd_paper_run,
        "status": cmd_status, "ledger": cmd_ledger, "review": cmd_review,
    }
    cmd_func = commands.get(args.command)
    if cmd_func:
        try:
            cmd_func(args)
        except Exception as e:
            logger.error("Command '%s' failed: %s", args.command, e, exc_info=True)
            print("\nError: %s" % e)
            sys.exit(1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
