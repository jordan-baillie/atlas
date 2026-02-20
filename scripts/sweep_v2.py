import json, sys, logging, copy
from pathlib import Path
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')

import pandas as pd
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

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

configs = [
    ('v6.0_current',     0.03, 0.03, 0.015, 0.015),
    ('v6.1a_moderate',   0.03, 0.05, 0.015, 0.030),
    ('v6.1b_aggressive', 0.03, 0.07, 0.015, 0.040),
    ('v6.1c_middle',     0.03, 0.06, 0.015, 0.035),
    ('v6.1d_asymTF',     0.03, 0.08, 0.015, 0.025),
    ('v6.1e_boost_pen',  0.04, 0.06, 0.020, 0.035),
]

all_results = []
for i, (label, tf_lb, tf_hp, mr_lb, mr_hp) in enumerate(configs):
    sys.stdout.write(f'[{i+1}/{len(configs)}] {label}...')
    sys.stdout.flush()
    cfg = copy.deepcopy(base_config)
    cfg['strategies']['trend_following']['breadth']['low_boost'] = tf_lb
    cfg['strategies']['trend_following']['breadth']['high_penalty'] = tf_hp
    cfg['strategies']['mean_reversion']['breadth']['low_boost'] = mr_lb
    cfg['strategies']['mean_reversion']['breadth']['high_penalty'] = mr_hp
    strats = [TrendFollowing(cfg), MeanReversion(cfg)]
    eng = BacktestEngine(cfg)
    res = eng.run_walkforward(data, strats)
    m = res.metrics
    pen = [t for t in res.trades if t.get('features',{}).get('breadth_confidence_adj',0) < 0]
    bst = [t for t in res.trades if t.get('features',{}).get('breadth_confidence_adj',0) > 0]
    r = {
        'label': label, 'tf_lb': tf_lb, 'tf_hp': tf_hp, 'mr_lb': mr_lb, 'mr_hp': mr_hp,
        'cagr': m['cagr'], 'dd': m['max_drawdown'], 'wr': m['win_rate'],
        'pf': m['profit_factor'], 'trades': m['total_trades'],
        'equity': m['final_equity'], 'pnl': m['total_pnl'],
        'sharpe': m.get('sharpe',0), 'sortino': m.get('sortino',0),
        'pen_count': len(pen), 'bst_count': len(bst),
        'pen_pnl': sum(t.get('pnl',0) for t in pen),
        'pen_wins': len([t for t in pen if t.get('pnl',0)>0]),
        'bst_pnl': sum(t.get('pnl',0) for t in bst),
    }
    all_results.append(r)
    print(f' CAGR={m["cagr"]*100:.2f}% DD={m["max_drawdown"]*100:.2f}% WR={m["win_rate"]*100:.1f}% PF={m["profit_factor"]:.2f} T={m["total_trades"]} P={len(pen)}(${r["pen_pnl"]:+.0f})', flush=True)

print('\n' + '='*115)
print(f'{"Config":<20} {"TF_hp":>6} {"MR_hp":>6} {"CAGR%":>7} {"DD%":>6} {"WR%":>6} {"PF":>6} {"Sharpe":>7} {"Trades":>6} {"Equity":>10} {"Pen":>4} {"PenPnL":>8} {"PenWR%":>7}')
print('-'*115)
for r in all_results:
    pwr = (r['pen_wins']/r['pen_count']*100) if r['pen_count']>0 else 0
    print(f'{r["label"]:<20} {r["tf_hp"]:>6.3f} {r["mr_hp"]:>6.3f} {r["cagr"]*100:>7.2f} {r["dd"]*100:>6.2f} {r["wr"]*100:>6.1f} {r["pf"]:>6.2f} {r["sharpe"]:>7.3f} {r["trades"]:>6} {r["equity"]:>10.2f} {r["pen_count"]:>4} {r["pen_pnl"]:>+8.2f} {pwr:>7.1f}')

with open('/a0/usr/projects/atlas-asx/backtest/results/penalty_sweep_results.json','w') as f:
    json.dump(all_results, f, indent=2)
print('\nResults saved.')
