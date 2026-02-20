#!/usr/bin/env python3
import sys, json, os, copy, time, logging
from pathlib import Path
PROJECT_ROOT = Path('/a0/usr/projects/atlas-asx')
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(str(PROJECT_ROOT))
import pandas as pd
import numpy as np
from datetime import datetime
logging.basicConfig(level=logging.WARNING)

from utils.config import get_active_config
from strategies.dividend_capture import DividendCapture
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from backtest.engine import BacktestEngine

config = get_active_config()

# Load top 30 tickers by volume
all_data = {}
for f in (PROJECT_ROOT / 'data' / 'cache').glob('*.parquet'):
    ticker = f.stem.replace('_', '.')
    df = pd.read_parquet(f)
    df.columns = [c.lower() for c in df.columns]
    if len(df) >= 252:
        all_data[ticker] = df

vols = {}
for t, df in all_data.items():
    if 'volume' in df.columns:
        vols[t] = df['volume'].tail(252).mean()
top30 = sorted(vols, key=vols.get, reverse=True)[:30]
data = {t: all_data[t] for t in top30}
print('Tickers: {}'.format(len(data)))

results = {}

# TEST A: 4 strategies baseline
cfg_a = copy.deepcopy(config)
cfg_a['strategies']['dividend_capture']['enabled'] = False
strats_a = []
for Cls, nm in [(MeanReversion,'mean_reversion'), (TrendFollowing,'trend_following'),
                (BBSqueeze,'bb_squeeze'), (OpeningGap,'opening_gap')]:
    if cfg_a['strategies'].get(nm,{}).get('enabled',False):
        strats_a.append(Cls(cfg_a))
print('TEST A: {} strategies'.format(len(strats_a)))
t0 = time.time()
res_a = BacktestEngine(cfg_a).run_walkforward(data, strats_a)
ma = res_a.metrics
print('A done {:.0f}s: trades={} CAGR={:.2f}%'.format(time.time()-t0, ma['total_trades'], ma['cagr']*100))
results['baseline'] = {'trades': ma['total_trades'], 'cagr': ma['cagr'], 'sharpe': ma['sharpe'], 'max_dd': ma['max_drawdown'], 'pf': ma.get('profit_factor',0)}

# TEST B: 5 strategies with dividend capture
cfg_b = copy.deepcopy(config)
cfg_b['strategies']['dividend_capture']['enabled'] = True
strats_b = []
for Cls, nm in [(MeanReversion,'mean_reversion'), (TrendFollowing,'trend_following'),
                (BBSqueeze,'bb_squeeze'), (OpeningGap,'opening_gap'),
                (DividendCapture,'dividend_capture')]:
    if cfg_b['strategies'].get(nm,{}).get('enabled',False):
        strats_b.append(Cls(cfg_b))
print('TEST B: {} strategies'.format(len(strats_b)))
t0 = time.time()
res_b = BacktestEngine(cfg_b).run_walkforward(data, strats_b)
mb = res_b.metrics
print('B done {:.0f}s: trades={} CAGR={:.2f}%'.format(time.time()-t0, mb['total_trades'], mb['cagr']*100))
results['combined'] = {'trades': mb['total_trades'], 'cagr': mb['cagr'], 'sharpe': mb['sharpe'], 'max_dd': mb['max_drawdown'], 'pf': mb.get('profit_factor',0)}

# Trade breakdown
for label, res in [('baseline', res_a), ('combined', res_b)]:
    by_s = {}
    for t in res.trades:
        s = t.get('strategy','unknown')
        by_s[s] = by_s.get(s, 0) + 1
    results[label]['by_strategy'] = by_s
    print('{} by strategy: {}'.format(label, by_s))

results['timestamp'] = datetime.now().isoformat()
with open('backtest/results/dividend_portfolio_comparison.json', 'w') as f:
    json.dump(results, f, indent=2)
print('Saved to backtest/results/dividend_portfolio_comparison.json')

print('\n=== COMPARISON ===')
print('Metric          | Baseline (4-strat) | Combined (5-strat)')
print('Trades          | {:>18} | {:>18}'.format(results['baseline']['trades'], results['combined']['trades']))
print('CAGR            | {:>17.2f}% | {:>17.2f}%'.format(results['baseline']['cagr']*100, results['combined']['cagr']*100))
print('Sharpe          | {:>18.3f} | {:>18.3f}'.format(results['baseline']['sharpe'], results['combined']['sharpe']))
print('Max DD          | {:>17.2f}% | {:>17.2f}%'.format(results['baseline']['max_dd']*100, results['combined']['max_dd']*100))
