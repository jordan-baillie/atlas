#!/usr/bin/env python3
"""Ceasefire Probability Tracker.

Maintains a weighted boolean factor model for Iran/US ceasefire probability.
Used by the hourly cron agent to track geopolitical developments and trigger
portfolio kill switch actions.

Usage:
    python3 scripts/ceasefire_tracker.py evaluate          # Print factor keywords + state for cron agent
    python3 scripts/ceasefire_tracker.py status             # Print current probability + active factors (JSON)
    python3 scripts/ceasefire_tracker.py toggle <id> <true|false> [--confidence high|medium|low] [--source "..."]
    python3 scripts/ceasefire_tracker.py recalculate        # Recalculate probability from current factor states

Probability formula:
    P = clamp(baseline + sum(weight for each active factor), 2, 95)

    Ceasefire factors have positive weights (increase P).
    Escalation factors have negative weights (decrease P).

Probability labels:
    ≤15%  VERY UNLIKELY → Hold all positions. Thesis intact.
    16-30% UNLIKELY     → Hold but tighten stops. Prepare exit plan.
    31-50% COIN FLIP    → ⚠️ Reduce energy concentration. Trim INSW, partial XOP profits.
    51-70% POSSIBLE     → 🚨 Execute kill switch. Exit INSW, sell 50% XOP, trim RTX.
    >70%   LIKELY       → 🚨🚨 FULL EXIT. Sell all conflict positions.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

DATA_FILE = PROJECT / "data" / "position_monitor" / "ceasefire_factors.json"
MAX_CHANGE_LOG = 50


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def load_data() -> dict:
    with open(DATA_FILE) as f:
        return json.load(f)


def save_data(data: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# Core probability logic
# ---------------------------------------------------------------------------

def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def compute_probability(data: dict) -> int:
    """Sum baseline + active factor weights, clamp to [2, 95]."""
    total = data["baseline"]
    for factor in data["factors"]:
        if factor["active"]:
            total += factor["weight"]
    return clamp(total, 2, 95)


def derive_labels(prob: int) -> dict:
    """Derive human-readable label, timeline, and portfolio action from probability."""
    if prob <= 15:
        label = "VERY UNLIKELY"
        timeline = "4+ weeks"
        action = "Hold all positions. Thesis intact."
    elif prob <= 30:
        label = "UNLIKELY"
        timeline = "2-4 weeks"
        action = "Hold but tighten stops. Prepare exit plan."
    elif prob <= 50:
        label = "COIN FLIP"
        timeline = "1-2 weeks"
        action = "⚠️ Reduce energy concentration. Trim INSW, partial XOP profits."
    elif prob <= 70:
        label = "POSSIBLE"
        timeline = "Days to 1 week"
        action = "🚨 Execute kill switch. Exit INSW, sell 50% XOP, trim RTX."
    else:
        label = "LIKELY"
        timeline = "Imminent"
        action = "🚨🚨 FULL EXIT. Sell all conflict positions."
    return {"label": label, "timeline": timeline, "action": action}


# ---------------------------------------------------------------------------
# Threshold alerts
# ---------------------------------------------------------------------------

def check_thresholds(old_prob: Optional[int], new_prob: int) -> None:
    """Check for threshold crossings and print/send alerts."""
    if old_prob is None:
        return

    alerts = []

    # Rising thresholds
    if old_prob <= 30 < new_prob:
        alerts.append(
            ("AMBER",
             f"⚠️ <b>AMBER ALERT — Ceasefire Tracker</b>\n"
             f"Probability crossed 30%: {old_prob}% → <b>{new_prob}%</b>\n"
             f"Action: Hold but tighten stops. Prepare exit plan.")
        )
    elif old_prob <= 50 < new_prob:
        alerts.append(
            ("RED",
             f"🚨 <b>RED ALERT — Ceasefire Tracker</b>\n"
             f"Probability crossed 50%: {old_prob}% → <b>{new_prob}%</b>\n"
             f"Action: Execute kill switch. Exit INSW, sell 50% XOP, trim RTX.")
        )
    elif old_prob <= 70 < new_prob:
        alerts.append(
            ("CRITICAL",
             f"🚨🚨 <b>CRITICAL ALERT — Ceasefire Tracker</b>\n"
             f"Probability crossed 70%: {old_prob}% → <b>{new_prob}%</b>\n"
             f"Action: FULL EXIT. Sell all conflict positions.")
        )

    # Falling threshold
    if old_prob >= 10 > new_prob:
        alerts.append(
            ("INFO",
             f"ℹ️ <b>Ceasefire Tracker</b>\n"
             f"Probability fell below 10%: {old_prob}% → {new_prob}%\n"
             f"Thesis intact. Hold all positions.")
        )

    telegram_available = False
    try:
        from utils.telegram import send_message
        telegram_available = True
    except Exception:
        pass

    for level, msg in alerts:
        print(f"[{level}] {msg}")
        if telegram_available:
            try:
                from utils.telegram import send_message
                send_message(msg)
            except Exception as e:
                print(f"Telegram alert failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_evaluate() -> None:
    """Print factor state + search keywords for the cron evaluation agent.

    The cron agent reads this output to know which factors to search for,
    then calls toggle to update any that changed.
    """
    data = load_data()
    now = datetime.now().isoformat(timespec="seconds")

    print(f"=== Ceasefire Factor Evaluation — {now} ===")
    print(f"Current probability: {data.get('probability', 'N/A')}% ({data.get('probability_label', 'N/A')})")
    print(f"Timeline: {data.get('timeline', 'N/A')}")
    print(f"Portfolio action: {data.get('portfolio_action', 'N/A')}")
    print(f"Last updated: {data.get('last_updated', 'N/A')}")
    print()

    # Group by category
    categories = {}
    for factor in data["factors"]:
        cat = factor["category"]
        categories.setdefault(cat, []).append(factor)

    for cat, factors in categories.items():
        print(f"=== {cat.upper()} FACTORS ===")
        for factor in factors:
            state_str = "TRUE ✓" if factor["active"] else "FALSE ✗"
            print(f"--- FACTOR: {factor['id']} ---")
            print(f"  Label:    {factor['label']}")
            print(f"  Weight:   {factor['weight']:+d} ({factor['direction']})")
            print(f"  Active:   {state_str}")
            print(f"  Confidence: {factor['confidence']}")
            print(f"  Last checked: {factor.get('last_checked') or 'Never'}")
            print(f"  Source:   {factor.get('source') or 'None'}")
            if factor.get("description"):
                print(f"  Notes:    {factor['description']}")
            print(f"  Search keywords: {', '.join(factor.get('search_keywords', []))}")
            print()

    print(f"=== END OF FACTORS ===")
    print(f"Total factors: {len(data['factors'])}")
    active_count = sum(1 for f in data["factors"] if f["active"])
    print(f"Active factors: {active_count}/{len(data['factors'])}")


def cmd_status() -> None:
    """Print JSON summary with probability, active factor count, and recent changes."""
    data = load_data()

    active_factors = [f for f in data["factors"] if f["active"]]
    recent_changes = data.get("change_log", [])[-5:]

    summary = {
        "probability": data.get("probability"),
        "probability_label": data.get("probability_label"),
        "timeline": data.get("timeline"),
        "portfolio_action": data.get("portfolio_action"),
        "last_updated": data.get("last_updated"),
        "active_factor_count": len(active_factors),
        "total_factors": len(data["factors"]),
        "baseline": data["baseline"],
        "active_factors": [
            {
                "id": f["id"],
                "category": f["category"],
                "label": f["label"],
                "weight": f["weight"],
                "direction": f["direction"],
                "confidence": f["confidence"],
                "source": f.get("source", ""),
                "last_checked": f.get("last_checked"),
            }
            for f in active_factors
        ],
        "recent_changes": recent_changes,
    }
    print(json.dumps(summary, indent=2, default=str))


def cmd_toggle(
    factor_id: str,
    new_active: bool,
    confidence: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Toggle a factor's active state, log the change, and recalculate probability."""
    data = load_data()

    factor = next((f for f in data["factors"] if f["id"] == factor_id), None)
    if not factor:
        print(f"ERROR: factor '{factor_id}' not found", file=sys.stderr)
        # Print available IDs to help the caller
        ids = [f["id"] for f in data["factors"]]
        print(f"Available factor IDs: {', '.join(ids)}", file=sys.stderr)
        sys.exit(1)

    old_active = factor["active"]
    old_prob = data.get("probability")

    # Update factor state
    factor["active"] = new_active
    factor["last_checked"] = datetime.now().isoformat(timespec="seconds")
    if confidence:
        factor["confidence"] = confidence
    if source is not None:
        factor["source"] = source

    # Append to change log ONLY if state actually changed (skip same→same)
    if old_active != new_active:
        log_entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "factor_id": factor_id,
            "old_active": old_active,
            "new_active": new_active,
            "confidence": factor["confidence"],
            "reason": reason or "Toggled via CLI",
            "source": source or factor.get("source", ""),
        }
        change_log = data.get("change_log", [])
        change_log.append(log_entry)
        data["change_log"] = change_log[-MAX_CHANGE_LOG:]

    # Recalculate
    new_prob = compute_probability(data)
    labels = derive_labels(new_prob)
    data["probability"] = new_prob
    data["probability_label"] = labels["label"]
    data["timeline"] = labels["timeline"]
    data["portfolio_action"] = labels["action"]
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")

    save_data(data)

    print(
        f"OK: '{factor_id}' active: {old_active} → {new_active} "
        f"(probability: {old_prob}% → {new_prob}%)"
    )
    print(f"    Label: {labels['label']} | Timeline: {labels['timeline']}")
    print(f"    Action: {labels['action']}")

    # Check and alert on threshold crossings
    check_thresholds(old_prob, new_prob)


