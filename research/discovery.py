#!/usr/bin/env python3
"""Atlas Research Discovery Engine — 6 channels for experiment generation.

Channels (in priority order):
1. Strategy Universe checklist (mechanical)
2. Lifecycle auto-advance (mechanical) — handled by evaluator, skip here
3. Combinatorial exploration (mechanical)
4. Web discovery (LLM, periodic) — stub for coordinator agent
5. Ablation studies (mechanical)
6. Sensitivity/robustness tests (mechanical)

Plus: hypothesis generation stubs for the coordinator agent.
"""

import sys
from pathlib import Path
ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from itertools import product

from research.models import (
    QueueEntry, ExperimentType, Priority, ExperimentStatus,
    append_to_queue, read_queue, generate_experiment_id,
)

logger = logging.getLogger("discovery")

# ─── Strategy Universe ──────────────────────────────────────────────────────

STRATEGY_UNIVERSE_PATH = ATLAS_ROOT / "research" / "vault" / "Strategy Universe.md"

# Master list of all strategies — existing + planned Tier 1
# Status: existing | not_built | screening | solo | optimize | combined | oos | active | dead_end
STRATEGY_UNIVERSE = {
    # Existing (13 strategies)
    "mean_reversion": {"type": "mean_reversion", "tier": 0, "status": "active", "reference": "Atlas original"},
    "trend_following": {"type": "trend_following", "tier": 0, "status": "active", "reference": "Atlas original"},
    "opening_gap": {"type": "mean_reversion", "tier": 0, "status": "active", "reference": "Atlas original"},
    "bb_squeeze": {"type": "volatility", "tier": 0, "status": "dormant", "reference": "Bollinger Band Squeeze"},
    "momentum_breakout": {"type": "momentum", "tier": 0, "status": "dormant", "reference": "Atlas original"},
    "short_term_mr": {"type": "mean_reversion", "tier": 0, "status": "dormant", "reference": "Atlas original"},
    "sector_rotation": {"type": "rotation", "tier": 0, "status": "broken", "reference": "0 trades in backtest"},
    "mtf_momentum": {"type": "momentum", "tier": 0, "status": "dormant", "reference": "Multi-timeframe"},
    "dividend_capture": {"type": "event", "tier": 0, "status": "untested", "reference": "Dividend event"},
    "connors_rsi2": {"type": "mean_reversion", "tier": 0, "status": "dormant", "reference": "Connors 2008"},
    "consecutive_down_days": {"type": "mean_reversion", "tier": 0, "status": "screening", "reference": "Quantified Strategies"},
    "lower_band_reversion": {"type": "mean_reversion", "tier": 0, "status": "dormant", "reference": "Bollinger lower band"},
    "triple_rsi": {"type": "mean_reversion", "tier": 0, "status": "dormant", "reference": "Multi-period RSI"},

    # Tier 1 — 18 academic strategies to build
    "inside_bar_nr7": {"type": "volatility_breakout", "tier": 1, "status": "not_built",
        "reference": "Toby Crabel 'Day Trading with Short Term Price Patterns' (1990)",
        "description": "NR7 (narrowest range of 7 days) or inside bar → breakout entry. Enter on break of NR7 high/low. Exit: trailing stop or time-based (3-5 days)."},
    "donchian_breakout": {"type": "trend_following", "tier": 1, "status": "not_built",
        "reference": "Richard Donchian, Turtle Traders (1983)",
        "description": "Buy on 20-day high breakout, sell on 10-day low. Classic trend following. ATR position sizing."},
    "williams_percent_r": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Larry Williams 'How I Made $1M' (1979)",
        "description": "Williams %R oversold (<-80) with trend filter. Exit on %R > -20 or time stop."},
    "stochastic_oversold": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "George Lane (1950s), quantified by Connors",
        "description": "Stochastic %K < 20 and %D < 20 in uptrend (>SMA200). Exit on %K > 80 or time stop."},
    "adx_trend_pullback": {"type": "trend_following", "tier": 1, "status": "not_built",
        "reference": "Welles Wilder 'New Concepts in Technical Trading' (1978)",
        "description": "ADX > 25 (strong trend) + pullback to 20-EMA. Enter on bounce from EMA. Exit: trailing ATR stop."},
    "overnight_return": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Cliff et al. 'Overnight Return' (2019), Quantpedia #53",
        "description": "Buy at close, sell at open. Captures overnight premium. Filter: strong recent performers only."},
    "pead_earnings_drift": {"type": "event", "tier": 1, "status": "not_built",
        "reference": "Ball & Brown (1968), Bernard & Thomas (1989)",
        "description": "Post-Earnings Announcement Drift. Buy after positive earnings surprise, hold 20-60 days. Needs earnings data."},
    "keltner_reversion": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Chester Keltner (1960), modernized by Linda Bradford Raschke",
        "description": "Price touches lower Keltner Channel (EMA ± ATR mult) → buy. Exit at middle band (EMA). Uptrend filter."},
    "rsi_divergence": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Andrew Cardwell RSI divergence methodology",
        "description": "Price makes new low but RSI makes higher low (bullish divergence). Enter long. Exit on RSI > 60 or time."},
    "macd_divergence": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Gerald Appel (1979), MACD divergence patterns",
        "description": "Price makes new low but MACD histogram makes higher low. Enter long. Exit on MACD crossover or time stop."},
    "volume_climax": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Quantified Strategies volume research, Wyckoff method",
        "description": "Extreme volume spike (>3x avg) on a down day in uptrend = capitulation selling. Buy reversal. Exit: time or strength."},
    "demark_sequential": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Tom DeMark 'The New Science of Technical Analysis' (1994)",
        "description": "TD Sequential buy setup: 9 consecutive closes below close 4 bars earlier. Enter on bar 9. Exit on TD sell setup or time."},
    "gap_and_go": {"type": "momentum", "tier": 1, "status": "not_built",
        "reference": "Quantified Strategies gap research, related to Opening Gap",
        "description": "Buy stocks that gap UP > 2% at open with volume confirmation. Ride momentum. Exit: intraday trailing stop or close."},
    "relative_strength_pullback": {"type": "momentum", "tier": 1, "status": "not_built",
        "reference": "O'Neil CANSLIM (1988), Minervini 'Trade Like a Stock Market Wizard'",
        "description": "Stocks with relative strength rank > 80th percentile that pull back to 10-EMA. Enter on bounce. Exit: trailing stop."},
    "heikin_ashi_reversal": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Japanese candlestick patterns, quantified by Quantified Strategies",
        "description": "3+ red Heikin-Ashi candles followed by green doji/reversal in uptrend. Enter long. Exit on 2 red HA candles."},
    "vwap_reversion": {"type": "mean_reversion", "tier": 1, "status": "not_built",
        "reference": "Institutional VWAP trading, Quantified Strategies",
        "description": "Price > 2 std below daily VWAP in uptrending stock. Enter long. Exit at VWAP or above. Needs intraday-proxy via daily estimate."},
    "monthly_rotation": {"type": "rotation", "tier": 1, "status": "not_built",
        "reference": "Faber 'A Quantitative Approach to TAA' (2007), Antonacci dual momentum",
        "description": "Monthly rebalance: rank sectors/stocks by 6-month momentum. Hold top N. Rotate monthly. Cash filter: below SMA-200 → cash."},
    "put_call_vix_proxy": {"type": "sentiment", "tier": 1, "status": "not_built",
        "reference": "VIX fear gauge research, CBOE put/call ratio studies",
        "description": "VIX > 30 or VIX spike > 20% in 1 day → buy SPY/broad market. Exit when VIX drops below 20. Contrarian sentiment play."},
}

