#!/usr/bin/env python3
"""
Seed Wave 5 experiments into the research queue.

Theme: Full Portfolio Reoptimization + Consecutive Down Days Strategy
       — Maximize Returns on Existing Portfolio and Add Uncorrelated Alpha

Two tracks:
  Track A: Reoptimize current portfolio (params stale since SMA-200 addition)
  Track B: Implement and test Consecutive Down Days (new uncorrelated strategy)
  Track C: Allocation pool validation (unlock dormant strategies)
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from research.models import QueueEntry, ExperimentType, append_to_queue


def seed_wave5():
    experiments = []

    # ═══════════════════════════════════════════════════════════════════════
    # TRACK A: Full Portfolio Reoptimization (highest expected value)
    # SMA-200 filter changed trade mix from 443→270 trades. All strategy
    # params were tuned WITHOUT SMA-200. They're almost certainly suboptimal.
    # ASX reopt in Wave 1 gave +0.17 Sharpe — huge win.
    # ═══════════════════════════════════════════════════════════════════════

    experiments.append(QueueEntry(
        id="wave5_full_reopt",
        title="Full SP500 portfolio reoptimization — re-tune params for SMA-200 era",
        category="param_drift",
        market="sp500",
        hypothesis=(
            "SMA-200 filter (promoted v2.1) fundamentally changed the trade mix "
            "(443→270 trades). All MR/TF/OG parameters were optimized WITHOUT "
            "SMA-200 active. Re-running coordinate descent with SMA-200 enabled "
            "should find better parameter combinations. ASX reopt in Wave 1 "
            "yielded +0.17 Sharpe improvement from a similar post-filter reopt."
        ),
        method=ExperimentType.REOPTIMIZATION,
        acceptance_criteria={
            "sharpe_improvement": 0.05,
            "min_sharpe": 0.90,
            "max_drawdown_pct": 8.0,
            "min_trades": 150,
        },
        estimated_runtime_min=120,
        priority="P2",
        tags=["wave5", "reoptimization", "track_a"],
        notes="Baseline: Sharpe 0.87, CAGR 11.7%, DD 5.3%. Last reopt was pre-SMA200.",
    ))

    experiments.append(QueueEntry(
        id="wave5_reopt_oos",
        title="OOS validation of reoptimized SP500 config",
        category="param_drift",
        market="sp500",
        hypothesis=(
            "Reoptimized parameters from wave5_full_reopt maintain edge "
            "on unseen out-of-sample data, confirming the improvement is "
            "genuine and not overfitting."
        ),
        method=ExperimentType.OOS_VALIDATION,
        acceptance_criteria={
            "min_sharpe": 0.75,
            "max_drawdown_pct": 10.0,
            "min_profit_factor": 1.2,
            "min_trades": 50,
        },
        estimated_runtime_min=15,
        priority="P2",
        depends_on=["wave5_full_reopt"],
        tags=["wave5", "oos", "track_a"],
        notes="Promotion candidate if passes. Compare to current v2.2 OOS baseline.",
    ))

    # ═══════════════════════════════════════════════════════════════════════
    # TRACK B: Individual Strategy Parameter Sweeps (combined mode)
    # Fine-tune exit parameters that haven't been swept post-SMA200.
    # Combined mode gives realistic Sharpe (solo is misleading at $4K).
    # ═══════════════════════════════════════════════════════════════════════

    experiments.append(QueueEntry(
        id="wave5_mr_profit_sweep",
        title="MR profit_target_atr_mult sweep (combined mode) — optimize take-profit",
        category="param_drift",
        market="sp500",
        strategy_name="mean_reversion",
        hypothesis=(
            "Current MR profit target (1.5x ATR) was set pre-SMA200. With the "
            "trend filter active, winners may run further (in uptrend). A higher "
            "profit target could capture more profit per trade. Sweeping 1.0-3.0x "
            "in combined mode to find optimal take-profit level."
        ),
        method=ExperimentType.PARAM_SWEEP,
        params_override={
            "sweep_param": "profit_target_atr_mult",
            "sweep_values": [1.0, 1.5, 2.0, 2.5, 3.0],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.03,
            "min_trades": 80,
        },
        estimated_runtime_min=20,
        priority="P3",
        tags=["wave5", "param_sweep", "track_b"],
        notes="Previous wave3 max_hold sweep showed Sharpe +0.035 for shorter holds. Profit target is the complementary parameter.",
    ))

    experiments.append(QueueEntry(
        id="wave5_tf_trail_sweep",
        title="TF trailing_stop_atr_mult sweep (combined mode) — optimize trailing stop width",
        category="param_drift",
        market="sp500",
        strategy_name="trend_following",
        hypothesis=(
            "TF trailing stop (currently 2.5x ATR) determines how much room "
            "trends have to breathe. With SMA-200 filtering out downtrends, "
            "surviving trades are higher quality and may benefit from tighter "
            "or wider stops. Sweep 1.5-3.5x in combined mode."
        ),
        method=ExperimentType.PARAM_SWEEP,
        params_override={
            "sweep_param": "trailing_stop_atr_mult",
            "sweep_values": [1.5, 2.0, 2.5, 3.0, 3.5],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.03,
            "min_trades": 80,
        },
        estimated_runtime_min=20,
        priority="P3",
        tags=["wave5", "param_sweep", "track_b"],
        notes="Wave2 chandelier_tf tested stop widths in solo mode (misleading). This tests combined mode.",
    ))

    experiments.append(QueueEntry(
        id="wave5_og_gap_sweep",
        title="OG gap_threshold sweep (combined mode) — tune gap sensitivity",
        category="param_drift",
        market="sp500",
        strategy_name="opening_gap",
        hypothesis=(
            "Opening gap only generates 9 trades over the backtest period with "
            "current thresholds. The -1.5% gap threshold may be too strict with "
            "SMA-200 filtering. Relaxing gap threshold to -1.0% or -0.5% could "
            "increase trade count while maintaining quality."
        ),
        method=ExperimentType.PARAM_SWEEP,
        params_override={
            "sweep_param": "gap_threshold",
            "sweep_values": [-0.005, -0.010, -0.015, -0.020, -0.025],
            "mode": "combined",
        },
        acceptance_criteria={
            "min_trades": 20,
            "sharpe_improvement": 0.01,
        },
        estimated_runtime_min=20,
        priority="P3",
        tags=["wave5", "param_sweep", "track_b"],
        notes="OG had only 9 trades in Wave 2 exit test. Need more trades for any conclusion. SMA-200 may allow relaxed thresholds.",
    ))

    # ═══════════════════════════════════════════════════════════════════════
    # TRACK C: Allocation Pool Validation
    # Built in Task #52. Tested 0 times. This is the ONLY mechanism
    # that can unlock dormant strategies for portfolio addition.
    # All 4 combined tests failed due to position contention without pools.
    # ═══════════════════════════════════════════════════════════════════════

    experiments.append(QueueEntry(
        id="wave5_pool_toggle",
        title="Allocation pool A/B test — validate pools don't degrade current portfolio",
        category="portfolio",
        market="sp500",
        hypothesis=(
            "Enabling allocation pools (TF:5, MR:5, OG:3) with current 3 active "
            "strategies should produce results within 5% of no-pools baseline. "
            "Pool totals (13) are under max_positions (15), so no crowding. "
            "This validates the pool system works correctly before adding strategies."
        ),
        method=ExperimentType.FILTER_TEST,
        params_override={
            "filter_param": "allocation.enabled",
            "variants": [
                {"name": "pools_off", "value": False},
                {"name": "pools_on", "value": True},
            ],
        },
        acceptance_criteria={
            "max_sharpe_degradation": 0.05,
            "min_sharpe": 0.82,
        },
        estimated_runtime_min=15,
        priority="P2",
        tags=["wave5", "allocation_pools", "track_c"],
        notes="Critical gate: if pools degrade, the pool system has bugs. Current pools: TF:5, MR:5, OG:3, MB:5, _other:2.",
    ))

    # ═══════════════════════════════════════════════════════════════════════
    # TRACK D: New Strategy — Consecutive Down Days
    # Based on Connors (2008) and Quantpedia short-term reversal research.
    # Designed for individual large-cap stocks (not ETF adaptation).
    # Signal (consecutive red candles) is uncorrelated with RSI-based MR.
    # ═══════════════════════════════════════════════════════════════════════

    experiments.append(QueueEntry(
        id="wave5_cdd_solo",
        title="Consecutive Down Days — solo backtest on SP500 (new strategy)",
        category="new_strategy",
        market="sp500",
        strategy_name="consecutive_down_days",
        hypothesis=(
            "Buying large-cap stocks after 3+ consecutive down closes in "
            "uptrends (above SMA-200) captures short-term reversal premium. "
            "Academic research (Quantpedia, Groot et al. 2012) shows this "
            "effect generates 30-50 bps/week net of costs on large caps "
            "(Sharpe ~1.09 in published results). Signal is fundamentally "
            "different from RSI-based MR — counts close-to-close direction "
            "rather than oscillator levels."
        ),
        method=ExperimentType.SINGLE_STRATEGY_TEST,
        acceptance_criteria={
            "min_trades": 100,
            "min_profit_factor": 0.9,
            "note": "Solo metrics are secondary at $4K equity (fee drag). Primary check is trade count and PF direction.",
        },
        estimated_runtime_min=10,
        priority="P3",
        tags=["wave5", "new_strategy", "track_d", "consecutive_down_days"],
        notes=(
            "New sandbox strategy at research/strategies/consecutive_down_days.py. "
            "Default params: min_down_days=3, SMA-200 on, IBS<0.3, strength exit "
            "(close > prev high), max_hold=5, ATR stop 2.0x. "
            "Pattern: solo tests are misleading at $4K but validate the signal generates enough trades."
        ),
    ))

    experiments.append(QueueEntry(
        id="wave5_cdd_opt",
        title="Consecutive Down Days — parameter optimization (coordinate descent)",
        category="new_strategy",
        market="sp500",
        strategy_name="consecutive_down_days",
        hypothesis=(
            "Default CDD params are from published research (index-level). "
            "Optimizing min_down_days, ibs_threshold, atr_stop_mult, and "
            "max_hold_days via coordinate descent should improve metrics "
            "for SP500 individual stock universe."
        ),
        method=ExperimentType.FULL_OPTIMIZATION,
        params_override={
            "param_grid": {
                "min_down_days": [2, 3, 4, 5],
                "ibs_threshold": [0.2, 0.3, 0.4, 0.5],
                "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
                "max_hold_days": [3, 5, 7, 10],
            },
        },
        acceptance_criteria={
            "min_sharpe": 0.20,
            "min_profit_factor": 1.10,
            "min_trades": 80,
        },
        estimated_runtime_min=60,
        priority="P3",
        depends_on=["wave5_cdd_solo"],
        tags=["wave5", "optimization", "track_d", "consecutive_down_days"],
        notes="Only run if solo test produces 100+ trades. Grid has 4^4=256 combos but coord descent is ~16 evals.",
    ))

    experiments.append(QueueEntry(
        id="wave5_cdd_combined",
        title="Consecutive Down Days — combined portfolio test (MR+TF+OG+CDD)",
        category="new_strategy",
        market="sp500",
        strategy_name="consecutive_down_days",
        hypothesis=(
            "CDD signal is uncorrelated with RSI-based MR (different signal source: "
            "consecutive closes vs oscillator level). Adding CDD to the portfolio "
            "should improve Sharpe through diversification without excessive "
            "position contention. If CDD generates fewer trades than MR (~100 vs 270), "
            "contention should be manageable even without allocation pools."
        ),
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        acceptance_criteria={
            "max_sharpe_degradation": 0.05,
            "min_sharpe": 0.82,
            "max_drawdown_pct": 8.0,
        },
        estimated_runtime_min=20,
        priority="P2",
        depends_on=["wave5_cdd_opt"],
        tags=["wave5", "combined_test", "track_d", "consecutive_down_days"],
        notes=(
            "PATTERN: All 4 prior dormant strategies failed combined test due to "
            "position contention. CDD may succeed because: (a) fewer trades than MB/SR/STMR, "
            "(b) uncorrelated signal timing. If it fails, retry with allocation pools enabled."
        ),
    ))

    experiments.append(QueueEntry(
        id="wave5_cdd_oos",
        title="Consecutive Down Days — OOS validation for promotion",
        category="new_strategy",
        market="sp500",
        strategy_name="consecutive_down_days",
        hypothesis=(
            "Optimized CDD params from wave5_cdd_opt hold up on unseen "
            "out-of-sample data, confirming the edge is real and not overfitted."
        ),
        method=ExperimentType.OOS_VALIDATION,
        acceptance_criteria={
            "min_sharpe": 0.50,
            "max_drawdown_pct": 10.0,
            "min_profit_factor": 1.15,
            "min_trades": 30,
        },
        estimated_runtime_min=15,
        priority="P2",
        depends_on=["wave5_cdd_combined"],
        tags=["wave5", "oos", "track_d", "consecutive_down_days"],
        notes="Promotion candidate if combined test also passes. Both must pass for promotion.",
    ))

    # ── Seed all experiments ──
    print(f"\n{'='*60}")
    print(f"  SEEDING WAVE 5: {len(experiments)} experiments")
    print(f"{'='*60}\n")

    for exp in experiments:
        try:
            exp_id = append_to_queue(exp)
            dep_str = f" (depends: {exp.depends_on})" if exp.depends_on else ""
            print(f"  ✓ [{exp.priority}] {exp_id:30s} | {exp.title[:55]}{dep_str}")
        except ValueError as e:
            print(f"  ✗ [{exp.priority}] {exp.id:30s} | VALIDATION FAILED: {e}")
            return False

    print(f"\n  Total: {len(experiments)} experiments seeded")
    print(f"  Track A (Reoptimization): 2 experiments")
    print(f"  Track B (Param Sweeps):   3 experiments")
    print(f"  Track C (Alloc Pools):    1 experiment")
    print(f"  Track D (CDD Strategy):   5 experiments")
    print()
    return True


if __name__ == "__main__":
    success = seed_wave5()
    sys.exit(0 if success else 1)
