#!/usr/bin/env python3
"""Phase 8BCD Comprehensive Comparison Test."""
import sys, os, json, time, copy
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

import logging
logging.basicConfig(level=logging.WARNING)

import pandas as pd
from pathlib import Path
from utils.config import load_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from strategies.sector_rotation import SectorRotation
from backtest.engine import BacktestEngine

config = load_config()
cache_dir = Path('data/cache')
with open('data/processed/universe.json') as f:
    tickers = json.load(f)['tickers']

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
    strat_info = {}
    for tr in result.trades:
        s = tr.get('strategy', 'unknown')
        strat_info.setdefault(s, {'trades': 0, 'wins': 0, 'pnl': 0})
        strat_info[s]['trades'] += 1
        if tr.get('pnl', 0) > 0: strat_info[s]['wins'] += 1
        strat_info[s]['pnl'] += tr.get('pnl', 0)
    print(f'\n{"="*60}')
    print(f'  {name}')
    print(f'{"="*60}')
    print(f'  Trades: {m["total_trades"]:3d}  | WR: {m["win_rate"]*100:5.1f}%  | PF: {m["profit_factor"]:5.2f}')
    print(f'  CAGR:  {m["cagr"]*100:5.2f}% | DD: {m["max_drawdown"]*100:5.2f}% | PnL: ${m["total_pnl"]:7.2f}')
    print(f'  Sharpe: {m["sharpe"]:5.2f}  | Exp: {m["exposure"]*100:5.1f}%  | Time: {dt:.0f}s')
    for s, info in sorted(strat_info.items()):
        wr = info['wins']/info['trades']*100 if info['trades'] > 0 else 0
        print(f'    {s:20s}: {info["trades"]:3d}t, WR={wr:5.1f}%, PnL=${info["pnl"]:8.2f}')
    return m, result

# === TEST 1: 8B only ===
print('\n>>> TEST 1: 8B only (TF+MR, expanded universe, fixed sizing)')
cfg1 = copy.deepcopy(config)
cfg1['dynamic_sizing']['enabled'] = False
m1, _ = run_test('8B: TF+MR, Expanded, Fixed Sizing', cfg1, [TrendFollowing(cfg1), MeanReversion(cfg1)])

# === TEST 2: 8B+8D ===
print('\n>>> TEST 2: 8B+8D (TF+MR, expanded, dynamic sizing)')
cfg2 = copy.deepcopy(config)
cfg2['dynamic_sizing']['enabled'] = True
m2, _ = run_test('8B+8D: TF+MR, Dynamic Sizing', cfg2, [TrendFollowing(cfg2), MeanReversion(cfg2)])

# === TEST 3: Full 8BCD ===
print('\n>>> TEST 3: Full 8BCD (TF+MR+SR, dynamic sizing, SR min_conf=0.80)')
cfg3 = copy.deepcopy(config)
cfg3['dynamic_sizing']['enabled'] = True
m3, r3 = run_test('8BCD: TF+MR+SR, Full Phase 8', cfg3, [TrendFollowing(cfg3), MeanReversion(cfg3), SectorRotation(cfg3)])

# === SUMMARY TABLE ===
print('\n' + '='*70)
print('  PHASE 8 COMPARISON SUMMARY')
print('='*70)
print(f'{"Config":>25s} {"Trades":>7s} {"WR%":>7s} {"CAGR%":>8s} {"DD%":>7s} {"PF":>7s} {"PnL$":>9s}')
for label, m in [('8B (baseline)', m1), ('8B+8D (dyn sizing)', m2), ('Full 8BCD', m3)]:
    print(f'{label:>25s} {m["total_trades"]:>7d} {m["win_rate"]*100:>6.1f}% {m["cagr"]*100:>7.2f}% {m["max_drawdown"]*100:>6.2f}% {m["profit_factor"]:>6.2f} {m["total_pnl"]:>8.2f}')
print('='*70)

# Save detailed results
results_out = {
    '8B_baseline': m1,
    '8B_8D': m2,
    '8BCD_full': m3
}
with open('backtest/results/phase8_comparison.json', 'w') as f:
    json.dump(results_out, f, indent=2, default=str)
print('\nSaved: backtest/results/phase8_comparison.json')
