"""
Gate #208 backtest runner — writes results to /tmp/gate208_result.json
Run via systemd to avoid process kill on bash tool timeout.
"""
from __future__ import annotations
import json, logging, sys
import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.WARNING,   # suppress signal-level noise
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
# Only show our own progress messages
log = logging.getLogger("gate208")
log.setLevel(logging.INFO)

RESULT_PATH = "/tmp/gate208_result.json"

log.info("Loading config …")
with open("config/active/sp500.json") as fh:
    config = json.load(fh)

START = "2019-01-01"
END   = "2026-03-31"

log.info("Running regime-aware backtest (trade-filter) [%s → %s] …", START, END)
from regime.backtest import RegimeAwareBacktest

bt = RegimeAwareBacktest(config, start_date=START, end_date=END)
ra_result = bt.run_trade_filter()
ra_metrics = ra_result.result.metrics

log.info("Regime-aware complete — trades=%s sharpe=%s max_dd=%s",
         ra_metrics.get("total_trades"), ra_metrics.get("sharpe"), ra_metrics.get("max_drawdown"))

# ── Regime distribution & whipsaw analysis ────────────────────────────────────
regime_dist = ra_result.regime_distribution
log.info("Regime dist: %s", regime_dist)

try:
    from db.atlas_db import get_db
    from regime.backtest import RegimeAwareBacktest
    with get_db() as db:
        rows = db.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (START, END),
        ).fetchall()
    # Apply same 3-day persistence smoothing used in the backtest
    raw_regime_map = {r["date"]: r["regime_state"] for r in rows}
    smoothed_map = RegimeAwareBacktest._smooth_regime_map(raw_regime_map, min_persistence_days=3)
    full_regime_seq = [smoothed_map[d] for d in sorted(smoothed_map.keys())]
    all_transitions = sum(1 for i in range(1, len(full_regime_seq)) if full_regime_seq[i] != full_regime_seq[i-1])
    total_regime_days = len(full_regime_seq)
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
except Exception as e:
    log.warning("Transition analysis failed: %s", e)
    all_transitions = 0
    total_regime_days = 0
    state_runs = []

# ── Gate evaluation ────────────────────────────────────────────────────────────
sharpe    = float(ra_metrics.get("sharpe", 0) or 0)
max_dd    = float(ra_metrics.get("max_drawdown", 0) or 0)
max_dd_pct = max_dd * 100
cagr      = float(ra_metrics.get("cagr", 0) or 0) * 100
win_rate  = float(ra_metrics.get("win_rate", 0) or 0) * 100
pf        = float(ra_metrics.get("profit_factor", 0) or 0)
total_trades = int(ra_metrics.get("total_trades", 0) or 0)
final_equity = float(ra_metrics.get("final_equity", 0) or 0)
trades_removed = int(ra_metrics.get("trades_removed_by_regime", 0) or 0)
baseline_count = int(ra_metrics.get("baseline_trades", 0) or 0)

baseline_metrics = ra_result.comparison_vs_sp500.get("baseline_metrics", {})
sp_sharpe = float(baseline_metrics.get("sharpe", 0) or 0)
sp_max_dd = float(baseline_metrics.get("max_drawdown", 0) or 0) * 100
sp_cagr   = float(baseline_metrics.get("cagr", 0) or 0) * 100

# Bear trades
bear_trades = [t for t in ra_result.result.trades if t.get("_regime", "") in {"bear_risk_off", "bear_capitulation"}]

# Whipsaw
avg_run = float(np.mean(state_runs)) if state_runs else 0
min_run = int(min(state_runs)) if state_runs else 0
transition_rate = all_transitions / total_regime_days if total_regime_days > 0 else 0

# Gate criteria
g1 = sharpe >= 0.6
g2 = max_dd_pct >= -15.0
dd_delta = max_dd_pct - sp_max_dd
g3 = dd_delta > 0 or len(bear_trades) == 0
g4 = avg_run >= 5 and transition_rate < 0.05
all_pass = g1 and g2 and g3 and g4

result = {
    "period": f"{START} to {END}",
    "regime_aware_metrics": {
        "sharpe": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "cagr_pct": round(cagr, 2),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(pf, 4),
        "total_trades": total_trades,
        "trades_removed_by_regime": trades_removed,
        "baseline_trades": baseline_count,
        "final_equity": round(final_equity, 2),
    },
    "sp500_baseline_metrics": {
        "sharpe": round(sp_sharpe, 4),
        "max_drawdown_pct": round(sp_max_dd, 2),
        "cagr_pct": round(sp_cagr, 2),
    },
    "regime_distribution": regime_dist,
    "whipsaw_analysis": {
        "total_transitions": all_transitions,
        "total_regime_days": total_regime_days,
        "transition_rate_pct": round(transition_rate * 100, 2),
        "avg_run_days": round(avg_run, 1),
        "min_run_days": min_run,
    },
    "bear_trades": len(bear_trades),
    "dd_delta_pp": round(dd_delta, 2),
    "gate_results": {
        "g1_sharpe_gte_0_6": g1,
        "g2_max_dd_lte_15pct": g2,
        "g3_bear_dd_better_than_baseline": g3,
        "g4_no_excessive_whipsawing": g4,
        "overall_pass": all_pass,
    },
}

with open(RESULT_PATH, "w") as fh:
    json.dump(result, fh, indent=2, default=str)

log.info("Results written to %s", RESULT_PATH)
log.info("VERDICT: %s (%d/4 gates passed)", "PASS" if all_pass else "FAIL", sum([g1,g2,g3,g4]))
