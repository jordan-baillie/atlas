#!/usr/bin/env python3
"""Hourly health-check: alert if any PAPER-lifecycle strategy has zero paper_trades.

Strategy validation gate: strategies in PAPER lifecycle state must accumulate
paper_trades for the paper→live promotion gates to evaluate.  If a PAPER-state
strategy has been running > 7 days but has 0 paper_trades in the last 14 days
it means the poller (sync_paper_orders.py) is not creating rows — either the
LIMIT prices never filled, or there is a bug in the write-back logic.

Sends a single Telegram alert per 24h window (throttled via state file).

Usage:
    python3 scripts/healthcheck_paper_executor.py [--state-file PATH]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Project bootstrap ────────────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ATLAS_ROOT))

from atlas_bootstrap import PROJECT_ROOT as PROJECT  # noqa: E402
from utils.logging_config import setup_logging       # noqa: E402

log = setup_logging(
    "healthcheck_paper_executor",
    extra_log_file="healthcheck_paper_executor",
)

_DEFAULT_STATE_FILE = PROJECT / "data" / "healthcheck_paper_executor_state.json"
_ALERT_THROTTLE_HOURS = 24
_PAPER_MIN_DAYS = 7     # must be in PAPER state for this many days before alerting
_LOOK_BACK_DAYS = 14    # look for paper_trades within this window


def _load_state(state_file: Path) -> dict:
    """Load throttle state (returns empty dict on missing/corrupt file)."""
    try:
        return json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state_file: Path, state: dict) -> None:
    """Persist throttle state (non-fatal)."""
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        log.warning("Failed to save state file: %s", exc)


def _is_throttled(state: dict) -> bool:
    """Return True if an alert was sent within the last 24 h."""
    last_alert = state.get("last_alert_at")
    if not last_alert:
        return False
    try:
        last_dt = datetime.fromisoformat(last_alert)
        now = datetime.now(timezone.utc)
        # Make both tz-aware for comparison
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age_h = (now - last_dt).total_seconds() / 3600
        return age_h < _ALERT_THROTTLE_HOURS
    except Exception:
        return False


def run_check(state_file: Path = _DEFAULT_STATE_FILE) -> dict:
    """Execute the health check. Returns a result dict.

    Returns:
        dict with keys: paper_combos, stuck_combos, alert_sent, errors.
    """
    from db import atlas_db

    result: dict = {
        "paper_combos": [],
        "stuck_combos": [],
        "alert_sent": False,
        "errors": [],
    }

    try:
        with atlas_db.get_db() as db:
            # All PAPER-lifecycle (strategy, universe) combos
            rows = db.execute(
                """SELECT strategy, universe, entered_state_at
                   FROM strategy_lifecycle
                   WHERE state = 'PAPER'"""
            ).fetchall()

            if not rows:
                log.info("No strategies in PAPER state — nothing to check")
                return result

            now = datetime.now(timezone.utc)

            for row in rows:
                strategy         = str(row[0])
                universe         = str(row[1])
                entered_state_at = str(row[2])
                result["paper_combos"].append(f"{strategy}/{universe}")

                # Only alert if been in PAPER long enough (> 7 days)
                try:
                    entered_dt = datetime.fromisoformat(entered_state_at)
                    if entered_dt.tzinfo is None:
                        entered_dt = entered_dt.replace(tzinfo=timezone.utc)
                    days_in_paper = (now - entered_dt).days
                except Exception:
                    days_in_paper = 999  # assume old enough

                if days_in_paper < _PAPER_MIN_DAYS:
                    log.debug(
                        "%s/%s has only been in PAPER %d days — skip",
                        strategy, universe, days_in_paper,
                    )
                    continue

                # Count paper_trades in last 14 days
                count_row = db.execute(
                    """SELECT COUNT(*) FROM paper_trades
                       WHERE strategy = ?
                         AND universe = ?
                         AND entry_date >= date('now', ?)""",
                    (strategy, universe, f"-{_LOOK_BACK_DAYS} days"),
                ).fetchone()
                trade_count = count_row[0] if count_row else 0

                log.info(
                    "%s/%s — %d paper_trades in last %d days (in PAPER %d days)",
                    strategy, universe, trade_count, _LOOK_BACK_DAYS, days_in_paper,
                )

                if trade_count == 0:
                    result["stuck_combos"].append(
                        f"{strategy}/{universe} (in PAPER {days_in_paper}d)"
                    )

    except Exception as exc:
        log.error("DB query failed: %s", exc, exc_info=True)
        result["errors"].append(str(exc))
        return result

    if not result["stuck_combos"]:
        log.info("All PAPER strategies have recent paper_trades — OK")
        return result

    # ── Send Telegram alert (throttled) ──────────────────────────────────────
    state = _load_state(state_file)

    if _is_throttled(state):
        log.info(
            "Alert throttled (last sent %s) — %d stuck combo(s) suppressed",
            state.get("last_alert_at"), len(result["stuck_combos"]),
        )
        return result

    n     = len(result["stuck_combos"])
    names = ", ".join(result["stuck_combos"])
    msg   = (
        f"⚠️ PAPER VALIDATION STUCK: {n} strategy combo(s) have 0 paper_trades "
        f"in last {_LOOK_BACK_DAYS}d.\n"
        f"Combos: {names}\n"
        f"Fix: check sync_paper_orders.py cron is running; "
        f"verify LIMIT prices were hit at Alpaca paper account."
    )
    log.warning(msg)

    try:
        from utils.telegram import send_message
        send_message(msg)
        result["alert_sent"] = True
        state["last_alert_at"] = datetime.now(timezone.utc).isoformat()
        state["stuck_combos"]  = result["stuck_combos"]
        _save_state(state_file, state)
        log.info("Telegram alert sent and state updated")
    except Exception as exc:
        log.warning("Telegram send failed (non-fatal): %s", exc)
        result["errors"].append(f"telegram:{exc}")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-file", type=Path, default=_DEFAULT_STATE_FILE,
        help=f"Path to throttle state file (default: {_DEFAULT_STATE_FILE})",
    )
    args = parser.parse_args(argv)

    result = run_check(state_file=args.state_file)

    if result["errors"]:
        log.warning("healthcheck_paper_executor completed with errors: %s", result["errors"])
        return 1

    if result["stuck_combos"]:
        log.warning(
            "Stuck PAPER combos: %s (alert_sent=%s)",
            result["stuck_combos"], result["alert_sent"],
        )
        return 2  # non-zero so cron logs notice it

    return 0


if __name__ == "__main__":
    sys.exit(main())
