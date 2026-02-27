#!/usr/bin/env python3
"""
Seed the Atlas research queue with Wave 1 experiments.
=====================================================
Creates structured queue entries for:
  - 5 dormant strategy activations (solo → optimize → combined → OOS)
  - ASX re-optimization with new features
  - VIX regime filter test
  - Volume regime filter test
  - Cross-market correlation analysis

Each dormant strategy follows a 4-step pipeline:
  1. Solo backtest (quick feasibility check)
  2. Parameter optimization (if solo shows promise)
  3. Combined portfolio test (with active strategies)
  4. OOS validation (if combined improves portfolio)

Run: python3 scripts/seed_research_queue.py [--dry-run]
"""

import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research.models import (
    QueueEntry, ExperimentType, Priority, ExperimentStatus,
    append_to_queue, read_queue, QUEUE_PATH,
)

# ---------------------------------------------------------------------------
# Current baseline (SP500 v2.0) — used for acceptance criteria
# ---------------------------------------------------------------------------
BASELINE = {
    "sp500": {
        "cagr": 15.69,
        "sharpe": 1.04,
        "sortino": 1.655,
        "max_dd": 5.39,
        "win_rate": 56.0,
        "profit_factor": 1.50,
        "total_trades": 425,
    }
}


def make_dormant_strategy_pipeline(
    strategy_name: str,
    display_name: str,
    market: str,
    priority: str,
    hypothesis: str,
    solo_notes: str = "",
    opt_params: dict = None,
    combined_notes: str = "",
    estimated_solo_min: int = 5,
    estimated_opt_min: int = 60,
    tags: list = None,
) -> list:
    """Create the 4-step pipeline for testing a dormant strategy."""
    prefix = f"wave1_{strategy_name[:6]}"
    tags = tags or [f"wave1", "dormant", strategy_name]
    bl = BASELINE[market]

    entries = []

    # Step 1: Solo backtest
    solo_id = f"{prefix}_solo"
    entries.append(QueueEntry(
        id=solo_id,
        title=f"Solo backtest: {display_name} on {market.upper()}",
        category="dormant",
        market=market,
        strategy_name=strategy_name,
        hypothesis=hypothesis,
        method=ExperimentType.SINGLE_STRATEGY_TEST,
        acceptance_criteria={
            "min_trades": 10,
            "min_win_rate": 40.0,
            "positive_pnl": True,
            "min_profit_factor": 1.0,
            "description": f"Strategy must show basic viability: >10 trades, >40% WR, positive PnL. {solo_notes}",
        },
        estimated_runtime_min=estimated_solo_min,
        priority=priority,
        tags=tags + ["solo"],
        notes=solo_notes,
    ))

    # Step 2: Parameter optimization
    opt_id = f"{prefix}_opt"
    entries.append(QueueEntry(
        id=opt_id,
        title=f"Optimize: {display_name} on {market.upper()}",
        category="dormant",
        market=market,
        strategy_name=strategy_name,
        hypothesis=f"Coordinate descent on {display_name} params will improve solo performance significantly.",
        method=ExperimentType.FULL_OPTIMIZATION,
        acceptance_criteria={
            "sharpe_improvement_vs_solo": 0.1,
            "min_trades": 15,
            "min_profit_factor": 1.1,
            "description": "Optimization should lift Sharpe by 0.1+ vs untuned solo, maintain 15+ trades.",
        },
        params_override=opt_params,
        estimated_runtime_min=estimated_opt_min,
        priority=priority,
        depends_on=[solo_id],
        tags=tags + ["optimization"],
    ))

    # Step 3: Combined portfolio test
    combined_id = f"{prefix}_comb"
    entries.append(QueueEntry(
        id=combined_id,
        title=f"Combined test: {display_name} + active portfolio on {market.upper()}",
        category="dormant",
        market=market,
        strategy_name=strategy_name,
        hypothesis=f"Adding optimized {display_name} to the active strategy set (MR+TF+OG) improves the portfolio.",
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        acceptance_criteria={
            "min_combined_sharpe": bl["sharpe"],
            "max_combined_dd": bl["max_dd"] + 1.0,  # Allow 1pp DD increase
            "min_combined_trades": bl["total_trades"],
            "min_strategy_trades": 10,
            "positive_delta_sharpe": True,
            "description": (
                f"Combined portfolio must maintain Sharpe >= {bl['sharpe']:.2f}, "
                f"DD <= {bl['max_dd'] + 1.0:.1f}%, "
                f"strategy contributes 10+ trades. {combined_notes}"
            ),
        },
        estimated_runtime_min=estimated_solo_min,
        priority=priority,
        depends_on=[opt_id],
        tags=tags + ["combined"],
    ))

    # Step 4: OOS validation
    oos_id = f"{prefix}_oos"
    entries.append(QueueEntry(
        id=oos_id,
        title=f"OOS validation: portfolio with {display_name} on {market.upper()}",
        category="dormant",
        market=market,
        strategy_name=strategy_name,
        hypothesis=f"The portfolio with {display_name} passes all 3 OOS validation tests (time split, perturbation, walk-forward).",
        method=ExperimentType.OOS_VALIDATION,
        acceptance_criteria={
            "time_split_oos_is_sharpe_ratio": 0.5,
            "perturbation_zero_negative_cagr_pct": 80.0,
            "walkforward_profitable_window_pct": 70.0,
            "walkforward_worst_window_return": -5.0,
            "description": "Standard 3-test OOS: time split ratio>0.5, <20% negative perturbation, >70% profitable windows, worst window >-5%.",
        },
        estimated_runtime_min=90,
        priority=priority,
        depends_on=[combined_id],
        tags=tags + ["oos", "validation"],
    ))

    return entries