# ─── Filters available for combinatorial exploration ─────────────────────────

AVAILABLE_FILTERS = [
    "sma200_filter",      # Price > SMA-200 (uptrend)
    "ibs_threshold",      # IBS < 0.3 (selling exhaustion)
    "volume_surge",       # Volume > 2x average
    "market_breadth",     # % of stocks > SMA-50 > 60%
    "vix_regime_low",     # VIX < 20 (calm market)
    "vix_regime_high",    # VIX > 25 (fear = opportunity for MR)
    "tom_filter",         # Turn of month (last 3 + first 3 trading days)
]

# ─── Channel 1: Universe Checklist ──────────────────────────────────────────

def get_unbuilt_strategies() -> List[Dict[str, Any]]:
    """Return Tier 1 strategies with status 'not_built', ordered by type diversity."""
    unbuilt = []
    for name, info in STRATEGY_UNIVERSE.items():
        if info["status"] == "not_built":
            unbuilt.append({"name": name, **info})

    # Sort: prioritize types we have fewer of (diversity)
    type_counts = {}
    for info in STRATEGY_UNIVERSE.values():
        if info["status"] not in ("not_built", "broken", "dead_end"):
            t = info["type"]
            type_counts[t] = type_counts.get(t, 0) + 1

    unbuilt.sort(key=lambda s: type_counts.get(s["type"], 0))
    return unbuilt


