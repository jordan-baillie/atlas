#!/usr/bin/env python3
"""Self-Annealing Improvement Loop.

At the end of each trading day (paper), runs:
1. Review: Summarize performance and drawdown
2. Hypothesis: Propose 1-2 small, testable changes
3. Experiment: Backtest changes with walk-forward validation
4. Promote or Reject: Based on OOS improvement
5. Versioning: Keep versioned configs and changelog

Constraint: Improvements must be incremental, measured, reversible.
"""

import sys
import json
import logging
import copy
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd
import numpy as np

from utils.config import get_active_config, save_config_version, list_versions
from utils.helpers import format_aud
from journal.logger import TradeLedger, MistakeLog, DecisionJournal, WeeklySummary
from strategies.momentum_breakout import MomentumBreakout
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from backtest.engine import BacktestEngine

logger = logging.getLogger("atlas.anneal")

CHANGELOG_FILE = PROJECT_ROOT / "journal" / "changelog.json"


def load_changelog() -> list:
    if CHANGELOG_FILE.exists():
        try:
            with open(CHANGELOG_FILE) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_changelog(entries: list):
    CHANGELOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CHANGELOG_FILE, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def load_data_for_backtest(config: dict) -> dict:
    """Load cached data for backtesting."""
    cache_dir = PROJECT_ROOT / config["data"]["cache_dir"]
    data = {}
    if cache_dir.exists():
        for f in cache_dir.iterdir():
            if f.suffix == ".parquet":
                ticker = f.stem.replace("_AX", ".AX")
                data[ticker] = pd.read_parquet(f)
    return data


def run_backtest_with_config(config: dict, data: dict) -> dict:
    """Run backtest with given config, return metrics."""
    strategies = []
    sc = config["strategies"]
    if sc["momentum_breakout"]["enabled"]:
        strategies.append(MomentumBreakout(config))
    if sc["mean_reversion"]["enabled"]:
        strategies.append(MeanReversion(config))
    if sc["trend_following"]["enabled"]:
        strategies.append(TrendFollowing(config))

    engine = BacktestEngine(config)
    result = engine.run_walkforward(data, strategies)

    metrics = result.metrics if hasattr(result, "metrics") else result.get("metrics", {})
    return metrics


# ═══════════════════════════════════════════════════════════════
# HYPOTHESIS GENERATION
# ═══════════════════════════════════════════════════════════════

