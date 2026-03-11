#!/usr/bin/env python3
"""Mean reversion research session — LLM creative cycle."""
import sys, json, time
sys.path.insert(0, '/root/atlas')
from research.loop import ResearchSession, leaderboard, combined_test

def p(msg):
    print(msg, flush=True)

s = ResearchSession('mean_reversion', 'sp500')

p("=== BASELINE ===")
t0 = time.time()
r = s.baseline()
elapsed = time.time() - t0
p(f"Sharpe: {r['sharpe']:.4f}  Trades: {r['total_trades']}  MaxDD: {r['max_drawdown_pct']:.2f}%  PF: {r['profit_factor']:.4f}  CAGR: {r['cagr_pct']:.2f}%  ({elapsed:.0f}s)")
p(f"Best params: {json.dumps(s.best()['params'], indent=None)}")
p("BASELINE_DONE")
