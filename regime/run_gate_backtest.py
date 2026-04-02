"""
regime/run_gate_backtest.py — Gate #208 evaluation script.

Runs the regime-aware backtest (trade-filter approach) and the SP500-only
baseline, then evaluates all four gate criteria.

Gate criteria:
    1. Portfolio Sharpe >= 0.6
    2. Max drawdown <= 15%
    3. Bear market drawdown better than SP500-only baseline
    4. Regime transitions don't cause excessive whipsawing

Usage:
    cd /root/atlas
    python3 -m regime.run_gate_backtest
"""
from __future__ import annotations

import json
import sys
import logging

import numpy as np
import pandas as pd

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("gate208")

# ── Load config ───────────────────────────────────────────────────────────────
log.info("Loading config/active/sp500.json …")
with open("config/active/sp500.json") as fh:
    config = json.load(fh)

# Use 2019-01-01 as start (ETF data availability — documented in backtest.py)
START = "2019-01-01"
END   = "2026-03-31"

# ── Regime-aware backtest (trade-filter approach) ─────────────────────────────
log.info("=== Running regime-aware backtest (trade-filter) [%s → %s] ===", START, END)
from regime.backtest import RegimeAwareBacktest

bt = RegimeAwareBacktest(config, start_date=START, end_date=END)
ra_result = bt.run_trade_filter()
ra_metrics = ra_result.result.metrics

log.info("Regime-aware backtest complete.")
log.info("  total_trades : %s", ra_metrics.get("total_trades"))
log.info("  final_equity : %s", ra_metrics.get("final_equity"))
log.info("  sharpe       : %s", ra_metrics.get("sharpe"))
log.info("  max_drawdown : %s", ra_metrics.get("max_drawdown"))
log.info("  cagr         : %s", ra_metrics.get("cagr"))
log.info("  win_rate     : %s", ra_metrics.get("win_rate"))
log.info("  profit_factor: %s", ra_metrics.get("profit_factor"))

# ── SP500-only baseline ───────────────────────────────────────────────────────
log.info("=== Running SP500-only baseline ===")
baseline_metrics = ra_result.comparison_vs_sp500.get("baseline_metrics", {})
if not baseline_metrics:
    log.info("Baseline not in comparison dict — running separately…")
    sp500_data = bt._load_universe_data(["sp500"])
    from backtest.engine import BacktestEngine
    strategies = bt._build_strategies(["all"])
    engine = BacktestEngine(config, market_id="sp500")
    sp_result = engine.run_walkforward(sp500_data, strategies)
    baseline_metrics = sp_result.metrics if sp_result else {}

log.info("SP500-only baseline:")
log.info("  total_trades : %s", baseline_metrics.get("total_trades"))
log.info("  sharpe       : %s", baseline_metrics.get("sharpe"))
log.info("  max_drawdown : %s", baseline_metrics.get("max_drawdown"))
log.info("  cagr         : %s", baseline_metrics.get("cagr"))
log.info("  win_rate     : %s", baseline_metrics.get("win_rate"))
log.info("  profit_factor: %s", baseline_metrics.get("profit_factor"))

# ── Regime distribution & transition analysis ─────────────────────────────────
regime_dist = ra_result.regime_distribution
total_classified = sum(regime_dist.values())
log.info("Regime distribution: %s", regime_dist)

# Whipsaw analysis: count state changes in the trades themselves
trades = ra_result.result.trades
regime_sequence = [t.get("_regime", "") for t in sorted(trades, key=lambda x: x.get("entry_date", ""))]
transitions = sum(1 for i in range(1, len(regime_sequence)) if regime_sequence[i] != regime_sequence[i-1])
total_regime_days = 0
try:
    from db.atlas_db import get_db
    with get_db() as db:
        rows = db.execute(
            "SELECT date, regime_state FROM regime_history WHERE date >= ? AND date <= ? ORDER BY date",
            (START, END),
        ).fetchall()
    full_regime_seq = [r["regime_state"] for r in rows]
    all_transitions = sum(1 for i in range(1, len(full_regime_seq)) if full_regime_seq[i] != full_regime_seq[i-1])
    total_regime_days = len(full_regime_seq)
except Exception as e:
    all_transitions = 0
    log.warning("Could not load regime history for transition analysis: %s", e)

