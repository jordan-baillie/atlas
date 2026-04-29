#!/usr/bin/env python3
"""Validate the auto-remediation triage classifier against 30 days of
historical errors from the system_log table.

Mandate: ≥94% IGNORE rate (per forensic finding; circuit-breaker + execution-blocked).
Exit codes:
   0  — pass (≥94% IGNORE)
   1  — warn (80-94% IGNORE; classifier needs tuning but not stop)
   2  — fail (<80% IGNORE; STOP per user mandate)
   3  — infrastructure error

Usage:
   python3 scripts/validate_classifier_30day.py
   python3 scripts/validate_classifier_30day.py --days 60 --output reports/phase1-classifier-validation-2026-04-29.md
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

from utils.logging_config import setup_logging
from db import atlas_db
from core.triage import TriageClassifier

logger = setup_logging("validate_classifier_30day", telegram_errors=False)


def load_system_log_errors(conn, days: int) -> list[dict]:
    """Fetch ERROR/CRITICAL rows from system_log over the past N days."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_log'"
    ).fetchone():
        logger.error("system_log table missing — cannot validate")
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    rows = conn.execute(
        """SELECT timestamp, service, level, message, detail
           FROM system_log
           WHERE level IN ('error','critical')
             AND timestamp >= ?
           ORDER BY timestamp ASC""",
        (cutoff,),
    ).fetchall()
    out = []
    for r in rows:
        # Construct an error-dict matching the errors-table column shape that
        # core.triage.TriageClassifier expects.
        msg = r["message"] or ""
        # system_log doesn't capture file_path/exc_type — best-effort extraction
        # from message (we do NOT make up data; missing means classifier sees None)
        out.append({
            "ts": r["timestamp"],
            "level": (r["level"] or "").upper(),
            "service": r["service"],
            "logger_name": r["service"],
            "message": msg,
            "exc_type": None,
            "traceback": None,
            "file_path": None,
            "line_number": None,
            "function_name": None,
        })
    return out


def replay(errors: list[dict], classifier: TriageClassifier) -> dict:
    """Run the classifier over the full corpus; return metrics + samples."""
    by_class: Counter = Counter()
    by_rule: Counter = Counter()
    by_service_class: dict[str, Counter] = defaultdict(Counter)
    samples_by_class: dict[str, list] = defaultdict(list)

    for e in errors:
        result = classifier.classify(e)
        by_class[result.classification] += 1
        by_rule[result.rule_id] += 1
        by_service_class[e.get("service") or "unknown"][result.classification] += 1
        if len(samples_by_class[result.classification]) < 5:
            samples_by_class[result.classification].append({
                "ts": e["ts"],
                "service": e.get("service"),
                "message": (e.get("message") or "")[:120],
                "rule_id": result.rule_id,
                "reason": result.reason,
            })

    total = sum(by_class.values())
    pct = {k: (v / total * 100) if total else 0.0 for k, v in by_class.items()}
    return {
        "total": total,
        "by_class": dict(by_class),
        "pct_by_class": {k: round(v, 2) for k, v in pct.items()},
        "by_rule": dict(by_rule.most_common(20)),
        "by_service_class": {k: dict(v) for k, v in by_service_class.items()},
        "samples_by_class": dict(samples_by_class),
    }


