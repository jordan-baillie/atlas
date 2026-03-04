#!/usr/bin/env python3
"""Iran Monitor — Position Updater.

Called by the pi agent to update manual toggles and notes on
iran-conflict positions in the Monitor tab.

Usage:
    # Set a toggle status
    python3 scripts/iran_monitor_update.py toggle <position_id> <condition_id> <passing|warning|failing>

    # Add a note to a position
    python3 scripts/iran_monitor_update.py note <position_id> "Note text here"

    # Add a note to ALL iran-conflict positions
    python3 scripts/iran_monitor_update.py note-all "Shared note text"

    # Update invalidation or target price
    python3 scripts/iran_monitor_update.py set-price <position_id> <invalidation|target> <price>

    # Re-evaluate all positions (prices + auto conditions)
    python3 scripts/iran_monitor_update.py evaluate

    # Record VLCC spot rate for cross-cycle trend tracking
    python3 scripts/iran_monitor_update.py rate vlcc <value_in_thousands>
    # e.g. rate vlcc 350  →  records $350k/day

    # Record last escalation event timestamp + description
    python3 scripts/iran_monitor_update.py escalation "Description of event" "Source (Reuters/AP/etc)"
"""

import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from monitor.models import PositionStore


def cmd_toggle(position_id: str, condition_id: str, new_status: str):
    """Set a manual_toggle condition to passing/warning/failing."""
    if new_status not in ("passing", "warning", "failing"):
        print(f"ERROR: status must be passing/warning/failing, got '{new_status}'")
        sys.exit(1)

    store = PositionStore()
    pos = store.get_position(position_id)
    if not pos:
        print(f"ERROR: position {position_id} not found")
        sys.exit(1)

    found = False
    old_status = None
    for c in pos.conditions:
        if c.id == condition_id:
            old_status = c.status
            c.status = new_status
            c.last_checked = datetime.now().isoformat(timespec="seconds")
            found = True
            break

    if not found:
        print(f"ERROR: condition {condition_id} not found on {pos.ticker}")
        sys.exit(1)

    pos.update_health()
    store.update_position(pos)

    # Record alert if status changed
    if old_status != new_status:
        store.add_alert({
            "position_id": pos.id,
            "ticker": pos.ticker,
            "condition_id": condition_id,
            "condition_label": next(c.label for c in pos.conditions if c.id == condition_id),
            "old_status": old_status,
            "new_status": new_status,
            "value": None,
            "source": "iran_monitor_agent",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
        })

    print(f"OK: {pos.ticker} condition '{condition_id}' {old_status} → {new_status} (health={pos.health_score})")


def cmd_note(position_id: str, text: str):
    """Add a note to a position."""
    store = PositionStore()
    pos = store.get_position(position_id)
    if not pos:
        print(f"ERROR: position {position_id} not found")
        sys.exit(1)

    pos.notes.append({
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "text": text,
    })
    store.update_position(pos)
    print(f"OK: note added to {pos.ticker}")


def cmd_note_all(text: str):
    """Add a note to ALL iran-conflict positions."""
    store = PositionStore()
    positions = store.load_positions()
    count = 0
    for pos in positions:
        if "iran-conflict" in pos.tags:
            pos.notes.append({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "text": text,
            })
            count += 1
    store.save_positions(positions)
    print(f"OK: note added to {count} iran-conflict positions")


def cmd_set_price(position_id: str, field: str, price: float):
    """Update invalidation or target price."""
    if field not in ("invalidation", "target"):
        print(f"ERROR: field must be 'invalidation' or 'target', got '{field}'")
        sys.exit(1)

    store = PositionStore()
    pos = store.get_position(position_id)
    if not pos:
        print(f"ERROR: position {position_id} not found")
        sys.exit(1)

    if field == "invalidation":
        pos.invalidation_price = price
    else:
        pos.target_price = price

    store.update_position(pos)
    print(f"OK: {pos.ticker} {field}_price = ${price:.2f}")


def cmd_evaluate():
    """Re-evaluate all positions (prices + auto conditions)."""
    from monitor.evaluator import evaluate_all
    result = evaluate_all(send_telegram=True)
    print(f"OK: evaluated {result['evaluated']} positions, {result['alerts']} alerts")


def cmd_rate(series: str, value: float):
    """Record a rate datapoint for cross-cycle trend tracking (e.g. VLCC spot)."""
    history_file = PROJECT / "data" / "position_monitor" / "rate_history.json"
    try:
        with open(history_file) as f:
            history = json.load(f)
    except Exception:
        history = {}

    now = datetime.now().isoformat(timespec="seconds")
    key = f"{series}_spot" if not series.endswith("_spot") else series
    history.setdefault(key, []).append({"t": now, "v": value})
    history[key] = history[key][-18:]  # Keep 72h at 4h intervals

    # Compute 3-cycle trend for VLCC
    if key == "vlcc_spot" and len(history[key]) >= 2:
        latest = history[key][-1]["v"]
        prev = history[key][-2]["v"]
        history["vlcc_3cycle_trend"] = (
            "rising" if latest > prev * 1.02 else
            "declining" if latest < prev * 0.98 else
            "stable"
        )
    if key == "vlcc_spot" and len(history[key]) >= 3:
        latest = history[key][-1]["v"]
        three_ago = history[key][-3]["v"]
        pct = (latest - three_ago) / three_ago * 100 if three_ago else 0
        history["vlcc_3cycle_change_pct"] = round(pct, 1)

    with open(history_file, "w") as f:
        json.dump(history, f, indent=2, default=str)
    print(f"OK: {key} = {value} recorded. History: {len(history[key])} points")

    if key == "vlcc_spot" and len(history[key]) >= 2:
        trend = history.get("vlcc_3cycle_trend", "?")
        print(f"    VLCC 3-cycle trend: {trend}")


def cmd_escalation(description: str, source: str):
    """Record a new escalation event timestamp."""
    history_file = PROJECT / "data" / "position_monitor" / "rate_history.json"
    try:
        with open(history_file) as f:
            history = json.load(f)
    except Exception:
        history = {}

    now = datetime.now().isoformat(timespec="seconds")
    history["last_escalation_event"] = {
        "timestamp": now,
        "description": description,
        "source": source,
    }
    history["hours_since_escalation"] = 0.0
    history["escalation_gap_status"] = "active"

    with open(history_file, "w") as f:
        json.dump(history, f, indent=2, default=str)
    print(f"OK: escalation event recorded at {now}")
    print(f"    {description} ({source})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "toggle" and len(sys.argv) == 5:
        cmd_toggle(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "note" and len(sys.argv) >= 4:
        cmd_note(sys.argv[2], " ".join(sys.argv[3:]))
    elif cmd == "note-all" and len(sys.argv) >= 3:
        cmd_note_all(" ".join(sys.argv[2:]))
    elif cmd == "set-price" and len(sys.argv) == 5:
        cmd_set_price(sys.argv[2], sys.argv[3], float(sys.argv[4]))
    elif cmd == "evaluate":
        cmd_evaluate()
    elif cmd == "rate" and len(sys.argv) == 4:
        cmd_rate(sys.argv[2], float(sys.argv[3]))
    elif cmd == "escalation" and len(sys.argv) >= 4:
        cmd_escalation(sys.argv[2], " ".join(sys.argv[3:]))
    else:
        print(__doc__)
        sys.exit(1)
