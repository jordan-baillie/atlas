#!/usr/bin/env python3
"""Phase 9B: Strategy-Specific Max Loss Cap Testing

Tests max_loss_per_trade applied selectively to BB Squeeze & Mean Reversion
while exempting Trend Following (which needs room to breathe).

Configurations tested:
  1. Baseline         - no cap (current active config)
  2. $40 cap ALL      - uniform cap on all strategies (Phase 9A reference)
  3. $40 cap BB+MR    - exempt trend_following
  4. $35 cap BB+MR    - tighter, exempt trend_following  
  5. $45 cap BB+MR    - looser, exempt trend_following
  6. $40 cap BB only  - exempt trend_following + mean_reversion + opening_gap
"""
import sys, json, copy, time, importlib
import numpy as np
from pathlib import Path
from datetime import datetime

sys.path.insert(0, "/a0/usr/projects/atlas-asx")
import pandas as pd

DATA_DIR = Path("/a0/usr/projects/atlas-asx/data/cache")
RESULTS_DIR = Path("/a0/usr/projects/atlas-asx/backtest/results")
CONFIG_PATH = Path("/a0/usr/projects/atlas-asx/config/active_config.json")

def load_data(min_rows=100):
    dd = {}
    for pf in sorted(DATA_DIR.glob("*.parquet")):
        if pf.stem == "IOZ_AX": continue
        ticker = pf.stem.replace("_AX", ".AX")
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df.index = pd.to_datetime(df.index)
        if len(df) >= min_rows:
            dd[ticker] = df
    return dd

def norm_m(m):
    cagr = m.get("cagr", 0)
    cp = cagr * 100 if abs(cagr) < 2 else cagr
    dd = m.get("max_drawdown", 0)
    dp = dd * 100 if abs(dd) < 2 else dd
    wr = m.get("win_rate", 0)
    wp = wr * 100 if abs(wr) < 2 else wr
    return {
        "total_trades": m.get("total_trades", 0),
        "total_pnl": round(m.get("total_pnl", 0), 2),
        "avg_trade": round(m.get("avg_trade", 0), 2),
        "win_rate_pct": round(wp, 2),
        "profit_factor": round(m.get("profit_factor", 0), 4),
        "sharpe": round(m.get("sharpe", 0), 4),
        "sortino": round(m.get("sortino", 0), 4),
        "cagr_pct": round(cp, 4),
        "max_drawdown_pct": round(dp, 4),
        "final_equity": round(m.get("final_equity", 0), 2),
    }

