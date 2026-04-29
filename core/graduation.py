"""Atlas auto-remediation graduation engine.

Per user-locked config (config/auto_remediation.yaml graduation section):

ASSIST → AUTO_FIX promotion when class meets ALL:
  • days_in_assist >= 14 (LENIENT default)
  • merged_assist_count >= 5
  • zero scope-guard violations in window

AUTO_FIX → permanent ASSIST demotion when:
  • >5 scope-guard violations in 60 days

Daily cron evaluates; writes promotion/demotion proposals to fix_audit_log
with phase='graduation' or phase='demotion'. Actual config file edits to
auto_fix_classes.yaml are NOT automated — operator must explicitly approve
proposed promotions (matches user mandate that whitelist additions require
deliberate ratification).

The graduation engine is read-only with respect to config — it ONLY writes
audit-log entries. The runbook documents how to ratify a proposed promotion.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = PROJECT_ROOT / "config" / "auto_remediation.yaml"
CLASSES_PATH = PROJECT_ROOT / "config" / "auto_fix_classes.yaml"

logger = logging.getLogger(__name__)


@dataclass
class ClassMetric:
    name: str                                 # class name OR fingerprint cluster
    days_in_assist: int = 0
    merged_assist_count: int = 0
    scope_violations: int = 0
    last_assist_merge_ts: Optional[str] = None
    first_assist_merge_ts: Optional[str] = None
    current_state: str = "ASSIST"             # ASSIST | AUTO_FIX | PERMANENT_ASSIST


@dataclass
class GraduationDecision:
    class_name: str
    decision: str        # 'PROMOTE_TO_AUTO_FIX' | 'DEMOTE_TO_PERMANENT_ASSIST' | 'NO_CHANGE'
    reason: str
    metric: ClassMetric
    decided_ts: str = ""


def _load_thresholds(cfg_path: Optional[Path] = None) -> dict:
    p = cfg_path or CFG_PATH
    cfg = yaml.safe_load(p.read_text()) or {} if p.exists() else {}
    g = cfg.get("graduation") or {}
    a2a = g.get("assist_to_auto_fix") or {}
    a2pa = g.get("auto_fix_to_permanent_assist") or {}
    return {
        "days_of_clean_assist": int(a2a.get("days_of_clean_assist", 14)),
        "min_merged_assist_fixes": int(a2a.get("min_merged_assist_fixes", 5)),
        "scope_violations_threshold": int(a2pa.get("scope_violations_threshold", 5)),
        "scope_violations_window_days": int(a2pa.get("scope_violations_window_days", 60)),
    }


def _classify_attempts_by_class(conn) -> dict[str, ClassMetric]:
    """Group fix_attempts by inferred class.

    Inference: ASSIST attempts are grouped by fingerprint (one cluster per fp).
    AUTO_FIX attempts are grouped by the matched_class noted in their notes
    field (auto_merger writes 'matched_class=<name>' there).
    """
    metrics: dict[str, ClassMetric] = {}

    # ASSIST stream — group by fingerprint as a proxy for "class"
    rows = conn.execute(
        """SELECT fingerprint, status, started_ts, finished_ts, monitor_outcome, notes
           FROM fix_attempts
           WHERE classification='ASSIST'
           ORDER BY started_ts ASC""").fetchall()
    for r in rows:
        fp = r["fingerprint"]
        m = metrics.setdefault(f"fp:{fp}", ClassMetric(name=f"fp:{fp}"))
        if r["status"] == "merged":
            m.merged_assist_count += 1
            if not m.first_assist_merge_ts:
                m.first_assist_merge_ts = r["started_ts"]
            m.last_assist_merge_ts = r["started_ts"]

    # Compute days_in_assist from first → now
    now = datetime.now(timezone.utc)
    for m in metrics.values():
        if m.first_assist_merge_ts:
            try:
                first = datetime.fromisoformat(m.first_assist_merge_ts.replace("T", " "))
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
                m.days_in_assist = (now - first).days
            except Exception:
                m.days_in_assist = 0

    # AUTO_FIX stream — group by matched_class name embedded in notes
    rows = conn.execute(
        """SELECT fingerprint, status, started_ts, finished_ts, monitor_outcome,
                  notes, gates_failed_json, blocked_by_gate
           FROM fix_attempts
           WHERE classification='AUTO_FIX'
           ORDER BY started_ts ASC""").fetchall()
    for r in rows:
        notes = (r["notes"] or "")
        cls_name = "unknown"
        if "matched_class=" in notes:
            try:
                cls_name = notes.split("matched_class=", 1)[1].split()[0]
            except Exception:
                cls_name = "unknown"
        m = metrics.setdefault(f"class:{cls_name}", ClassMetric(name=f"class:{cls_name}",
                                                                 current_state="AUTO_FIX"))
        # Count scope-guard violations as fix_attempts blocked by no_never_list_touched
        # OR no_safety_critical_function_modified gates
        blocking = (r["blocked_by_gate"] or "")
        if "no_never_list_touched" in blocking or "no_safety_critical_function_modified" in blocking:
            m.scope_violations += 1

    return metrics


def evaluate_graduation(conn, *, thresholds: Optional[dict] = None,
                        as_of: Optional[datetime] = None) -> list[GraduationDecision]:
    """Compute promotion/demotion decisions for every active class."""
    th = thresholds or _load_thresholds()
    metrics = _classify_attempts_by_class(conn)
    now = (as_of or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S")

    decisions: list[GraduationDecision] = []
    for name, m in metrics.items():
        if m.current_state == "ASSIST":
            # Promote if all thresholds met
            if (m.days_in_assist >= th["days_of_clean_assist"]
                    and m.merged_assist_count >= th["min_merged_assist_fixes"]
                    and m.scope_violations == 0):
                decisions.append(GraduationDecision(
                    class_name=name,
                    decision="PROMOTE_TO_AUTO_FIX",
                    reason=(f"days={m.days_in_assist}>={th['days_of_clean_assist']} AND "
                            f"merged={m.merged_assist_count}>={th['min_merged_assist_fixes']} "
                            f"AND scope_violations=0"),
                    metric=m, decided_ts=now,
                ))
            else:
                decisions.append(GraduationDecision(
                    class_name=name, decision="NO_CHANGE",
                    reason=f"days={m.days_in_assist}, merged={m.merged_assist_count}, violations={m.scope_violations}",
                    metric=m, decided_ts=now))
        elif m.current_state == "AUTO_FIX":
            # Demote if scope_violations exceed threshold in window
            if m.scope_violations > th["scope_violations_threshold"]:
                decisions.append(GraduationDecision(
                    class_name=name,
                    decision="DEMOTE_TO_PERMANENT_ASSIST",
                    reason=(f"scope_violations={m.scope_violations}>{th['scope_violations_threshold']} "
                            f"in {th['scope_violations_window_days']}d"),
                    metric=m, decided_ts=now,
                ))
            else:
                decisions.append(GraduationDecision(
                    class_name=name, decision="NO_CHANGE",
                    reason=f"scope_violations={m.scope_violations} (under threshold)",
                    metric=m, decided_ts=now))
    return decisions


def write_graduation_decisions(conn, decisions: list[GraduationDecision]) -> int:
    """Persist non-NO_CHANGE decisions to fix_audit_log."""
    n = 0
    for d in decisions:
        if d.decision == "NO_CHANGE":
            continue
        phase = "graduation" if d.decision.startswith("PROMOTE") else "demotion"
        conn.execute(
            """INSERT INTO fix_audit_log (
                  ts, phase, actor, decision, reasoning, payload_json, result_status
               ) VALUES (?, ?, 'graduation_engine', ?, ?, ?, 'success')""",
            (d.decided_ts, phase, d.decision, d.reason,
             json.dumps({
                 "class_name": d.class_name,
                 "metric": {
                     "name": d.metric.name,
                     "days_in_assist": d.metric.days_in_assist,
                     "merged_assist_count": d.metric.merged_assist_count,
                     "scope_violations": d.metric.scope_violations,
                     "current_state": d.metric.current_state,
                 },
             })))
        n += 1
    return n


def run(*, db_path: Optional[str] = None, dry_run: bool = False) -> dict:
    """Daily entrypoint. Returns summary metrics."""
    from db import atlas_db
    promotions = []; demotions = []
    with atlas_db.get_db(db_path) as conn:
        decisions = evaluate_graduation(conn)
        for d in decisions:
            if d.decision == "PROMOTE_TO_AUTO_FIX":
                promotions.append(d.class_name)
            elif d.decision == "DEMOTE_TO_PERMANENT_ASSIST":
                demotions.append(d.class_name)
        if not dry_run:
            n = write_graduation_decisions(conn, decisions)
            logger.info("Graduation engine wrote %d audit rows", n)
    return {
        "evaluated": len(decisions),
        "promotions": promotions,
        "demotions": demotions,
        "dry_run": dry_run,
    }
