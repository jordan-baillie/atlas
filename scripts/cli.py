#!/usr/bin/env python3
"""Atlas CLI - Main entry point for all operations."""

import sys
import os
import json
import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Windows terminals may default to cp1252 and choke on Unicode plan formatting.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import pandas as pd
import numpy as np

from markets import get_market, list_markets as list_registered_markets
from utils.config import get_active_config, save_config_version, list_versions
from utils.helpers import format_aud, format_pct, format_currency
from data.ingest import download_ticker, download_universe, get_market_tickers, cache_stats
from universe.builder import build_universe, load_universe, get_universe_tickers

# Default market (can be overridden by --market flag)
DEFAULT_MARKET = "asx"
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
        logging.FileHandler(LOGS_DIR / "atlas.log", mode="a"),
    ],
)
logger = logging.getLogger("atlas")


def load_data(tickers, config):
    """Load OHLCV data for tickers from cache (per-market or legacy)."""
    market_id = config.get("market", DEFAULT_MARKET)
    base_cache = PROJECT_ROOT / config["data"]["cache_dir"]
    market_cache = base_cache / market_id
    data = {}
    for ticker in tickers:
        fname = ticker.replace(".", "_") + ".parquet"
        # Try per-market cache first, then legacy flat cache
        path = market_cache / fname
        if not path.exists():
            path = base_cache / fname
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


def get_tickers(market_id: str = None):
    """Get universe tickers or fallback to market profile."""
    market_id = market_id or DEFAULT_MARKET
    try:
        return get_universe_tickers(market_id)
    except Exception:
        return get_market_tickers(market_id)[:20]


# ===================================================================
# COMMANDS
# ===================================================================

def cmd_ingest(args):
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    tickers = get_tickers(market_id)
    years = config["data"]["history_years"]
    end = datetime.now()
    start = end - timedelta(days=years * 365)
    logger.info("Downloading %d tickers for %s from %s to %s", len(tickers), market_id, start.date(), end.date())
    results = download_universe(tickers, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), market_id=market_id)
    stats = cache_stats(market_id)
    print("\nIngestion complete [%s]" % market_id)
    print("  Tickers downloaded: %d" % len(results))
    file_count = stats.get("file_count", stats.get("total_files", 0))
    print("  Cache: %d files, %.1f MB" % (file_count, stats.get("total_size_mb", 0)))


def cmd_universe(args):
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
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
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    tickers = get_tickers(market_id)
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
    engine = BacktestEngine(config, market_id=market_id)
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
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    tickers = get_tickers(market_id)
    data = load_data(tickers, config)
    if not data:
        print("ERROR: No data available. Run 'ingest' first.")
        return
    prices = get_latest_prices(data)
    portfolio = PaperPortfolio(config, market_id=market_id)
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
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    portfolio = PaperPortfolio(config, market_id=market_id)
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
    tickers = get_tickers(market_id)
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
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")
    portfolio = PaperPortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.approve_plan(trade_date)
    if plan:
        print("Plan for %s APPROVED" % trade_date)
    else:
        print("No plan found for %s" % trade_date)


def cmd_status(args):
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    tickers = get_tickers(market_id)
    data = load_data(tickers, config) if tickers else {}
    prices = get_latest_prices(data) if data else {}
    portfolio = PaperPortfolio(config, market_id=market_id)
    summary = portfolio.portfolio_summary(prices)
    market = get_market(market_id)
    print("\n" + "=" * 50)
    print("  ATLAS STATUS [%s]" % market.display_name)
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
    stats = cache_stats(market_id)
    print("\nDATA:")
    file_count = stats.get("file_count", stats.get("total_files", 0))
    print("   Cache: %d files, %.1f MB" % (file_count, stats.get("total_size_mb", 0)))
    try:
        uni = get_universe_tickers(market_id)
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


def cmd_markets(args):
    """List all available markets."""
    print("\n" + "=" * 50)
    print("  AVAILABLE MARKETS")
    print("=" * 50)
    for mid in list_registered_markets():
        m = get_market(mid)
        print(f"\n  {m.market_id:10s}  {m.display_name}")
        print(f"             Country:    {m.country}")
        print(f"             Currency:   {m.currency}")
        print(f"             Benchmark:  {m.benchmark_ticker}")
        print(f"             Tickers:    {len(m.get_universe_tickers())}")
        print(f"             Suffix:     '{m.yfinance_suffix}' (yfinance)")
    print()