def write_report(metrics: dict, days: int, output_path: Path) -> None:
    """Write a human-readable markdown report to output_path."""
    ignore_pct = metrics["pct_by_class"].get("IGNORE", 0.0)
    pending_clear_pct = metrics["pct_by_class"].get("IGNORE_PENDING_CLEAR", 0.0)
    deferred_pct = metrics["pct_by_class"].get("ESCALATE_DEFERRED", 0.0)
    escalate_pct = metrics["pct_by_class"].get("ESCALATE", 0.0)
    assist_pct = metrics["pct_by_class"].get("ASSIST", 0.0)
    auto_fix_pct = metrics["pct_by_class"].get("AUTO_FIX", 0.0)

    # Note: IGNORE_PENDING_CLEAR is a transient state when HALT file present.
    # For the gate calculation we treat IGNORE_PENDING_CLEAR + IGNORE as
    # "not requiring action".
    effective_ignore_pct = ignore_pct + pending_clear_pct
    gate_pass = effective_ignore_pct >= 94.0
    gate_warn = 80.0 <= effective_ignore_pct < 94.0
    gate_fail = effective_ignore_pct < 80.0

    verdict = "PASS" if gate_pass else ("WARN" if gate_warn else "FAIL")

    lines = [
        f"# Phase 1 Classifier Validation — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "",
        f"**Replay window:** last {days} days",
        f"**Total errors:** {metrics['total']}",
        f"**Verdict:** **{verdict}**",
        (
            f"**Effective IGNORE rate:** {effective_ignore_pct:.2f}% "
            f"(IGNORE {ignore_pct:.2f}% + IGNORE_PENDING_CLEAR {pending_clear_pct:.2f}%)"
        ),
        f"**Mandate gate:** ≥94.00% IGNORE → {'PASS' if gate_pass else 'BELOW THRESHOLD'}",
        "",
        "## Distribution",
        "",
        "| Class | Count | Pct |",
        "|---|---:|---:|",
    ]
    for cls in [
        "IGNORE",
        "IGNORE_PENDING_CLEAR",
        "ESCALATE_DEFERRED",
        "ESCALATE",
        "ASSIST",
        "AUTO_FIX",
    ]:
        n = metrics["by_class"].get(cls, 0)
        p = metrics["pct_by_class"].get(cls, 0.0)
        lines.append(f"| {cls} | {n} | {p:.2f}% |")

    lines.extend([
        "",
        "## Top 20 rules fired",
        "",
        "| Rule ID | Count |",
        "|---|---:|",
    ])
    for rule, n in metrics["by_rule"].items():
        lines.append(f"| `{rule}` | {n} |")

    lines.extend([
        "",
        "## By service",
        "",
        "| Service | IGNORE | ESCALATE | ASSIST | DEFERRED | PENDING_CLEAR |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    services = sorted(
        metrics["by_service_class"].keys(),
        key=lambda s: -sum(metrics["by_service_class"][s].values()),
    )[:30]
    for svc in services:
        d = metrics["by_service_class"][svc]
        lines.append(
            f"| `{svc}` | {d.get('IGNORE', 0)} | {d.get('ESCALATE', 0)} "
            f"| {d.get('ASSIST', 0)} | {d.get('ESCALATE_DEFERRED', 0)} "
            f"| {d.get('IGNORE_PENDING_CLEAR', 0)} |"
        )

    lines.extend(["", "## Samples by classification", ""])
    for cls, samples in metrics["samples_by_class"].items():
        if not samples:
            continue
        lines.append(f"### {cls}")
        lines.append("")
        for s in samples:
            lines.append(
                f"- [{s['ts']}] `{s.get('service')}` — {s['message']}"
            )
            lines.append(f"    rule=`{s['rule_id']}`, reason={s['reason']}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines))
    logger.info("Report written: %s", output_path)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Validate triage classifier against 30-day system_log history."
    )
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--db", default=None, help="Override DB path (default: atlas.db)")
    p.add_argument(
        "--output",
        default=None,
        help=(
            "Output report path; default "
            "reports/phase1-classifier-validation-<DATE>.md"
        ),
    )
    args = p.parse_args()

    out = Path(args.output) if args.output else (
        PROJECT_ROOT
        / "reports"
        / f"phase1-classifier-validation-{datetime.now().strftime('%Y-%m-%d')}.md"
    )

    classifier = TriageClassifier()
    with atlas_db.get_db(args.db) as conn:
        errors = load_system_log_errors(conn, args.days)

    if not errors:
        logger.warning(
            "No errors found in last %d days — cannot validate", args.days
        )
        return 3

    metrics = replay(errors, classifier)
    write_report(metrics, args.days, out)

    # Gate enforcement
    eff_ignore = (
        metrics["pct_by_class"].get("IGNORE", 0.0)
        + metrics["pct_by_class"].get("IGNORE_PENDING_CLEAR", 0.0)
    )
    print(json.dumps({
        "total": metrics["total"],
        "effective_ignore_pct": round(eff_ignore, 2),
        "by_class_pct": metrics["pct_by_class"],
        "report": str(out),
    }, indent=2))

    if eff_ignore >= 94.0:
        logger.info(
            "Gate PASS: effective IGNORE rate %.2f%% ≥ 94.00%%", eff_ignore
        )
        return 0
    if eff_ignore >= 80.0:
        logger.warning(
            "Below 94%% IGNORE gate (%.2f%%) — classifier needs tuning", eff_ignore
        )
        return 1
    logger.error(
        "Below 80%% IGNORE gate (%.2f%%) — STOP per user mandate", eff_ignore
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
