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
print('Running backtest with Phase 7B RS modifier active...', flush=True)
result = engine.run_walkforward(data, strategies)
m = result.metrics
print(f'CAGR={m["cagr"]*100:.2f}% DD={m["max_drawdown"]*100:.2f}% WR={m["win_rate"]*100:.1f}% PF={m["profit_factor"]:.2f} T={m["total_trades"]}', flush=True)
print(f'Equity: ${m["final_equity"]:.2f}', flush=True)
print(f'Baseline v6.0: CAGR=3.24% DD=1.00% WR=60.4% PF=2.65 T=53', flush=True)

rs_boosted = [t for t in result.trades if t.get('features', {}).get('rs_confidence_adj', 0) > 0]
rs_penalized = [t for t in result.trades if t.get('features', {}).get('rs_confidence_adj', 0) < 0]
rs_neutral = [t for t in result.trades if t.get('features', {}).get('rs_confidence_adj', 0) == 0]

print(f'RS impact: Boosted={len(rs_boosted)}, Penalized={len(rs_penalized)}, Neutral={len(rs_neutral)}', flush=True)

for label, group in [('Boosted', rs_boosted), ('Penalized', rs_penalized), ('Neutral', rs_neutral)]:
    if group:
        wins = len([t for t in group if t.get('pnl', 0) > 0])
        pnl = sum(t.get('pnl', 0) for t in group)
        wr = wins / len(group) * 100
        print(f'  {label}: n={len(group)}, WR={wr:.1f}%, PnL=${pnl:.2f}', flush=True)

for strat in ['trend_following', 'mean_reversion']:
    st = [t for t in result.trades if t.get('strategy') == strat]
    wins = len([t for t in st if t.get('pnl', 0) > 0])
    pnl = sum(t.get('pnl', 0) for t in st)
    wr = wins / len(st) * 100 if st else 0
    print(f'  {strat}: n={len(st)}, WR={wr:.1f}%, PnL=${pnl:.2f}', flush=True)