def generate_hypotheses(config: dict, mistake_summary: dict,
                        perf_summary: dict) -> list:
    """Generate 1-2 small, testable parameter changes based on mistakes.

    Returns list of dicts with:
        - name: short description
        - changes: dict of config path -> new value
        - rationale: why this change
    """
    hypotheses = []
    top_mistakes = mistake_summary.get("top_categories", [])

    # Map mistake categories to parameter adjustments
    for mistake in top_mistakes[:2]:  # Max 2 changes
        cat = mistake["category"]
        count = mistake["count"]
        impact = mistake["total_impact"]

        if cat == "false_breakout":
            # Tighten breakout confirmation
            current_vol = config["strategies"]["momentum_breakout"]["volume_confirmation_mult"]
            new_vol = round(current_vol + 0.25, 2)
            hypotheses.append({
                "name": f"Increase volume confirmation {current_vol} -> {new_vol}",
                "changes": {
                    "strategies.momentum_breakout.volume_confirmation_mult": new_vol,
                },
                "rationale": f"{count} false breakouts (impact ${impact:.2f}). "
                            f"Requiring stronger volume confirmation.",
            })

        elif cat == "stop_too_tight":
            # Widen stops
            for strat in ["momentum_breakout", "mean_reversion", "trend_following"]:
                current = config["strategies"][strat]["atr_stop_mult"]
                new_val = round(current + 0.25, 2)
                hypotheses.append({
                    "name": f"Widen {strat} stop {current} -> {new_val} ATR",
                    "changes": {
                        f"strategies.{strat}.atr_stop_mult": new_val,
                    },
                    "rationale": f"{count} premature stops (impact ${impact:.2f}). "
                                f"Widening ATR stop multiplier.",
                })
                break  # Only one change per mistake

        elif cat == "stop_too_wide":
            for strat in ["momentum_breakout", "mean_reversion", "trend_following"]:
                current = config["strategies"][strat]["atr_stop_mult"]
                new_val = round(max(1.0, current - 0.25), 2)
                hypotheses.append({
                    "name": f"Tighten {strat} stop {current} -> {new_val} ATR",
                    "changes": {
                        f"strategies.{strat}.atr_stop_mult": new_val,
                    },
                    "rationale": f"{count} wide stop losses (impact ${impact:.2f}). "
                                f"Tightening ATR stop multiplier.",
                })
                break

        elif cat == "held_too_long":
            for strat in ["momentum_breakout", "mean_reversion", "trend_following"]:
                current = config["strategies"][strat]["max_hold_days"]
                new_val = max(2, current - 1)
                if new_val != current:
                    hypotheses.append({
                        "name": f"Reduce {strat} max hold {current} -> {new_val} days",
                        "changes": {
                            f"strategies.{strat}.max_hold_days": new_val,
                        },
                        "rationale": f"{count} trades held too long (impact ${impact:.2f}). "
                                    f"Reducing max holding period.",
                    })
                    break

        elif cat == "regime_mismatch":
            # Adjust MA periods for trend detection
            current_slow = config["strategies"]["trend_following"]["slow_ma"]
            new_slow = current_slow + 5
            hypotheses.append({
                "name": f"Increase trend slow MA {current_slow} -> {new_slow}",
                "changes": {
                    "strategies.trend_following.slow_ma": new_slow,
                },
                "rationale": f"{count} regime mismatches (impact ${impact:.2f}). "
                            f"Using longer MA for trend detection.",
            })

        elif cat == "volatility_spike":
            # Widen trailing stops
            for strat in ["momentum_breakout", "trend_following"]:
                key = "trailing_stop_atr_mult"
                if key in config["strategies"][strat]:
                    current = config["strategies"][strat][key]
                    new_val = round(current + 0.5, 2)
                    hypotheses.append({
                        "name": f"Widen {strat} trailing stop {current} -> {new_val} ATR",
                        "changes": {
                            f"strategies.{strat}.{key}": new_val,
                        },
                        "rationale": f"{count} volatility spikes (impact ${impact:.2f}). "
                                    f"Widening trailing stop.",
                    })
                    break

        elif cat == "liquidity_issue":
            current_min = config["universe"]["min_median_daily_value"]
            new_min = int(current_min * 1.5)
            hypotheses.append({
                "name": f"Increase min daily value {current_min:,} -> {new_min:,}",
                "changes": {
                    "universe.min_median_daily_value": new_min,
                },
                "rationale": f"{count} liquidity issues (impact ${impact:.2f}). "
                            f"Raising minimum daily traded value filter.",
            })

    # If no mistake-driven hypotheses, try generic improvements
    if not hypotheses:
        perf = perf_summary
        if perf.get("win_rate", 50) < 40:
            # Low win rate -> tighten entry criteria
            current_rsi = config["strategies"]["mean_reversion"]["rsi_oversold"]
            new_rsi = max(20, current_rsi - 5)
            hypotheses.append({
                "name": f"Tighten mean reversion RSI {current_rsi} -> {new_rsi}",
                "changes": {
                    "strategies.mean_reversion.rsi_oversold": new_rsi,
                },
                "rationale": f"Win rate {perf.get('win_rate', 0):.1f}% is low. "
                            f"Requiring deeper oversold for mean reversion entries.",
            })

        if perf.get("profit_factor", 1) < 1.0:
            # Negative expectancy -> reduce position sizes via tighter risk
            hypotheses.append({
                "name": "No specific hypothesis - system under review",
                "changes": {},
                "rationale": f"Profit factor {perf.get('profit_factor', 0):.2f} < 1.0. "
                            f"System needs broader review. No parameter change proposed.",
            })

    # Limit to max changes per cycle
    max_changes = config.get("annealing", {}).get("max_changes_per_cycle", 2)
    return hypotheses[:max_changes]


