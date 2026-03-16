#!/usr/bin/env python3
"""Backtest trend_following short signals vs long-only baseline.

Runs four walk-forward backtests:
  1. TF Long Only (solo) — current production params, no shorts
  2. TF Long+Short (solo) — with short_enabled=true
  3. Full Portfolio Baseline — all 7 strategies, no shorts
  4. Full Portfolio + TF Shorts — all 7 strategies, TF shorts enabled
"""
import copy
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import pandas as pd
import numpy as np
from backtest.engine import BacktestEngine
from utils.config import get_active_config
from universe.builder import get_universe_tickers
from data.ingest import get_market_tickers
from strategies.mean_reversion import MeanReversion
from strategies.momentum_breakout import MomentumBreakout
from strategies.trend_following import TrendFollowing
from strategies.sector_rotation import SectorRotation
from strategies.short_term_mr import ShortTermMR
from strategies.opening_gap import OpeningGap
from strategies.connors_rsi2 import ConnorsRSI2

MARKET = "sp500"
OUTPUT_DIR = PROJECT / "research" / "results"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def load_data(tickers, config):
    market_id = config.get("market", MARKET)
    base_cache = PROJECT / config["data"]["cache_dir"]
    market_cache = base_cache / market_id
    data = {}
    for ticker in tickers:
        fname = ticker.replace(".", "_") + ".parquet"
        path = market_cache / fname
        if not path.exists():
            path = base_cache / fname
        if path.exists():
            data[ticker] = pd.read_parquet(path)
    return data


def get_tickers(market_id):
    try:
        return get_universe_tickers(market_id)
    except Exception:
        return get_market_tickers(market_id)[:20]


def get_strategies(config):
    strats = []
    sc = config["strategies"]
    if sc.get("momentum_breakout", {}).get("enabled"):
        strats.append(MomentumBreakout(config))
    if sc.get("mean_reversion", {}).get("enabled"):
        strats.append(MeanReversion(config))
    if sc.get("trend_following", {}).get("enabled"):
        strats.append(TrendFollowing(config))
    if sc.get("sector_rotation", {}).get("enabled"):
        strats.append(SectorRotation(config))
    if sc.get("short_term_mr", {}).get("enabled"):
        strats.append(ShortTermMR(config))
    if sc.get("opening_gap", {}).get("enabled"):
        strats.append(OpeningGap(config))
    if sc.get("connors_rsi2", {}).get("enabled"):
        strats.append(ConnorsRSI2(config))
    return strats


