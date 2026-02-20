#!/usr/bin/env python3
"""Diagnostic: Compare trade counts with different strategy combos."""
import json, os, sys, time
import pandas as pd
from pathlib import Path
from collections import Counter
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.opening_gap import OpeningGap
from strategies.bb_squeeze import BBSqueeze

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
    base_cfg = json.load(f)

def run_test(label, strategy_classes):
    import copy
    cfg = copy.deepcopy(base_cfg)
    # Disable all, then enable selected
    for sn in ['mean_reversion', 'trend_following', 'opening_gap', 'bb_squeeze']:
        cfg['strategies'][sn]['enabled'] = False
    class_map = {
        'MeanReversion': 'mean_reversion',
        'TrendFollowing': 'trend_following', 
        'OpeningGap': 'opening_gap',
        'BBSqueeze': 'bb_squeeze'
    }
    for cls in strategy_classes:
        sn = class_map[cls.__name__]
        cfg['strategies'][sn]['enabled'] = True
    
    strategies = [cls(cfg) for cls in strategy_classes]
    engine = BacktestEngine(cfg)
    t0 = time.time()
    result = engine.run_walkforward(data_dict, strategies)
    elapsed = time.time() - t0
    m = result.metrics
    trades = m.get('total_trades', 0)
    cagr = m.get('cagr', 0)
    cagr = cagr * 100 if abs(cagr) < 2 else cagr
    
    strat_breakdown = ""
    if hasattr(result, 'trades') and result.trades:
        counts = Counter(t.get('strategy', '?') for t in result.trades)
        strat_breakdown = ", ".join(f"{k}={v}" for k,v in sorted(counts.items()))
    
    print(f"  {label}: {trades} trades, CAGR={cagr:.2f}%, [{strat_breakdown}] ({elapsed:.0f}s)")
    return trades, cagr

print("\n=== TRADE COUNT DIAGNOSTICS ===")
run_test("MR only", [MeanReversion])
run_test("TF only", [TrendFollowing])
run_test("OG only", [OpeningGap])
run_test("MR+TF", [MeanReversion, TrendFollowing])
run_test("MR+OG", [MeanReversion, OpeningGap])
run_test("TF+OG", [TrendFollowing, OpeningGap])
run_test("MR+TF+OG", [MeanReversion, TrendFollowing, OpeningGap])
print("\nDone.")
