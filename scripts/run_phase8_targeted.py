#!/usr/bin/env python3
"""Phase 8BCD: Targeted comparison with max_positions=5."""
import sys, os, json, time, copy
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')
import logging; logging.basicConfig(level=logging.WARNING)
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
    print(f'\n  {name}')
    print(f'  Trades: {m["total_trades"]:3d} | WR: {m["win_rate"]*100:5.1f}% | PF: {m["profit_factor"]:5.2f} | CAGR: {m["cagr"]*100:5.2f}% | DD: {m["max_drawdown"]*100:5.2f}% | PnL: ${m["total_pnl"]:7.2f} | {dt:.0f}s')
    for s, info in sorted(strat_info.items()):
        wr = info['wins']/info['trades']*100 if info['trades'] > 0 else 0
        print(f'    {s:20s}: {info["trades"]:3d}t, WR={wr:5.1f}%, PnL=${info["pnl"]:8.2f}')
    return m

results = []

# All tests with max_positions=5 (v7.0 proven value)
for label, dynsz, sr, maxpos in [
    ('A: v7.0 base (5pos, fixed)', False, False, 5),
    ('B: 8B expand (5pos, fixed)', False, False, 5),
    ('C: 8D only (5pos, dyn)', True, False, 5),
    ('D: 8B+8D (8pos, dyn)', True, False, 8),
    ('E: 8B+8C (5pos, fixed+SR)', False, True, 5),
    ('F: Full 8BCD (5pos, dyn+SR)', True, True, 5),
]:
    cfg = copy.deepcopy(config)
    cfg['risk']['max_open_positions'] = maxpos
    cfg['dynamic_sizing']['enabled'] = dynsz
    strats = [TrendFollowing(cfg), MeanReversion(cfg)]
    if sr:
        strats.append(SectorRotation(cfg))
    print(f'\n>>> {label}')
    m = run_test(label, cfg, strats)
    results.append((label, m))

print('\n' + '='*90)
print('  COMPREHENSIVE COMPARISON')
print('='*90)
print(f'{"Config":>35s} {"Trades":>7s} {"WR%":>7s} {"CAGR%":>8s} {"DD%":>7s} {"PF":>7s} {"PnL$":>9s}')
for label, m in results:
    print(f'{label:>35s} {m["total_trades"]:>7d} {m["win_rate"]*100:>6.1f}% {m["cagr"]*100:>7.2f}% {m["max_drawdown"]*100:>6.2f}% {m["profit_factor"]:>6.2f} {m["total_pnl"]:>8.2f}')
print('='*90)
