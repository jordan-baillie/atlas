#!/usr/bin/env python3
"""
Atlas Research Wave Planner
============================
Analyzes completed wave results and produces a structured brief for the
Pi agent to use when designing the next research wave.

Output: research/waves/wave_N_brief.json — consumed by the research-loop skill.

This script does NOT generate experiments itself. It gathers the data and
context that the Pi agent needs to make informed decisions about what to
research next.

Usage:
    python3 scripts/wave_planner.py                  # Generate brief
    python3 scripts/wave_planner.py --status          # Show current state
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from research.models import atomic_json_write

RESEARCH_DIR = PROJECT_ROOT / "research"
WAVES_DIR = RESEARCH_DIR / "waves"
QUEUE_PATH = RESEARCH_DIR / "queue.json"
JOURNAL_PATH = RESEARCH_DIR / "journal.json"


def load_json(path, default=None):
    if not path.exists():
        return default if default is not None else []
    with open(path) as f:
        return json.load(f)


def get_current_wave_number() -> int:
    """Determine next wave number from existing wave files.

    If the latest wave brief is still 'pending' (not yet executed),
    return that wave number so the agent plans it instead of creating
    a new one. Only increment when the latest wave is complete.
    """
    WAVES_DIR.mkdir(parents=True, exist_ok=True)
    existing = sorted(WAVES_DIR.glob("wave_*_brief.json"))
    if not existing:
        return 2  # Wave 1 was manually seeded

    # Check if the latest wave is still pending — reuse it
    latest = existing[-1]
    try:
        data = json.load(open(latest))
        if data.get("status") == "pending":
            return data.get("wave_number", 2)
    except Exception:
        pass

    nums = []
    for p in existing:
        try:
            nums.append(int(p.stem.split("_")[1]))
        except (IndexError, ValueError):
            pass
    return max(nums, default=1) + 1


def analyze_journal() -> dict:
    """Extract learnings, patterns, and gaps from the research journal."""
    journal = load_json(JOURNAL_PATH, [])
    if not journal:
        return {"total": 0, "findings": [], "gaps": [], "patterns": []}

    findings = []
    strategies_tested = set()
    strategies_passed = set()
    strategies_failed = set()
    all_learnings = []
    metrics_by_strategy = {}

    for entry in journal:
        strat = entry.get("strategy") or "unknown"
        verdict = entry.get("verdict") or "unknown"
        km = entry.get("key_metrics", {})
        learnings = entry.get("learnings", [])

        strategies_tested.add(strat)
        if verdict == "pass":
            strategies_passed.add(strat)
        elif verdict == "fail":
            strategies_failed.add(strat)

        all_learnings.extend(learnings)

        if strat not in metrics_by_strategy:
            metrics_by_strategy[strat] = []
        metrics_by_strategy[strat].append({
            "experiment": entry.get("experiment_id"),
            "verdict": verdict,
            "sharpe": km.get("sharpe"),
            "cagr_pct": km.get("cagr_pct"),
            "max_drawdown_pct": km.get("max_drawdown_pct"),
            "total_trades": km.get("total_trades"),
            "win_rate_pct": km.get("win_rate_pct"),
            "profit_factor": km.get("profit_factor"),
        })

        findings.append({
            "experiment": entry.get("experiment_id"),
            "strategy": strat,
            "category": entry.get("category"),
            "verdict": verdict,
            "key_insight": learnings[0] if learnings else f"{strat} {verdict}",
        })

    # Identify gaps — what hasn't been tested
    from utils.config import get_active_config
    config = get_active_config("sp500")
    all_strategies = list(config.get("strategies", {}).keys())
    untested = [s for s in all_strategies if s not in strategies_tested]

    # Identify patterns
    patterns = []
    if strategies_failed:
        patterns.append(f"Failed strategies: {', '.join(s for s in strategies_failed if s)}")
    if strategies_passed:
        patterns.append(f"Passed strategies: {', '.join(s for s in strategies_passed if s)}")

    # Check for position allocation bottleneck
    fail_reasons = [e.get("key_metrics", {}).get("total_trades", 0)
                    for e in journal if e.get("verdict") == "fail"]
    if any(t > 300 for t in fail_reasons):
        patterns.append("High trade count strategies fail combined tests — position allocation bottleneck")

    return {
        "total_experiments": len(journal),
        "strategies_tested": sorted(strategies_tested),
        "strategies_passed": sorted(strategies_passed),
        "strategies_failed": sorted(strategies_failed),
        "untested_strategies": untested,
        "all_learnings": all_learnings,
        "findings": findings,
        "metrics_by_strategy": metrics_by_strategy,
        "patterns": patterns,
        "gaps": untested,
    }


def analyze_queue() -> dict:
    """Analyze the current queue state."""
    queue = load_json(QUEUE_PATH, [])
    status_counts = Counter(e.get("status", "unknown") for e in queue)
    return {
        "total": len(queue),
        "by_status": dict(status_counts),
        "queued_count": status_counts.get("queued", 0),
        "completed_count": sum(status_counts.get(s, 0) for s in
                               ["passed", "failed", "deferred", "rejected", "promoted"]),
    }


def get_active_config_summary() -> dict:
    """Summarize the current active trading config."""
    from utils.config import get_active_config
    config = get_active_config("sp500")
    strategies = config.get("strategies", {})
    enabled = [name for name, cfg in strategies.items() if cfg.get("enabled")]
    disabled = [name for name, cfg in strategies.items() if not cfg.get("enabled")]
    risk = config.get("risk", {})
    return {
        "version": config.get("version", "unknown"),
        "enabled_strategies": enabled,
        "disabled_strategies": disabled,
        "max_positions": risk.get("max_open_positions", 10),
        "starting_equity": risk.get("starting_equity", 4000),
        "max_risk_per_trade_pct": risk.get("max_risk_per_trade_pct", 0.005),
    }


def generate_brief() -> dict:
    """Generate a research brief for the Pi agent to use for wave planning."""
    wave_num = get_current_wave_number()
    journal_analysis = analyze_journal()
    queue_analysis = analyze_queue()
    config_summary = get_active_config_summary()

    brief = {
        "wave_number": wave_num,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",  # pending → planned → seeded → completed

        "previous_findings": journal_analysis,
        "queue_state": queue_analysis,
        "active_config": config_summary,

        # These get filled by the Pi agent after web research
        "theme": None,
        "theme_rationale": None,
        "web_research_queries": [],
        "web_research_findings": [],
        "experiments": [],  # Filled by agent
    }

    # Save brief — but if this wave already has a pending brief, reuse it
    WAVES_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = WAVES_DIR / f"wave_{wave_num}_brief.json"
    if brief_path.exists():
        try:
            existing = json.load(open(brief_path))
            if existing.get("status") == "pending":
                print(f"Wave {wave_num} brief already exists (pending) — reusing")
                return existing
        except Exception:
            pass

    atomic_json_write(brief_path, brief)

    return brief


def show_status():
    """Print current research pipeline status."""
    queue = analyze_queue()
    journal = analyze_journal()

    print("\n" + "=" * 55)
    print("  ATLAS RESEARCH STATUS")
    print("=" * 55)
    print(f"\n  Queue: {queue['total']} total, {queue['queued_count']} queued, {queue['completed_count']} completed")
    print(f"  Journal: {journal['total_experiments']} experiments logged")
    print(f"  Strategies tested: {', '.join(s for s in journal['strategies_tested'] if s) or 'none'}")
    print(f"  Strategies passed: {', '.join(s for s in journal['strategies_passed'] if s) or 'none'}")
    print(f"  Strategies failed: {', '.join(s for s in journal['strategies_failed'] if s) or 'none'}")
    print(f"  Untested: {', '.join(s for s in journal['untested_strategies'] if s) or 'none'}")

    if journal["patterns"]:
        print("\n  Patterns:")
        for p in journal["patterns"]:
            print(f"    • {p}")

    if journal["all_learnings"]:
        print(f"\n  Key Learnings ({len(journal['all_learnings'])}):")
        for l in journal["all_learnings"][:10]:
            print(f"    • {l}")

    # Wave history
    waves = sorted(WAVES_DIR.glob("wave_*_brief.json")) if WAVES_DIR.exists() else []
    if waves:
        print(f"\n  Wave History:")
        for wp in waves:
            wb = load_json(wp, {})
            theme = wb.get("theme", "not yet planned")
            status = wb.get("status", "?")
            n_exp = len(wb.get("experiments", []))
            print(f"    Wave {wb.get('wave_number', '?')}: {theme} ({status}, {n_exp} experiments)")

    next_wave = get_current_wave_number()
    print(f"\n  Next wave: {next_wave}")


def main():
    parser = argparse.ArgumentParser(description="Atlas Research Wave Planner")
    parser.add_argument("--status", action="store_true", help="Show current research status")
    parser.add_argument("--generate", action="store_true", help="Generate brief for next wave")
    args = parser.parse_args()

    if args.status:
        show_status()
        return

    brief = generate_brief()
    print(f"Wave {brief['wave_number']} brief generated: research/waves/wave_{brief['wave_number']}_brief.json")
    print(f"  Previous findings: {brief['previous_findings']['total_experiments']} experiments")
    print(f"  Patterns: {len(brief['previous_findings']['patterns'])}")
    print(f"  Gaps: {brief['previous_findings']['gaps']}")


if __name__ == "__main__":
    main()