# ===================================================================
# BROKER-AWARE COMMANDS
# ===================================================================

def _get_broker(market_id: str = None):
    """Get configured broker instance."""
    market_id = market_id or DEFAULT_MARKET
    config = get_active_config(market_id)
    from brokers.registry import get_broker
    broker = get_broker(market_id, config)
    broker.connect()
    return broker


def cmd_broker_status(args):
    """Show broker connection and account status."""
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    broker_name = config.get("trading", {}).get("broker", "paper")
    mode = config.get("trading", {}).get("mode", "paper")

    print("\n" + "=" * 55)
    print("  BROKER STATUS")
    print("=" * 55)
    print("\n  Config:")
    print("    Broker:     %s" % broker_name)
    print("    Mode:       %s" % mode)

    if broker_name == "moomoo":
        moomoo_cfg = config.get("moomoo", {})
        print("    OpenD:      %s:%s" % (moomoo_cfg.get("opend_host"), moomoo_cfg.get("opend_port")))
        print("    Firm:       %s" % moomoo_cfg.get("security_firm"))
        print("    TrdEnv:     %s" % moomoo_cfg.get("trd_env"))

    try:
        broker = _get_broker(market_id)
        print("\n  Connection:   ✅ %s" % broker)

        info = broker.get_account_info()
        print("\n  Account:")
        print("    Equity:     %s" % format_aud(info.equity))
        print("    Cash:       %s" % format_aud(info.cash))
        print("    Mkt Value:  %s" % format_aud(info.market_value))
        print("    Buy Power:  %s" % format_aud(info.buying_power))
        print("    PnL:        %s (%.1f%%)" % (format_aud(info.total_pnl), info.total_pnl_pct))
        print("    Positions:  %d" % info.num_positions)
        print("    Currency:   %s" % info.currency)
        if info.halted:
            print("    ⚠️  HALTED:  %s" % info.halt_reason)

        positions = broker.get_positions()
        if positions:
            print("\n  Positions:")
            for p in positions:
                pnl_str = "%+.2f" % p.unrealized_pnl
                print("    %s  %d @ $%.2f  now $%.2f  PnL %s" % (
                    p.ticker.ljust(8), p.shares, p.entry_price,
                    p.current_price, pnl_str))

        broker.disconnect()
    except Exception as e:
        print("\n  Connection:   ❌ Failed: %s" % e)


