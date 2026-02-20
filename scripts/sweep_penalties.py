import json, sys, logging, copy
from pathlib import Path
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

# Load data once
base_config = get_active_config()
cache_dir = Path('/a0/usr/projects/atlas-asx/data/cache')
with open('/a0/usr/projects/atlas-asx/data/processed/universe.json') as f:
    tickers = json.load(f).get('tickers', [])
data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        data[t] = pd.read_parquet(p)
print(f'Loaded {len(data)} tickers', flush=True)

# Test configurations: (label, tf_low_boost, tf_high_penalty, mr_low_boost, mr_high_penalty)
configs = [
    ('v6.0 current',    0.03, 0.03, 0.015, 0.015),
    ('v6.1a moderate',   0.03, 0.05, 0.015, 0.030),
    ('v6.1b aggressive', 0.03, 0.07, 0.015, 0.040),
    ('v6.1c middle',     0.03, 0.06, 0.015, 0.035),
    ('v6.1d asym-TF',    0.03, 0.08, 0.015, 0.025),
    ('v6.1e boost+pen',  0.04, 0.06, 0.020, 0.035),
]

results = []
for label, tf_lb, tf_hp, mr_lb, mr_hp in configs:
    cfg = copy.deepcopy(base_config)
    cfg['strategies']['trend_following']['breadth']['low_boost'] = tf_lb
    cfg['strategies']['trend_following']['breadth']['high_penalty'] = tf_hp
    cfg['strategies']['mean_reversion']['breadth']['low_boost'] = mr_lb
    cfg['strategies']['mean_reversion']['breadth']['high_penalty'] = mr_hp
    
    strategies = []
    if cfg['strategies']['trend_following'].get('enabled'):
        strategies.append(TrendFollowing(cfg))
    if cfg['strategies']['mean_reversion'].get('enabled'):
        strategies.append(MeanReversion(cfg))
    
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data, strategies)
    m = result.metrics
    
    # Count trade types
    boosted = len([t for t in result.trades if t.get('features',{}).get('breadth_confidence_adj',0) > 0])
    penalized = len([t for t in result.trades if t.get('features',{}).get('breadth_confidence_adj',0) < 0])
    neutral = len([t for t in result.trades if t.get('features',{}).get('breadth_confidence_adj',0) == 0])
    
    pen_trades = [t for t in result.trades if t.get('features',{}).get('breadth_confidence_adj',0) < 0]
    pen_pnl = sum(t.get('pnl',0) for t in pen_trades)
    pen_wins = len([t for t in pen_trades if t.get('pnl',0) > 0])
    boost_pnl = sum(t.get('pnl',0) for t in result.trades if t.get('features',{}).get('breadth_confidence_adj',0) > 0)
    
    results.append({
        'label': label, 'tf_hp': tf_hp, 'mr_hp': mr_hp,
        'cagr': m['cagr'], 'dd': m['max_drawdown'], 'wr': m['win_rate'],
        'pf': m['profit_factor'], 'trades': m['total_trades'],
        'equity': m['final_equity'], 'pnl': m['total_pnl'],
        'sharpe': m.get('sharpe', 0), 'sortino': m.get('sortino', 0),
        'boosted': boosted, 'penalized': penalized, 'neutral': neutral,
        'boost_pnl': boost_pnl, 'pen_pnl': pen_pnl, 'pen_wins': pen_wins
    })
    print(f'{label}: CAGR={m["cagr"]*100:.2f}% DD={m["max_drawdown"]*100:.2f}% '
          f'WR={m["win_rate"]*100:.1f}% PF={m["profit_factor"]:.2f} '
          f'T={m["total_trades"]} Eq=${m["final_equity"]:.2f} '
          f'B={boosted}(${boost_pnl:+.0f}) P={penalized}(${pen_pnl:+.0f}) N={neutral}', flush=True)

print('\n' + '=' * 120)
print(f'{"Config":<22} {"TF_hp":>6} {"MR_hp":>6} {"CAGR%":>7} {"DD%":>6} {"WR%":>6} {"PF":>6} {"Trades":>7} {"Equity":>10} {"Pen":>5} {"PenPnL":>9} {"PenWR":>6}')
print('-' * 120)
for r in results:
    pen_wr = (r['pen_wins']/r['penalized']*100) if r['penalized'] > 0 else 0
    print(f'{r["label"]:<22} {r["tf_hp"]:>6.3f} {r["mr_hp"]:>6.3f} {r["cagr"]*100:>7.2f} {r["dd"]*100:>6.2f} {r["wr"]*100:>6.1f} '
          f'{r["pf"]:>6.2f} {r["trades"]:>7d} {r["equity"]:>10.2f} '
          f'{r["penalized"]:>5d} {r["pen_pnl"]:>+9.2f} {pen_wr:>6.1f}')

# Save
with open('/a0/usr/projects/atlas-asx/backtest/results/penalty_sweep_results.json', 'w') as f:
    json.dump(results, f, indent=2)
print('\nSweep results saved.')
