"""Atlas auto-remediation budget enforcement.

Per user-locked config (config/auto_remediation.yaml):
  budget.max_commits_per_day: 10
  budget.reverts_to_halt: 2          (2 reverts in 24h → auto-halt)
  budget.revert_rate_alert_pct: 15   (15% revert rate / 24h → Telegram alert)
  budget.revert_rate_halt_pct: 25    (25% revert rate / 24h → auto-halt)

All budget queries hit fix_attempts. The budget API returns a Decision struct:
  - PROCEED: budget OK
  - ALERT: send Telegram (no halt)
  - HALT: auto-halt (creates AUTO_REMEDIATION_HALT)
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = PROJECT_ROOT / "config" / "auto_remediation.yaml"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BudgetDecision:
    action: str    # 'PROCEED' | 'ALERT' | 'HALT'
    reason: str
    metric: dict   # {commits_24h, reverts_24h, revert_rate_pct, ...}


def _load_budget_config(cfg_path: Optional[Path] = None) -> dict:
    """Load budget section from YAML config. Falls back to hardcoded defaults on error."""
    p = cfg_path or CFG_PATH
    try:
        with open(p) as f:
            cfg = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError):
        cfg = {}
    return cfg.get("budget") or {
        "max_commits_per_day": 10,
        "reverts_to_halt": 2,
        "revert_rate_alert_pct": 15,
        "revert_rate_halt_pct": 25,
    }


def count_commits_24h(conn: sqlite3.Connection) -> int:
    """Count fix_attempts with status='merged' in the last 24 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    return conn.execute(
        """SELECT COUNT(*) FROM fix_attempts
           WHERE status = 'merged' AND finished_ts >= ?""",
        (cutoff,),
    ).fetchone()[0]


def count_reverts_24h(conn: sqlite3.Connection) -> int:
    """Count fix_attempts with status='reverted' in the last 24 hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
    return conn.execute(
        """SELECT COUNT(*) FROM fix_attempts
           WHERE status = 'reverted' AND reverted_ts >= ?""",
        (cutoff,),
    ).fetchone()[0]


def revert_rate_24h(conn: sqlite3.Connection) -> tuple[int, int, float]:
    """Returns (merged_24h, reverted_24h, revert_rate_pct).

    Rate is 0.0 when merged == 0 (avoids division by zero).
    """
    merged = count_commits_24h(conn)
    reverted = count_reverts_24h(conn)
    rate = (reverted / merged * 100) if merged else 0.0
    return merged, reverted, rate


def check_budget(
    *, db_path: Optional[str] = None, cfg_path: Optional[Path] = None
) -> BudgetDecision:
    """Run all 3 budget checks. Returns the WORST decision (HALT > ALERT > PROCEED)."""
    cfg = _load_budget_config(cfg_path)
    max_commits = int(cfg.get("max_commits_per_day", 10))
    reverts_to_halt = int(cfg.get("reverts_to_halt", 2))
    rate_alert_pct = float(cfg.get("revert_rate_alert_pct", 15))
    rate_halt_pct = float(cfg.get("revert_rate_halt_pct", 25))

    path = db_path or str(PROJECT_ROOT / "data" / "atlas.db")
    with sqlite3.connect(path, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        commits_24h = count_commits_24h(conn)
        merged_24h, reverted_24h, rate = revert_rate_24h(conn)

    metric = {
        "commits_24h": commits_24h,
        "max_commits_per_day": max_commits,
        "merged_24h": merged_24h,
        "reverted_24h": reverted_24h,
        "revert_rate_pct": round(rate, 2),
        "alert_threshold_pct": rate_alert_pct,
        "halt_threshold_pct": rate_halt_pct,
        "reverts_to_halt": reverts_to_halt,
    }

    # Layer A: commit cap
    if commits_24h >= max_commits:
        return BudgetDecision(
            "HALT",
            f"Commit cap exceeded: {commits_24h}/{max_commits} in 24h",
            metric,
        )

    # Layer B: absolute revert count
    if reverted_24h >= reverts_to_halt:
        return BudgetDecision(
            "HALT",
            f"Revert count exceeded: {reverted_24h}/{reverts_to_halt} in 24h",
            metric,
        )

    # Layer C: revert rate (only meaningful with a minimum sample)
    if merged_24h >= 4 and rate >= rate_halt_pct:
        return BudgetDecision(
            "HALT",
            f"Revert rate {rate:.2f}% >= {rate_halt_pct}% halt threshold",
            metric,
        )
    if merged_24h >= 4 and rate >= rate_alert_pct:
        return BudgetDecision(
            "ALERT",
            f"Revert rate {rate:.2f}% >= {rate_alert_pct}% alert threshold",
            metric,
        )

    return BudgetDecision("PROCEED", "Budget OK", metric)


def enforce_budget(
    *,
    db_path: Optional[str] = None,
    cfg_path: Optional[Path] = None,
    send_alert: bool = True,
) -> BudgetDecision:
    """Check budget; if HALT, set AUTO_REMEDIATION_HALT; if ALERT, optional Telegram."""
    decision = check_budget(db_path=db_path, cfg_path=cfg_path)

    if decision.action == "HALT":
        from core.remediation_kill_switch import halt as _halt
        _halt(decision.reason, source="budget")
        if send_alert:
            _try_telegram(
                f"🛑 Auto-remediation HALTED by budget: {decision.reason}\n\n{decision.metric}"
            )
    elif decision.action == "ALERT":
        if send_alert:
            _try_telegram(
                f"⚠️ Auto-remediation budget warning: {decision.reason}\n\n{decision.metric}"
            )

    return decision


def _try_telegram(msg: str) -> None:
    try:
        from utils.telegram import send_message
        send_message(msg[:3500])
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