def build_wave1_queue() -> list:
    """Build all Wave 1 queue entries."""
    entries = []

    # =========================================================================
    # 1. MOMENTUM BREAKOUT — P2 (already coded, 296 lines, breakout strategy)
    # =========================================================================
    entries.extend(make_dormant_strategy_pipeline(
        strategy_name="momentum_breakout",
        display_name="Momentum Breakout",
        market="sp500",
        priority=Priority.P2_HIGH,
        hypothesis=(
            "Momentum breakout captures trend initiation events that the existing "
            "trend_following strategy misses. TF waits for MA crossover (lagging); "
            "breakout enters at the point of N-day high breach (leading). "
            "Expects moderate trade count (30-80), higher avg win, lower win rate than MR."
        ),
        solo_notes="Strategy enters on N-day high breakout with trend MA alignment. Key params: lookback_days, atr_stop_mult, trailing_stop_atr_mult.",
        opt_params={
            "param_grid": {
                "lookback_days": [10, 15, 20, 30],
                "atr_stop_mult": [2.0, 2.5, 3.0, 3.5],
                "trailing_stop_atr_mult": [2.0, 2.5, 3.0, 3.5],
                "max_hold_days": [10, 15, 20, 25],
                "trend_ma_period": [50, 100, 150, 200],
            }
        },
        combined_notes="OG currently drags portfolio solo but helps via diversification — breakout may do similar or better.",
        estimated_opt_min=60,
        tags=["wave1", "dormant", "momentum_breakout", "trend"],
    ))

    # =========================================================================
    # 2. SHORT-TERM MEAN REVERSION — P2 (RSI(2)/IBS based, Connors-style)
    # =========================================================================
    entries.extend(make_dormant_strategy_pipeline(
        strategy_name="short_term_mr",
        display_name="Short-Term Mean Reversion",
        market="sp500",
        priority=Priority.P2_HIGH,
        hypothesis=(
            "Short-term MR (RSI(2)/IBS) captures rapid 1-5 day reversals that the "
            "existing mean_reversion (RSI(14)/z-score) strategy misses. "
            "Different timeframe = different signals = diversification benefit. "
            "Connors research shows RSI(2)<10 has 70-75% win rate on SP500. "
            "Key risk: signal overlap with existing MR — need <30% overlap for value."
        ),
        solo_notes="RSI(2) was rejected during coord descent optimization as too noisy, but short_term_mr has IBS confirmation + SMA(5) filter which may clean up signals.",
        opt_params={
            "param_grid": {
                "rsi_period": [2, 3],
                "rsi_oversold": [5, 10, 15],
                "ibs_oversold": [0.15, 0.2, 0.25, 0.3],
                "sma_period": [3, 5, 8],
                "atr_stop_mult": [1.0, 1.5, 2.0],
                "max_hold_days": [3, 5, 7, 10],
            }
        },
        combined_notes="Critical: measure signal overlap with existing mean_reversion. If overlap >30%, diversification value is too low.",
        estimated_opt_min=60,
        tags=["wave1", "dormant", "short_term_mr", "mean_reversion"],
    ))

    # =========================================================================
    # 3. SECTOR ROTATION — P3 (top-down, fundamentally different signal source)
    # =========================================================================
    entries.extend(make_dormant_strategy_pipeline(
        strategy_name="sector_rotation",
        display_name="Sector Rotation",
        market="sp500",
        priority=Priority.P3_MEDIUM,
        hypothesis=(
            "Sector rotation provides a top-down signal that is uncorrelated with "
            "the bottom-up technical strategies (MR, TF, OG). It selects the strongest "
            "sectors by momentum then buys the strongest stocks within those sectors. "
            "204 SP500 tickers mapped across 11 GICS sectors. "
            "Risk: longer holding periods and lower trade frequency may limit impact."
        ),
        solo_notes="Requires sector_map.json (204 tickers, 11 sectors confirmed). Rebalances every N days — structurally different from signal-per-bar strategies.",
        opt_params={
            "param_grid": {
                "sector_momentum_period": [40, 60, 90],
                "top_sectors": [2, 3, 4],
                "rebalance_days": [10, 15, 20, 30],
                "atr_stop_mult": [2.0, 2.5, 3.0],
                "max_hold_days": [15, 20, 25, 30],
                "stocks_per_sector": [1, 2, 3],
            }
        },
        combined_notes="Sector rotation is fundamentally different signal source. Even modest solo performance may improve portfolio via decorrelation.",
        estimated_opt_min=45,
        tags=["wave1", "dormant", "sector_rotation", "macro"],
    ))

    # =========================================================================
    # 4. MTF MOMENTUM — P3 (weekly trend + daily pullback entry)
    # =========================================================================
    entries.extend(make_dormant_strategy_pipeline(
        strategy_name="mtf_momentum",
        display_name="Multi-Timeframe Momentum",
        market="sp500",
        priority=Priority.P3_MEDIUM,
        hypothesis=(
            "MTF momentum enters daily pullbacks within weekly uptrends. This is "
            "similar in spirit to trend_following but uses a different timeframe for "
            "trend confirmation (weekly SMA vs daily MA crossover). "
            "Key question: signal overlap with trend_following. If >60% overlap, "
            "the strategy adds complexity without diversification."
        ),
        solo_notes="Requires weekly data aggregation from daily bars. Uses weekly SMA + RSI for trend, daily RSI + SMA proximity for entry.",
        opt_params={
            "param_grid": {
                "weekly_sma_period": [10, 15, 20],
                "weekly_rsi_min": [40, 50, 60],
                "daily_rsi_max": [30, 35, 40, 45],
                "pullback_sma_pct": [0.02, 0.03, 0.05],
                "atr_stop_mult": [2.0, 2.5, 3.0],
                "max_hold_days": [10, 15, 20],
            }
        },
        combined_notes="Must measure signal overlap with trend_following. >60% overlap = reject for complexity without benefit.",
        estimated_opt_min=60,
        tags=["wave1", "dormant", "mtf_momentum", "trend"],
    ))

    # =========================================================================
    # 5. BB SQUEEZE — P3 (volatility contraction breakout)
    # =========================================================================
    entries.extend(make_dormant_strategy_pipeline(
        strategy_name="bb_squeeze",
        display_name="BB Squeeze (Volatility Breakout)",
        market="sp500",
        priority=Priority.P3_MEDIUM,
        hypothesis=(
            "BB Squeeze (Bollinger Band inside Keltner Channel) identifies periods "
            "of low volatility that precede explosive moves. When the squeeze fires "
            "(BBs expand outside KCs) with positive momentum, it signals a high-probability "
            "directional move. This is a volatility regime strategy — fundamentally "
            "different from trend/MR/gap signals."
        ),
        solo_notes="Uses BB width vs KC width for squeeze detection, linear regression slope for momentum confirmation.",
        opt_params={
            "param_grid": {
                "bb_period": [15, 20, 25],
                "bb_std": [1.5, 2.0, 2.5],
                "kc_atr_mult": [1.0, 1.5, 2.0],
                "momentum_period": [10, 15, 20],
                "atr_stop_mult": [1.5, 2.0, 2.5],
                "max_hold_days": [10, 15, 20, 25],
            }
        },
        combined_notes="Volatility regime signal is orthogonal to trend/MR — high potential for decorrelation benefit.",
        estimated_opt_min=45,
        tags=["wave1", "dormant", "bb_squeeze", "volatility"],
    ))

    # =========================================================================
    # 6. ASX RE-OPTIMIZATION — P3 (test new features on existing market)
    # =========================================================================
    entries.append(QueueEntry(
        id="wave1_asx_reopt",
        title="Re-optimize ASX with SMA-200, IBS, RSI period features",
        category="param_drift",
        market="asx",
        hypothesis=(
            "The SMA-200 filter, IBS confirmation, and configurable RSI period were "
            "added during SP500 optimization but never tested on ASX. These features "
            "may improve ASX performance, particularly: SMA-200 filtering out downtrend "
            "entries, IBS improving mean reversion entry quality, and RSI period tuning "
            "finding a better signal frequency for the smaller ASX universe."
        ),
        method=ExperimentType.REOPTIMIZATION,
        acceptance_criteria={
            "sharpe_improvement": 0.05,
            "max_dd_increase": 1.0,
            "min_trades": 250,
            "description": "Sharpe improvement >= 0.05 OR DD reduction >= 1pp without Sharpe degradation. Min 250 trades.",
        },
        estimated_runtime_min=120,
        priority=Priority.P3_MEDIUM,
        tags=["wave1", "reoptimization", "asx", "features"],
        notes=(
            "SMA-200 was too aggressive on SP500 (reduced trades to insignificant levels). "
            "May behave differently on smaller ASX universe. IBS filter already proven on "
            "SP500 opening_gap — test on ASX mean_reversion."
        ),
    ))

    # =========================================================================
    # 7. VIX REGIME FILTER — P3 (market regime overlay)
    # =========================================================================
    entries.append(QueueEntry(
        id="wave1_vix_filter",
        title="VIX regime filter for SP500 strategies",
        category="filter",
        market="sp500",
        hypothesis=(
            "Reducing exposure during high-VIX periods (VIX > 25-30) improves "
            "risk-adjusted returns by avoiding entries during market panic. "
            "Alternative: only allow mean_reversion entries during high VIX "
            "(buy the panic). Williams VIX Fix (already in helpers.py) can be "
            "used as stock-level proxy if index VIX data unavailable."
        ),
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={
            "sharpe_improvement": 0.03,
            "max_cagr_drop": 2.0,
            "min_trades": 200,
            "description": "Sharpe improvement >= 0.03 without CAGR dropping > 2pp. Min 200 trades.",
        },
        estimated_runtime_min=30,
        priority=Priority.P3_MEDIUM,
        tags=["wave1", "filter", "vix", "regime", "sp500"],
        notes=(
            "Requires VIX (^VIX) data ingestion — check if already in SP500 cache. "
            "If not, add to data pipeline first. Williams VIX Fix (calc_wvf in helpers.py) "
            "is a stock-level proxy that doesn't need index data. "
            "Test variants: (a) skip entries VIX>25, (b) skip VIX>30, "
            "(c) halve position VIX>20, (d) tighten stops VIX>25, "
            "(e) only MR entries when VIX>25."
        ),
    ))

    # =========================================================================
    # 8. VOLUME REGIME FILTER — P4 (entry quality improvement)
    # =========================================================================
    entries.append(QueueEntry(
        id="wave1_vol_filter",
        title="Volume regime filter across active SP500 strategies",
        category="filter",
        market="sp500",
        hypothesis=(
            "Only entering trades when daily volume exceeds the N-day average "
            "improves signal quality by filtering out low-liquidity, low-conviction "
            "price moves. Higher volume on entry day = more institutional participation "
            "= higher probability of follow-through."
        ),
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={
            "win_rate_improvement": 2.0,
            "sharpe_improvement": 0.02,
            "min_trades": 200,
            "description": "Win rate improvement >= 2pp OR Sharpe improvement >= 0.02. Min 200 trades to avoid over-filtering.",
        },
        estimated_runtime_min=20,
        priority=Priority.P4_LOW,
        tags=["wave1", "filter", "volume", "sp500"],
        notes=(
            "Test variants: (a) vol > 1.0x 20-day avg, (b) vol > 1.5x 20-day avg, "
            "(c) vol > 2.0x 20-day avg, (d) vol > 50-day avg. "
            "WARNING: aggressive volume filters reduced trades to insignificant levels "
            "during SMA-200 testing. Monitor trade count carefully."
        ),
    ))

    # =========================================================================
    # 9. CROSS-MARKET CORRELATION — P5 (exploratory)
    # =========================================================================
    entries.append(QueueEntry(
        id="wave1_cross_mkt",
        title="Cross-market correlation signals: ASX ↔ SP500",
        category="cross_market",
        market="sp500",
        hypothesis=(
            "SP500 overnight moves predict ASX opening direction (ASX opens ~14h "
            "after US close), and ASX session moves predict SP500 opening direction "
            "(US opens ~7h after ASX close). If correlation > 0.3, a cross-market "
            "filter could skip entries when the other market had a large adverse move."
        ),
        method=ExperimentType.FILTER_TEST,
        acceptance_criteria={
            "min_correlation": 0.3,
            "p_value_max": 0.05,
            "sharpe_improvement": 0.03,
            "description": "Statistically significant correlation (p < 0.05) AND Sharpe improvement >= 0.03 when used as filter.",
        },
        estimated_runtime_min=45,
        priority=Priority.P5_BACKLOG,
        tags=["wave1", "cross_market", "correlation", "exploratory"],
        notes=(
            "This is exploratory — may not yield actionable results. "
            "Data alignment: must handle AEST/ET timezone differences and date line. "
            "ASX daily data in data/cache/asx/, SP500 in data/cache/sp500/. "
            "Use SPY as SP500 proxy, STW.AX or XJO as ASX proxy."
        ),
    ))

    return entries


