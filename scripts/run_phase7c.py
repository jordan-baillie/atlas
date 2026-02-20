import json, sys, logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

config = get_active_config()
print(f'Config: {config["version"]}', flush=True)

cache_dir = Path('/a0/usr/projects/atlas-asx/data/cache')
with open('/a0/usr/projects/atlas-asx/data/processed/universe.json') as f:
    tickers = json.load(f).get('tickers', [])

data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        data[t] = pd.read_parquet(p)
print(f'Loaded {len(data)} tickers', flush=True)

strategies = []
if config['strategies']['trend_following'].get('enabled'):
    strategies.append(TrendFollowing(config))
if config['strategies']['mean_reversion'].get('enabled'):
    strategies.append(MeanReversion(config))
print(f'Strategies: {[s.name for s in strategies]}', flush=True)
print('Running backtest...', flush=True)

engine = BacktestEngine(config)
result = engine.run_walkforward(data, strategies)
m = result.metrics

print(flush=True)
print('=' * 52)
print('  PHASE 7C RESULTS (v6.0 breadth modifiers)')
print('=' * 52)
for k, v in m.items():
    if isinstance(v, float):
        print(f'  {k:<20}: {v:.4f}')
    else:
        print(f'  {k:<20}: {v}')

print()
print('=' * 52)
print('  BASELINE COMPARISON')
print('=' * 52)
bl = {'cagr': 0.0319, 'max_drawdown': 0.0119, 'win_rate': 0.617,
      'profit_factor': 2.77, 'sharpe': 0.846, 'sortino': 1.538,
      'total_trades': 47, 'final_equity': 5326.29}

fmt = [
    ('cagr',          'pct'),
    ('max_drawdown',  'pct'),
    ('win_rate',      'pct'),
    ('profit_factor', 'dec'),
    ('sharpe',        'dec'),
    ('sortino',       'dec'),
    ('total_trades',  'int'),
    ('final_equity',  'usd'),
]
for k, style in fmt:
    bv = bl[k]
    nv = m[k]
    if style == 'pct':
        print(f'  {k:<20}: {bv*100:>7.2f}% -> {nv*100:>7.2f}%  ({(nv-bv)*100:+.2f}%)')
    elif style == 'dec':
        print(f'  {k:<20}: {bv:>8.3f} -> {nv:>8.3f}  ({nv-bv:+.3f})')
    elif style == 'int':
        print(f'  {k:<20}: {int(bv):>8d} -> {int(nv):>8d}  ({int(nv)-int(bv):+d})')
    elif style == 'usd':
        print(f'  {k:<20}: ${bv:>8.2f} -> ${nv:>8.2f}  (${nv-bv:+.2f})')

# Breadth adjustment analysis
print()
print('=' * 52)
print('  BREADTH ADJUSTMENT DETAILS')
print('=' * 52)
adj_trades = [t for t in result.trades if 'breadth_confidence_adj' in t.get('features', {})]
non_adj = [t for t in result.trades if 'breadth_confidence_adj' not in t.get('features', {})]
print(f'Trades with breadth adj: {len(adj_trades)}/{len(result.trades)}')
print(f'Trades without adj (neutral zone): {len(non_adj)}/{len(result.trades)}')
print()
for t in adj_trades:
    f = t.get('features', {})
    pnl = t.get('pnl', t.get('total_pnl', 0))
    outcome = 'WIN ' if pnl > 0 else 'LOSS'
    bval = f.get('breadth_pct_above_50ma', 0)
    print(f'  {t["ticker"]:<10} {t["strategy"]:<18} adj={f["breadth_confidence_adj"]:+.4f} '
          f'orig={f.get("breadth_confidence_orig","?"):.3f} '
          f'new={t.get("confidence","?"):.3f} '
          f'breadth={bval:.2f} {outcome} pnl=${pnl:.2f}')

# Count boosted vs penalized
boosted = [t for t in adj_trades if t['features']['breadth_confidence_adj'] > 0]
penalized = [t for t in adj_trades if t['features']['breadth_confidence_adj'] < 0]
print(f'\nBoosted (low breadth): {len(boosted)}')
print(f'Penalized (high breadth): {len(penalized)}')

# Were any trades filtered out by confidence threshold due to penalty?
print(f'\nMin confidence threshold: {config["risk"]["min_confidence"]}')

# Save results
result_data = {'version': config['version'], 'metrics': m, 'trades': result.trades}
with open('/a0/usr/projects/atlas-asx/backtest/results/backtest_phase7c_v6.0.json', 'w') as f:
    json.dump(result_data, f, indent=2, default=str)
print(f'\nResults saved.')
