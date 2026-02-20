#!/usr/bin/env python3
"""Phase 9: Cointegration Filter Comparison Test."""
import sys, os, json, time, copy
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(name)s %(levelname)s %(message)s')
# Suppress noisy loggers
for name in ['utils.earnings', 'utils.helpers', 'backtest.engine', 'strategies.trend_following']:
    logging.getLogger(name).setLevel(logging.WARNING)

import pandas as pd
from pathlib import Path
from utils.config import load_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

# Load config
config = load_config()
cache_dir = Path('data/cache')
with open('data/processed/universe.json') as f:
    tickers = json.load(f)['tickers']

# Load data
data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        df = pd.read_parquet(p)
        if len(df) > 100:
            data[t] = df
print(f'Loaded {len(data)} tickers')

def run_test(name, cfg, strats):
    engine = BacktestEngine(cfg)
    t0 = time.time()
    result = engine.run_walkforward(data, strats)
    dt = time.time() - t0
    m = result.metrics

    # Strategy breakdown
    strat_info = {}
    for tr in result.trades:
        s = tr.get('strategy', 'unknown')
        strat_info.setdefault(s, {'trades': 0, 'wins': 0, 'pnl': 0})
        strat_info[s]['trades'] += 1
        if tr.get('pnl', 0) > 0:
            strat_info[s]['wins'] += 1
        strat_info[s]['pnl'] += tr.get('pnl', 0)

    print(f'\n{"="*60}')
    print(f'  {name}')
    print(f'{"="*60}')
    print(f'  Trades: {m["total_trades"]:3d}  | WR: {m["win_rate"]*100:5.1f}%  | PF: {m["profit_factor"]:5.2f}')
    print(f'  CAGR:  {m["cagr"]*100:5.2f}% | DD: {m["max_drawdown"]*100:5.2f}% | PnL: ${m["total_pnl"]:7.2f}')
    print(f'  Sharpe: {m.get("sharpe", 0):5.2f}  | Exp: {m["exposure"]*100:5.1f}%  | Time: {dt:.0f}s')

    for s, info in sorted(strat_info.items()):
        wr = info['wins'] / info['trades'] * 100 if info['trades'] > 0 else 0
        print(f'    {s}: {info["trades"]} trades, {wr:.1f}% WR, ${info["pnl"]:.2f}')

    # Check for cointegration features in mean_reversion trades
    coint_trades = [t for t in result.trades if t.get('strategy') == 'mean_reversion']
    coint_boosts = [t.get('features', {}).get('coint_adjustment', 0) for t in coint_trades]
    coint_pairs = [t.get('features', {}).get('coint_pairs_checked', 0) for t in coint_trades]
    if any(a != 0 for a in coint_boosts):
        print(f'  Coint adjustments: min={min(coint_boosts):.4f}, max={max(coint_boosts):.4f}, '
              f'mean={sum(coint_boosts)/len(coint_boosts):.4f}')
        print(f'  Pairs checked per trade: min={min(coint_pairs)}, max={max(coint_pairs)}, '
              f'mean={sum(coint_pairs)/len(coint_pairs):.1f}')
    elif coint_trades:
        print(f'  Mean reversion: {len(coint_trades)} trades, no coint adjustments applied')

    return m, result.trades


print('\n' + '='*60)
print('  PHASE 9: COINTEGRATION FILTER COMPARISON')
print('='*60)

# --- Test 1: Baseline (cointegration disabled) ---
print('\n>>> Running BASELINE (cointegration disabled)...')
cfg_baseline = copy.deepcopy(config)
cfg_baseline['strategies']['mean_reversion']['cointegration_filter'] = {'enabled': False}
strats_baseline = [TrendFollowing(cfg_baseline), MeanReversion(cfg_baseline)]
m_base, trades_base = run_test('BASELINE (v8.2 - no cointegration)', cfg_baseline, strats_baseline)

# --- Test 2: Cointegration enabled ---
print('\n>>> Running COINTEGRATION FILTER (enabled)...')
cfg_coint = copy.deepcopy(config)
cfg_coint['strategies']['mean_reversion']['cointegration_filter']['enabled'] = True
strats_coint = [TrendFollowing(cfg_coint), MeanReversion(cfg_coint)]
m_coint, trades_coint = run_test('PHASE 9 (cointegration filter ON)', cfg_coint, strats_coint)

# --- Summary ---
print('\n' + '='*60)
print('  COMPARISON SUMMARY')
print('='*60)
print(f'{"Metric":<20} {"Baseline":>12} {"Coint Filter":>12} {"Delta":>12}')
print('-'*56)
for key, fmt, mult in [
    ('total_trades', '{:.0f}', 1),
    ('win_rate', '{:.1f}%', 100),
    ('cagr', '{:.2f}%', 100),
    ('max_drawdown', '{:.2f}%', 100),
    ('profit_factor', '{:.2f}', 1),
    ('total_pnl', '${:.2f}', 1),
    ('sharpe', '{:.2f}', 1),
]:
    v1 = m_base.get(key, 0) * mult
    v2 = m_coint.get(key, 0) * mult
    d = v2 - v1
    print(f'{key:<20} {fmt.format(v1):>12} {fmt.format(v2):>12} {"+" if d>=0 else ""}{fmt.format(d):>11}')

# Save results
results = {
    'baseline': m_base,
    'cointegration': m_coint,
    'baseline_trades': len(trades_base),
    'coint_trades': len(trades_coint),
}
with open('backtest/results/phase9_cointegration_comparison.json', 'w') as f:
    json.dump(results, f, indent=2, default=str)
print('\nResults saved to backtest/results/phase9_cointegration_comparison.json')
