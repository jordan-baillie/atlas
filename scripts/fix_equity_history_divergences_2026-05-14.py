#!/usr/bin/env python3
"""Fix equity_history dual-write divergences for commodity_etfs + sector_etfs.

Items 2 + 3 from 2026-05-14 data-hygiene audit:
  Item 2 — SQLite 2026-05-05 values are stale; JSON last-wins value is truth.
  Item 3 — SQLite has 2026-05-01 rows absent from JSON; backfill JSON from SQLite.

Both markets are passive (live_enabled=false) — zero live capital impact.

Usage:
    python3 scripts/fix_equity_history_divergences_2026-05-14.py --dry-run  (default)
    python3 scripts/fix_equity_history_divergences_2026-05-14.py --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
BROKER_STATE_DIR = PROJECT_ROOT / "brokers" / "state"
LOGS_DIR = PROJECT_ROOT / "logs"
AUDIT_LOG = LOGS_DIR / "equity_backfill_2026-05-14.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Truth values (sourced from JSON last-wins and SQLite respectively)
# ---------------------------------------------------------------------------
# Item 2: SQLite UPDATE — target (JSON truth, last-wins dedup)
SQLITE_UPDATES: list[dict[str, Any]] = [
    {
        "market_id": "commodity_etfs",
        "date": "2026-05-05",
        "target_equity": 956.82,
        "source_of_truth": "brokers/state/live_commodity_etfs.json (last entry 2026-05-05)",
    },
    {
        "market_id": "sector_etfs",
        "date": "2026-05-05",
        "target_equity": 3202.08,
        "source_of_truth": "brokers/state/live_sector_etfs.json (last entry 2026-05-05)",
    },
]

# Item 3: JSON APPEND — sourced from SQLite
JSON_APPENDS: list[dict[str, Any]] = [
    {
        "market_id": "commodity_etfs",
        "date": "2026-05-01",
        "source_of_truth": "SQLite equity_history (commodity_etfs, 2026-05-01)",
    },
    {
        "market_id": "sector_etfs",
        "date": "2026-05-01",
        "source_of_truth": "SQLite equity_history (sector_etfs, 2026-05-01)",
    },
]


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _get_sqlite_equity(conn: sqlite3.Connection, market_id: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT equity FROM equity_history WHERE market_id = ? AND date = ?",
        (market_id, date),
    ).fetchone()
    return row[0] if row else None


def _sqlite_update_equity(
    conn: sqlite3.Connection,
    market_id: str,
    date: str,
    equity: float,
) -> None:
    conn.execute(
        "UPDATE equity_history SET equity = ? WHERE market_id = ? AND date = ?",
        (equity, market_id, date),
    )


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _load_json_state(market_id: str, state_dir: Path = BROKER_STATE_DIR) -> dict:
    path = state_dir / f"live_{market_id}.json"
    return json.loads(path.read_text())


def _save_json_state(market_id: str, state: dict, state_dir: Path = BROKER_STATE_DIR) -> None:
    path = state_dir / f"live_{market_id}.json"
    path.write_text(json.dumps(state, indent=2))


def _json_has_date(state: dict, date: str) -> bool:
    """Return True if equity_history contains any row for *date* (pre-dedup)."""
    return any(
        r.get("date") == date
        for r in state.get("equity_history", [])
        if isinstance(r, dict)
    )


def _json_last_equity_for_date(state: dict, date: str) -> float | None:
    """Return the equity from the LAST row with this date (last-wins semantics)."""
    matches = [
        r.get("equity")
        for r in state.get("equity_history", [])
        if isinstance(r, dict) and r.get("date") == date
    ]
    return matches[-1] if matches else None


# ---------------------------------------------------------------------------
# Main fix logic
# ---------------------------------------------------------------------------

def run_fix(
    dry_run: bool = True,
    db_path: Path = PROJECT_ROOT / "data" / "atlas.db",
    state_dir: Path = BROKER_STATE_DIR,
    audit_log_path: Path = AUDIT_LOG,
) -> dict[str, Any]:
    """Execute (or simulate) all 4 fix operations.

    Returns the audit dict (written to *audit_log_path* when not dry_run).
    """
    run_at = datetime.now(tz=timezone.utc).isoformat()
    actions: list[dict[str, Any]] = []
    errors: list[str] = []

    # ------------------------------------------------------------------
    # Open SQLite connection (single transaction)
    # ------------------------------------------------------------------
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # ----------------------------------------------------------------
        # Item 2 — SQLite UPDATE (2026-05-05 divergences)
        # ----------------------------------------------------------------
        for spec in SQLITE_UPDATES:
            market_id = spec["market_id"]
            date = spec["date"]
            target = spec["target_equity"]

            current = _get_sqlite_equity(conn, market_id, date)
            if current is None:
                msg = f"SQLite: no row found for ({market_id}, {date}) — expected one; skipping"
                logger.warning(msg)
                errors.append(msg)
                continue

            if abs(current - target) < 0.005:
                logger.info(
                    "SKIP (already correct): SQLite %s/%s equity=%.2f (target=%.2f)",
                    market_id, date, current, target,
                )
                actions.append({
                    "market": market_id,
                    "date": date,
                    "action": "sqlite_update_skipped_idempotent",
                    "before": current,
                    "after": current,
                    "source_of_truth": spec["source_of_truth"],
                })
                continue

            logger.info(
                "%s: SQLite UPDATE %s/%s: %.2f → %.2f",
                "DRY-RUN" if dry_run else "APPLY",
                market_id, date, current, target,
            )
            actions.append({
                "market": market_id,
                "date": date,
                "action": "sqlite_update_dry_run" if dry_run else "sqlite_update",
                "before": current,
                "after": target,
                "source_of_truth": spec["source_of_truth"],
            })

            if not dry_run:
                _sqlite_update_equity(conn, market_id, date, target)

        if not dry_run:
            conn.commit()
            logger.info("SQLite transaction committed.")

        # ----------------------------------------------------------------
        # Item 3 — JSON APPEND (2026-05-01 backfill)
        # ----------------------------------------------------------------
        for spec in JSON_APPENDS:
            market_id = spec["market_id"]
            date = spec["date"]

            # Read SQLite source value
            sqlite_equity = _get_sqlite_equity(conn, market_id, date)
            if sqlite_equity is None:
                msg = f"SQLite: no row for ({market_id}, {date}) to backfill JSON; skipping"
                logger.warning(msg)
                errors.append(msg)
                continue

            # Load JSON state
            state = _load_json_state(market_id, state_dir=state_dir)

            # Idempotency: skip if date already present
            if _json_has_date(state, date):
                existing = _json_last_equity_for_date(state, date)
                logger.info(
                    "SKIP (already present): JSON %s/%s equity=%.2f",
                    market_id, date, existing,
                )
                actions.append({
                    "market": market_id,
                    "date": date,
                    "action": "json_append_skipped_idempotent",
                    "before": None,
                    "after": existing,
                    "source_of_truth": spec["source_of_truth"],
                })
                continue

            # Build new row — minimal shape matching SQLite schema
            new_row: dict[str, Any] = {"date": date, "equity": sqlite_equity}

            logger.info(
                "%s: JSON APPEND %s/%s: equity=%.2f",
                "DRY-RUN" if dry_run else "APPLY",
                market_id, date, sqlite_equity,
            )
            actions.append({
                "market": market_id,
                "date": date,
                "action": "json_append_dry_run" if dry_run else "json_append",
                "before": None,
                "after": sqlite_equity,
                "source_of_truth": spec["source_of_truth"],
            })

            if not dry_run:
                eq_hist = state.setdefault("equity_history", [])
                # Insert in chronological order (find insertion point)
                insert_idx = len(eq_hist)
                for i, row in enumerate(eq_hist):
                    if isinstance(row, dict) and row.get("date", "") > date:
                        insert_idx = i
                        break
                eq_hist.insert(insert_idx, new_row)
                _save_json_state(market_id, state, state_dir=state_dir)
                logger.info("JSON state file updated for %s.", market_id)

    finally:
        conn.close()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    audit: dict[str, Any] = {
        "run_at": run_at,
        "dry_run": dry_run,
        "status": "dry_run" if dry_run else ("ok" if not errors else "partial"),
        "market_id_changes": actions,
        "errors": errors,
    }

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    audit_log_path.write_text(json.dumps(audit, indent=2))
    logger.info("Audit log written → %s", audit_log_path)

    return audit


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_summary(audit: dict, dry_run: bool) -> None:
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n{'='*60}")
    print(f"  equity_history fix — {mode}")
    print(f"  Run at: {audit['run_at']}")
    print(f"{'='*60}")
    for a in audit["market_id_changes"]:
        act = a["action"]
        mkt = a["market"]
        dt = a["date"]
        bef = f"${a['before']:.2f}" if a["before"] is not None else "N/A"
        aft = f"${a['after']:.2f}" if a["after"] is not None else "N/A"
        print(f"  [{act}] {mkt}/{dt}: {bef} → {aft}")
        print(f"    source: {a['source_of_truth']}")
    if audit["errors"]:
        print(f"\n  ERRORS:")
        for e in audit["errors"]:
            print(f"    ✗ {e}")
    print(f"\n  Status: {audit['status']}")
    print(f"  Actions: {len(audit['market_id_changes'])} total")
    if dry_run:
        print("\n  ⚠  DRY-RUN — no changes written. Use --apply to execute.")
    else:
        print("\n  ✅ Changes applied.")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Fix equity_history dual-write divergences (2026-05-14 hygiene)."
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate without writing (default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually apply changes.",
    )
    return p.parse_args(argv)


def main(
    argv: list[str] | None = None,
    db_path: Path | None = None,
    state_dir: Path | None = None,
    audit_log_path: Path | None = None,
) -> int:
    args = _parse_args(argv)
    dry_run = not args.apply

    kw: dict[str, Any] = {}
    if db_path is not None:
        kw["db_path"] = db_path
    if state_dir is not None:
        kw["state_dir"] = state_dir
    if audit_log_path is not None:
        kw["audit_log_path"] = audit_log_path

    audit = run_fix(dry_run=dry_run, **kw)
    _print_summary(audit, dry_run)
    return 0 if audit["status"] in ("ok", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
