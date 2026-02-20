#!/usr/bin/env python3
"""Atlas-ASX DividendCapture - Full Universe Validation

Runs validation across ALL tickers with dividend data, and compares:
  A) 4-strategy baseline (MR+TF+BBS+OG) without dividend capture
  B) 5-strategy combined (MR+TF+BBS+OG+DC) with dividend capture
"""
import sys, json, os, copy, logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path("/a0/usr/projects/atlas-asx")
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))

import pandas as pd
import numpy as np

from utils.config import get_active_config
from utils.dividends import fetch_dividend_calendar, estimate_franking_pct, calc_grossed_up_yield, get_sector_for_ticker
from strategies.dividend_capture import DividendCapture
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from backtest.engine import BacktestEngine

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("div_full")
logger.setLevel(logging.INFO)

def load_all_data():
    data = {}
    cache_dir = PROJECT_ROOT / "data" / "cache"
    for f in cache_dir.glob("*.parquet"):
        ticker = f.stem.replace("_", ".")  # BHP_AX -> BHP.AX
        df = pd.read_parquet(f)
        df.columns = [c.lower() for c in df.columns]
        if len(df) >= 252:  # need at least 1yr
            data[ticker] = df
    return data

def run_backtest(config, data, label, strategies):
    engine = BacktestEngine(config)
    result = engine.run_walkforward(data, strategies)
    m = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
    t = result.trades if hasattr(result, "trades") else result.get("trades", [])
    b = result.benchmark_metrics if hasattr(result, "benchmark_metrics") else result.get("benchmark_metrics", {})
    # Count trades by strategy
    strat_counts = {}
    for trade in t:
        s = trade.get("strategy", "unknown")
        strat_counts[s] = strat_counts.get(s, 0) + 1
    return {"label": label, "metrics": m, "trades": t, "bench": b, "strat_counts": strat_counts}