def cmd_live_run(args):
    """Execute approved plan via live broker with safety gates."""
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    mode = config.get("trading", {}).get("mode", "paper")
    broker_name = config.get("trading", {}).get("broker", "paper")

    if broker_name == "paper":
        print("ERROR: trading.broker is 'paper'. Set to 'moomoo' for live execution.")
        return
    if mode not in ("live", "paper"):
        print("ERROR: trading.mode must be 'live' or 'paper'")
        return

    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")

    # Load plan
    portfolio = PaperPortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)
    if not plan:
        print("ERROR: No plan found for %s. Run 'plan' first." % trade_date)
        return
    if plan["status"] != "APPROVED":
        print("Plan status is '%s'. Need APPROVED to execute." % plan["status"])
        return

    safety = config.get("trading", {}).get("live_safety", {})

    # Safety gate 1: dry run preview
    print("\n" + "=" * 55)
    print("  LIVE EXECUTION PREVIEW — %s" % trade_date)
    print("  Broker: %s | Mode: %s" % (broker_name, mode))
    print("=" * 55)

    entries = plan.get("proposed_entries", [])
    exits = plan.get("proposed_exits", [])
    print("\n  Exits:   %d" % len(exits))
    for ex in exits:
        print("    SELL %s — %s" % (ex.get("ticker"), ex.get("reason", "planned")))
    print("  Entries: %d" % len(entries))
    for e in entries:
        value = e["entry_price"] * e["position_size"]
        print("    BUY  %s  %d @ $%.2f = $%.2f  [%s]" % (
            e["ticker"], e["position_size"], e["entry_price"],
            value, e["strategy"]))

    # Safety gate 2: max daily orders
    max_orders = safety.get("max_daily_orders", 10)
    total_orders = len(entries) + len(exits)
    if total_orders > max_orders:
        print("\n⚠️  %d orders exceeds max_daily_orders (%d). Aborting." % (total_orders, max_orders))
        return

    # Safety gate 3: require double approval for live
    if mode == "live" and safety.get("require_double_approval", True):
        print("\n⚠️  LIVE MODE — This will place REAL orders with REAL money.")
        confirm = input("  Type 'EXECUTE' to confirm: ").strip()
        if confirm != "EXECUTE":
            print("  Aborted.")
            return

    # Connect broker and execute
    from brokers.registry import get_broker
    broker = get_broker(market_id, config)
    if not broker.connect():
        print("ERROR: Failed to connect to broker")
        return

    try:
        print("\nExecuting via %s..." % broker.name)
        ledger = TradeLedger()
        mistake_log = MistakeLog()

        # Get real prices if broker supports it
        all_tickers = [e.get("ticker") for e in entries] + [ex.get("ticker") for ex in exits]
        all_tickers = [t for t in all_tickers if t]
        broker_prices = broker.get_prices(all_tickers)

        # Execute exits first
        for exit_rec in exits:
            ticker = exit_rec.get("ticker")
            if not ticker:
                continue
            price = broker_prices.get(ticker, exit_rec.get("entry_price", 0))
            reason = exit_rec.get("reason", "planned_exit")

            if broker.is_live:
                # Live: place sell order via broker
                pos_info = next((p for p in broker.get_positions() if p.ticker == ticker), None)
                if not pos_info:
                    print("  ⚠️  No position for %s — skipping exit" % ticker)
                    continue
                result = broker.sell(ticker, pos_info.shares, price)
                print("  EXIT %s: %s (order_id=%s)" % (ticker, result.status.value, result.order_id))
            else:
                # Paper broker: direct execution
                result = broker.sell(ticker, 0, price, remark=reason, trade_date=trade_date)
                if result.success:
                    ledger.record_exit(result.raw)
                    print("  EXIT %s: PnL %s" % (ticker, format_aud(result.raw.get("pnl", 0))))

        # Execute entries
        for entry in entries:
            ticker = entry.get("ticker")
            if not ticker:
                continue
            price = broker_prices.get(ticker, entry["entry_price"])

            if broker.is_live:
                # Live: place buy order via broker
                result = broker.buy(
                    ticker, entry["position_size"], price,
                    remark="atlas_%s_%s" % (entry["strategy"], trade_date),
                )
                print("  ENTRY %s: %s %d @ $%.2f (order_id=%s)" % (
                    ticker, result.status.value, entry["position_size"],
                    price, result.order_id))
            else:
                # Paper broker: direct execution
                result = broker.place_order(
                    ticker, side=__import__('broker.base', fromlist=['OrderSide']).OrderSide.BUY,
                    qty=entry["position_size"], price=price,
                    strategy=entry["strategy"], stop_price=entry["stop_price"],
                    take_profit=entry.get("take_profit"), confidence=entry["confidence"],
                    rationale=entry["rationale"], sector=entry.get("sector", "Unknown"),
                    trade_date=trade_date,
                )
                if result.success:
                    ledger.record_entry(result.raw)
                    print("  ENTRY %s: %d @ %s" % (ticker, result.filled_qty,
                          format_aud(result.fill_price)))

        # Post-execution account check
        info = broker.get_account_info()
        print("\n  Post-execution:")
        print("    Equity: %s" % format_aud(info.equity))
        print("    Cash:   %s" % format_aud(info.cash))
        print("    PnL:    %s (%.1f%%)" % (format_aud(info.total_pnl), info.total_pnl_pct))

    finally:
        broker.disconnect()


def cmd_orders(args):
    """Show open orders from broker."""
    market_id = getattr(args, "market", DEFAULT_MARKET)
    broker = _get_broker(market_id)
    try:
        orders = broker.get_open_orders()
        print("\n" + "=" * 55)
        print("  OPEN ORDERS")
        print("=" * 55)
        if not orders:
            print("\n  No open orders.")
            return
        for o in orders:
            print("  %s  %s %s  %d @ $%.2f  filled=%d  status=%s" % (
                o.order_id[:12], o.side.value, o.ticker,
                o.requested_qty, o.requested_price,
                o.filled_qty, o.status.value))
    finally:
        broker.disconnect()


def cmd_setup_secrets(args):
    """Interactive secure credential setup."""
    from brokers.secrets import setup_secrets_interactive
    setup_secrets_interactive()


