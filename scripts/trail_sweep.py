"""
Trailing Stop A/B Sweep — activation_pct x atr_multiplier grid
Full 185-ticker universe, same strategies as active config.
"""
import json, sys, time, copy
from pathlib import Path
import pandas as pd

sys.path.insert(0, '/a0/usr/projects/atlas-asx')

from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture

PROJ = Path('/a0/usr/projects/atlas-asx')
CFG_PATH = PROJ / 'config/active_config.json'
RESULT_PATH = PROJ / 'backtest/results/trailing_stop_sweep.json'

cfg = json.loads(CFG_PATH.read_text())

# ─── Load data once ───────────────────────────────────────────────
print("Loading parquet data...", flush=True)
data = {}
for pf in sorted((PROJ / 'data/cache').glob('*.parquet')):
    if pf.stem == 'IOZ_AX':
        continue  # benchmark excluded from strategy data
    try:
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        ticker = pf.stem.replace('_AX', '.AX')
        data[ticker] = df
    except Exception as e:
        print(f"  skip {pf.stem}: {e}")
print(f"Loaded {len(data)} tickers", flush=True)

# ─── Run one config ───────────────────────────────────────────────
def run_config(label, trail_cfg):
    c = copy.deepcopy(cfg)
    c.setdefault('risk', {})
    c['risk']['trailing_stop'] = trail_cfg

    strategies = [
        MeanReversion(c),
        TrendFollowing(c),
        BBSqueeze(c),
        OpeningGap(c),
        DividendCapture(c),
    ]
    engine = BacktestEngine(c)
    t0 = time.time()
    result = engine.run_walkforward(data, strategies)
    elapsed = round(time.time() - t0, 1)
    m = result.metrics

    # Normalise fraction-stored metrics
    cagr    = m.get('cagr', 0);       cagr    = cagr*100    if abs(cagr) < 2 else cagr
    mdd     = m.get('max_drawdown',0); mdd     = mdd*100     if abs(mdd)  < 2 else mdd
    wr      = m.get('win_rate', 0);   wr      = wr*100      if wr <= 1   else wr

    trail_exits = sum(1 for t in (result.trades or []) if t.get('exit_reason') == 'trailing_stop')

    print(f"  {label:40s}  CAGR={cagr:+6.2f}%  Sharpe={m.get('sharpe',0):+6.3f}  "
          f"MaxDD={mdd:5.2f}%  PF={m.get('profit_factor',0):.3f}  "
          f"Trades={m.get('total_trades',0)}  TrailExits={trail_exits}  [{elapsed}s]",
          flush=True)
    return {
        'label':          label,
        'config':         trail_cfg,
        'cagr':           round(cagr, 4),
        'sharpe':         round(m.get('sharpe', 0), 4),
        'max_drawdown':   round(mdd, 4),
        'profit_factor':  round(m.get('profit_factor', 0), 4),
        'win_rate':       round(wr, 2),
        'total_trades':   m.get('total_trades', 0),
        'total_pnl':      round(m.get('total_pnl', 0), 2),
        'trailing_exits': trail_exits,
        'elapsed_s':      elapsed,
    }

results = []

# ─── Baseline ─────────────────────────────────────────────────────
print("\n[A] BASELINE — no trailing stop", flush=True)
results.append(run_config('baseline', {'enabled': False}))
baseline = results[0]

# ─── Grid sweep ───────────────────────────────────────────────────
activations  = [0.02, 0.03, 0.04, 0.05]
multipliers  = [1.0,  1.5,  2.0,  2.5]

print("\n[B] PARAMETER SWEEP (activation_pct × atr_multiplier)", flush=True)
for act in activations:
    for mult in multipliers:
        label = f'act={int(act*100)}% mult={mult:.1f}x'
        res = run_config(label, {
            'enabled':        True,
            'activation_pct': act,
            'atr_multiplier': mult,
        })
        results.append(res)
        # Save incrementally
        RESULT_PATH.write_text(json.dumps({'baseline': baseline, 'sweep': results[1:]}, indent=2))

# ─── Summary ──────────────────────────────────────────────────────
sweep_only = results[1:]
best_sharpe = sorted(sweep_only, key=lambda x: x['sharpe'],      reverse=True)
best_cagr   = sorted(sweep_only, key=lambda x: x['cagr'],        reverse=True)

print("\n" + "="*75)
print(f"BASELINE  CAGR={baseline['cagr']:+.2f}%  Sharpe={baseline['sharpe']:+.3f}  "
      f"MaxDD={baseline['max_drawdown']:.2f}%  PF={baseline['profit_factor']:.3f}")
print("="*75)
print("TOP 5 BY SHARPE:")
for r in best_sharpe[:5]:
    print(f"  {r['label']:35s}  Sharpe {baseline['sharpe']:+.3f}→{r['sharpe']:+.3f} "
          f"({r['sharpe']-baseline['sharpe']:+.3f})  "
          f"CAGR {r['cagr']:+.2f}%  DD {r['max_drawdown']:.2f}%  "
          f"TrailExits={r['trailing_exits']}")

print("\nTOP 5 BY CAGR:")
for r in best_cagr[:5]:
    print(f"  {r['label']:35s}  CAGR {baseline['cagr']:+.2f}%→{r['cagr']:+.2f}% "
          f"({r['cagr']-baseline['cagr']:+.2f}%)  "
          f"Sharpe {r['sharpe']:+.3f}  DD {r['max_drawdown']:.2f}%")

best = best_sharpe[0] if best_sharpe else None
print(f"\n✅ Best overall: {best['label'] if best else 'N/A'}")
print(f"   Results saved → {RESULT_PATH}")
