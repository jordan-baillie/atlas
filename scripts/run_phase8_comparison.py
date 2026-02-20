"""Compare baseline vs 8B (expanded universe) vs 8BCD."""
import json, sys, logging, time
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import load_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from strategies.sector_rotation import SectorRotation
from backtest.engine import BacktestEngine

config = load_config()

# Load ALL available data (expanded universe)
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
print(f'Loaded {len(data)} tickers', flush=True)

def run_test(label, strats, max_pos):
    print(f'\n{"="*60}', flush=True)
    print(f'  {label}', flush=True)
    print(f'{"="*60}', flush=True)
    cfg = json.loads(json.dumps(config))  # deep copy
    cfg['risk']['max_open_positions'] = max_pos
    engine = BacktestEngine(cfg)
    t0 = time.time()
    result = engine.run_walkforward(data, strats)
    dt = time.time() - t0
    
    strat_trades = {}
    for t in result.trades:
        s = t.get('strategy', 'unknown')
        strat_trades.setdefault(s, []).append(t)
    
    print(f'  Time: {dt:.1f}s', flush=True)
    print(f'  Trades: {result.metrics["total_trades"]}', flush=True)
    print(f'  Win Rate: {result.metrics["win_rate"]*100:.1f}%', flush=True)
    print(f'  CAGR: {result.metrics["cagr"]*100:.2f}%', flush=True)
    print(f'  Max DD: {result.metrics["max_drawdown"]*100:.2f}%', flush=True)
    print(f'  PF: {result.metrics["profit_factor"]:.2f}', flush=True)
    print(f'  PnL: ${result.metrics["total_pnl"]:.2f}', flush=True)
    print(f'  Exposure: {result.metrics["exposure"]*100:.1f}%', flush=True)
    
    for strat, trades in sorted(strat_trades.items()):
        wins = sum(1 for t in trades if t.get('pnl', 0) > 0)
        pnl = sum(t.get('pnl', 0) for t in trades)
        wr = wins/len(trades)*100 if trades else 0
        print(f'    {strat}: {len(trades)}t, WR={wr:.1f}%, PnL=${pnl:.2f}', flush=True)
    
    return result

# Test 1: Baseline 2 strategies, 5 positions (on expanded universe)
print('\nTEST 1: TF+MR on expanded universe, 5 positions', flush=True)
r1 = run_test('BASELINE (TF+MR, 5pos)', 
    [TrendFollowing(config), MeanReversion(config)], 5)

# Test 2: Baseline 2 strategies, 8 positions (8B+8C effect)
print('\nTEST 2: TF+MR on expanded universe, 8 positions', flush=True)
r2 = run_test('8B ONLY (TF+MR, 8pos)',
    [TrendFollowing(config), MeanReversion(config)], 8)

# Test 3: All 3 strategies, 8 positions (full 8BCD minus dynamic sizing)
print('\nTEST 3: TF+MR+SR on expanded universe, 8 positions', flush=True)
r3 = run_test('8BCD (TF+MR+SR, 8pos)',
    [TrendFollowing(config), MeanReversion(config), SectorRotation(config)], 8)

print(f'\n{"="*60}', flush=True)
print('COMPARISON SUMMARY', flush=True)
print(f'{"="*60}', flush=True)
print(f'{"Test":<30s} {"Trades":>6s} {"WR%":>6s} {"CAGR%":>7s} {"DD%":>6s} {"PF":>6s} {"PnL$":>8s}', flush=True)
for label, r in [('Baseline TF+MR 5pos', r1), ('8B: TF+MR 8pos', r2), ('8BCD: +SR 8pos', r3)]:
    m = r.metrics
    print(f'{label:<30s} {m["total_trades"]:>6d} {m["win_rate"]*100:>5.1f}% {m["cagr"]*100:>6.2f}% {m["max_drawdown"]*100:>5.2f}% {m["profit_factor"]:>5.2f} {m["total_pnl"]:>7.2f}', flush=True)