def get_untested_existing() -> List[Dict[str, Any]]:
    """Return existing strategies that haven't completed solo testing."""
    untested = []
    for name, info in STRATEGY_UNIVERSE.items():
        if info["status"] in ("dormant", "untested", "screening") and info["tier"] == 0:
            untested.append({"name": name, **info})
    return untested


def update_strategy_status(strategy_name: str, new_status: str):
    """Update a strategy's status in the universe."""
    if strategy_name in STRATEGY_UNIVERSE:
        STRATEGY_UNIVERSE[strategy_name]["status"] = new_status
        # Also update the vault markdown
        _write_universe_vault_note()


# ─── Channel 3: Combinatorial Exploration ────────────────────────────────────

COMBINATION_LOG_PATH = ATLAS_ROOT / "research" / "vault" / "Meta" / "Combination Log.md"


def get_tested_combinations() -> set:
    """Read the combination log to know what's already been tested."""
    if not COMBINATION_LOG_PATH.exists():
        return set()
    content = COMBINATION_LOG_PATH.read_text()
    tested = set()
    for line in content.split("\n"):
        if line.startswith("| ") and " | " in line and "---" not in line and "Strategy" not in line:
            parts = [p.strip() for p in line.split("|") if p.strip()]
            if len(parts) >= 2:
                tested.add(f"{parts[0]}+{parts[1]}")
    return tested


def generate_filter_combinations(max_combos: int = 20) -> List[Dict[str, Any]]:
    """Generate untested filter-stacking combinations.

    For each active/passing strategy, try adding each filter it doesn't already use.
    Returns list of experiment specs ready to queue.
    """
    tested = get_tested_combinations()
    combos = []

    # Strategies eligible for filter stacking (active or passed solo)
    eligible = [name for name, info in STRATEGY_UNIVERSE.items()
                if info["status"] in ("active", "dormant", "screening")]

    for strategy_name in eligible:
        for filter_name in AVAILABLE_FILTERS:
            combo_key = f"{strategy_name}+{filter_name}"
            if combo_key in tested:
                continue
            combos.append({
                "strategy_name": strategy_name,
                "filter_name": filter_name,
                "combo_key": combo_key,
                "type": "filter_stack",
            })

    return combos[:max_combos]


