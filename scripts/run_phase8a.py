import json, sys, logging, time
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from strategies.momentum_breakout import MomentumBreakout
from backtest.engine import BacktestEngine

config = get_active_config()
print(f'Config: {config["version"]}', flush=True)

# Load data
cache_dir = Path('/a0/usr/projects/atlas-asx/data/cache')
with open('/a0/usr/projects/atlas-asx/data/processed/universe.json') as f:
    tickers = json.load(f).get('tickers', [])

data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        data[t] = pd.read_parquet(p)
print(f'Loaded {len(data)} tickers', flush=True)

# ==========================================================
# TEST 1: STANDALONE MOMENTUM BREAKOUT
# ==========================================================
print()
print('=' * 60)
print('  TEST 1: STANDALONE MOMENTUM BREAKOUT')
print('=' * 60)

mb_strategies = [MomentumBreakout(config)]
print(f'Strategies: {[s.name for s in mb_strategies]}', flush=True)
print('Running standalone momentum backtest...', flush=True)

t0 = time.time()
engine1 = BacktestEngine(config)
result1 = engine1.run_walkforward(data, mb_strategies)
m1 = result1.metrics
dt1 = time.time() - t0

print()
print(f'Completed in {dt1:.1f}s', flush=True)
for k, v in m1.items():
    if isinstance(v, float):
        print(f'  {k:<25}: {v:.4f}')
    else:
        print(f'  {k:<25}: {v}')

trades1 = result1.trades if hasattr(result1, 'trades') else []
print()
print(f'Total trades: {len(trades1)}')
wins1 = [t for t in trades1 if t.get('pnl', 0) > 0]
losses1 = [t for t in trades1 if t.get('pnl', 0) <= 0]
print(f'Winners: {len(wins1)}, Losers: {len(losses1)}')
if trades1:
    total_pnl1 = sum(t.get('pnl', 0) for t in trades1)
    print(f'Total PnL: ${total_pnl1:.2f}')

# ==========================================================
# TEST 2: COMBINED 3-STRATEGY BACKTEST
# ==========================================================
print()
print('=' * 60)
print('  TEST 2: COMBINED 3-STRATEGY (TF + MR + MB)')
print('=' * 60)

all_strategies = []
if config['strategies']['trend_following'].get('enabled'):
    all_strategies.append(TrendFollowing(config))
if config['strategies']['mean_reversion'].get('enabled'):
    all_strategies.append(MeanReversion(config))
if config['strategies']['momentum_breakout'].get('enabled'):
    all_strategies.append(MomentumBreakout(config))
print(f'Strategies: {[s.name for s in all_strategies]}', flush=True)
print('Running combined backtest...', flush=True)

t0 = time.time()
engine2 = BacktestEngine(config)
result2 = engine2.run_walkforward(data, all_strategies)
m2 = result2.metrics
dt2 = time.time() - t0

print()
print(f'Completed in {dt2:.1f}s', flush=True)
for k, v in m2.items():
    if isinstance(v, float):
        print(f'  {k:<25}: {v:.4f}')
    else:
        print(f'  {k:<25}: {v}')

trades2 = result2.trades if hasattr(result2, 'trades') else []
print()
print(f'Total trades: {len(trades2)}')

# Strategy breakdown
by_strat = {}
for t in trades2:
    s = t.get('strategy', 'unknown')
    if s not in by_strat:
        by_strat[s] = {'count': 0, 'wins': 0, 'pnl': 0}
    by_strat[s]['count'] += 1
    by_strat[s]['pnl'] += t.get('pnl', 0)
    if t.get('pnl', 0) > 0:
        by_strat[s]['wins'] += 1

print('Strategy Breakdown:')
for s, stats in by_strat.items():
    wr = stats['wins'] / stats['count'] * 100 if stats['count'] > 0 else 0
    print(f'  {s:<25}: {stats["count"]} trades, {wr:.1f}% WR, ${stats["pnl"]:.2f} PnL')

# ==========================================================
# COMPARISON WITH BASELINE
# ==========================================================
print()
print('=' * 60)
print('  COMPARISON: v7.0 Baseline vs v8.0 3-Strategy')
print('=' * 60)

baseline_file = Path('/a0/usr/projects/atlas-asx/backtest/results/baseline_2strat.json')
if baseline_file.exists():
    with open(baseline_file) as f:
        baseline = json.load(f)
    bm = baseline.get('metrics', {})

    compare_keys = ['cagr', 'total_return', 'max_drawdown', 'win_rate', 'profit_factor', 'total_trades', 'sharpe_ratio']
    print(f'{"Metric":<25} {"Baseline (2-strat)":>18} {"Phase 8A (3-strat)":>18} {"Delta":>10}')
    print('-' * 75)
    for k in compare_keys:
        bv = bm.get(k, 0)
        nv = m2.get(k, 0)
        if isinstance(bv, (int, float)) and isinstance(nv, (int, float)):
            delta = nv - bv
            if isinstance(bv, float):
                print(f'{k:<25} {bv:>18.4f} {nv:>18.4f} {delta:>+10.4f}')
            else:
                print(f'{k:<25} {bv:>18} {nv:>18} {delta:>+10}')
        else:
            print(f'{k:<25} {str(bv):>18} {str(nv):>18}')
else:
    print('No baseline file found for comparison')

# Save results
result_data = {
    'version': config['version'],
    'timestamp': pd.Timestamp.now().isoformat(),
    'standalone_momentum': {
        'metrics': {k: v for k, v in m1.items()},
        'trade_count': len(trades1)
    },
    'combined_3strat': {
        'metrics': {k: v for k, v in m2.items()},
        'trade_count': len(trades2),
        'strategy_breakdown': by_strat
    }
}

with open('backtest/results/phase8a_momentum_results.json', 'w') as f:
    json.dump(result_data, f, indent=2, default=str)
print()
print('Results saved to backtest/results/phase8a_momentum_results.json')
