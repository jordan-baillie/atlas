#!/usr/bin/env python3
"""Atlas Portfolio Experiment Generators — Tier 2 & 3 research.

Tier 2: Portfolio-level parameter sweeps
  - Max positions (5-25)
  - Risk per trade (0.25%-2%)
  - Allocation pools
  - Fee sensitivity ($0/$1/$5)
  - Starting equity ($2K-$50K)
  - Walk-forward window sizes

Tier 3: Cross-strategy optimization
  - Strategy correlation matrix
  - Optimal strategy weights
  - Combined regime filtering
"""

import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

import logging
from typing import Any, Dict, List, Optional

from research.models import (
    ExperimentType,
    Priority,
    QueueEntry,
    append_to_queue,
    generate_experiment_id,
)

logger = logging.getLogger("portfolio_experiments")

# Active strategy set label used as strategy_name for portfolio-level experiments.
# These experiments test the combined portfolio config, not a single strategy.
_PORTFOLIO_LABEL = "portfolio"

# Estimated runtime for a combined portfolio backtest (walk-forward, full universe)
_COMBINED_RUNTIME_MIN = 25


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_portfolio_entry(
    exp_id: str,
    title: str,
    hypothesis: str,
    params_override: Dict[str, Any],
    notes: str,
    priority: str,
    category: str,
    market: str,
    tags: Optional[List[str]] = None,
) -> QueueEntry:
    """Build a portfolio-level QueueEntry (Tier 2 or Tier 3)."""
    return QueueEntry(
        id=exp_id,
        title=title,
        category=category,
        market=market,
        hypothesis=hypothesis,
        method=ExperimentType.COMBINED_PORTFOLIO_TEST,
        strategy_name=_PORTFOLIO_LABEL,
        params_override=params_override,
        acceptance_criteria={},  # informational — no hard pass/fail
        estimated_runtime_min=_COMBINED_RUNTIME_MIN,
        priority=priority,
        notes=notes,
        tags=tags or [],
    )


# ─── Tier 2: Portfolio Parameter Sweeps ──────────────────────────────────────


def generate_max_positions_sweep(
    positions_range: List[int] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate experiments sweeping max_open_positions.

    Default range: [5, 10, 15, 20, 25]
    Each experiment runs the full active portfolio (MR+TF+OG) with a
    different max_positions setting.
    """
    if positions_range is None:
        positions_range = [5, 10, 15, 20, 25]

    entries: List[QueueEntry] = []
    for n in positions_range:
        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Max positions sweep: {n} positions — {market.upper()}",
                hypothesis=(
                    f"Running the active portfolio (MR+TF+OG) with max_open_positions={n} "
                    f"reveals the optimal capacity vs diversification trade-off."
                ),
                params_override={"max_open_positions": n},
                notes=(
                    f"Tier 2 sweep. Tests max_open_positions={n} across the active "
                    f"MR+TF+OG portfolio. Informational — compare Sharpe, CAGR, and "
                    f"max drawdown across the range [5, 10, 15, 20, 25]."
                ),
                priority=Priority.P3_MEDIUM,
                category="portfolio_sweep",
                market=market,
                tags=["tier2", "portfolio_sweep", "max_positions"],
            )
        )
    return entries


def generate_risk_per_trade_sweep(
    risk_range: List[float] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate experiments sweeping max_risk_per_trade_pct.

    Default range: [0.0025, 0.005, 0.01, 0.015, 0.02]
    (0.25%, 0.5%, 1.0%, 1.5%, 2.0%)
    """
    if risk_range is None:
        risk_range = [0.0025, 0.005, 0.01, 0.015, 0.02]

    entries: List[QueueEntry] = []
    for r in risk_range:
        pct_label = f"{r * 100:.2f}%"
        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Risk per trade sweep: {pct_label} — {market.upper()}",
                hypothesis=(
                    f"Running the active portfolio with max_risk_per_trade_pct={r} ({pct_label}) "
                    f"reveals the optimal Kelly fraction for this strategy set."
                ),
                params_override={"max_risk_per_trade_pct": r},
                notes=(
                    f"Tier 2 sweep. Tests max_risk_per_trade_pct={r} ({pct_label}). "
                    f"Informational — compare risk-adjusted returns across the range."
                ),
                priority=Priority.P3_MEDIUM,
                category="portfolio_sweep",
                market=market,
                tags=["tier2", "portfolio_sweep", "risk_per_trade"],
            )
        )
    return entries