def analyze_strategy_trades(trades, strategy_name):
    """Analyze trades for a specific strategy."""
    strat_trades = [t for t in trades if t.get("strategy") == strategy_name]
    if not strat_trades:
        return {"count": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0}
    pnls = [t["pnl"] for t in strat_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    big_losses = [p for p in pnls if p < -35]
    exit_reasons = {}
    for t in strat_trades:
        r = t.get("exit_reason", "unknown")
        exit_reasons[r] = exit_reasons.get(r, 0) + 1
    gross_wins = sum(wins) if wins else 0
    gross_losses = abs(sum(losses)) if losses else 0.001
    return {
        "count": len(strat_trades),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl": round(np.mean(pnls), 2),
        "win_rate": round(len(wins) / len(strat_trades) * 100, 1) if strat_trades else 0,
        "profit_factor": round(gross_wins / gross_losses, 4),
        "losses_count": len(losses),
        "avg_loss": round(np.mean(losses), 2) if losses else 0,
        "worst_loss": round(min(pnls), 2) if losses else 0,
        "big_losses_gt35": len(big_losses),
        "big_losses_total": round(sum(big_losses), 2) if big_losses else 0,
        "exit_reasons": exit_reasons,
    }

def run_backtest(config, data):
    """Run backtest with given config. Returns (metrics, trades)."""
    # Reload engine to pick up config changes
    import backtest.engine as eng_mod
    importlib.reload(eng_mod)
    from backtest.engine import BacktestEngine
    
    # Reload all strategy modules
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("strategies.") and mod_name != "strategies.base":
            importlib.reload(sys.modules[mod_name])
    
    engine = BacktestEngine(config)
    results = engine.run(data)
    metrics = results.get("metrics", {})
    trades = results.get("trades", [])
    return metrics, trades

def main():
    print("="*70)
    print("PHASE 9B: Strategy-Specific Max Loss Cap Testing")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)
    
    # Load data
    print("\nLoading data...")
    data = load_data()
    print(f"Loaded {len(data)} tickers")
    
    # Load base config
    base_config = json.load(open(CONFIG_PATH))
    
    # Define test configurations
    tests = [
        {
            "name": "1_baseline",
            "label": "Baseline (no cap)",
            "max_loss": None,
            "exempt": [],
        },
        {
            "name": "2_cap40_all",
            "label": "$40 cap ALL strategies",
            "max_loss": 40.0,
            "exempt": [],
        },
        {
            "name": "3_cap40_exempt_tf",
            "label": "$40 cap, exempt TF",
            "max_loss": 40.0,
            "exempt": ["trend_following"],
        },
        {
            "name": "4_cap35_exempt_tf",
            "label": "$35 cap, exempt TF",
            "max_loss": 35.0,
            "exempt": ["trend_following"],
        },
        {
            "name": "5_cap45_exempt_tf",
            "label": "$45 cap, exempt TF",
            "max_loss": 45.0,
            "exempt": ["trend_following"],
        },
        {
            "name": "6_cap40_bb_only",
            "label": "$40 cap BB only",
            "max_loss": 40.0,
            "exempt": ["trend_following", "mean_reversion", "opening_gap"],
        },
    ]
    
    strategies = ["bb_squeeze", "mean_reversion", "trend_following", "opening_gap"]
    all_results = {}
    
    for i, test in enumerate(tests):
        print(f"\n{'='*60}")
        print(f"Test {i+1}/{len(tests)}: {test['label']}")
        print(f"  max_loss_per_trade: {test['max_loss']}")
        print(f"  exempt_strategies: {test['exempt']}")
        print(f"{'='*60}")
        
        cfg = copy.deepcopy(base_config)
        
        # Set max loss cap
        if test["max_loss"] is not None:
            cfg["risk"]["max_loss_per_trade"] = test["max_loss"]
        else:
            cfg["risk"].pop("max_loss_per_trade", None)
        
        # Set exempt strategies
        if test["exempt"]:
            cfg["risk"]["max_loss_exempt_strategies"] = test["exempt"]
        else:
            cfg["risk"].pop("max_loss_exempt_strategies", None)
        
        t0 = time.time()
        metrics, trades = run_backtest(cfg, data)
        elapsed = time.time() - t0
        
        nm = norm_m(metrics)
        
        # Per-strategy breakdown
        strat_breakdown = {}
        for s in strategies:
            strat_breakdown[s] = analyze_strategy_trades(trades, s)
        
        # Max loss cap exits analysis
        mlc_exits = [t for t in trades if t.get("exit_reason") == "max_loss_cap"]
        mlc_by_strat = {}
        for t in mlc_exits:
            s = t.get("strategy", "unknown")
            if s not in mlc_by_strat:
                mlc_by_strat[s] = {"count": 0, "total_pnl": 0}
            mlc_by_strat[s]["count"] += 1
            mlc_by_strat[s]["total_pnl"] += t["pnl"]
        for s in mlc_by_strat:
            mlc_by_strat[s]["total_pnl"] = round(mlc_by_strat[s]["total_pnl"], 2)
        
        result = {
            "config": {
                "max_loss_per_trade": test["max_loss"],
                "exempt_strategies": test["exempt"],
            },
            "metrics": nm,
            "strategy_breakdown": strat_breakdown,
            "max_loss_cap_exits": {
                "total": len(mlc_exits),
                "by_strategy": mlc_by_strat,
            },
            "elapsed_seconds": round(elapsed, 1),
        }
        all_results[test["name"]] = result
        
        # Print summary
        print(f"\n  Elapsed: {elapsed:.0f}s")
        print(f"  Trades: {nm['total_trades']}  |  PnL: ${nm['total_pnl']:.2f}  |  CAGR: {nm['cagr_pct']:.2f}%")
        print(f"  Sharpe: {nm['sharpe']:.4f}  |  PF: {nm['profit_factor']:.4f}  |  MaxDD: {nm['max_drawdown_pct']:.2f}%")
        print(f"  Max Loss Cap Exits: {len(mlc_exits)}")
        for s in strategies:
            sb = strat_breakdown[s]
            print(f"    {s:20s}: {sb['count']:3d} trades, PnL ${sb['total_pnl']:8.2f}, WR {sb['win_rate']:5.1f}%", end="")
            if s in mlc_by_strat:
                print(f"  [MLC: {mlc_by_strat[s]['count']} exits, ${mlc_by_strat[s]['total_pnl']:.2f}]")
            else:
                print()
    
    # === COMPARISON TABLE ===
    print("\n" + "="*90)
    print("COMPARISON TABLE")
    print("="*90)
    
    header = f"{'Config':<25s} {'Trades':>6s} {'PnL$':>8s} {'CAGR%':>7s} {'Sharpe':>7s} {'PF':>7s} {'MaxDD%':>7s} {'MLC#':>5s}"
    print(header)
    print("-"*len(header))
    
    baseline_metrics = None
    for name, res in all_results.items():
        nm = res["metrics"]
        mlc_total = res["max_loss_cap_exits"]["total"]
        label = name.split("_", 1)[1] if "_" in name else name
        if baseline_metrics is None:
            baseline_metrics = nm
        print(f"{label:<25s} {nm['total_trades']:>6d} {nm['total_pnl']:>8.2f} {nm['cagr_pct']:>7.2f} {nm['sharpe']:>7.4f} {nm['profit_factor']:>7.4f} {nm['max_drawdown_pct']:>7.2f} {mlc_total:>5d}
