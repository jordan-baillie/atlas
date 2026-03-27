#!/usr/bin/env python3
"""
TP ATR Sweep — Find optimal profit_target_atr_mult for strategies without TP.

Sweeps take-profit ATR multipliers for: trend_following, momentum_breakout,
sector_rotation, opening_gap. Runs full combined portfolio backtests.

Usage:
    python3 scripts/tp_sweep.py [--workers 6] [--strategy trend_following]
"""
import sys, os, json, copy, time, argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.strategy_evaluator import load_market_data, run_backtest


def load_config():
    with open("config/active/sp500.json") as f:
        return json.load(f)


def make_config_variant(base_cfg, strategy_name, tp_mult):
    """Create a config variant with a specific TP ATR mult for one strategy."""
    cfg = copy.deepcopy(base_cfg)
    if strategy_name in cfg.get("strategies", {}):
        cfg["strategies"][strategy_name]["profit_target_atr_mult"] = tp_mult
    return cfg


def run_single_backtest(args):
    """Run a single backtest variant. Returns (strategy, tp_mult, metrics)."""
    strategy_name, tp_mult, config, market_data = args
    cfg = make_config_variant(config, strategy_name, tp_mult)
    
    try:
        result = run_backtest(cfg, market_data)
        metrics = {
            "sharpe": result.get("sharpe", 0),
            "cagr": result.get("cagr", 0),
            "max_drawdown": result.get("max_drawdown", 0),
            "total_trades": result.get("total_trades", 0),
            "win_rate": result.get("win_rate", 0),
            "profit_factor": result.get("profit_factor", 0),
            "final_equity": result.get("final_equity", 0),
            "calmar": result.get("calmar", 0),
            "avg_r": result.get("avg_r", 0),
            "expectancy_r": result.get("expectancy_r", 0),
        }
        return strategy_name, tp_mult, metrics
    except Exception as e:
        import traceback
        return strategy_name, tp_mult, {"error": f"{e}\n{traceback.format_exc()}"}