def generate_fee_sensitivity(
    fee_levels: List[float] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate fee sensitivity experiments.

    Default levels: [0, 1.0, 2.2, 5.0, 10.0] (dollars per side)
    Tests how much the portfolio degrades as commission costs rise.
    """
    if fee_levels is None:
        fee_levels = [0.0, 1.0, 2.2, 5.0, 10.0]

    entries: List[QueueEntry] = []
    for fee in fee_levels:
        label = f"${fee:.2f}/trade"
        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Fee sensitivity: {label} — {market.upper()}",
                hypothesis=(
                    f"With commission_per_trade={label}, the active portfolio "
                    f"remains profitable / breaks even / degrades — establishing "
                    f"the minimum viable commission for this strategy set."
                ),
                params_override={"commission_per_trade": fee},
                notes=(
                    f"Tier 2 sweep. Tests commission_per_trade={fee} ({label}). "
                    f"Baseline is $1.10 (Moomoo). $0 = Alpaca benchmark. "
                    f"Informational — find the fee break-even point."
                ),
                priority=Priority.P3_MEDIUM,
                category="portfolio_sweep",
                market=market,
                tags=["tier2", "portfolio_sweep", "fee_sensitivity"],
            )
        )
    return entries


def generate_equity_sweep(
    equity_levels: List[int] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate experiments at different starting equity levels.

    Default levels: [2000, 4000, 10000, 25000, 50000]
    Tests how portfolio metrics scale with capital (fee drag, min position size).
    """
    if equity_levels is None:
        equity_levels = [2000, 4000, 10000, 25000, 50000]

    entries: List[QueueEntry] = []
    for equity in equity_levels:
        label = f"${equity:,}"
        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Starting equity sweep: {label} — {market.upper()}",
                hypothesis=(
                    f"Starting with {label} affects per-trade fee drag and "
                    f"position sizing — revealing at what account size this "
                    f"strategy set becomes viable."
                ),
                params_override={"starting_equity": equity},
                notes=(
                    f"Tier 2 sweep. Tests starting_equity={equity} ({label}). "
                    f"Informational — compare CAGR and Sharpe across capital levels "
                    f"to find minimum viable account size."
                ),
                priority=Priority.P3_MEDIUM,
                category="portfolio_sweep",
                market=market,
                tags=["tier2", "portfolio_sweep", "equity_sweep"],
            )
        )
    return entries


