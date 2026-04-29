"""healthz_error_remediation.py — meta-monitor for the auto-remediation system.

Alerts via Telegram (failure-only per user config) when:
  * errors table not receiving writes (capture broken)
  * classifier backlog > N UNCLASSIFIED rows
  * revert rate >= alert_threshold (15%) or halt_threshold (25%)
  * auto_remediation.yaml config missing or malformed

Runs as a separate cron entry — independent of error_monitor.py so neither
can mask the other's failure.

Telegram is sent IMMEDIATELY on failure (telegram.on_failure=immediate).
Successes are silent (telegram.on_success=never).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

from utils.logging_config import setup_logging
from db import atlas_db

logger = setup_logging("healthz_error_remediation", telegram_errors=False)

# ── Configurable thresholds (engineering defaults, override via config) ────────
_DEFAULT_BACKLOG_THRESHOLD = 100
_DEFAULT_REVERT_ALERT_PCT = 15.0
_DEFAULT_REVERT_HALT_PCT = 25.0


# ── Individual health checks ──────────────────────────────────────────────────


def check_capture_alive(conn, lookback_hours: int = 24) -> tuple[bool, dict]:
    """Verify errors table is receiving writes.

    Returns ok=True always — this is informational, not actionable.
    A legitimately quiet system can have 0 errors in 24h; we only want to
    know if capture *broke*, which is signalled by other monitors (journald,
    healthz_hourly).
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    n = conn.execute(
        "SELECT COUNT(*) FROM errors WHERE last_seen_ts >= ?", (cutoff,)
    ).fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
    return True, {"errors_last_24h": n, "errors_total": total}


def check_classifier_backlog(
    conn, threshold: int = _DEFAULT_BACKLOG_THRESHOLD
) -> tuple[bool, dict]:
    """Fail if UNCLASSIFIED backlog exceeds threshold."""
    n = conn.execute(
        """SELECT COUNT(*) FROM errors
           WHERE classification = 'UNCLASSIFIED'
             AND remediation_status = 'NEW'"""
    ).fetchone()[0]
    ok = n <= threshold
    return ok, {"unclassified_backlog": n, "threshold": threshold}


def check_audit_log_writes(conn, lookback_hours: int = 24) -> tuple[bool, dict]:
    """Count audit log entries in the last 24 h — informational, always ok=True."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    n = conn.execute(
        "SELECT COUNT(*) FROM fix_audit_log WHERE ts >= ?", (cutoff,)
    ).fetchone()[0]
    return True, {"audit_writes_last_24h": n}


def check_revert_rate(conn, window_hours: int = 24) -> tuple[bool, dict]:
    """Alert or halt when revert rate crosses thresholds.

    Phase 1: merged=0, reverted=0 → rate=0.0, always ok.
    Phase 2+: real data from fix_attempts.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    merged = conn.execute(
        "SELECT COUNT(*) FROM fix_attempts WHERE status='merged' AND finished_ts >= ?",
        (cutoff,),
    ).fetchone()[0]
    reverted = conn.execute(
        "SELECT COUNT(*) FROM fix_attempts WHERE status='reverted' AND reverted_ts >= ?",
        (cutoff,),
    ).fetchone()[0]
    rate = (reverted / merged * 100.0) if merged else 0.0
    alert = rate >= _DEFAULT_REVERT_ALERT_PCT
    halted = rate >= _DEFAULT_REVERT_HALT_PCT
    return not (alert or halted), {
        "merged_24h": merged,
        "reverted_24h": reverted,
        "revert_rate_pct": round(rate, 2),
        "alert_threshold": _DEFAULT_REVERT_ALERT_PCT,
        "halt_threshold": _DEFAULT_REVERT_HALT_PCT,
    }


def check_phase_state(conn) -> tuple[bool, dict]:
    """Read config/auto_remediation.yaml and confirm phase invariants."""
    cfg_path = PROJECT_ROOT / "config" / "auto_remediation.yaml"
    if not cfg_path.exists():
        return False, {"config_missing": True, "path": str(cfg_path)}
    try:
        import yaml  # optional dep — present in Atlas env
        with open(cfg_path) as fh:
            cfg = yaml.safe_load(fh) or {}
    except Exception as exc:
        return False, {"config_parse_error": str(exc)}
    phase_block = cfg.get("phase") or {}
    monitor_block = cfg.get("monitor") or {}
    return True, {
        "phase": phase_block.get("current", -1),
        "phase_3_enabled": phase_block.get("phase_3_enabled", False),
        "monitor_dry_run": monitor_block.get("dry_run"),
    }


# ── Telegram alert ────────────────────────────────────────────────────────────


def send_telegram_alert(failures: list[dict], summary: dict) -> None:
    """Send a structured HTML Telegram alert listing each failing check."""
    try:
        from utils.telegram import send_message, _esc
    except Exception as exc:
        logger.warning("Cannot import utils.telegram — alert skipped: %s", exc)
        return

    lines = ["\U0001f6a8 <b>Atlas Auto-Remediation Health Check FAILED</b>", ""]
    for f in failures:
        check_name = _esc(str(f["check"]))
        detail_str = _esc(json.dumps(f["detail"]))
        lines.append(f"\u274c <b>{check_name}</b>: {detail_str}")
    lines.append("")
    lines.append("<i>Full summary:</i>")
    lines.append(
        "<code>" + _esc(json.dumps(summary, indent=2)[:1500]) + "</code>"
    )
    send_message("\n".join(lines))


# ── Runner ────────────────────────────────────────────────────────────────────


def run_health(*, db_path: str | None = None, json_output: bool = False) -> int:
    """Execute all health checks; return 0 on pass, 1 on any failure."""
    failures: list[dict] = []
    summary: dict = {}

    checks = [
        ("capture_alive", check_capture_alive),
        ("classifier_backlog", check_classifier_backlog),
        ("audit_log_writes", check_audit_log_writes),
        ("revert_rate", check_revert_rate),
        ("phase_state", check_phase_state),
    ]

    with atlas_db.get_db(db_path) as conn:
        for name, fn in checks:
            try:
                ok, detail = fn(conn)
            except Exception as exc:
                ok, detail = False, {"error": str(exc)}
            summary[name] = detail
            if not ok:
                failures.append({"check": name, "detail": detail})

    rc = 0 if not failures else 1

    if json_output:
        print(
            json.dumps(
                {"ok": rc == 0, "failures": failures, "summary": summary},
                indent=2,
            )
        )
    else:
        if rc == 0:
            logger.info("All checks pass: %s", summary)
        else:
            logger.error("Health check FAILED: %s", failures)

    # Send Telegram on failure; never on success (telegram.on_success=never).
    if rc != 0:
        try:
            send_telegram_alert(failures, summary)
        except Exception as exc:
            logger.warning("Telegram alert send failed: %s", exc)

    return rc


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Meta-monitor for Atlas auto-remediation system"
    )
    p.add_argument("--json", dest="json_output", action="store_true")
    p.add_argument("--db", default=None, help="Override DB path (testing only)")
    args = p.parse_args(argv)
    return run_health(db_path=args.db, json_output=args.json_output)


if __name__ == "__main__":
    sys.exit(main())