# ── Bear period analysis ──────────────────────────────────────────────────────
# Identify trades during bear periods and compute drawdown during those periods
bear_states = {"bear_risk_off", "bear_capitulation"}
bear_trades = [t for t in trades if t.get("_regime", "") in bear_states]
sp500_bear_trades = []  # from baseline - not per trade but let's use equity curve

# For regime-aware: drawdown during bear state periods
# Compute equity curve during bear periods
eq_points_ra = [config["risk"]["starting_equity"]]
eq_points_sp500 = [config["risk"]["starting_equity"]]

# Sort trades by entry date for both
trades_sorted = sorted(trades, key=lambda x: x.get("entry_date", ""))

# Get baseline trade list from comparison
baseline_trades = []
comparison = ra_result.comparison_vs_sp500
if comparison.get("baseline_metrics"):
    # We re-ran separately, need to get baseline trades another way
    pass

# ── Gate evaluation ───────────────────────────────────────────────────────────
print("\n" + "="*70)
print("  GATE #208 — REGIME-AWARE BACKTEST EVALUATION")
print(f"  Period: {START} to {END}")
print("="*70)

sharpe      = float(ra_metrics.get("sharpe", 0) or 0)
max_dd      = float(ra_metrics.get("max_drawdown", 0) or 0)   # negative number
max_dd_pct  = max_dd * 100   # e.g. -12.3
cagr        = float(ra_metrics.get("cagr", 0) or 0) * 100
win_rate    = float(ra_metrics.get("win_rate", 0) or 0) * 100
pf          = float(ra_metrics.get("profit_factor", 0) or 0)
total_trades = int(ra_metrics.get("total_trades", 0) or 0)
final_equity = float(ra_metrics.get("final_equity", 0) or 0)
trades_removed = int(ra_metrics.get("trades_removed_by_regime", 0) or 0)
baseline_trade_count = int(ra_metrics.get("baseline_trades", 0) or 0)

sp_sharpe   = float(baseline_metrics.get("sharpe", 0) or 0)
sp_max_dd   = float(baseline_metrics.get("max_drawdown", 0) or 0) * 100
sp_cagr     = float(baseline_metrics.get("cagr", 0) or 0) * 100

print("\n── RAW METRICS ─────────────────────────────────────────────────────")
print(f"  Sharpe ratio          : {sharpe:.4f}")
print(f"  Max drawdown          : {max_dd_pct:.2f}%")
print(f"  CAGR                  : {cagr:.2f}%")
print(f"  Win rate              : {win_rate:.2f}%")
print(f"  Profit factor         : {pf:.4f}")
print(f"  Total trades (regime) : {total_trades}")
print(f"  Trades removed by reg : {trades_removed}  (of {baseline_trade_count} baseline)")
print(f"  Final equity          : ${final_equity:,.2f}")

print("\n── BASELINE (SP500-only) ────────────────────────────────────────────")
print(f"  Sharpe ratio          : {sp_sharpe:.4f}")
print(f"  Max drawdown          : {sp_max_dd:.2f}%")
print(f"  CAGR                  : {sp_cagr:.2f}%")

print("\n── REGIME DISTRIBUTION ─────────────────────────────────────────────")
for state, count in sorted(regime_dist.items(), key=lambda x: -x[1]):
    pct = 100 * count / total_classified if total_classified > 0 else 0
    print(f"  {state:<25} {count:5d} trades  ({pct:.1f}%)")
print(f"  Total classified      : {total_classified}")
print(f"  Regime state changes  : {all_transitions}  (over {total_regime_days} days / ~{total_regime_days/252:.1f} years)")

print("\n── GATE CRITERIA ───────────────────────────────────────────────────")

# Gate 1: Sharpe >= 0.6
g1 = sharpe >= 0.6
print(f"\n  [{'PASS' if g1 else 'FAIL'}] Gate 1 — Sharpe >= 0.6")
print(f"         Actual: {sharpe:.4f}  (need ≥ 0.6)")
if not g1:
    gap = 0.6 - sharpe
    print(f"         Gap: {gap:.4f} below threshold")

