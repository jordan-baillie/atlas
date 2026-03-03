#!/usr/bin/env python3
"""Re-queue Wave 2 experiments with corrected params_override formats."""
import sys
sys.path.insert(0, '/root/atlas')

from research.models import QueueEntry, ExperimentType, Priority, append_to_queue

experiments = [
    # ── Runnable NOW ─────────────────────────────────────────────────────

    # 1. Volume filter on combined portfolio
    QueueEntry(
        id="wave2_vol_combined",
        title="Volume filter 1.5x on combined portfolio (MR+TF+OG)",
        category="filter",
        market="sp500",
        hypothesis="Wave 1 found volume_entry_min=1.5 improved MR solo Sharpe from -0.02 to 0.38. Test on full combined portfolio.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.03, "max_cagr_drop": 2.0, "min_trades": 200},
        estimated_runtime_min=20,
        priority=Priority.P2_HIGH,
        strategy_name=None,  # portfolio-wide
        params_override={
            "filter_param": "volume_entry_min",
            "variants": [
                {"name": "baseline (disabled)", "value": 0.0},
                {"name": "1.0x avg volume", "value": 1.0},
                {"name": "1.5x avg volume", "value": 1.5},
                {"name": "2.0x avg volume", "value": 2.0},
            ]
        },
    ),

    # 2. Connors RSI(2) solo backtest
    QueueEntry(
        id="wave2_rsi2_solo",
        title="Connors RSI(2) — solo backtest",
        category="new_strategy",
        market="sp500",
        hypothesis="RSI(2) extreme oversold entries with SMA(5) exit capture short-term mean reversion with 74%+ win rate.",
        method=ExperimentType.SINGLE_STRATEGY_TEST,
        acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15, "max_max_drawdown_pct": 10},
        estimated_runtime_min=15,
        priority=Priority.P2_HIGH,
        strategy_name="connors_rsi2",
    ),

    # 3. RSI(2) optimization
    QueueEntry(
        id="wave2_rsi2_opt",
        title="Connors RSI(2) — parameter optimization",
        category="new_strategy",
        market="sp500",
        hypothesis="Optimizing RSI(2) threshold, exit SMA period, and position sizing improves risk-adjusted returns.",
        method=ExperimentType.FULL_OPTIMIZATION,
        acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15},
        estimated_runtime_min=60,
        priority=Priority.P2_HIGH,
        strategy_name="connors_rsi2",
        params_override={
            "param_grid": {
                "rsi_period": [2, 3, 4],
                "rsi_entry_threshold": [5, 10, 15, 20],
                "exit_sma_period": [3, 5, 7, 10],
                "sma_trend_period": [150, 200, 250],
            }
        },
        depends_on=["wave2_rsi2_solo"],
    ),

    # 4. RSI(2) combined portfolio test
    QueueEntry(
        id="wave2_rsi2_combined",
        title="Connors RSI(2) — combined portfolio test",
        category="portfolio",
        market="sp500",
        hypothesis="Adding RSI(2) to the portfolio provides uncorrelated alpha without degrading existing strategies.",
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        acceptance_criteria={"min_sharpe": 0.3, "min_trades": 200},
        estimated_runtime_min=30,
        priority=Priority.P2_HIGH,
        strategy_name="connors_rsi2",
        depends_on=["wave2_rsi2_opt"],
    ),

    # 5. RSI(2) OOS validation
    QueueEntry(
        id="wave2_rsi2_oos",
        title="Connors RSI(2) — OOS validation",
        category="portfolio",
        market="sp500",
        hypothesis="RSI(2) edge persists out-of-sample.",
        method=ExperimentType.OOS_VALIDATION,
        acceptance_criteria={"min_sharpe": 0.2, "min_trades": 10},
        estimated_runtime_min=45,
        priority=Priority.P2_HIGH,
        strategy_name="connors_rsi2",
        depends_on=["wave2_rsi2_combined"],
    ),

    # 6. Volume filter promotion (OOS validation)
    QueueEntry(
        id="wave2_vol_promotion",
        title="Volume filter 1.5x — OOS validation for promotion",
        category="filter",
        market="sp500",
        hypothesis="Volume filter edge persists out-of-sample, ready for promotion.",
        method=ExperimentType.OOS_VALIDATION,
        acceptance_criteria={"min_sharpe": 0.2, "min_trades": 100},
        estimated_runtime_min=45,
        priority=Priority.P2_HIGH,
        strategy_name="mean_reversion",
        params_override={"volume_entry_min": 1.5},
        depends_on=["wave2_vol_combined"],
    ),

    # 7. MR exit optimization — param sweep on profit_target_atr
    QueueEntry(
        id="wave2_exit_mr",
        title="MR exit optimization — profit target ATR multiplier sweep",
        category="param_drift",
        market="sp500",
        hypothesis="Current MR profit_target_atr may not be optimal. Sweep to find best risk/reward balance.",
        method=ExperimentType.PARAM_SWEEP,
        acceptance_criteria={"sharpe_improvement": 0.05, "description": "Sharpe +0.05 or PF +0.1 vs baseline"},
        estimated_runtime_min=20,
        priority=Priority.P3_MEDIUM,
        strategy_name="mean_reversion",
        params_override={
            "sweep_param": "profit_target_atr",
            "sweep_values": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
        },
    ),

    # 8. OG exit optimization — holding_period sweep
    QueueEntry(
        id="wave2_exit_og",
        title="OG exit optimization — max holding period sweep",
        category="param_drift",
        market="sp500",
        hypothesis="Opening gap trades may benefit from shorter/longer max holding periods.",
        method=ExperimentType.PARAM_SWEEP,
        acceptance_criteria={"sharpe_improvement": 0.05, "description": "Sharpe +0.05 or PF +0.1 vs baseline"},
        estimated_runtime_min=15,
        priority=Priority.P3_MEDIUM,
        strategy_name="opening_gap",
        params_override={
            "sweep_param": "max_hold_days",
            "sweep_values": [1, 2, 3, 5, 7, 10],
        },
    ),

    # 9. TF Chandelier trailing stop — ATR multiplier sweep
    QueueEntry(
        id="wave2_chandelier_tf",
        title="TF trailing stop — ATR multiplier sweep",
        category="param_drift",
        market="sp500",
        hypothesis="Wider ATR trailing stop captures more trend while tighter stop reduces drawdown.",
        method=ExperimentType.PARAM_SWEEP,
        acceptance_criteria={"sharpe_improvement": 0.05, "description": "Sharpe +0.05 on TF"},
        estimated_runtime_min=20,
        priority=Priority.P3_MEDIUM,
        strategy_name="trend_following",
        params_override={
            "sweep_param": "atr_stop_multiplier",
            "sweep_values": [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0],
        },
    ),

    # 10. Turn-of-month filter
    QueueEntry(
        id="wave2_tom_filter",
        title="Turn-of-month seasonality — entry day filter",
        category="filter",
        market="sp500",
        hypothesis="S&P 500 returns concentrate in last 3 and first 3 trading days of month. Restricting entries to these windows improves Sharpe.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.03, "max_cagr_drop": 2.0, "min_trades": 100},
        estimated_runtime_min=20,
        priority=Priority.P3_MEDIUM,
        strategy_name=None,  # portfolio-wide
        params_override={
            "filter_param": "entry_day_window",
            "variants": [
                {"name": "baseline (all days)", "value": 0},
                {"name": "last 3 + first 3", "value": 3},
                {"name": "last 5 + first 5", "value": 5},
                {"name": "last 2 + first 2", "value": 2},
            ]
        },
    ),

    # 11-14. VIX regime filter variants (engine-level vix_filter)
    QueueEntry(
        id="wave2_vix_roc_05_roc_spike_20pct",
        title="VIX regime filter — max_entry=20 (skip entries VIX > 20)",
        category="filter",
        market="sp500",
        hypothesis="Skipping entries when VIX > 20 avoids volatile regime entries. Wave 1 proxy test showed +0.29 Sharpe improvement.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.03, "max_cagr_drop": 2.0, "min_trades": 200},
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        params_override={
            "filter_param": "vix_filter",
            "variants": [
                {"name": "baseline (disabled)", "value": {"enabled": False}},
                {"name": "VIX < 20", "value": {"enabled": True, "max_entry": 20}},
                {"name": "VIX < 25", "value": {"enabled": True, "max_entry": 25}},
                {"name": "VIX < 30", "value": {"enabled": True, "max_entry": 30}},
                {"name": "VIX < 35", "value": {"enabled": True, "max_entry": 35}},
            ]
        },
        tags=["wave2", "filter", "vix"],
    ),

    QueueEntry(
        id="wave2_vix_roc_06_roc_spike_30pct",
        title="VIX regime filter — max_entry=30 (conservative threshold)",
        category="filter",
        market="sp500",
        hypothesis="VIX > 30 marks extreme fear. Only skip entries during true panic.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.02, "max_cagr_drop": 1.0, "min_trades": 250},
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        params_override={
            "filter_param": "vix_filter",
            "variants": [
                {"name": "baseline (disabled)", "value": {"enabled": False}},
                {"name": "VIX < 28", "value": {"enabled": True, "max_entry": 28}},
                {"name": "VIX < 30", "value": {"enabled": True, "max_entry": 30}},
                {"name": "VIX < 33", "value": {"enabled": True, "max_entry": 33}},
            ]
        },
        tags=["wave2", "filter", "vix"],
    ),

    QueueEntry(
        id="wave2_vix_roc_07_roc_spike_50pct",
        title="VIX regime filter — high threshold only (VIX > 35-40)",
        category="filter",
        market="sp500",
        hypothesis="Only block entries during extreme VIX spikes (>35) to preserve trade count while avoiding crashes.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.01, "min_trades": 280},
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        params_override={
            "filter_param": "vix_filter",
            "variants": [
                {"name": "baseline (disabled)", "value": {"enabled": False}},
                {"name": "VIX < 35", "value": {"enabled": True, "max_entry": 35}},
                {"name": "VIX < 40", "value": {"enabled": True, "max_entry": 40}},
                {"name": "VIX < 45", "value": {"enabled": True, "max_entry": 45}},
            ]
        },
        tags=["wave2", "filter", "vix"],
    ),

    QueueEntry(
        id="wave2_vix_roc_08_roc_abs_30pct",
        title="VIX regime filter — moderate threshold (VIX > 25-30)",
        category="filter",
        market="sp500",
        hypothesis="VIX 25-30 is the transition zone between calm and fear. Test tight range.",
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={"sharpe_improvement": 0.02, "min_trades": 220},
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        params_override={
            "filter_param": "vix_filter",
            "variants": [
                {"name": "baseline (disabled)", "value": {"enabled": False}},
                {"name": "VIX < 23", "value": {"enabled": True, "max_entry": 23}},
                {"name": "VIX < 25", "value": {"enabled": True, "max_entry": 25}},
                {"name": "VIX < 27", "value": {"enabled": True, "max_entry": 27}},
                {"name": "VIX < 30", "value": {"enabled": True, "max_entry": 30}},
            ]
        },
        tags=["wave2", "filter", "vix"],
    ),
]

if __name__ == '__main__':
    for entry in experiments:
        append_to_queue(entry)
        print(f"  ✅ Queued: {entry.id}")
    print(f"\nTotal: {len(experiments)} experiments queued")
