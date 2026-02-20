#!/usr/bin/env python3
"""Diagnose WHY Opening Gap kills MR/TF trade counts."""
import json, os, sys, time, copy
import pandas as pd
import numpy as np
from pathlib import Path
from collections import Counter, defaultdict
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.opening_gap import OpeningGap

# Load data
print("Loading data...")
data_dict = {}
cache = Path('data/cache')
for pf in sorted(cache.glob('*.parquet')):
    if pf.stem == 'IOZ_AX': continue
    df = pd.read_parquet(pf)
    df.columns = [c.lower() for c in df.columns]
    if 'date' in df.columns:
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')
    df.index = pd.to_datetime(df.index)
    if len(df) < 100: continue
    ticker = pf.stem.replace('_AX', '.AX')
    data_dict[ticker] = df
print(f"Loaded {len(data_dict)} tickers")

with open('config/active_config.json') as f:
    cfg = json.load(f)

# Test 1: Check OG trade durations and P&L
print("\n=== TEST 1: Opening Gap Trade Details ===")
cfg_og = copy.deepcopy(cfg)
for sn in ['mean_reversion', 'trend_following', 'bb_squeeze']:
    cfg_og['strategies'][sn]['enabled'] = False
cfg_og['strategies']['opening_gap']['enabled'] = True

strategies = [OpeningGap(cfg_og)]
engine = BacktestEngine(cfg_og)
result = engine.run_walkforward(data_dict, strategies)
if hasattr(result, 'trades') and result.trades:
    for t in result.trades:
        print(f"  {t.get('ticker','?'):10s} entry={t.get('entry_date','?')} exit={t.get('exit_date','?')} "
              f"hold={t.get('hold_days','?')}d PnL=${t.get('pnl',0):.2f} reason={t.get('exit_reason', t.get('reason','?'))}")
        # Check position value
        shares = t.get('shares', t.get('position_size', 0))
        entry_price = t.get('entry_price', 0)
        print(f"           shares={shares} entry_price=${entry_price:.2f} value=${shares*entry_price:.2f}")

# Test 2: Check MR+TF trade overlap with OG dates
print("\n=== TEST 2: MR+TF trades during OG holding periods ===")
cfg_mrtf = copy.deepcopy(cfg)
for sn in ['bb_squeeze', 'opening_gap']:
    cfg_mrtf['strategies'][sn]['enabled'] = False
cfg_mrtf['strategies']['mean_reversion']['enabled'] = True
cfg_mrtf['strategies']['trend_following']['enabled'] = True

strategies = [MeanReversion(cfg_mrtf), TrendFollowing(cfg_mrtf)]
engine = BacktestEngine(cfg_mrtf)
result_mrtf = engine.run_walkforward(data_dict, strategies)

# Get OG holding periods
og_periods = []
if hasattr(result, 'trades'):
    for t in result.trades:
        entry = pd.Timestamp(t.get('entry_date', '2000-01-01'))
        exit_d = pd.Timestamp(t.get('exit_date', '2000-01-01'))
        og_periods.append((entry, exit_d, t.get('ticker', '?')))

# Count MR+TF trades that overlap OG periods
overlapping = 0
if hasattr(result_mrtf, 'trades'):
    for t in result_mrtf.trades:
        entry = pd.Timestamp(t.get('entry_date', '2000-01-01'))
        for og_start, og_end, og_ticker in og_periods:
            if og_start <= entry <= og_end:
                overlapping += 1
                break
    print(f"  MR+TF total trades: {len(result_mrtf.trades)}")
    print(f"  MR+TF trades during OG holding: {overlapping}")

# Test 3: Check max_daily_drawdown_pct effect
print("\n=== TEST 3: Daily drawdown limiting? ===")
print(f"  max_daily_drawdown_pct: {cfg.get('risk', {}).get('max_daily_drawdown_pct', 'N/A')}")
print(f"  max_open_positions: {cfg.get('risk', {}).get('max_open_positions', 'N/A')}")
print(f"  max_risk_per_trade_pct: {cfg.get('risk', {}).get('max_risk_per_trade_pct', 'N/A')}")
print(f"  min_position_value: {cfg.get('fees', {}).get('min_position_value', 'N/A')}")
print(f"  starting_equity: {cfg.get('risk', {}).get('starting_equity', 'N/A')}")

# Test 4: Check engine signal generation counts
print("\n=== TEST 4: Signal generation count comparison ===")
print("Running MR+TF+OG with debug signal counting...")

# Monkey-patch to count signals generated vs filtered
import types

cfg_all = copy.deepcopy(cfg)
cfg_all['strategies']['bb_squeeze']['enabled'] = False
cfg_all['strategies']['opening_gap']['enabled'] = True
cfg_all['strategies']['mean_reversion']['enabled'] = True
cfg_all['strategies']['trend_following']['enabled'] = True

strategies_all = [MeanReversion(cfg_all), TrendFollowing(cfg_all), OpeningGap(cfg_all)]
engine_all = BacktestEngine(cfg_all)

# Check if engine has signal filtering logic
import inspect
engine_src = inspect.getsource(type(engine_all))

# Look for key filtering mechanisms
filter_keywords = ['max_open', 'max_risk', 'min_position', 'daily_drawdown', 'sector_concentration', 'can_open', 'position_limit']
print("\nEngine filtering mechanisms found:")
for kw in filter_keywords:
    count = engine_src.lower().count(kw.lower())
    if count > 0:
        print(f"  '{kw}': {count} occurrences")

# Find the _simulate_day or equivalent method
for method_name in ['_simulate_day', 'simulate_day', '_process_day', 'process_day']:
    if hasattr(engine_all, method_name):
        method = getattr(engine_all, method_name)
        src = inspect.getsource(method)
        print(f"\n  {method_name} source length: {len(src)} chars")
        # Find position checking logic
        for line_num, line in enumerate(src.split('\n')):
            line_lower = line.strip().lower()
            if any(kw in line_lower for kw in ['open_positions', 'max_open', 'len(self.pos', 'len(positions', 'position_count']):
                print(f"    Line {line_num}: {line.strip()[:120]}")

print("\nDone.")