def cmd_halt(args):
    """Emergency: cancel all open orders."""
    market_id = getattr(args, "market", DEFAULT_MARKET)
    broker = _get_broker(market_id)
    try:
        print("\n⚠️  EMERGENCY HALT — Cancelling all open orders...")
        if broker.is_live:
            confirm = input("  Type 'HALT' to confirm: ").strip()
            if confirm != "HALT":
                print("  Aborted.")
                return
        results = broker.cancel_all_orders()
        for r in results:
            print("  %s: %s" % (r.status.value, r.message))
        print("\n  Done. Verify with 'atlas orders'.")
    finally:
        broker.disconnect()


def cmd_sync(args):
    """Reconcile Atlas paper state with live broker positions."""
    market_id = getattr(args, "market", DEFAULT_MARKET)
    config = get_active_config(market_id)
    from brokers.registry import get_broker
    broker = get_broker(market_id, config)
    if not broker.connect():
        print("ERROR: Failed to connect to broker")
        return

    try:
        # Get broker truth
        broker_positions = broker.get_positions()
        broker_info = broker.get_account_info()

        # Get Atlas paper state
        paper = PaperPortfolio(config, market_id=market_id)
        tickers = [p.ticker for p in paper.positions]
        paper_summary = paper.portfolio_summary()

        print("\n" + "=" * 55)
        print("  POSITION RECONCILIATION")
        print("=" * 55)

        print("\n  Atlas Paper State:")
        print("    Equity:    %s" % format_aud(paper_summary["equity"]))
        print("    Cash:      %s" % format_aud(paper_summary["cash"]))
        print("    Positions: %d" % paper_summary["num_open"])

        print("\n  Broker State:")
        print("    Equity:    %s" % format_aud(broker_info.equity))
        print("    Cash:      %s" % format_aud(broker_info.cash))
        print("    Positions: %d" % broker_info.num_positions)

        # Compare positions
        atlas_tickers = {p.ticker for p in paper.positions}
        broker_tickers = {p.ticker for p in broker_positions}

        only_atlas = atlas_tickers - broker_tickers
        only_broker = broker_tickers - atlas_tickers
        common = atlas_tickers & broker_tickers

        if only_atlas:
            print("\n  ⚠️  In Atlas but NOT in broker:")
            for t in sorted(only_atlas):
                print("    %s" % t)
        if only_broker:
            print("\n  ⚠️  In broker but NOT in Atlas:")
            for t in sorted(only_broker):
                print("    %s" % t)
        if common:
            print("\n  ✅ Matching positions:")
            for t in sorted(common):
                ap = next(p for p in paper.positions if p.ticker == t)
                bp = next(p for p in broker_positions if p.ticker == t)
                match = "✅" if ap.shares == bp.shares else "⚠️  QTY MISMATCH"
                print("    %s  atlas=%d  broker=%d  %s" % (t, ap.shares, bp.shares, match))

        if not only_atlas and not only_broker:
            print("\n  ✅ All positions reconciled.")

    finally:
        broker.disconnect()


def main():
    parser = argparse.ArgumentParser(prog="atlas", description="Atlas Multi-Market Swing Trading Lab")
    # Global --market flag
    parser.add_argument(
        "-m", "--market", type=str, default=DEFAULT_MARKET,
        help="Market to operate on (default: %(default)s). Available: " +
             ", ".join(list_registered_markets()),
    )
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
    subparsers.add_parser("markets", help="List available markets")
    # Broker-aware commands
    subparsers.add_parser("broker", help="Show broker connection & account status")
    p = subparsers.add_parser("live-run", help="Execute approved plan via live broker")
    p.add_argument("--date", type=str, default=None)
    subparsers.add_parser("orders", help="Show open orders from broker")
    subparsers.add_parser("halt", help="Emergency: cancel all open orders")
    subparsers.add_parser("sync", help="Reconcile Atlas state with broker positions")
    subparsers.add_parser("setup-secrets", help="Securely configure broker credentials")
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    commands = {
        "ingest": cmd_ingest, "universe": cmd_universe, "backtest": cmd_backtest,
        "plan": cmd_plan, "approve": cmd_approve, "paper-run": cmd_paper_run,
        "status": cmd_status, "ledger": cmd_ledger, "review": cmd_review,
        "markets": cmd_markets,
        "broker": cmd_broker_status, "live-run": cmd_live_run,
        "orders": cmd_orders, "halt": cmd_halt, "sync": cmd_sync,
        "setup-secrets": cmd_setup_secrets,
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