def generate_param_cross_pollination(max_combos: int = 10) -> List[Dict[str, Any]]:
    """Generate param cross-pollination experiments.

    If a parameter value worked well for one strategy, try it on similar strategies.
    Reads parameter insight notes from vault.
    """
    params_dir = ATLAS_ROOT / "research" / "vault" / "Parameters"
    if not params_dir.exists():
        return []

    combos = []
    tested = get_tested_combinations()

    for note_path in params_dir.glob("*.md"):
        content = note_path.read_text()
        # Extract strategy and param from filename: "Strategy - param.md"
        name_parts = note_path.stem.split(" - ")
        if len(name_parts) != 2:
            continue
        source_strategy = name_parts[0].strip().lower().replace(" ", "_")
        param_name = name_parts[1].strip()

        # Find optimal value in the note
        optimal_match = re.search(r"Optimal.*?:\s*(\S+)", content)
        if not optimal_match:
            continue
        optimal_value = optimal_match.group(1)

        # Try this param on similar strategy types
        source_type = STRATEGY_UNIVERSE.get(source_strategy, {}).get("type", "")
        for target_name, target_info in STRATEGY_UNIVERSE.items():
            if target_name == source_strategy:
                continue
            if target_info["type"] != source_type:
                continue
            if target_info["status"] in ("not_built", "broken", "dead_end"):
                continue

            combo_key = f"{target_name}+{param_name}={optimal_value}"
            if combo_key in tested:
                continue
            combos.append({
                "strategy_name": target_name,
                "param_name": param_name,
                "param_value": optimal_value,
                "source_strategy": source_strategy,
                "combo_key": combo_key,
                "type": "param_cross_pollination",
            })

    return combos[:max_combos]


# ─── Channel 5: Ablation Studies ────────────────────────────────────────────

def generate_ablation_experiments(max_experiments: int = 10) -> List[Dict[str, Any]]:
    """Generate ablation experiments for strategies that passed combined test.

    For each passing strategy, create experiments that remove one component at a time:
    - Remove volume filter
    - Remove IBS check
    - Remove SMA-200 filter
    - Remove profit target

    Returns list of experiment specs.
    """
    journal_path = ATLAS_ROOT / "research" / "journal.json"
    if not journal_path.exists():
        return []

    journal = json.loads(journal_path.read_text())

    # Find strategies that passed combined test
    passed_combined = set()
    for entry in journal:
        if entry.get("verdict") == "pass" and "combined" in entry.get("category", "").lower():
            strat = entry.get("strategy")
            if strat:
                passed_combined.add(strat)

    # Components to ablate
    ablation_params = {
        "sma200_filter": False,
        "ibs_enabled": False,
        "profit_target_atr_mult": 0,
        "volume_filter_enabled": False,
    }

    experiments = []
    for strategy_name in passed_combined:
        for param_name, disabled_value in ablation_params.items():
            experiments.append({
                "strategy_name": strategy_name,
                "ablation_param": param_name,
                "ablation_value": disabled_value,
                "type": "ablation",
            })

    return experiments[:max_experiments]


# ─── Channel 6: Sensitivity/Robustness ──────────────────────────────────────

def generate_robustness_experiments(strategy_name: str = None) -> List[Dict[str, Any]]:
    """Generate sensitivity tests for promoted/candidate configs.

    Tests: fee levels, slippage, universe size, walk-forward window, starting equity.
    """
    experiments = []

    fee_levels = [0, 1.0, 5.0]
    equity_levels = [2000, 4000, 10000, 25000, 50000]

    target = strategy_name or "combined"

    for fee in fee_levels:
        experiments.append({
            "strategy_name": target,
            "test_type": "fee_sensitivity",
            "fee_per_trade": fee,
            "type": "robustness",
        })

    for equity in equity_levels:
        experiments.append({
            "strategy_name": target,
            "test_type": "equity_sensitivity",
            "starting_equity": equity,
            "type": "robustness",
        })

    return experiments


# ─── Priority Dispatcher ────────────────────────────────────────────────────

