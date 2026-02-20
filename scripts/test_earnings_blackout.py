#!/usr/bin/env python3
"""
Earnings Blackout A/B/C Comparison Test
========================================
A: earnings_blackout disabled (mean_reversion)
B: earnings_blackout enabled for mean_reversion only (current config)
C: earnings_blackout enabled for mean_reversion + bb_squeeze

Full 185-ticker universe, max_positions=10 (v9.1 config)
"""
import sys, os, json, copy, time
from pathlib import Path
sys.path.insert(0, '/a0/usr/projects/atlas-asx')
os.chdir('/a0/usr/projects/atlas-asx')

import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture

# ── Load config ────────────────────────────────────────────────────────────────
base_config = json.loads(Path('config/active_config.json').read_text())
print(f"Config: {base_config.get('version','?')}")
print(f"Max positions: {base_config['risk']['max_open_positions']}")

# ── Load full universe ─────────────────────────────────────────────────────────
cache_dir = Path('data/cache')
parquet_files = sorted([f for f in cache_dir.glob('*.parquet') if f.stem != 'IOZ_AX'])
print(f"Loading {len(parquet_files)} tickers...", flush=True)

data_dict = {}
for pf in parquet_files:
    ticker = pf.stem.replace('_AX', '.AX')
    try:
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) >= 100:
            data_dict[ticker] = df
    except Exception as e:
        print(f"  Skip {ticker}: {e}")

print(f"Loaded {len(data_dict)} valid tickers", flush=True)

# ── Strategy factory ───────────────────────────────────────────────────────────
def make_strategies(cfg):
    return [
        MeanReversion(cfg),
        TrendFollowing(cfg),
        BBSqueeze(cfg),
        OpeningGap(cfg),
        DividendCapture(cfg),
    ]

# ── Helper: run backtest ───────────────────────────────────────────────────────
def run_test(label, cfg):
    print(f"\n{'='*60}", flush=True)
    print(f"Running: {label}", flush=True)
    t0 = time.time()
    strategies = make_strategies(cfg)
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, strategies)
    elapsed = time.time() - t0
    m = result.metrics
    # Normalise fractions → percent
    cagr  = m.get('cagr', 0);         cagr  = cagr*100  if abs(cagr) < 2   else cagr
    wr    = m.get('win_rate', 0);     wr    = wr*100    if wr <= 1          else wr
    dd    = m.get('max_drawdown', 0); dd    = dd*100    if abs(dd) < 2      else dd
    pf    = m.get('profit_factor', 0)
    sh    = m.get('sharpe', 0)
    tt    = m.get('total_trades', 0)
    print(f"  {elapsed:.0f}s | Trades:{tt} | CAGR:{cagr:.2f}% | Sharpe:{sh:.3f} | WR:{wr:.1f}% | PF:{pf:.3f} | MaxDD:{dd:.2f}%", flush=True)
    return {'cagr': cagr, 'sharpe': sh, 'win_rate': wr, 'profit_factor': pf,
            'max_drawdown': dd, 'total_trades': tt, 'time_s': elapsed}

# ── Test configs ───────────────────────────────────────────────────────────────
# A: mean_reversion earnings blackout OFF
cfg_a = copy.deepcopy(base_config)
cfg_a['strategies']['mean_reversion']['earnings_blackout']['enabled'] = False

# B: mean_reversion earnings blackout ON (current active state)
cfg_b = copy.deepcopy(base_config)
cfg_b['strategies']['mean_reversion']['earnings_blackout']['enabled'] = True

# C: mean_reversion + bb_squeeze earnings blackout ON
cfg_c = copy.deepcopy(base_config)
cfg_c['strategies']['mean_reversion']['earnings_blackout']['enabled'] = True
cfg_c['strategies']['bb_squeeze']['earnings_blackout'] = {
    'enabled': True, 'days_before': 5, 'days_after': 1
}

print("\nAtlas-ASX Earnings Blackout Comparison")
print("A=blackout OFF | B=mean_rev ON | C=mean_rev+bb_squeeze ON")
print(f"Full universe: {len(data_dict)} tickers, max_positions=10\n")

results = {}
results['A'] = run_test("A: Earnings Blackout OFF", cfg_a)
results['B'] = run_test("B: Mean Reversion Blackout ON", cfg_b)
results['C'] = run_test("C: Mean Rev + BB Squeeze Blackout ON", cfg_c)

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "="*80)
print("RESULTS SUMMARY")
print("="*80)
print(f"{'Metric':<20} {'A (OFF)':>12} {'B (MR ON)':>12} {'C (MR+BB)':>12} {'Best':>6}")
print("-"*65)

for key, label, higher_is_better in [
    ('cagr',          'CAGR %',       True),
    ('sharpe',        'Sharpe',        True),
    ('win_rate',      'Win Rate %',    True),
    ('profit_factor', 'Profit Factor', True),
    ('max_drawdown',  'Max Drawdown%', False),
    ('total_trades',  'Total Trades',  None),
]:
    vals = {k: v[key] for k,v in results.items()}
    if higher_is_better is True:
        best = max(vals, key=lambda x: vals[x])
    elif higher_is_better is False:
        best = min(vals, key=lambda x: vals[x])
    else:
        best = '-'
    a, b, c = vals['A'], vals['B'], vals['C']
    if key in ('cagr','win_rate','max_drawdown'):
        fmt = '{:.2f}%'
    elif key == 'total_trades':
        fmt = '{:.0f}'
    else:
        fmt = '{:.3f}'
    print(f"{label:<20} {fmt.format(a):>12} {fmt.format(b):>12} {fmt.format(c):>12} {best:>6}")

# Delta analysis
print("\n-- Deltas vs A (baseline) --")
for key, label in [('cagr','CAGR'), ('sharpe','Sharpe'), ('win_rate','WR'), ('profit_factor','PF')]:
    db = results['B'][key] - results['A'][key]
    dc = results['C'][key] - results['A'][key]
    sign_b = '+' if db >= 0 else ''
    sign_c = '+' if dc >= 0 else ''
    print(f"  {label}: B={sign_b}{db:.3f}  C={sign_c}{dc:.3f}")

filtered_by_b = int(results['A']['total_trades'] - results['B']['total_trades'])
filtered_by_c = int(results['A']['total_trades'] - results['C']['total_trades'])
print(f"  Trades filtered: B={filtered_by_b}  C={filtered_by_c}")

# ── Determine winner & recommendation ─────────────────────────────────────────
best_sharpe = max(results, key=lambda x: results[x]['sharpe'])
best_cagr   = max(results, key=lambda x: results[x]['cagr'])
print(f"\nBest Sharpe: {best_sharpe} | Best CAGR: {best_cagr}")

# ── Save results ───────────────────────────────────────────────────────────────
out = {
    'test': 'earnings_blackout_comparison',
    'timestamp': pd.Timestamp.now().isoformat(),
    'universe_size': len(data_dict),
    'results': results
}
out_path = Path('backtest/results/earnings_blackout_comparison.json')
out_path.write_text(json.dumps(out, indent=2))
print(f"\nResults saved → {out_path}")
