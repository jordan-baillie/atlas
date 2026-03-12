#!/usr/bin/env python3
"""Task #89: Multi-equity-level backtest sweep.

Runs the active sp500 config at multiple starting equity levels:
$2K, $4K, $10K, $25K, $50K.

Shows how edge scales with capital and at what level each strategy becomes viable.
"""
import sys, json, logging, copy
sys.path.insert(0, "/root/atlas")
logging.basicConfig(level=logging.WARNING, format="%(message)s")

from scripts.strategy_evaluator import load_market_data, run_backtest
from utils.config import get_active_config

EQUITY_LEVELS = [2_000, 4_000, 10_000, 25_000, 50_000]


def main():
    print("Loading market data...")
    data = load_market_data("sp500")
    base_cfg = get_active_config("sp500")
    
    results = {}
    
    for equity in EQUITY_LEVELS:
        print(f"\n{'='*60}")
        print(f"Running backtest at ${equity:,.0f} starting equity...")
        print(f"{'='*60}")
        
        cfg = copy.deepcopy(base_cfg)
        cfg["risk"]["starting_equity"] = equity
        
        metrics = run_backtest(cfg, data)
        
        results[equity] = {
            "starting_equity": equity,
            "sharpe": round(metrics.get("sharpe", 0), 4),
            "cagr_pct": round(metrics.get("cagr_pct", 0), 2),
            "max_dd_pct": round(metrics.get("max_drawdown_pct", 0), 2),
            "total_return_pct": round((metrics.get("final_equity", equity) / equity - 1) * 100, 2),
            "total_trades": metrics.get("total_trades", 0),
            "win_rate_pct": round(metrics.get("win_rate_pct", 0), 1),
            "profit_factor": round(metrics.get("profit_factor", 0), 4),
            "avg_trade": round(metrics.get("avg_trade", 0), 2),
            "calmar": round(metrics.get("calmar", 0), 4),
            "sortino": round(metrics.get("sortino", 0), 4),
            "strategy_breakdown": metrics.get("strategy_breakdown", {}),
            "regime_metrics": metrics.get("regime_metrics", {}),
        }
        
        r = results[equity]
        print(f"  Sharpe: {r['sharpe']}")
        print(f"  CAGR: {r['cagr_pct']}%")
        print(f"  Max DD: {r['max_dd_pct']}%")
        print(f"  Trades: {r['total_trades']}")
        print(f"  Win Rate: {r['win_rate_pct']}%")
        print(f"  Profit Factor: {r['profit_factor']}")
        print(f"  Avg Trade: ${r['avg_trade']}")
    
    # Summary table
    print(f"\n\n{'='*80}")
    print("EQUITY SWEEP SUMMARY")
    print(f"{'='*80}")
    print(f"{'Equity':>10} {'Sharpe':>8} {'CAGR%':>8} {'MaxDD%':>8} {'Trades':>7} {'WR%':>6} {'PF':>8} {'AvgTrade':>10} {'Calmar':>8}")
    print("-" * 80)
    for eq in EQUITY_LEVELS:
        r = results[eq]
        print(f"${eq:>9,} {r['sharpe']:>8.4f} {r['cagr_pct']:>7.2f}% {r['max_dd_pct']:>7.2f}% {r['total_trades']:>7} {r['win_rate_pct']:>5.1f}% {r['profit_factor']:>8.4f} ${r['avg_trade']:>9.2f} {r['calmar']:>8.4f}")
    
    # Strategy-level breakdown
    print(f"\n\n{'='*80}")
    print("PER-STRATEGY BREAKDOWN BY EQUITY LEVEL")
    print(f"{'='*80}")
    
    all_strategies = set()
    for eq in EQUITY_LEVELS:
        all_strategies.update(results[eq].get("strategy_breakdown", {}).keys())
    
    for strat in sorted(all_strategies):
        print(f"\n  {strat}:")
        print(f"  {'Equity':>10} {'Trades':>7} {'TotalPnL':>10} {'WR%':>6}")
        for eq in EQUITY_LEVELS:
            sb = results[eq].get("strategy_breakdown", {}).get(strat, {})
            if sb:
                print(f"  ${eq:>9,} {sb['trades']:>7} ${sb['total_pnl']:>9.2f} {sb['win_rate_pct']:>5.1f}%")
    
    # Regime breakdown
    print(f"\n\n{'='*80}")
    print("PER-REGIME METRICS BY EQUITY LEVEL")
    print(f"{'='*80}")
    print(f"{'Equity':>10} {'Regime':>8} {'Trades':>7} {'WR%':>6} {'PF':>8} {'AvgTrade':>10} {'Sharpe~':>8}")
    print("-" * 80)
    for eq in EQUITY_LEVELS:
        rm = results[eq].get("regime_metrics", {})
        for regime in ["bull", "neutral", "bear"]:
            if regime in rm:
                rv = rm[regime]
                print(f"${eq:>9,} {regime:>8} {rv['trades']:>7} {rv['win_rate_pct']:>5.1f}% {rv['profit_factor']:>8.4f} ${rv['avg_trade']:>9.2f} {rv['sharpe_approx']:>8.4f}")
    
    # Save results
    outpath = "/root/atlas/research/results/equity_sweep.json"
    with open(outpath, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n\nResults saved to {outpath}")


if __name__ == "__main__":
    main()
