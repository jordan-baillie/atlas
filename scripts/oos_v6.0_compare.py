
import json, sys, logging, copy
from pathlib import Path
logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, '/a0/usr/projects/atlas-asx')
import pandas as pd
from utils.config import get_active_config
from strategies.trend_following import TrendFollowing
from strategies.mean_reversion import MeanReversion
from backtest.engine import BacktestEngine

config = get_active_config()
# Revert to v6.0 penalties for this run only
config['strategies']['trend_following']['breadth']['high_penalty'] = 0.03
config['strategies']['mean_reversion']['breadth']['high_penalty'] = 0.015

cache_dir = Path('/a0/usr/projects/atlas-asx/data/cache')
with open('/a0/usr/projects/atlas-asx/data/processed/universe.json') as f:
    tickers = json.load(f).get('tickers', [])
data = {}
for t in tickers:
    p = cache_dir / (t.replace('.', '_') + '.parquet')
    if p.exists():
        data[t] = pd.read_parquet(p)
print(f'Loaded {len(data)} tickers', flush=True)

strategies = [TrendFollowing(config), MeanReversion(config)]
engine = BacktestEngine(config)
result = engine.run_walkforward(data, strategies)

split_date = pd.Timestamp('2025-03-22')
is_t = [t for t in result.trades if pd.Timestamp(t['entry_date']) < split_date]
oos_t = [t for t in result.trades if pd.Timestamp(t['entry_date']) >= split_date]

def stats(trades):
    if not trades: return {}
    wins = [t for t in trades if t.get('pnl',0)>0]
    losses = [t for t in trades if t.get('pnl',0)<=0]
    gp = sum(t['pnl'] for t in wins)
    gl = abs(sum(t['pnl'] for t in losses))
    return {'n': len(trades), 'wr': len(wins)/len(trades)*100, 'pnl': sum(t['pnl'] for t in trades), 'pf': gp/gl if gl>0 else 999}

is_s = stats(is_t)
oos_s = stats(oos_t)
print(f'v6.0 IS:  T={is_s["n"]} WR={is_s["wr"]:.1f}% PF={is_s["pf"]:.2f} PnL=${is_s["pnl"]:.2f}')
print(f'v6.0 OOS: T={oos_s["n"]} WR={oos_s["wr"]:.1f}% PF={oos_s["pf"]:.2f} PnL=${oos_s["pnl"]:.2f}')