def get_next_experiments(max_count: int = 5) -> List[QueueEntry]:
    """Priority-ordered experiment generation.

    Called by the daemon when queue is low. Returns up to max_count
    experiments to queue, ordered by priority.

    Priority order:
    1. Untested existing strategies (Tier 0) → queue solo test
    2. Unbuilt Tier 1 strategies → return spec for factory (needs LLM)
    3. Combinatorial exploration → queue filter/param combos
    4. Ablation studies → queue component removal tests
    5. Robustness tests → queue fee/equity sensitivity

    Note: Web discovery (Channel 2) and hypothesis generation (Channel 4)
    are handled by the coordinator agent, not the daemon.
    """
    experiments = []

    # 1. Untested existing strategies
    untested = get_untested_existing()
    for strat in untested[:max_count - len(experiments)]:
        entry = QueueEntry(
            id=generate_experiment_id(),
            title=f"Solo test: {strat['name']}",
            category="dormant",
            market="sp500",
            hypothesis=f"Test if {strat['name']} is viable as standalone strategy",
            method=ExperimentType.SINGLE_STRATEGY_TEST,
            acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15, "max_max_drawdown_pct": 15},
            estimated_runtime_min=5,
            priority=Priority.P2_HIGH,
            strategy_name=strat["name"],
        )
        experiments.append(entry)
        if len(experiments) >= max_count:
            break

    # 2. Unbuilt Tier 1 (return as specs — daemon needs to call factory first)
    if len(experiments) < max_count:
        unbuilt = get_unbuilt_strategies()
        for strat in unbuilt[:max_count - len(experiments)]:
            entry = QueueEntry(
                id=generate_experiment_id(),
                title=f"Build + screen: {strat['name']}",
                category="new_strategy",
                market="sp500",
                hypothesis=f"Build {strat['name']} from {strat['reference']} and test viability",
                method=ExperimentType.SINGLE_STRATEGY_TEST,
                acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15, "max_max_drawdown_pct": 15},
                estimated_runtime_min=10,
                priority=Priority.P3_MEDIUM,
                strategy_name=strat["name"],
                notes=f"NEEDS_BUILD: {strat.get('description', '')}",
            )
            experiments.append(entry)
            if len(experiments) >= max_count:
                break

    # 3. Combinatorial exploration
    if len(experiments) < max_count:
        combos = generate_filter_combinations(max_combos=max_count - len(experiments))
        for combo in combos:
            entry = QueueEntry(
                id=generate_experiment_id(),
                title=f"Filter combo: {combo['strategy_name']} + {combo['filter_name']}",
                category="combinatorial",
                market="sp500",
                hypothesis=f"Adding {combo['filter_name']} to {combo['strategy_name']} improves Sharpe",
                method=ExperimentType.FILTER_TEST,
                acceptance_criteria={"min_sharpe": 0.3, "min_trades": 10},
                estimated_runtime_min=5,
                priority=Priority.P4_LOW,
                strategy_name=combo["strategy_name"],
                params_override={combo["filter_name"]: True},
            )
            experiments.append(entry)
            if len(experiments) >= max_count:
                break

    # 4. Ablation studies
    if len(experiments) < max_count:
        ablations = generate_ablation_experiments(max_experiments=max_count - len(experiments))
        for abl in ablations:
            entry = QueueEntry(
                id=generate_experiment_id(),
                title=f"Ablation: {abl['strategy_name']} without {abl['ablation_param']}",
                category="ablation",
                market="sp500",
                hypothesis=f"Removing {abl['ablation_param']} from {abl['strategy_name']} reveals its contribution",
                method=ExperimentType.SINGLE_STRATEGY_TEST,
                acceptance_criteria={"min_sharpe": -999},  # no threshold — measuring contribution
                estimated_runtime_min=5,
                priority=Priority.P4_LOW,
                strategy_name=abl["strategy_name"],
                params_override={abl["ablation_param"]: abl["ablation_value"]},
            )
            experiments.append(entry)
            if len(experiments) >= max_count:
                break

    # 5. Robustness tests (lowest priority)
    if len(experiments) < max_count:
        robust = generate_robustness_experiments()[:max_count - len(experiments)]
        for r in robust:
            entry = QueueEntry(
                id=generate_experiment_id(),
                title=f"Robustness: {r['test_type']} ({r.get('fee_per_trade', r.get('starting_equity', '?'))})",
                category="robustness",
                market="sp500",
                hypothesis=f"Test sensitivity of {r['strategy_name']} to {r['test_type']}",
                method=ExperimentType.COMBINED_PORTFOLIO_TEST,
                acceptance_criteria={},
                estimated_runtime_min=5,
                priority=Priority.P5_BACKLOG,
                strategy_name=r["strategy_name"],
            )
            experiments.append(entry)
            if len(experiments) >= max_count:
                break

    return experiments


