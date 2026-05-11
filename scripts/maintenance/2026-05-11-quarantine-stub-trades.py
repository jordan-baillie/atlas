#!/usr/bin/env python3
"""
2026-05-11 — Quarantine stub closed_trades from live_sp500.json.

Identifies all closed_trades with entry_date=None (stubs created by pre-2026-04-29
sync before entry_date was captured).  Moves them to a new top-level key
``closed_trades_quarantine`` so the active closed_trades list stays clean.

Usage (dry-run):
    python3 scripts/maintenance/2026-05-11-quarantine-stub-trades.py

Usage (apply):
    python3 scripts/maintenance/2026-05-11-quarantine-stub-trades.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_ATLAS_ROOT = Path(__file__).resolve().parent.parent.parent
_STATE_FILE = _ATLAS_ROOT / "brokers" / "state" / "live_sp500.json"
_QUARANTINE_REASON = "missing entry_date — stub from pre-2026-04-29 sync"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identify_stubs(closed_trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (clean_trades, stub_trades) partitioned by whether entry_date is None."""
    clean: list[dict] = []
    stubs: list[dict] = []
    for t in closed_trades:
        if t.get("entry_date") is None:
            stubs.append(t)
        else:
            clean.append(t)
    return clean, stubs


def run(state_path: Path = _STATE_FILE, *, apply: bool = False) -> dict:
    """
    Load state file, identify stubs, quarantine them, optionally persist.

    Returns a summary dict with keys: n_clean, n_quarantined, stubs.
    """
    raw = state_path.read_text(encoding="utf-8")
    data: dict = json.loads(raw)

    closed_trades: list[dict] = data.get("closed_trades", [])
    existing_quarantine: list[dict] = data.get("closed_trades_quarantine", [])

    clean_trades, new_stubs = identify_stubs(closed_trades)

    ts = _now_iso()
    annotated_stubs = []
    for stub in new_stubs:
        annotated = dict(stub)
        annotated["_quarantine_reason"] = _QUARANTINE_REASON
        annotated["_quarantined_at"] = ts
        annotated_stubs.append(annotated)

    merged_quarantine = existing_quarantine + annotated_stubs

    summary = {
        "n_original": len(closed_trades),
        "n_clean": len(clean_trades),
        "n_quarantined": len(annotated_stubs),
        "n_existing_quarantine": len(existing_quarantine),
        "n_total_quarantine": len(merged_quarantine),
        "stubs": [s.get("ticker", "?") for s in annotated_stubs],
    }

    if not apply:
        print("[DRY RUN] No changes written.")
        print(f"  closed_trades: {len(closed_trades)} -> {len(clean_trades)} (clean)")
        print(f"  stubs to quarantine ({len(annotated_stubs)}): {summary['stubs']}")
        return summary

    if not new_stubs:
        print("No new stubs found -- nothing to quarantine.")
        return summary

    data["closed_trades"] = clean_trades
    data["closed_trades_quarantine"] = merged_quarantine

    tmp_path = state_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp_path, state_path)

    print(f"[APPLIED] Quarantined {len(annotated_stubs)} stub trades from {state_path.name}")
    print(f"  closed_trades: {summary['n_original']} -> {summary['n_clean']}")
    print(f"  closed_trades_quarantine: {summary['n_existing_quarantine']} -> {summary['n_total_quarantine']}")
    for s in annotated_stubs:
        print(
            f"  -> {s.get('ticker','?'):6s} entry_price={s.get('entry_price',0)}"
            f"  pnl={s.get('pnl','?')}"
            f"  exit_date={s.get('exit_date','?')}"
        )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to disk (default: dry-run only)",
    )
    parser.add_argument(
        "--state-file",
        default=str(_STATE_FILE),
        help="Path to live_sp500.json (default: auto-detected)",
    )
    args = parser.parse_args(argv)

    state_path = Path(args.state_file)
    if not state_path.exists():
        print(f"ERROR: State file not found: {state_path}", file=sys.stderr)
        return 1

    run(state_path, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