def run_backtest(label, config, data):
    print(f"\n{'='*60}")
    print(f"  Running: {label}")
    print(f"{'='*60}")
    t0 = time.time()
    strategies = get_strategies(config)
    print(f"  Strategies: {[s.name for s in strategies]}")
    print(f"  Tickers: {len(data)}")

    engine = BacktestEngine(config, market_id=MARKET)
    result = engine.run_walkforward(data, strategies)
    elapsed = time.time() - t0

    metrics = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
    trades = result.trades if hasattr(result, "trades") else result.get("trades", [])

    long_trades = [t for t in trades if t.get("direction", "long") == "long"]
    short_trades = [t for t in trades if t.get("direction", "long") == "short"]
    long_wins = sum(1 for t in long_trades if t.get("pnl", 0) > 0)
    short_wins = sum(1 for t in short_trades if t.get("pnl", 0) > 0)
    long_pnl = sum(t.get("pnl", 0) for t in long_trades)
    short_pnl = sum(t.get("pnl", 0) for t in short_trades)

    # Year-by-year short breakdown
    yearly_short = {}
    for t in short_trades:
        ed = str(t.get("entry_date", ""))[:4]
        if ed not in yearly_short:
            yearly_short[ed] = {"count": 0, "wins": 0, "pnl": 0}
        yearly_short[ed]["count"] += 1
        yearly_short[ed]["wins"] += 1 if t.get("pnl", 0) > 0 else 0
        yearly_short[ed]["pnl"] += t.get("pnl", 0)

    info = {
        "label": label,
        "elapsed_s": round(elapsed, 1),
        "sharpe": metrics.get("sharpe", 0),
        "cagr_pct": metrics.get("cagr", 0) * 100,
        "max_dd_pct": metrics.get("max_drawdown", 0) * 100,
        "profit_factor": metrics.get("profit_factor", 0),
        "total_trades": metrics.get("total_trades", len(trades)),
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "win_rate_pct": metrics.get("win_rate", 0) * 100,
        "long_win_rate": round(100 * long_wins / max(1, len(long_trades)), 1),
        "short_win_rate": round(100 * short_wins / max(1, len(short_trades)), 1),
        "long_pnl": round(long_pnl, 2),
        "short_pnl": round(short_pnl, 2),
        "avg_long_pnl": round(long_pnl / max(1, len(long_trades)), 2),
        "avg_short_pnl": round(short_pnl / max(1, len(short_trades)), 2),
        "total_return_pct": metrics.get("total_return", 0) * 100,
        "sortino": metrics.get("sortino", 0),
        "calmar": metrics.get("calmar", 0),
        "yearly_short": yearly_short,
    }

    safe_label = label.lower().replace(' ', '_').replace('+', '_')
    outfile = OUTPUT_DIR / f"tf_short_{safe_label}.json"
    result_data = {
        "timestamp": time.strftime("%Y%m%dT%H%M%S"),
        "label": label,
        "metrics": metrics,
        "trade_count": len(trades),
        "long_trades": len(long_trades),
        "short_trades": len(short_trades),
        "yearly_short": yearly_short,
        "short_trade_details": [
            {
                "ticker": t.get("ticker"),
                "entry_date": str(t.get("entry_date", "")),
                "exit_date": str(t.get("exit_date", "")),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "pnl": t.get("pnl"),
                "direction": t.get("direction"),
                "exit_reason": t.get("exit_reason", ""),
                "hold_days": t.get("holding_days", t.get("hold_days", 0)),
            }
            for t in short_trades
        ],
    }
    with open(outfile, "w") as f:
        json.dump(result_data, f, indent=2, default=str)
    print(f"  Saved: {outfile.name}")
    return info


def print_comparison(results):
    print(f"\n{'='*100}")
    print(f"  TREND FOLLOWING SHORT TRADING — BACKTEST COMPARISON")
    print(f"{'='*100}")

    header = f"{'Metric':<25}"
    for r in results:
        header += f"  {r['label']:>16}"
    print(header)
    print("-" * 100)

    rows = [
        ("Sharpe", "sharpe", ".3f"),
        ("CAGR %", "cagr_pct", ".2f"),
        ("Max Drawdown %", "max_dd_pct", ".2f"),
        ("Profit Factor", "profit_factor", ".2f"),
        ("Sortino", "sortino", ".3f"),
        ("Calmar", "calmar", ".3f"),
        ("Total Trades", "total_trades", "d"),
        ("  Long Trades", "long_trades", "d"),
        ("  Short Trades", "short_trades", "d"),
        ("Win Rate %", "win_rate_pct", ".1f"),
        ("  Long Win Rate %", "long_win_rate", ".1f"),
        ("  Short Win Rate %", "short_win_rate", ".1f"),
        ("Long P&L $", "long_pnl", ".2f"),
        ("Short P&L $", "short_pnl", ".2f"),
        ("Avg Long Trade $", "avg_long_pnl", ".2f"),
        ("Avg Short Trade $", "avg_short_pnl", ".2f"),
        ("Total Return %", "total_return_pct", ".2f"),
        ("Runtime (s)", "elapsed_s", ".1f"),
    ]

    for label, key, fmt in rows:
        row = f"{label:<25}"
        for r in results:
            val = r.get(key, 0) or 0
            row += f"  {val:>16{fmt}}"
        print(row)

    # Print yearly short breakdown for the solo L+S test
    for r in results:
        if r.get("yearly_short") and r["short_trades"] > 0:
            print(f"\n  {r['label']} — Short trades by year:")
            ys = r["yearly_short"]
            for year in sorted(ys.keys()):
                d = ys[year]
                wr = 100 * d["wins"] / max(1, d["count"])
                print(f"    {year}: {d['count']:>3} trades, WR {wr:4.0f}%, P&L ${d['pnl']:>8.2f}")

    print(f"\n{'='*100}")