def main():
    print("\n" + "#" * 65)
    print("  ATLAS-ASX: DividendCapture Full-Universe Validation")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#" * 65)

    config = get_active_config()
    print("\n  Loading all cached price data...")
    data = load_all_data()
    print(f"  Loaded {len(data)} tickers with >= 252 bars")

    # ===== TEST A: Baseline (4 strategies, no dividend capture) =====
    print("\n" + "=" * 65)
    print("  TEST A: Baseline (MR + TF + BBS + OG)")
    print("=" * 65)
    cfg_a = copy.deepcopy(config)
    cfg_a["strategies"]["dividend_capture"]["enabled"] = False
    strats_a = [
        MeanReversion(cfg_a), TrendFollowing(cfg_a),
        BBSqueeze(cfg_a), OpeningGap(cfg_a),
    ]
    strats_a = [s for s in strats_a if cfg_a["strategies"].get(s.name, {}).get("enabled", False)]
    print(f"  Active strategies: {[s.name for s in strats_a]}")
    res_a = run_backtest(cfg_a, data, "Baseline (4-strat)", strats_a)

    # ===== TEST B: Combined (5 strategies with dividend capture) =====
    print("\n" + "=" * 65)
    print("  TEST B: Combined (MR + TF + BBS + OG + DC)")
    print("=" * 65)
    cfg_b = copy.deepcopy(config)
    cfg_b["strategies"]["dividend_capture"]["enabled"] = True
    strats_b = [
        MeanReversion(cfg_b), TrendFollowing(cfg_b),
        BBSqueeze(cfg_b), OpeningGap(cfg_b), DividendCapture(cfg_b),
    ]
    strats_b = [s for s in strats_b if cfg_b["strategies"].get(s.name, {}).get("enabled", False)]
    print(f"  Active strategies: {[s.name for s in strats_b]}")
    res_b = run_backtest(cfg_b, data, "Combined (5-strat)", strats_b)

    # ===== TEST C: Standalone dividend capture =====
    print("\n" + "=" * 65)
    print("  TEST C: Standalone Dividend Capture")
    print("=" * 65)
    cfg_c = copy.deepcopy(config)
    for sname in cfg_c["strategies"]:
        cfg_c["strategies"][sname]["enabled"] = False
    cfg_c["strategies"]["dividend_capture"]["enabled"] = True
    strats_c = [DividendCapture(cfg_c)]
    print(f"  Active strategies: {[s.name for s in strats_c]}")
    res_c = run_backtest(cfg_c, data, "Standalone DC", strats_c)

    # ===== RESULTS COMPARISON =====
    print("\n" + "=" * 65)
    print("  RESULTS COMPARISON")
    print("=" * 65)
    hdr = f"  {'Config':<22} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>8} {'WinR%':>8} {'PF':>8} {'#Trades':>8}"
    print(hdr)
    print("  " + "-" * 64)
    for r in [res_a, res_b, res_c]:
        m = r["metrics"]
        print(f"  {r['label']:<22} {m.get('cagr',0)*100:>+7.2f}% {m.get('max_drawdown',0)*100:>7.2f}% "
              f"{m.get('sharpe',0):>7.3f} {m.get('win_rate',0)*100:>7.1f}% "
              f"{m.get('profit_factor',0):>7.2f} {m.get('total_trades',len(r['trades'])):>8}")

    # Trade breakdown by strategy
    print("\n  Trade counts by strategy:")
    for r in [res_a, res_b, res_c]:
        print(f"    {r['label']}: {r['strat_counts']}")

    # Delta analysis
    ma = res_a["metrics"]
    mb = res_b["metrics"]
    print("\n  Delta (Combined vs Baseline):")
    print(f"    CAGR:        {(mb.get('cagr',0)-ma.get('cagr',0))*100:>+.3f}%")
    print(f"    Max DD:      {(mb.get('max_drawdown',0)-ma.get('max_drawdown',0))*100:>+.3f}%")
    print(f"    Sharpe:      {mb.get('sharpe',0)-ma.get('sharpe',0):>+.3f}")
    print(f"    Win Rate:    {(mb.get('win_rate',0)-ma.get('win_rate',0))*100:>+.1f}%")
    print(f"    Trades:      {len(res_b['trades'])-len(res_a['trades']):>+d}")

    # Dividend income estimation
    print("\n" + "=" * 65)
    print("  FRANKING CREDIT INCOME ESTIMATION")
    print("=" * 65)
    dc_trades = [t for t in res_b["trades"] if t.get("strategy") == "dividend_capture"]
    total_div_income = 0
    total_franking = 0
    for t in dc_trades:
        feat = t.get("features", {})
        div_amt = feat.get("div_amount", 0)
        frank = feat.get("franking_pct", 0)
        shares = t.get("shares", t.get("position_size", 0))
        div_income = div_amt * shares
        frank_credit = div_income * frank * 0.30 / 0.70
        total_div_income += div_income
        total_franking += frank_credit
    print(f"  Dividend capture trades: {len(dc_trades)}")
    print(f"  Estimated cash dividends: ${total_div_income:,.2f}")
    print(f"  Estimated franking credits: ${total_franking:,.2f}")
    print(f"  Total grossed-up income: ${total_div_income + total_franking:,.2f}")
    print(f"  (This is ADDITIONAL to price-based P&L)")

    # Save results
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = {
        "timestamp": datetime.now().isoformat(),
        "universe_size": len(data),
        "results": {
            "baseline": {"metrics": res_a["metrics"], "trade_count": len(res_a["trades"]), "strat_counts": res_a["strat_counts"]},
            "combined": {"metrics": res_b["metrics"], "trade_count": len(res_b["trades"]), "strat_counts": res_b["strat_counts"]},
            "standalone_dc": {"metrics": res_c["metrics"], "trade_count": len(res_c["trades"]), "strat_counts": res_c["strat_counts"]},
        },
        "franking_estimate": {"dc_trades": len(dc_trades), "cash_dividends": total_div_income, "franking_credits": total_franking},
        "config": config["strategies"]["dividend_capture"],
    }
    out_path = f"backtest/results/dividend_capture_full_validation_{ts}.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  Results saved: {out_path}")
    print("\n" + "#" * 65)

if __name__ == "__main__":
    main()
