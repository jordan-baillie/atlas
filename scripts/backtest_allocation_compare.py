#!/usr/bin/env python3
"""Compare 4 allocation cap strategies via backtest."""
import sys, os, copy, json, time

# Add project root to path
sys.path.insert(0, '/root/atlas')
os.chdir('/root/atlas')

from utils.config import get_active_config
from backtest.engine import BacktestEngine
from scripts.cli import load_data, get_strategies, get_tickers

def run_variant(name, config, data):
    """Run a single backtest variant and return metrics."""
    print(f"\n{'='*60}")
    print(f"  RUNNING: {name}")
    print(f"  Allocation: enabled={config['allocation']['enabled']}, overflow={config['allocation'].get('overflow_enabled')}")
    pools = config['allocation']['pools']
    for k, v in pools.items():
        if isinstance(v, dict) and 'max_positions' in v:
            print(f"    {k}: max_positions={v['max_positions']}")
    print(f"{'='*60}")
    
    start = time.time()
    strategies = get_strategies(config)
    engine = BacktestEngine(config, market_id='sp500')
    result = engine.run_walkforward(data, strategies)
    elapsed = time.time() - start
    
    metrics = result.metrics if hasattr(result, 'metrics') else result.get('metrics', {})
    bench = result.benchmark_metrics if hasattr(result, 'benchmark_metrics') else result.get('benchmark_metrics', {})
    trades = result.trades if hasattr(result, 'trades') else result.get('trades', [])
    
    print(f"  Completed in {elapsed:.1f}s")
    return {
        'name': name,
        'cagr': metrics.get('cagr', 0),
        'max_drawdown': metrics.get('max_drawdown', 0),
        'sharpe': metrics.get('sharpe', 0),
        'sortino': metrics.get('sortino', 0),
        'win_rate': metrics.get('win_rate', 0),
        'profit_factor': metrics.get('profit_factor', 0),
        'total_trades': metrics.get('total_trades', len(trades)),
        'avg_trade': metrics.get('avg_trade', 0),
        'total_pnl': metrics.get('total_pnl', 0),
        'final_equity': metrics.get('final_equity', 0),
        'elapsed': elapsed,
        'benchmark_cagr': bench.get('cagr', 0),
        'benchmark_sharpe': bench.get('sharpe', 0),
    }


def main():
    print("Loading SP500 config and data (one-time)...")
    base_config = get_active_config('sp500')
    tickers = get_tickers('sp500')
    data = load_data(tickers, base_config)
    print(f"Loaded {len(data)} tickers")
    
    results = []
    
    # ---- VARIANT 1: BASELINE (current caps) ----
    cfg1 = copy.deepcopy(base_config)
    results.append(run_variant("BASELINE (current caps)", cfg1, data))
    
    # ---- VARIANT 2: momentum_breakout cap 2 → 4 ----
    cfg2 = copy.deepcopy(base_config)
    cfg2['allocation']['pools']['momentum_breakout']['max_positions'] = 4
    results.append(run_variant("Momentum cap 2→4", cfg2, data))
    
    # ---- VARIANT 3: overflow (_other) cap 2 → 4 ----
    cfg3 = copy.deepcopy(base_config)
    cfg3['allocation']['pools']['_other']['max_positions'] = 4
    results.append(run_variant("Overflow cap 2→4", cfg3, data))
    
    # ---- VARIANT 4: UNCAPPED (all pools max_positions=10) ----
    cfg4 = copy.deepcopy(base_config)
    for pool_name, pool_cfg in cfg4['allocation']['pools'].items():
        if isinstance(pool_cfg, dict) and 'max_positions' in pool_cfg:
            pool_cfg['max_positions'] = 10
    results.append(run_variant("UNCAPPED (all=10)", cfg4, data))
    
    # ---- COMPARISON TABLE ----
    print("\n\n" + "=" * 100)
    print("  ALLOCATION CAP COMPARISON — SP500 BACKTEST RESULTS")
    print("=" * 100)
    
    header = f"{'Variant':<28} {'CAGR':>8} {'MaxDD':>8} {'Sharpe':>8} {'Sortino':>8} {'WinRate':>8} {'PF':>8} {'Trades':>7} {'AvgTrd':>8} {'PnL':>10} {'Time':>6}"
    print(header)
    print("-" * len(header))
    
    for r in results:
        print(f"{r['name']:<28} {r['cagr']*100:>+7.2f}% {r['max_drawdown']*100:>7.2f}% {r['sharpe']:>8.3f} {r['sortino']:>8.3f} {r['win_rate']*100:>7.1f}% {r['profit_factor']:>8.2f} {r['total_trades']:>7d} {r['avg_trade']:>8.2f} {r['total_pnl']:>10.2f} {r['elapsed']:>5.0f}s")
    
    print(f"\nBenchmark (Buy & Hold SPY): CAGR={results[0]['benchmark_cagr']*100:+.2f}%, Sharpe={results[0]['benchmark_sharpe']:.3f}")
    
    # Find best
    best_sharpe = max(results, key=lambda x: x['sharpe'])
    best_cagr = max(results, key=lambda x: x['cagr'])
    best_pf = max(results, key=lambda x: x['profit_factor'])
    
    print(f"\nBest Sharpe:        {best_sharpe['name']} ({best_sharpe['sharpe']:.3f})")
    print(f"Best CAGR:          {best_cagr['name']} ({best_cagr['cagr']*100:+.2f}%)")
    print(f"Best Profit Factor: {best_pf['name']} ({best_pf['profit_factor']:.2f})")
    
    # Save results
    with open('/root/atlas/backtest/results/allocation_cap_comparison.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print("\nResults saved to backtest/results/allocation_cap_comparison.json")


if __name__ == '__main__':
    main()