def apply_changes(config: dict, changes: dict) -> dict:
    """Apply dotted-path changes to config."""
    new_config = copy.deepcopy(config)
    for path, value in changes.items():
        keys = path.split(".")
        obj = new_config
        for key in keys[:-1]:
            obj = obj[key]
        obj[keys[-1]] = value
    return new_config


# ═══════════════════════════════════════════════════════════════
# MAIN ANNEALING CYCLE
# ═══════════════════════════════════════════════════════════════

def run_annealing_cycle():
    """Execute one full self-annealing cycle."""
    print("\n" + "═" * 60)
    print("  SELF-ANNEALING REVIEW CYCLE")
    print("═" * 60)

    config = get_active_config()
    anneal_config = config.get("annealing", {})

    # ── Step 1: Review ────────────────────────────────────────
    print("\n📊 STEP 1: REVIEW")

    ledger = TradeLedger()
    mistake_log = MistakeLog()
    decision_journal = DecisionJournal()

    perf = ledger.performance_summary(days=7)
    mistakes = mistake_log.summary(days=7)
    decisions = decision_journal.summary(days=7)

    print(f"   Trades (7d): {perf.get('total_trades', 0)}")
    print(f"   Win rate: {perf.get('win_rate', 0):.1f}%")
    print(f"   PnL: {format_aud(perf.get('total_pnl', 0))}")
    print(f"   Profit factor: {perf.get('profit_factor', 0):.2f}")
    print(f"   Mistakes: {mistakes.get('total_mistakes', 0)}")

    if mistakes.get("top_categories"):
        print(f"   Top mistake categories:")
        for cat in mistakes["top_categories"]:
            print(f"     • {cat['category']}: {cat['count']}x, "
                  f"{format_aud(cat['total_impact'])}")

    # ── Step 2: Hypothesis ────────────────────────────────────
    print("\n🔬 STEP 2: HYPOTHESIS")

    hypotheses = generate_hypotheses(config, mistakes, perf)

    if not hypotheses:
        print("   No hypotheses generated. System performing within bounds.")
        print("   Cycle complete — no changes.")
        return

    for i, h in enumerate(hypotheses):
        print(f"   Hypothesis {i+1}: {h['name']}")
        print(f"   Rationale: {h['rationale']}")
        if h["changes"]:
            for path, val in h["changes"].items():
                print(f"     Change: {path} = {val}")

    # ── Step 3: Experiment ────────────────────────────────────
    print("\n🧪 STEP 3: EXPERIMENT")

    data = load_data_for_backtest(config)
    if not data:
        print("   ❌ No data for backtesting. Skipping experiment.")
        return

    # Baseline backtest
    print("   Running baseline backtest...")
    baseline_metrics = run_backtest_with_config(config, data)
    baseline_sharpe = baseline_metrics.get("sharpe", 0)
    baseline_dd = baseline_metrics.get("max_drawdown", 0)
    print(f"   Baseline: Sharpe={baseline_sharpe:.3f}, MaxDD={baseline_dd:.2%}")

    # Test each hypothesis
    results = []
    for i, h in enumerate(hypotheses):
        if not h["changes"]:
            results.append({"hypothesis": h, "promoted": False,
                          "reason": "No changes to test"})
            continue

        print(f"   Testing hypothesis {i+1}: {h['name']}...")
        test_config = apply_changes(config, h["changes"])
        test_metrics = run_backtest_with_config(test_config, data)

        test_sharpe = test_metrics.get("sharpe", 0)
        test_dd = test_metrics.get("max_drawdown", 0)
        print(f"   Result: Sharpe={test_sharpe:.3f}, MaxDD={test_dd:.2%}")

        results.append({
            "hypothesis": h,
            "baseline_sharpe": baseline_sharpe,
            "test_sharpe": test_sharpe,
            "baseline_dd": baseline_dd,
            "test_dd": test_dd,
            "sharpe_delta": test_sharpe - baseline_sharpe,
            "dd_delta": test_dd - baseline_dd,
        })

    # ── Step 4: Promote or Reject ─────────────────────────────
    print("\n📋 STEP 4: PROMOTE / REJECT")

    min_sharpe_improvement = anneal_config.get("min_oos_sharpe_improvement", 0.05)
    max_dd_increase = anneal_config.get("max_drawdown_increase_pct", 0.01)

    promoted_changes = {}
    changelog_entries = []

    for r in results:
        h = r["hypothesis"]

        if "reason" in r:
            print(f"   ❌ SKIP: {h['name']} — {r['reason']}")
            changelog_entries.append({
                "timestamp": datetime.now().isoformat(),
                "hypothesis": h["name"],
                "action": "skipped",
                "reason": r["reason"],
            })
            continue

        sharpe_improved = r["sharpe_delta"] >= min_sharpe_improvement
        dd_acceptable = r["dd_delta"] <= max_dd_increase

        if sharpe_improved and dd_acceptable:
            print(f"   ✅ PROMOTE: {h['name']}")
            print(f"      Sharpe: {r['baseline_sharpe']:.3f} -> {r['test_sharpe']:.3f} "
                  f"(+{r['sharpe_delta']:.3f})")
            print(f"      MaxDD: {r['baseline_dd']:.2%} -> {r['test_dd']:.2%}")
            promoted_changes.update(h["changes"])
            changelog_entries.append({
                "timestamp": datetime.now().isoformat(),
                "hypothesis": h["name"],
                "action": "promoted",
                "sharpe_delta": round(r["sharpe_delta"], 4),
                "dd_delta": round(r["dd_delta"], 4),
                "changes": h["changes"],
                "rationale": h["rationale"],
            })
        elif sharpe_improved and not dd_acceptable:
            print(f"   ❌ REJECT: {h['name']} — drawdown increased too much")
            print(f"      Sharpe improved +{r['sharpe_delta']:.3f} but "
                  f"DD worsened {r['dd_delta']:.2%} > {max_dd_increase:.2%}")
            changelog_entries.append({
                "timestamp": datetime.now().isoformat(),
                "hypothesis": h["name"],
                "action": "rejected",
                "reason": f"DD increase {r['dd_delta']:.4f} > max {max_dd_increase}",
                "sharpe_delta": round(r["sharpe_delta"], 4),
                "dd_delta": round(r["dd_delta"], 4),
            })
        else:
            print(f"   ❌ REJECT: {h['name']} — insufficient Sharpe improvement")
            print(f"      Sharpe delta: {r['sharpe_delta']:+.3f} "
                  f"(need >= +{min_sharpe_improvement:.3f})")
            changelog_entries.append({
                "timestamp": datetime.now().isoformat(),
                "hypothesis": h["name"],
                "action": "rejected",
                "reason": f"Sharpe delta {r['sharpe_delta']:.4f} < min {min_sharpe_improvement}",
                "sharpe_delta": round(r["sharpe_delta"], 4),
            })

    # ── Step 5: Version and Save ──────────────────────────────
    print("\n💾 STEP 5: VERSIONING")

    if promoted_changes:
        new_config = apply_changes(config, promoted_changes)
        new_version = save_config_version(new_config)
        print(f"   New config version: {new_version}")
        print(f"   Changes applied: {len(promoted_changes)}")
        for path, val in promoted_changes.items():
            print(f"     {path} = {val}")
    else:
        print("   No changes promoted. Config unchanged.")

    # Save changelog
    changelog = load_changelog()
    changelog.extend(changelog_entries)
    save_changelog(changelog)
    print(f"   Changelog updated: {len(changelog_entries)} entries added")

    print("\n✅ Annealing cycle complete.")
    return {
        "hypotheses": hypotheses,
        "results": results,
        "promoted_changes": promoted_changes,
        "changelog_entries": changelog_entries,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_annealing_cycle()