def main():
    print("Loading SP500 active config v3.0...")
    base_config = get_active_config(MARKET)

    print("Loading ticker data...")
    tickers = get_tickers(MARKET)
    data = load_data(tickers, base_config)
    print(f"  Loaded {len(data)} tickers")

    results = []

    # Test 1: Solo TF long-only
    cfg1 = copy.deepcopy(base_config)
    for s in cfg1["strategies"]:
        cfg1["strategies"][s]["enabled"] = s == "trend_following"
    cfg1["strategies"]["trend_following"]["short_enabled"] = False
    r1 = run_backtest("TF Long", cfg1, data)
    results.append(r1)

    # Test 2: Solo TF long+short
    cfg2 = copy.deepcopy(base_config)
    for s in cfg2["strategies"]:
        cfg2["strategies"][s]["enabled"] = s == "trend_following"
    cfg2["strategies"]["trend_following"]["short_enabled"] = True
    r2 = run_backtest("TF Long+Short", cfg2, data)
    results.append(r2)

    # Test 3: Full portfolio baseline
    cfg3 = copy.deepcopy(base_config)
    r3 = run_backtest("Full Base", cfg3, data)
    results.append(r3)

    # Test 4: Full portfolio + TF shorts
    cfg4 = copy.deepcopy(base_config)
    cfg4["strategies"]["trend_following"]["short_enabled"] = True
    r4 = run_backtest("Full+TF Shorts", cfg4, data)
    results.append(r4)

    print_comparison(results)

    # Verdict
    print("\n" + "="*60)
    print("  VERDICT")
    print("="*60)

    base_s = r3["sharpe"]
    short_s = r4["sharpe"]
    base_dd = r3["max_dd_pct"]
    short_dd = r4["max_dd_pct"]
    base_cagr = r3["cagr_pct"]
    short_cagr = r4["cagr_pct"]
    sc = r4["short_trades"]
    swr = r4["short_win_rate"]
    spnl = r4["short_pnl"]
    asp = r4["avg_short_pnl"]

    print(f"\n  Full Portfolio Impact:")
    print(f"    Short trades: {sc}")
    print(f"    Short win rate: {swr:.1f}%")
    print(f"    Short P&L: ${spnl:.2f} (avg ${asp:.2f}/trade)")
    print(f"    Sharpe: {base_s:.3f} → {short_s:.3f} (Δ{short_s - base_s:+.3f})")
    print(f"    CAGR: {base_cagr:.2f}% → {short_cagr:.2f}% (Δ{short_cagr - base_cagr:+.2f}%)")
    print(f"    MaxDD: {base_dd:.2f}% → {short_dd:.2f}% (Δ{short_dd - base_dd:+.2f}%)")

    if sc < 15:
        print(f"\n  ⚠️  INSUFFICIENT DATA: Only {sc} short trades (need ≥15)")

    if short_s > base_s and short_dd <= base_dd * 1.15 and spnl > 0:
        print(f"\n  ✅ POSITIVE: TF shorts improve risk-adjusted returns!")
    elif spnl > 0 and short_s >= base_s * 0.95:
        print(f"\n  🟡 NEUTRAL_POSITIVE: TF shorts are modestly positive")
    elif spnl <= 0:
        print(f"\n  ❌ NEGATIVE: TF shorts lose money")
    else:
        print(f"\n  ❌ NEGATIVE: TF shorts degrade portfolio metrics")

    summary = {
        "test_date": time.strftime("%Y-%m-%d"),
        "config_version": "v3.0",
        "market": MARKET,
        "results": results,
    }
    with open(OUTPUT_DIR / "tf_short_comparison_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  Summary saved")


if __name__ == "__main__":
    main()
