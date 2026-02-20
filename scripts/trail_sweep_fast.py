#!/usr/bin/env python3
"""
Trailing Stop Sweep — Fast (25-ticker subset)
Tests activation_pct × atr_multiplier combos to find optimal trailing stop params.
Uses top-25 tickers by volume (same method as optimize_strategies.py).
"""
import sys, json, copy, time
from pathlib import Path
from datetime import datetime

PROJ = Path('/a0/usr/projects/atlas-asx')
sys.path.insert(0, str(PROJ))
import os; os.chdir(PROJ)

import pandas as pd
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap
from strategies.dividend_capture import DividendCapture

# ── 1. Load top-25 tickers by volume (same method as optimize_strategies.py) ──
print("Loading top-25 tickers by volume...", flush=True)
all_data = {}
for f in sorted(PROJ.joinpath('data/cache').glob('*.parquet')):
    if f.stem == 'IOZ_AX':
        continue
    ticker = f.stem.replace('_', '.')
    try:
        df = pd.read_parquet(f)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) >= 252:
            all_data[ticker] = df
    except Exception as e:
        pass

vols = {}
for t, df in all_data.items():
    if 'volume' in df.columns:
        vols[t] = df['volume'].tail(252).mean()

top25 = sorted(vols, key=vols.get, reverse=True)[:25]
data = {t: all_data[t] for t in top25}
print(f"Top-25 tickers: {top25[:5]} ... ({len(data)} total)", flush=True)

# ── 2. Base config ──
cfg_base = json.loads((PROJ / 'config/active_config.json').read_text())
print(f"Config: {cfg_base.get('version','?')}", flush=True)

# ── 3. Parameter grid ──
configs = [
    {"label": "BASELINE",          "enabled": False, "act": None,  "mult": None},
    {"label": "act=2% mult=1.0x",  "enabled": True,  "act": 0.02,  "mult": 1.0},
    {"label": "act=2% mult=1.5x",  "enabled": True,  "act": 0.02,  "mult": 1.5},
    {"label": "act=3% mult=1.5x",  "enabled": True,  "act": 0.03,  "mult": 1.5},
    {"label": "act=3% mult=2.0x",  "enabled": True,  "act": 0.03,  "mult": 2.0},
    {"label": "act=4% mult=1.5x",  "enabled": True,  "act": 0.04,  "mult": 1.5},
    {"label": "act=4% mult=2.0x",  "enabled": True,  "act": 0.04,  "mult": 2.0},
    {"label": "act=5% mult=2.5x",  "enabled": True,  "act": 0.05,  "mult": 2.5},
]

# ── 4. Run sweep ──
results = []
for i, combo in enumerate(configs):
    cfg = copy.deepcopy(cfg_base)
    trail_cfg = {"enabled": combo["enabled"]}
    if combo["enabled"]:
        trail_cfg["activation_pct"] = combo["act"]
        trail_cfg["atr_multiplier"] = combo["mult"]
    cfg.setdefault("risk", {})["trailing_stop"] = trail_cfg

    strategies = [
        MeanReversion(cfg), TrendFollowing(cfg),
        BBSqueeze(cfg), OpeningGap(cfg), DividendCapture(cfg),
    ]
    engine = BacktestEngine(cfg)

    print(f"\n[{i+1}/{len(configs)}] {combo['label']}...", flush=True)
    t0 = time.time()
    r = engine.run_walkforward(data, strategies)
    elapsed = time.time() - t0
    m = r.metrics

    cagr = m.get('cagr', 0)
    if abs(cagr) < 2:
        cagr *= 100

    trail_exits = sum(1 for t in r.trades if t.get('exit_reason') == 'trailing_stop')
    row = {
        "label": combo["label"],
        "act": combo["act"],
        "mult": combo["mult"],
        "trades": m.get('total_trades', 0),
        "trail_exits": trail_exits,
        "wr_pct": round(m.get('win_rate', 0) * 100, 1),
        "cagr_pct": round(cagr, 3),
        "sharpe": round(m.get('sharpe', 0), 4),
        "pf": round(m.get('profit_factor', 0), 4),
        "maxdd_pct": round(m.get('max_drawdown', 0) * 100, 2),
        "elapsed_s": round(elapsed, 1),
    }
    results.append(row)
    print(f"  ✅ {elapsed:.0f}s | Trades={row['trades']} (trail={trail_exits}) | "
          f"CAGR={cagr:+.2f}% | Sharpe={row['sharpe']:+.4f} | "
          f"PF={row['pf']:.4f} | MaxDD={row['maxdd_pct']:.2f}% | WR={row['wr_pct']}%",
          flush=True)

# ── 5. Summary ──
print("\n" + "="*90)
print(f"{'Label':<22} {'Trades':>7} {'Trail':>6} {'WR%':>5} {'CAGR%':>8} {'Sharpe':>8} {'PF':>7} {'MaxDD%':>8}")
print("-"*90)
for row in results:
    marker = " ◀ BASELINE" if row['label'] == 'BASELINE' else ""
    print(f"{row['label']:<22} {row['trades']:>7} {row['trail_exits']:>6} "
          f"{row['wr_pct']:>5.1f} {row['cagr_pct']:>+8.3f} {row['sharpe']:>+8.4f} "
          f"{row['pf']:>7.4f} {row['maxdd_pct']:>8.2f}{marker}")

# Identify winner vs baseline by Sharpe improvement
baseline = next(r for r in results if r['label'] == 'BASELINE')
best = max([r for r in results if r['label'] != 'BASELINE'],
           key=lambda r: r['sharpe'])
print("="*90)
print(f"\nBest trailing config: {best['label']}")
print(f"  Sharpe:  {baseline['sharpe']:+.4f} → {best['sharpe']:+.4f}  (Δ{best['sharpe']-baseline['sharpe']:+.4f})")
print(f"  CAGR:    {baseline['cagr_pct']:+.3f}% → {best['cagr_pct']:+.3f}%")
print(f"  MaxDD:   {baseline['maxdd_pct']:.2f}% → {best['maxdd_pct']:.2f}%")
print(f"  Trailing exits: {best['trail_exits']} trades")

# ── 6. Save results ──
out = {
    "timestamp": datetime.now().isoformat(),
    "n_tickers": len(data),
    "tickers": top25,
    "results": results,
    "best": best,
    "baseline": baseline,
}
out_path = PROJ / 'backtest/results/trailing_stop_sweep.json'
out_path.write_text(json.dumps(out, indent=2))
print(f"\nResults saved → {out_path}")
print("\nNext step: validate best config on full 185-ticker universe.")
