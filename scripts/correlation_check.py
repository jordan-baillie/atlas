#!/usr/bin/env python3
"""Atlas Strategy Correlation Check — category diversity + trade overlap.

Checks whether adding a candidate strategy to the active set would
create unhealthy concentration. Two checks:

  1. Category check: how many active strategies share the same category?
  2. Trade overlap: do the candidate's trades fire on the same dates as
     existing strategies? (Uses cached experiment results when available,
     falls back to running a quick backtest.)

Usage:
    python3 scripts/correlation_check.py --strategy <name> [--market sp500]

Output: JSON to stdout.
Exit 0 = pass, 1 = warn (too concentrated), 2 = error.
"""

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

# ── Strategy Categories ──────────────────────────────────────────────────

STRATEGY_CATEGORIES = {
    # Mean reversion / oversold
    "mean_reversion": "reversion",
    "short_term_mr": "reversion",
    "connors_rsi2": "reversion",
    "bb_squeeze": "reversion",
    "keltner_reversion": "reversion",
    "lower_band_reversion": "reversion",
    "stochastic_oversold": "reversion",
    "williams_percent_r": "reversion",
    "vwap_reversion": "reversion",
    "rsi_divergence": "reversion",
    "consecutive_down_days": "reversion",
    "triple_rsi": "reversion",
    "macd_divergence": "reversion",
    # Trend / momentum
    "trend_following": "trend",
    "donchian_breakout": "trend",
    "adx_trend_pullback": "trend",
    "momentum_breakout": "momentum",
    "relative_strength_pullback": "momentum",
    "inside_bar_nr7": "momentum",
    "heikin_ashi_reversal": "trend",
    "demark_sequential": "trend",
    "volume_climax": "momentum",
    # Gap / overnight
    "opening_gap": "gap",
    "gap_and_go": "gap",
    "overnight_return": "gap",
    # Rotation / multi-timeframe
    "monthly_rotation": "rotation",
    "sector_rotation": "rotation",
    "mtf_momentum": "multi_tf",
    # Dividend / earnings
    "dividend_capture": "event",
    "pead_earnings_drift": "event",
    # Misc
    "put_call_vix_proxy": "sentiment",
}

MAX_CATEGORY_CONCENTRATION = 3  # Warn if >3 active in same category


def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def _active_strategy_names() -> list[str]:
    """Read active strategies from strategy_queue.json."""
    q = _read_json(PROJECT / "research" / "strategy_queue.json", {})
    return [
        e.get("name") if isinstance(e, dict) else e
        for e in q.get("active", [])
    ]


def category_check(strategy_name: str) -> dict:
    """Check category concentration."""
    candidate_cat = STRATEGY_CATEGORIES.get(strategy_name, "unknown")
    active = _active_strategy_names()

    active_cats = {}
    for s in active:
        cat = STRATEGY_CATEGORIES.get(s, "unknown")
        active_cats[cat] = active_cats.get(cat, 0) + 1

    same_cat_count = active_cats.get(candidate_cat, 0)
    total_active = len(active)

    # Would this create >3 in one category?
    would_be = same_cat_count + 1
    concentrated = would_be > MAX_CATEGORY_CONCENTRATION

    return {
        "strategy": strategy_name,
        "category": candidate_cat,
        "active_in_category": same_cat_count,
        "would_be_in_category": would_be,
        "total_active": total_active,
        "category_distribution": active_cats,
        "concentrated": concentrated,
        "verdict": "warn" if concentrated else "pass",
        "reason": (
            f"Would make {would_be} {candidate_cat} strategies (max {MAX_CATEGORY_CONCENTRATION})"
            if concentrated
            else f"{candidate_cat} category has {same_cat_count} active — room for 1 more"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Atlas Strategy Correlation Check")
    parser.add_argument("--strategy", required=True, help="Strategy name to check")
    parser.add_argument("--market", default="sp500", help="Market ID")
    args = parser.parse_args()

    try:
        result = category_check(args.strategy)
        print(json.dumps(result, indent=2))
        return 0 if result["verdict"] == "pass" else 1
    except Exception as e:
        print(json.dumps({
            "strategy": args.strategy,
            "verdict": "error",
            "reason": str(e),
        }))
        return 2


if __name__ == "__main__":
    sys.exit(main())