def generate_walkforward_window_sweep(
    window_sizes: List[int] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate experiments with different walk-forward training windows.

    Default sizes: [126, 189, 252, 378, 504] (trading days)
    Tests how in-sample window length affects out-of-sample performance.
    """
    if window_sizes is None:
        window_sizes = [126, 189, 252, 378, 504]

    entries: List[QueueEntry] = []
    for days in window_sizes:
        months = round(days / 21)
        label = f"{days}d (~{months}mo)"
        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Walk-forward window sweep: {label} — {market.upper()}",
                hypothesis=(
                    f"A training window of {days} trading days ({months} months) "
                    f"balances recency vs statistical robustness in walk-forward "
                    f"optimization for this strategy set."
                ),
                params_override={"train_window_days": days},
                notes=(
                    f"Tier 2 sweep. Tests backtest.train_window_days={days} ({label}). "
                    f"Informational — longer windows are more stable but slower to adapt; "
                    f"shorter windows are adaptive but overfit-prone."
                ),
                priority=Priority.P3_MEDIUM,
                category="portfolio_sweep",
                market=market,
                tags=["tier2", "portfolio_sweep", "walkforward_window"],
            )
        )
    return entries


# ─── Tier 3: Cross-Strategy ───────────────────────────────────────────────────


def generate_correlation_analysis(market: str = "sp500") -> QueueEntry:
    """Generate a correlation matrix experiment.

    Runs each active strategy independently, collects trade dates,
    computes pairwise overlap. Result goes to Portfolio/Correlation Matrix.md
    """
    exp_id = generate_experiment_id()
    return _make_portfolio_entry(
        exp_id=exp_id,
        title=f"Strategy correlation matrix — {market.upper()}",
        hypothesis=(
            "The active strategies (MR, TF, OG) have low pairwise trade overlap, "
            "providing genuine diversification rather than correlated bet sizing."
        ),
        params_override={"analysis_type": "correlation_matrix"},
        notes=(
            "Tier 3 cross-strategy analysis. Runs MR, TF, OG independently, "
            "collects trade entry/exit dates, computes pairwise Jaccard overlap "
            "and Pearson correlation of daily P&L series. "
            "Output written to vault: Portfolio/Correlation Matrix.md"
        ),
        priority=Priority.P4_LOW,
        category="cross_strategy",
        market=market,
        tags=["tier3", "cross_strategy", "correlation"],
    )


def generate_allocation_pool_experiments(
    pool_configs: List[Dict] = None,
    market: str = "sp500",
) -> List[QueueEntry]:
    """Generate allocation pool experiments.

    Default configs (per-strategy max_positions):
    - Equal:    {mean_reversion: 5, trend_following: 5, opening_gap: 5}
    - MR-heavy: {mean_reversion: 8, trend_following: 4, opening_gap: 3}
    - TF-heavy: {mean_reversion: 4, trend_following: 8, opening_gap: 3}
    - Tight:    {mean_reversion: 3, trend_following: 3, opening_gap: 3}
    - Loose:    {mean_reversion: 7, trend_following: 7, opening_gap: 6}
    """
    if pool_configs is None:
        pool_configs = [
            {
                "name": "Equal",
                "pools": {
                    "mean_reversion": 5,
                    "trend_following": 5,
                    "opening_gap": 5,
                },
            },
            {
                "name": "MR-heavy",
                "pools": {
                    "mean_reversion": 8,
                    "trend_following": 4,
                    "opening_gap": 3,
                },
            },
            {
                "name": "TF-heavy",
                "pools": {
                    "mean_reversion": 4,
                    "trend_following": 8,
                    "opening_gap": 3,
                },
            },
            {
                "name": "Tight",
                "pools": {
                    "mean_reversion": 3,
                    "trend_following": 3,
                    "opening_gap": 3,
                },
            },
            {
                "name": "Loose",
                "pools": {
                    "mean_reversion": 7,
                    "trend_following": 7,
                    "opening_gap": 6,
                },
            },
        ]

    entries: List[QueueEntry] = []
    for cfg in pool_configs:
        name = cfg["name"]
        pools = cfg["pools"]
        mr = pools.get("mean_reversion", 5)
        tf = pools.get("trend_following", 5)
        og = pools.get("opening_gap", 3)
        total = sum(pools.values())

        exp_id = generate_experiment_id()
        entries.append(
            _make_portfolio_entry(
                exp_id=exp_id,
                title=f"Allocation pool: {name} (MR={mr}/TF={tf}/OG={og}) — {market.upper()}",
                hypothesis=(
                    f"The '{name}' allocation pool (MR={mr}, TF={tf}, OG={og}, total={total}) "
                    f"reduces position contention vs global max_open_positions and improves "
                    f"risk-adjusted returns by ensuring each strategy gets fair slot access."
                ),
                params_override={
                    "allocation": {
                        "enabled": True,
                        "mode": "hard_pool",
                        "overflow_enabled": False,
                        "pools": {
                            "mean_reversion": {"max_positions": mr},
                            "trend_following": {"max_positions": tf},
                            "opening_gap": {"max_positions": og},
                        },
                    }
                },
                notes=(
                    f"Tier 3 cross-strategy. '{name}' allocation pool config: "
                    f"MR={mr}, TF={tf}, OG={og} (total={total} slots). "
                    f"Addresses position contention pattern seen in Wave 5 combined tests. "
                    f"Compare vs global max_positions=15 baseline."
                ),
                priority=Priority.P4_LOW,
                category="cross_strategy",
                market=market,
                tags=["tier3", "cross_strategy", "allocation_pool", name.lower().replace("-", "_")],
            )
        )
    return entries


# ─── Queue All ────────────────────────────────────────────────────────────────


def queue_tier2_experiments(market: str = "sp500") -> int:
    """Queue all Tier 2 portfolio experiments. Returns count successfully queued."""
    generators = [
        generate_max_positions_sweep(market=market),
        generate_risk_per_trade_sweep(market=market),
        generate_fee_sensitivity(market=market),
        generate_equity_sweep(market=market),
        generate_walkforward_window_sweep(market=market),
    ]

    count = 0
    for experiment_list in generators:
        for entry in experiment_list:
            try:
                append_to_queue(entry, skip_validation=True)
                count += 1
                logger.info(f"Queued Tier 2: {entry.title}")
            except Exception as exc:
                logger.error(f"Failed to queue '{entry.title}': {exc}")
    return count


def queue_tier3_experiments(market: str = "sp500") -> int:
    """Queue all Tier 3 cross-strategy experiments. Returns count successfully queued."""
    experiments: List[QueueEntry] = [
        generate_correlation_analysis(market=market),
    ]
    experiments.extend(generate_allocation_pool_experiments(market=market))

    count = 0
    for entry in experiments:
        try:
            append_to_queue(entry, skip_validation=True)
            count += 1
            logger.info(f"Queued Tier 3: {entry.title}")
        except Exception as exc:
            logger.error(f"Failed to queue '{entry.title}': {exc}")
    return count


def queue_all_portfolio_experiments(market: str = "sp500") -> int:
    """Queue all Tier 2 + Tier 3 experiments. Returns total count successfully queued."""
    t2 = queue_tier2_experiments(market=market)
    t3 = queue_tier3_experiments(market=market)
    total = t2 + t3
    logger.info(f"Queued {t2} Tier 2 + {t3} Tier 3 = {total} portfolio experiments for {market}")
    return total


# ─── CLI entry point ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Queue Atlas portfolio experiments")
    parser.add_argument("--market", default="sp500", help="Market to target (default: sp500)")
    parser.add_argument(
        "--tier",
        choices=["2", "3", "all"],
        default="all",
        help="Which tier to queue (default: all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print experiments without queuing",
    )
    args = parser.parse_args()

    if args.dry_run:
        all_entries: List[QueueEntry] = []
        all_entries.extend(generate_max_positions_sweep(market=args.market))
        all_entries.extend(generate_risk_per_trade_sweep(market=args.market))
        all_entries.extend(generate_fee_sensitivity(market=args.market))
        all_entries.extend(generate_equity_sweep(market=args.market))
        all_entries.extend(generate_walkforward_window_sweep(market=args.market))
        all_entries.append(generate_correlation_analysis(market=args.market))
        all_entries.extend(generate_allocation_pool_experiments(market=args.market))
        print(f"\nDry run — {len(all_entries)} experiments would be queued:\n")
        for e in all_entries:
            print(f"  [{e.priority}] {e.title}")
            print(f"         params: {e.params_override}")
        print()
    elif args.tier == "2":
        n = queue_tier2_experiments(market=args.market)
        print(f"Queued {n} Tier 2 experiments for {args.market}")
    elif args.tier == "3":
        n = queue_tier3_experiments(market=args.market)
        print(f"Queued {n} Tier 3 experiments for {args.market}")
    else:
        n = queue_all_portfolio_experiments(market=args.market)
        print(f"Queued {n} total portfolio experiments for {args.market}")