def queue_discovery_batch(max_count: int = 5) -> int:
    """Generate and queue up to max_count experiments. Returns count queued."""
    experiments = get_next_experiments(max_count)
    queued = 0
    for entry in experiments:
        try:
            append_to_queue(entry, skip_validation=True)
            queued += 1
            logger.info("Queued discovery experiment: %s", entry.title)
        except Exception as e:
            logger.error("Failed to queue %s: %s", entry.title, e)
    return queued


# ─── Vault Helpers ───────────────────────────────────────────────────────────

def _write_universe_vault_note():
    """Write/update the Strategy Universe vault note."""
    output_path = STRATEGY_UNIVERSE_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        "tags: [meta, strategy-universe]",
        f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "---",
        "",
        "# Strategy Universe",
        "",
        "Master registry of all trading strategies — existing and planned.",
        "",
        "## Summary",
        "",
        f"- **Total strategies**: {len(STRATEGY_UNIVERSE)}",
        f"- **Active**: {sum(1 for s in STRATEGY_UNIVERSE.values() if s['status'] == 'active')}",
        f"- **Not built**: {sum(1 for s in STRATEGY_UNIVERSE.values() if s['status'] == 'not_built')}",
        f"- **Dead end**: {sum(1 for s in STRATEGY_UNIVERSE.values() if s['status'] == 'dead_end')}",
        "",
        "## Registry",
        "",
        "| Strategy | Type | Tier | Status | Reference |",
        "|----------|------|------|--------|-----------|",
    ]

    for name, info in sorted(STRATEGY_UNIVERSE.items()):
        status_emoji = {
            "active": "🟢", "dormant": "🟡", "screening": "🔵",
            "not_built": "⬜", "broken": "🔴", "dead_end": "⚫",
            "untested": "⬜", "solo": "🔵", "optimize": "🔵",
            "combined": "🔵", "oos": "🔵",
        }.get(info["status"], "❓")

        lines.append(
            f"| [[{name.replace('_', ' ').title()}]] | {info['type']} | {info['tier']} | "
            f"{status_emoji} {info['status']} | {info['reference'][:50]} |"
        )

    lines.extend(["", "## Tier 1 Descriptions", ""])
    for name, info in sorted(STRATEGY_UNIVERSE.items()):
        if info.get("description"):
            lines.append(f"### {name.replace('_', ' ').title()}")
            lines.append(f"**Reference:** {info['reference']}")
            lines.append(f"**Description:** {info['description']}")
            lines.append("")

    output_path.write_text("\n".join(lines))


def log_combination(combo_key: str, result: str):
    """Append a tested combination to the log."""
    COMBINATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not COMBINATION_LOG_PATH.exists():
        COMBINATION_LOG_PATH.write_text(
            "---\ntags: [meta, combinations]\n---\n\n"
            "# Combination Log\n\n"
            "| Strategy | Filter/Param | Result | Date |\n"
            "|----------|-------------|--------|------|\n"
        )

    parts = combo_key.split("+", 1)
    strategy = parts[0] if parts else combo_key
    filter_param = parts[1] if len(parts) > 1 else "?"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    with open(COMBINATION_LOG_PATH, "a") as f:
        f.write(f"| {strategy} | {filter_param} | {result} | {date} |\n")


# ─── Initialize on import ───────────────────────────────────────────────────

def init_vault_notes():
    """Create initial vault notes if they don't exist."""
    if not STRATEGY_UNIVERSE_PATH.exists():
        _write_universe_vault_note()
    if not COMBINATION_LOG_PATH.exists():
        log_combination("_init", "initialized")