def main():
    parser = argparse.ArgumentParser(description="TP ATR Sweep")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--strategy", type=str, default=None, 
                        help="Sweep single strategy (default: all 4)")
    parser.add_argument("--top-n", type=int, default=0,
                        help="Use top N tickers only (0=all)")
    args = parser.parse_args()

    # Strategies to sweep and their ATR multiplier ranges
    sweep_targets = {
        "trend_following": [0.0, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0],
        "momentum_breakout": [0.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
        "sector_rotation": [0.0, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
        "opening_gap": [0.0, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0],
    }

    if args.strategy:
        if args.strategy not in sweep_targets:
            print(f"Unknown strategy: {args.strategy}")
            print(f"Available: {list(sweep_targets.keys())}")
            sys.exit(1)
        sweep_targets = {args.strategy: sweep_targets[args.strategy]}

    base_cfg = load_config()
    
    print("Loading market data...")
    t0 = time.time()
    market_data = load_market_data("sp500")
    n_tickers = len(market_data)
    
    if args.top_n > 0:
        # Keep only top N tickers (by data length, as proxy for liquidity)
        sorted_tickers = sorted(market_data.keys(), key=lambda t: len(market_data[t]), reverse=True)
        keep = sorted_tickers[:args.top_n]
        market_data = {t: market_data[t] for t in keep}
        n_tickers = len(market_data)
    
    print(f"  {n_tickers} tickers loaded in {time.time()-t0:.1f}s")

    # Count total experiments
    total = sum(len(vals) for vals in sweep_targets.values())
    print(f"\nSweeping {total} variants across {len(sweep_targets)} strategies")
    print(f"  Workers: {args.workers}")
    print()

    # Build all experiment args
    experiments = []
    for strat_name, tp_values in sweep_targets.items():
        for tp_mult in tp_values:
            experiments.append((strat_name, tp_mult, base_cfg, market_data))

    # Run in parallel
    results = {}
    completed = 0
    t_start = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(run_single_backtest, exp): (exp[0], exp[1])
            for exp in experiments
        }
        for future in as_completed(futures):
            strat, tp = futures[future]
            completed += 1
            try:
                strat_name, tp_mult, metrics = future.result()
                if strat_name not in results:
                    results[strat_name] = []
                results[strat_name].append({"tp_mult": tp_mult, **metrics})
                
                elapsed = time.time() - t_start
                eta = (elapsed / completed) * (total - completed)
                
                if "error" in metrics:
                    print(f"  [{completed}/{total}] {strat_name} tp={tp_mult:.1f}x — ERROR: {metrics['error']}")
                else:
                    sharpe = metrics.get('sharpe', 0)
                    cagr = metrics.get('cagr', 0) * 100
                    trades = metrics.get('total_trades', 0)
                    print(f"  [{completed}/{total}] {strat_name} tp={tp_mult:.1f}x — "
                          f"Sharpe={sharpe:.3f}, CAGR={cagr:.1f}%, trades={trades} "
                          f"[{elapsed:.0f}s elapsed, ~{eta:.0f}s remaining]")
            except Exception as e:
                print(f"  [{completed}/{total}] {strat} tp={tp:.1f}x — EXCEPTION: {e}")

    # Save results
    output_path = "backtest/results/tp_sweep_results.json"
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")

    # Print summary per strategy
    print("\n" + "=" * 100)
    print("TP ATR SWEEP RESULTS")
    print("=" * 100)
    
    for strat_name, strat_results in sorted(results.items()):
        print(f"\n{'─' * 80}")
        print(f"  {strat_name.upper()}")
        print(f"{'─' * 80}")
        print(f"  {'TP Mult':>8s} │ {'Sharpe':>8s} │ {'CAGR':>8s} │ {'MaxDD':>8s} │ {'Trades':>7s} │ {'WinRate':>8s} │ {'PF':>6s} │ {'Calmar':>8s}")
        print(f"  {'─'*8} │ {'─'*8} │ {'─'*8} │ {'─'*8} │ {'─'*7} │ {'─'*8} │ {'─'*6} │ {'─'*8}")
        
        # Sort by tp_mult
        sorted_results = sorted(strat_results, key=lambda x: x["tp_mult"])
        best_sharpe = max(r.get("sharpe", -999) for r in sorted_results)
        
        for r in sorted_results:
            if "error" in r:
                print(f"  {r['tp_mult']:>7.1f}x │ ERROR: {r['error']}")
                continue
            
            tp = r["tp_mult"]
            sharpe = r.get("sharpe", 0)
            cagr = r.get("cagr", 0) * 100
            dd = r.get("max_drawdown", 0) * 100
            trades = r.get("total_trades", 0)
            wr = r.get("win_rate", 0) * 100
            pf = r.get("profit_factor", 0)
            calmar = r.get("calmar", 0)
            
            marker = " ★" if sharpe == best_sharpe else ""
            tp_label = f"{tp:.1f}x" if tp > 0 else "NONE"
            
            print(f"  {tp_label:>8s} │ {sharpe:>+8.3f} │ {cagr:>7.1f}% │ {dd:>7.1f}% │ {trades:>7d} │ {wr:>7.1f}% │ {pf:>6.2f} │ {calmar:>+8.2f}{marker}")
    
    # Print recommendation
    print(f"\n{'=' * 100}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 100}")
    for strat_name, strat_results in sorted(results.items()):
        valid = [r for r in strat_results if "error" not in r]
        if not valid:
            print(f"  {strat_name}: No valid results")
            continue
        
        baseline = next((r for r in valid if r["tp_mult"] == 0.0), None)
        best = max(valid, key=lambda x: x.get("sharpe", -999))
        
        if baseline:
            delta = best.get("sharpe", 0) - baseline.get("sharpe", 0)
            print(f"  {strat_name}: Best TP = {best['tp_mult']:.1f}x ATR "
                  f"(Sharpe {best.get('sharpe',0):+.3f} vs baseline {baseline.get('sharpe',0):+.3f}, "
                  f"delta {delta:+.3f})")
        else:
            print(f"  {strat_name}: Best TP = {best['tp_mult']:.1f}x ATR "
                  f"(Sharpe {best.get('sharpe',0):+.3f})")


if __name__ == "__main__":
    main()