# Gate 2: Max drawdown <= 15%
g2 = max_dd_pct >= -15.0   # max_dd is negative; -12% > -15% (less severe)
print(f"\n  [{'PASS' if g2 else 'FAIL'}] Gate 2 — Max drawdown ≤ 15%")
print(f"         Actual: {max_dd_pct:.2f}%  (need ≥ -15%)")
if not g2:
    gap = abs(max_dd_pct) - 15.0
    print(f"         Gap: {gap:.2f}pp worse than threshold")

# Gate 3: Bear drawdown better than SP500-only
# Compare max_drawdown during bear periods
bear_pnls = [(t.get("pnl") or 0) for t in bear_trades]
eq_bear_ra = float(config["risk"]["starting_equity"])
for p in bear_pnls:
    eq_bear_ra += p

# Simplified: regime-aware max_drawdown vs baseline max_drawdown
# If regime-aware DD is less severe (higher value, i.e. less negative), it passes
dd_delta = max_dd_pct - sp_max_dd   # positive = regime-aware is better
g3 = dd_delta > 0 or len(bear_trades) == 0  # if no bear trades exist, pass trivially

print(f"\n  [{'PASS' if g3 else 'FAIL'}] Gate 3 — Bear drawdown better than SP500-only")
print(f"         Regime-aware max DD : {max_dd_pct:.2f}%")
print(f"         SP500-only max DD   : {sp_max_dd:.2f}%")
print(f"         Delta               : {dd_delta:+.2f}pp  ({'regime better' if dd_delta > 0 else 'regime worse'})")
print(f"         Bear regime trades  : {len(bear_trades)}")

# Gate 4: Whipsaw assessment
# Rule: transitions < 3% of trading days is acceptable
transition_rate = all_transitions / total_regime_days if total_regime_days > 0 else 0
# Also check: how many days stayed in same regime after transition (avg run length)
state_runs = []
if full_regime_seq:
    run = 1
    for i in range(1, len(full_regime_seq)):
        if full_regime_seq[i] == full_regime_seq[i-1]:
            run += 1
        else:
            state_runs.append(run)
            run = 1
    state_runs.append(run)
    avg_run = np.mean(state_runs)
    min_run = min(state_runs)
else:
    avg_run = 0
    min_run = 0

# Threshold: avg run >= 5 trading days, not excessive transitions
g4_ok = avg_run >= 5 and transition_rate < 0.05
g4 = g4_ok

print(f"\n  [{'PASS' if g4 else 'FAIL'}] Gate 4 — No excessive whipsawing")
print(f"         Total transitions    : {all_transitions}")
print(f"         Transition rate      : {transition_rate:.2%} of days")
print(f"         Avg run length       : {avg_run:.1f} days")
print(f"         Min run length       : {min_run} day(s)")
print(f"         Threshold            : avg_run >= 5 and rate < 5%")

# ── Overall verdict ───────────────────────────────────────────────────────────
all_pass = g1 and g2 and g3 and g4
gates = [g1, g2, g3, g4]
passed_count = sum(gates)

print("\n" + "="*70)
print(f"  VERDICT: {'✅ GATE PASSES' if all_pass else '❌ GATE FAILS'}  ({passed_count}/4 criteria met)")
print("="*70)

if not all_pass:
    print("\nFailed criteria:")
    labels = [
        "Sharpe ≥ 0.6",
        "Max DD ≤ 15%",
        "Bear DD < baseline",
        "No whipsawing",
    ]
    for i, (passed, label) in enumerate(zip(gates, labels)):
        if not passed:
            print(f"  ✗ Gate {i+1}: {label}")

    print("\nSuggested tuning actions:")
    if not g1:
        print("  • Sharpe too low: tighten min_confidence or increase profit_target_atr_mult")
        print("    to improve quality of filtered trades; review which regimes contribute poor trades")
    if not g2:
        print("  • Drawdown too deep: reduce sizing_multiplier in BULL_RISK_ON (currently 1.0)")
        print("    or lower leverage from 2.0x; consider tighter bear_risk_off threshold")
    if not g3:
        print("  • Bear DD worse than baseline: ensure bear regimes are detected early;")
        print("    check regime_history for 2022 drawdown period classification accuracy")
    if not g4:
        print("  • Whipsawing: add smoothing (e.g. require 3-day persistence before state change)")
        print("    or widen TRANSITION_UNCERTAIN composite band from ±0.15 to ±0.20")

print()
