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
print('Running backtest with Phase 7B RS integration...', flush=True)
result = engine.run_walkforward(data, strategies)
m = result.metrics
print(f'CAGR={m["cagr"]*100:.2f}% DD={m["max_drawdown"]*100:.2f}% WR={m["win_rate"]*100:.1f}% PF={m["profit_factor"]:.2f} T={m["total_trades"]}', flush=True)
print(f'Equity: ${m["final_equity"]:.2f}', flush=True)

rs_count = 0
for t in result.trades:
    if 'rs_percentile' in t.get('features', {}):
        rs_count += 1
print(f'Trades with RS data: {rs_count}/{len(result.trades)}', flush=True)

for t in result.trades:
    feats = t.get('features', {})
    if 'rs_percentile' in feats:
        print(f'Sample ({t["ticker"]}): RS%={feats.get("rs_percentile")}, score={feats.get("rs_score")}, mom={feats.get("rs_momentum")}, roc20={feats.get("roc_20")}, roc60={feats.get("roc_60")}, roc120={feats.get("roc_120")}', flush=True)
        break

print(f'Baseline: CAGR=3.24% DD=1.00% WR=60.4% PF=2.65 T=53', flush=True)

trades_out = []
for t in result.trades:
    td = {k: v for k, v in t.items()}
    for k in ['entry_date', 'exit_date']:
        if k in td and hasattr(td[k], 'isoformat'):
            td[k] = td[k].isoformat()
    trades_out.append(td)
with open('/a0/usr/projects/atlas-asx/backtest/results/trades_with_rs.json', 'w') as f:
    json.dump(trades_out, f, indent=2, default=str)
print('Trades saved.', flush=True)