def cmd_recalculate() -> None:
    """Recalculate probability from current factor states and print result."""
    data = load_data()

    old_prob = data.get("probability")
    new_prob = compute_probability(data)
    labels = derive_labels(new_prob)

    data["probability"] = new_prob
    data["probability_label"] = labels["label"]
    data["timeline"] = labels["timeline"]
    data["portfolio_action"] = labels["action"]
    data["last_updated"] = datetime.now().isoformat(timespec="seconds")

    save_data(data)

    active_factors = [f for f in data["factors"] if f["active"]]
    factor_sum = sum(f["weight"] for f in active_factors)
    raw_total = data["baseline"] + factor_sum

    print(f"Probability: {new_prob}% ({labels['label']})")
    if raw_total != new_prob:
        print(f"  Baseline {data['baseline']:+d} + factor weights {factor_sum:+d} = {raw_total} (clamped to {new_prob}%)")
    else:
        print(f"  Baseline {data['baseline']:+d} + factor weights {factor_sum:+d} = {new_prob}%")
    print(f"Timeline: {labels['timeline']}")
    print(f"Portfolio action: {labels['action']}")
    print(f"Active factors: {len(active_factors)}/{len(data['factors'])}")

    if active_factors:
        print("\nActive factor breakdown:")
        for f in active_factors:
            direction_icon = "📈" if f["direction"] == "ceasefire" else "📉"
            print(f"  {direction_icon} {f['id']}: {f['weight']:+d} ({f['confidence']} confidence)")

    # Check and alert on threshold crossings
    check_thresholds(old_prob, new_prob)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ceasefire Probability Tracker — Iran/US conflict factor model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # evaluate
    subparsers.add_parser(
        "evaluate",
        help="Print factor keywords + current state for the cron evaluation agent",
    )

    # status
    subparsers.add_parser(
        "status",
        help="Print current probability + active factors as JSON",
    )

    # toggle
    toggle_parser = subparsers.add_parser(
        "toggle",
        help="Toggle a factor's active state and recalculate probability",
    )
    toggle_parser.add_argument("factor_id", help="Factor ID (e.g. iran_collapse)")
    toggle_parser.add_argument(
        "active", choices=["true", "false"], help="New active state"
    )
    toggle_parser.add_argument(
        "--confidence",
        choices=["high", "medium", "low"],
        default=None,
        help="Confidence level for this assessment",
    )
    toggle_parser.add_argument(
        "--source",
        default=None,
        help='Source of the information (e.g. "Reuters 2026-03-05")',
    )
    toggle_parser.add_argument(
        "--reason",
        default=None,
        help="Optional reason for the change (for change log)",
    )

    # recalculate
    subparsers.add_parser(
        "recalculate",
        help="Recalculate probability from current factor states",
    )

    args = parser.parse_args()

    if args.command == "evaluate":
        cmd_evaluate()
    elif args.command == "status":
        cmd_status()
    elif args.command == "toggle":
        new_active = args.active == "true"
        cmd_toggle(
            args.factor_id,
            new_active,
            confidence=args.confidence,
            source=args.source,
            reason=getattr(args, "reason", None),
        )
    elif args.command == "recalculate":
        cmd_recalculate()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
