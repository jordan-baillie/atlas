#!/usr/bin/env python3
"""
Earnings Blackout A/B Comparison with Estimated Dates (v2)
===================================================
A: earnings_blackout disabled
B: earnings_blackout enabled using new two-tier (precise + estimated) dates

Expected: now that we have 1,847 dates (vs 56), blackout should filter trades.
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

# Verify earnings cache stats
earnings_dir = Path('data/cache/earnings')
files = list(earnings_dir.glob('*.json'))
total_precise = 0
total_estimated = 0
for f in files:
    try:
        d = json.loads(f.read_text())
        total_precise += len(d.get('dates', []))
        total_estimated += len(d.get('estimated_dates', []))
    except: pass
print(f"Earnings cache: {len(files)} files, {total_precise} precise + {total_estimated} estimated = {total_precise+total_estimated} total dates")

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
    cagr  = m.get('cagr', 0);         cagr  = cagr*100  if abs(cagr) < 2   else cagr
    wr    = m.get('win_rate', 0);     wr    = wr*100    if wr <= 1          else wr
    dd    = m.get('max_drawdown', 0); dd    = dd*100    if abs(dd) < 2      else dd
    pf    = m.get('profit_factor', 0)
    sh    = m.get('sharpe', 0)
    tt    = m.get('total_trades', 0)
    print(f"  {elapsed:.0f}s | Trades:{tt} | CAGR:{cagr:.2f}% | Sharpe:{sh:.3f} | WR:{wr:.1f}% | PF:{pf:.3f} | MaxDD:{dd:.2f}%", flush=True)
    return {'cagr': cagr, 'sharpe': sh, 'win_rate': wr, 'profit_factor': pf,
            'max_drawdown': dd, 'total_trades': tt, 'time_s': round(elapsed, 1)}

# ── Config A: earnings blackout DISABLED ───────────────────────────────────────
cfg_a = copy.deepcopy(base_config)
cfg_a['strategies']['mean_reversion']['earnings_blackout']['enabled'] = False
cfg_a['strategies']['bb_squeeze']['earnings_blackout']['enabled'] = False

# ── Config B: earnings blackout ENABLED (estimated dates now available) ────────
cfg_b = copy.deepcopy(base_config)
cfg_b['strategies']['mean_reversion']['earnings_blackout']['enabled'] = True
cfg_b['strategies']['mean_reversion']['earnings_blackout']['days_before'] = 5
cfg_b['strategies']['mean_reversion']['earnings_blackout']['days_after'] = 1
cfg_b['strategies']['bb_squeeze']['earnings_blackout']['enabled'] = True
cfg_b['strategies']['bb_squeeze']['earnings_blackout']['days_before'] = 5
cfg_b['strategies']['bb_squeeze']['earnings_blackout']['days_after'] = 1

results = {}
results['A_no_blackout'] = run_test('A: No Blackout (baseline)', cfg_a)
results['B_with_blackout'] = run_test('B: Blackout Enabled (estimated dates)', cfg_b)

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("SUMMARY")
print("="*60)
print(f"{'Metric':<20} {'A (No Blackout)':>16} {'B (Blackout On)':>16} {'Delta':>10}")
print("-"*65)
for k, label in [('total_trades','Trades'), ('cagr','CAGR%'), ('sharpe','Sharpe'),
                 ('win_rate','WinRate%'), ('profit_factor','PF'), ('max_drawdown','MaxDD%')]:
    a = results['A_no_blackout'].get(k, 0)
    b = results['B_with_blackout'].get(k, 0)
    delta = b - a
    print(f"{label:<20} {a:>16.3f} {b:>16.3f} {delta:>+10.3f}")

trades_filtered = results['A_no_blackout']['total_trades'] - results['B_with_blackout']['total_trades']
print(f"\nTrades filtered by blackout: {trades_filtered}")
if trades_filtered > 0:
    pct = trades_filtered / results['A_no_blackout']['total_trades'] * 100
    print(f"Filter rate: {pct:.1f}%")

# Save results
out_path = Path('backtest/results/earnings_blackout_v2_estimated.json')
out_path.write_text(json.dumps({'A_no_blackout': results['A_no_blackout'],
                                 'B_with_blackout': results['B_with_blackout'],
                                 'trades_filtered': trades_filtered,
                                 'earnings_cache': {'files': len(files), 'precise': total_precise, 'estimated': total_estimated}},
                                indent=2))
print(f"\nSaved: {out_path}")
