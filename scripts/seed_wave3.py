#!/usr/bin/env python3
"""Seed Wave 3 experiments into the research queue.

Theme: New strategy: Triple RSI + MR alpha stacking
  — high-conviction signals and entry filter optimization

10 experiments in 4 phases with dependency chains.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from research.models import (
    QueueEntry, ExperimentType, ExperimentStatus, Priority,
    append_to_queue,
)

MARKET = "sp500"

# Current MR volume config for full-dict sweep
VOL_BASE = {"lookback": 20, "surge_threshold": 1.5, "surge_boost": 0.0, "dry_penalty": 0.0}

experiments = [
    # ───────────────────────────────────────────────────────────
    # Phase 1: MR Enhancement (no code changes, run in parallel)
    # ───────────────────────────────────────────────────────────
    QueueEntry(
        id="wave3_ibs_sweep",
        title="IBS entry filter sweep on MR (combined mode)",
        category="filter",
        market=MARKET,
        hypothesis=(
            "Requiring low IBS (close near day's low) for MR entries improves "
            "signal quality. Alvarez research shows IBS < 25 gives 58% avg gain "
            "improvement on RSI(2) strategy. Our MR has ibs_max=1.0 (disabled). "
            "Testing restrictive thresholds should filter out weak MR signals."
        ),
        method=ExperimentType.PARAM_SWEEP,
        strategy_name="mean_reversion",
        params_override={
            "sweep_param": "ibs_max",
            "sweep_values": [0.15, 0.20, 0.25, 0.30, 0.50, 1.0],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.03,  # Best variant vs baseline (current ibs=1.0)
            "min_trades": 50,
        },
        estimated_runtime_min=30,
        priority=Priority.P2_HIGH,
        tags=["wave3", "filter", "ibs", "mean_reversion", "combined"],
        notes="From Alvarez Quant Trading & Pagonidis IBS research. IBS < 0.25 is the sweet spot.",
    ),

    QueueEntry(
        id="wave3_vol_sweep",
        title="Volume min_ratio sweep on MR (combined mode)",
        category="filter",
        market=MARKET,
        hypothesis=(
            "Higher volume threshold for MR entries improves trade quality. "
            "Wave 1 proved 1.5x volume on MR solo: Sharpe -0.02→0.38, PF 1.30→1.62. "
            "Wave 2 combined test FAILED due to infrastructure bug (nested params). "
            "This experiment uses full volume dict sweep to bypass the nested param issue. "
            "Expect 1.5x to be optimal in combined mode too."
        ),
        method=ExperimentType.PARAM_SWEEP,
        strategy_name="mean_reversion",
        params_override={
            "sweep_param": "volume",
            "sweep_values": [
                {**VOL_BASE, "min_ratio": 0.5},   # Current default
                {**VOL_BASE, "min_ratio": 1.0},
                {**VOL_BASE, "min_ratio": 1.25},
                {**VOL_BASE, "min_ratio": 1.5},   # Wave 1 winner
                {**VOL_BASE, "min_ratio": 2.0},
            ],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.03,
            "min_trades": 80,
        },
        estimated_runtime_min=30,
        priority=Priority.P2_HIGH,
        tags=["wave3", "filter", "volume", "mean_reversion", "combined"],
        notes="Retry of wave2_vol_combined with full-dict sweep to bypass nested param bug.",
    ),

    # ───────────────────────────────────────────────────────────
    # Phase 2: New Triple RSI Strategy (sequential chain)
    # ───────────────────────────────────────────────────────────
    QueueEntry(
        id="wave3_trsi_solo",
        title="Triple RSI — solo backtest on SP500",
        category="new_strategy",
        market=MARKET,
        hypothesis=(
            "Triple RSI (RSI(5) declining 3 days, below 30, with lookback check) "
            "generates rare but high-conviction mean reversion signals on individual "
            "SP500 stocks. Published edge on SPY: 90% WR, PF 4.0. Adapted for "
            "individual stocks with SMA-200 filter and volume confirmation. "
            "Expects fewer but higher-quality trades than existing MR strategy."
        ),
        method=ExperimentType.SINGLE_STRATEGY_TEST,
        strategy_name="triple_rsi",
        params_override={
            "rsi_period": 5,
            "rsi_entry": 30,
            "rsi_exit": 50,
            "decline_days": 3,
            "rsi_lookback_max": 60,
            "sma200_filter": True,
            "atr_stop_mult": 2.5,
            "max_hold_days": 10,
            "volume": {"lookback": 20, "min_ratio": 1.0},
            "ibs_max": 1.0,
        },
        acceptance_criteria={
            "min_sharpe": -0.5,    # Solo at $4K has fee drag; relative quality matters
            "min_profit_factor": 0.9,
            "min_trades": 30,      # Need enough trades for statistical significance
            "min_win_rate": 50,
        },
        estimated_runtime_min=15,
        priority=Priority.P3_MEDIUM,
        tags=["wave3", "new_strategy", "triple_rsi", "solo"],
        notes=(
            "Strategy implemented in research/strategies/triple_rsi.py (sandbox). "
            "Solo metrics unreliable at $4K — focus on trade count, WR, PF for viability."
        ),
    ),

    QueueEntry(
        id="wave3_trsi_opt",
        title="Triple RSI — parameter optimization",
        category="new_strategy",
        market=MARKET,
        hypothesis=(
            "Coordinate descent on Triple RSI parameters will find optimal balance "
            "between selectivity and trade count. Key tensions: shorter RSI period "
            "= more signals but noisier; fewer decline days = more trades but lower "
            "quality; tighter stop = less drawdown but more whipsaws."
        ),
        method=ExperimentType.FULL_OPTIMIZATION,
        strategy_name="triple_rsi",
        params_override={
            "param_grid": {
                "rsi_period": [3, 5, 7, 10],
                "rsi_entry": [20, 25, 30, 35],
                "decline_days": [2, 3, 4],
                "rsi_exit": [40, 50, 60, 70],
                "atr_stop_mult": [1.5, 2.0, 2.5, 3.0],
                "max_hold_days": [5, 7, 10, 15],
            },
        },
        acceptance_criteria={
            "min_sharpe": 0.0,
            "min_profit_factor": 1.1,
            "min_trades": 30,
        },
        estimated_runtime_min=60,
        priority=Priority.P3_MEDIUM,
        depends_on=["wave3_trsi_solo"],
        tags=["wave3", "new_strategy", "triple_rsi", "optimization"],
        notes="If solo test shows < 30 trades, reduce decline_days to 2 or lower rsi_entry.",
    ),

    QueueEntry(
        id="wave3_trsi_comb",
        title="Triple RSI — combined portfolio test",
        category="new_strategy",
        market=MARKET,
        hypothesis=(
            "Triple RSI generates rare signals (estimated 30-80/year) that won't "
            "compete significantly with MR/TF/OG for position slots. Unlike dormant "
            "strategies that generated 300-700 trades and degraded combined Sharpe, "
            "Triple RSI's low trade count should have minimal position contention. "
            "Combined portfolio may see slight Sharpe improvement from diversification."
        ),
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        strategy_name="triple_rsi",
        params_override=None,  # Will load optimized params from wave3_trsi_opt
        acceptance_criteria={
            "min_sharpe": 0.80,       # Must not degrade combined below 0.80
            "max_drawdown_pct": 8.0,  # Must not increase DD significantly
            "min_trades": 200,        # Combined total must stay reasonable
        },
        estimated_runtime_min=20,
        priority=Priority.P3_MEDIUM,
        depends_on=["wave3_trsi_opt"],
        tags=["wave3", "new_strategy", "triple_rsi", "combined"],
        notes=(
            "CRITICAL TEST: All 4 previous dormant strategies failed combined test "
            "due to position contention. Triple RSI's low trade count is designed "
            "to avoid this failure mode."
        ),
    ),

    # ───────────────────────────────────────────────────────────
    # Phase 3: Integration (depends on Phase 1 + Phase 2)
    # ───────────────────────────────────────────────────────────
    QueueEntry(
        id="wave3_stacked_mr",
        title="MR with best IBS + volume filters in combined mode",
        category="filter",
        market=MARKET,
        hypothesis=(
            "Stacking the best IBS threshold from wave3_ibs_sweep AND the best "
            "volume threshold from wave3_vol_sweep will compound their benefits. "
            "Each filter independently improves signal quality; combined effect "
            "should be multiplicative (better entries = higher PF, lower DD). "
            "Risk: over-filtering may reduce trade count below useful threshold."
        ),
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        strategy_name="mean_reversion",
        params_override=None,  # Will be populated from dependency results
        acceptance_criteria={
            "min_sharpe": 0.90,
            "min_profit_factor": 1.50,
            "min_trades": 150,
        },
        estimated_runtime_min=20,
        priority=Priority.P2_HIGH,
        depends_on=["wave3_ibs_sweep", "wave3_vol_sweep"],
        tags=["wave3", "filter", "stacked", "mean_reversion", "combined"],
        notes=(
            "If IBS and volume sweeps both pass, manually construct params_override "
            "with best IBS + best volume values before running this test."
        ),
    ),

    QueueEntry(
        id="wave3_full_reopt",
        title="Full SP500 portfolio reoptimization (post-filter + TRSI)",
        category="param_drift",
        market=MARKET,
        hypothesis=(
            "After adding IBS filter, volume filter, and (if passed) Triple RSI, "
            "the portfolio parameters may be suboptimal. Full coordinate descent "
            "reoptimization should find the best parameter combination across all "
            "active strategies. Expect Sharpe improvement from current v2.2 baseline."
        ),
        method=ExperimentType.REOPTIMIZATION,
        strategy_name=None,  # Full portfolio reopt
        params_override={},
        acceptance_criteria={
            "min_sharpe": 0.90,
            "min_cagr_pct": 12.0,
            "max_drawdown_pct": 7.0,
        },
        estimated_runtime_min=120,
        priority=Priority.P2_HIGH,
        depends_on=["wave3_stacked_mr"],
        tags=["wave3", "reoptimization", "portfolio"],
        notes=(
            "Run AFTER filter stacking test. If Triple RSI combined test also passed, "
            "enable it in config before reoptimization. Otherwise reopt with MR+TF+OG only."
        ),
    ),

    QueueEntry(
        id="wave3_oos_val",
        title="OOS + perturbation validation of reoptimized config",
        category="param_drift",
        market=MARKET,
        hypothesis=(
            "Reoptimized config must survive out-of-sample validation before "
            "promotion. Three-test suite: (1) time-split OOS Sharpe > 0, "
            "(2) perturbation ±15% stability > 50%, (3) walk-forward window "
            "win rate > 50%. All three must pass per lesson #6."
        ),
        method=ExperimentType.OOS_VALIDATION,
        strategy_name=None,
        params_override={},
        acceptance_criteria={
            "min_oos_sharpe": 0.0,
            "min_oos_ratio": 0.5,
            "min_perturbation_pass_rate": 0.5,
        },
        estimated_runtime_min=60,
        priority=Priority.P2_HIGH,
        depends_on=["wave3_full_reopt"],
        tags=["wave3", "validation", "oos"],
        notes="GATE: No promotion without OOS pass. Use validate_oos.py with candidate config.",
    ),

    # ───────────────────────────────────────────────────────────
    # Phase 4: Exploratory (independent, lower priority)
    # ───────────────────────────────────────────────────────────
    QueueEntry(
        id="wave3_rsi_period",
        title="RSI period sweep on MR (combined mode) — RSI(5) vs RSI(14)",
        category="param_drift",
        market=MARKET,
        hypothesis=(
            "Web research (Triple RSI, Connors, Alvarez) consistently shows RSI(2-5) "
            "outperforming RSI(14) for mean reversion signals. Our MR uses RSI(14). "
            "Shorter RSI periods may improve entry timing. Combined-mode sweep gives "
            "realistic portfolio-level impact unlike unreliable solo sweeps (lesson #30)."
        ),
        method=ExperimentType.PARAM_SWEEP,
        strategy_name="mean_reversion",
        params_override={
            "sweep_param": "rsi_period",
            "sweep_values": [2, 3, 5, 7, 10, 14],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.02,
            "min_trades": 80,
        },
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        tags=["wave3", "param_sweep", "rsi", "mean_reversion", "combined"],
        notes=(
            "From web research: RSI(2) and RSI(5) are the standard for MR strategies. "
            "If a shorter period wins, it could be a simple config change for immediate profit."
        ),
    ),

    QueueEntry(
        id="wave3_hold_combined",
        title="max_hold_days sweep on MR (combined mode)",
        category="param_drift",
        market=MARKET,
        hypothesis=(
            "Wave 2 tested max_hold_days in SOLO mode (all negative Sharpe due to "
            "fee drag at $4K). Relative ranking showed 10 > 15 > 7 > 5 > 3. "
            "Combined-mode sweep gives realistic absolute Sharpe. Short holds (3-5d) "
            "may reduce time risk; longer holds (10-15d) capture more reversion."
        ),
        method=ExperimentType.PARAM_SWEEP,
        strategy_name="mean_reversion",
        params_override={
            "sweep_param": "max_hold_days",
            "sweep_values": [3, 5, 7, 10, 12, 15],
            "mode": "combined",
        },
        acceptance_criteria={
            "sharpe_improvement": 0.02,
            "min_trades": 80,
        },
        estimated_runtime_min=30,
        priority=Priority.P4_LOW,
        tags=["wave3", "param_sweep", "hold_days", "mean_reversion", "combined"],
        notes=(
            "Lesson #30: Solo param sweeps unreliable at $4K. Combined mode is required. "
            "Current default is max_hold_days=10 (from v2.2). May confirm or change."
        ),
    ),
]

def main():
    print(f"Seeding Wave 3 experiments ({len(experiments)} total)...")
    for exp in experiments:
        try:
            eid = append_to_queue(exp)
            print(f"  ✅ {eid}: {exp.title}")
        except ValueError as e:
            print(f"  ❌ {exp.id}: VALIDATION ERROR\n     {e}")
        except Exception as e:
            print(f"  ❌ {exp.id}: {e}")
    print(f"\nDone. Run: python3 scripts/wave_planner.py --status")

if __name__ == "__main__":
    main()