def main():
    parser = argparse.ArgumentParser(description="Seed Atlas research queue with Wave 1 experiments")
    parser.add_argument("--dry-run", action="store_true", help="Print entries without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing queue entries")
    args = parser.parse_args()

    entries = build_wave1_queue()

    # Check for existing queue
    existing = read_queue()
    existing_ids = {e["id"] for e in existing}

    if existing and not args.force:
        new_entries = [e for e in entries if e.id not in existing_ids]
        skip_count = len(entries) - len(new_entries)
        if skip_count > 0:
            print(f"⚠ Skipping {skip_count} entries already in queue (use --force to overwrite)")
        entries = new_entries

    if args.dry_run:
        print(f"\n{'='*70}")
        print(f"  WAVE 1 RESEARCH QUEUE — {len(entries)} experiments")
        print(f"{'='*70}\n")
        for i, e in enumerate(entries, 1):
            deps = f" (depends: {', '.join(e.depends_on)})" if e.depends_on else ""
            print(f"  {i:2d}. [{e.priority}] {e.title}")
            print(f"      ID: {e.id} | Category: {e.category} | Market: {e.market}")
            print(f"      Method: {e.method} | ETA: {e.estimated_runtime_min}min{deps}")
            print(f"      Hypothesis: {e.hypothesis[:120]}...")
            print()

        total_time = sum(e.estimated_runtime_min for e in entries)
        print(f"  Total estimated runtime: {total_time}min ({total_time/60:.1f}h)")
        print(f"\n  Priority breakdown:")
        for p in ["P1", "P2", "P3", "P4", "P5"]:
            count = sum(1 for e in entries if e.priority == p)
            if count:
                print(f"    {p}: {count} experiments")

        print(f"\n  Run without --dry-run to write to {QUEUE_PATH}")
        return

    # Write to queue
    if args.force and existing:
        # Remove existing wave1 entries, keep others
        from research.models import _locked_write
        cleaned = [e for e in existing if not e["id"].startswith("wave1_")]
        _locked_write(QUEUE_PATH, cleaned)
        print(f"♻ Cleared {len(existing) - len(cleaned)} existing wave1 entries")

    count = 0
    for entry in entries:
        append_to_queue(entry)
        count += 1
        deps = f" → depends: {', '.join(entry.depends_on)}" if entry.depends_on else ""
        print(f"  ✓ [{entry.priority}] {entry.id}: {entry.title}{deps}")

    total_time = sum(e.estimated_runtime_min for e in entries)
    print(f"\n✅ Seeded {count} experiments into research queue")
    print(f"   Total estimated runtime: {total_time}min ({total_time/60:.1f}h)")
    print(f"   Queue path: {QUEUE_PATH}")


if __name__ == "__main__":
    main()
