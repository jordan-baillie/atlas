"""Error remediation API routes — read-only Phase 1 endpoints.

Backs the dashboard panel showing:
  • Error volume time-series (last 24h, 7d)
  • Classification breakdown (AUTO_FIX/ASSIST/ESCALATE/IGNORE counts)
  • Top error fingerprints (most-occurring last 24h)
  • Fix attempts summary (Phase 2+ — always 0 in Phase 1)
  • Health: capture alive, classifier backlog, halt state, phase state

Endpoints:
  GET /api/error_remediation/summary
  GET /api/error_remediation/timeseries?hours=24
  GET /api/error_remediation/fingerprints?hours=24&limit=20
  GET /api/error_remediation/attempts?status=&limit=50
  GET /api/error_remediation/health
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from services.auth import check_auth

router = APIRouter(prefix="/api/error_remediation", tags=["error_remediation"])
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path("/root/atlas")
_DB_PATH = PROJECT_ROOT / "data" / "atlas.db"

# Cache for cheap queries (60s TTL)
_cache: dict = {}
_CACHE_TTL = 60.0


def _get_conn():
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _cached(key: str, ttl: float, fn):
    now = time.time()
    if key in _cache and (now - _cache[key]["ts"]) < ttl:
        return _cache[key]["data"]
    data = fn()
    _cache[key] = {"data": data, "ts": now}
    return data


@router.get("/summary", dependencies=[Depends(check_auth)])
def summary():
    def _q():
        with _get_conn() as conn:
            now_utc = datetime.now(timezone.utc)
            cutoff_24h = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
            total = conn.execute("SELECT COUNT(*) FROM errors").fetchone()[0]
            last_24h = conn.execute("SELECT COUNT(*) FROM errors WHERE last_seen_ts >= ?", (cutoff_24h,)).fetchone()[0]
            unclassified = conn.execute("SELECT COUNT(*) FROM errors WHERE classification='UNCLASSIFIED'").fetchone()[0]
            by_class = {row["classification"]: row["n"]
                        for row in conn.execute("SELECT classification, COUNT(*) AS n FROM errors GROUP BY classification")}
            by_status = {row["remediation_status"]: row["n"]
                         for row in conn.execute("SELECT remediation_status, COUNT(*) AS n FROM errors GROUP BY remediation_status")}
            attempts_total = conn.execute("SELECT COUNT(*) FROM fix_attempts").fetchone()[0]
            attempts_by_status = {row["status"]: row["n"]
                                  for row in conn.execute("SELECT status, COUNT(*) AS n FROM fix_attempts GROUP BY status")}
            audit_total = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
            return {
                "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "errors_total": total,
                "errors_last_24h": last_24h,
                "errors_unclassified": unclassified,
                "by_classification": by_class,
                "by_remediation_status": by_status,
                "attempts_total": attempts_total,
                "attempts_by_status": attempts_by_status,
                "audit_log_total": audit_total,
                "phase": 1,
                "phase_3_enabled": False,
                "dry_run": True,
            }
    return _cached("summary", _CACHE_TTL, _q)


@router.get("/timeseries", dependencies=[Depends(check_auth)])
def timeseries(hours: int = Query(24, ge=1, le=168)):
    def _q():
        with _get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            # Bucket by hour
            rows = conn.execute(
                """SELECT substr(last_seen_ts, 1, 13) AS hour_bucket,
                          COUNT(*) AS n,
                          SUM(occurrence_count) AS occurrences
                   FROM errors
                   WHERE last_seen_ts >= ?
                   GROUP BY hour_bucket
                   ORDER BY hour_bucket ASC""",
                (cutoff,),
            ).fetchall()
            return {"hours": hours, "buckets": [
                {"hour": r["hour_bucket"], "errors": r["n"], "occurrences": r["occurrences"]}
                for r in rows
            ]}
    return _cached(f"timeseries:{hours}", _CACHE_TTL, _q)


@router.get("/fingerprints", dependencies=[Depends(check_auth)])
def fingerprints(hours: int = Query(24, ge=1, le=720), limit: int = Query(20, ge=1, le=100)):
    def _q():
        with _get_conn() as conn:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            rows = conn.execute(
                """SELECT fingerprint, occurrence_count, message, service, level,
                          classification, tier, file_path, line_number, exc_type,
                          first_seen_ts, last_seen_ts
                   FROM errors
                   WHERE last_seen_ts >= ?
                   ORDER BY occurrence_count DESC, last_seen_ts DESC
                   LIMIT ?""",
                (cutoff, limit),
            ).fetchall()
            return {"hours": hours, "limit": limit, "fingerprints": [dict(r) for r in rows]}
    return _cached(f"fp:{hours}:{limit}", _CACHE_TTL, _q)


@router.get("/attempts", dependencies=[Depends(check_auth)])
def attempts(status: str | None = None, limit: int = Query(50, ge=1, le=200)):
    def _q():
        with _get_conn() as conn:
            sql = """SELECT id, error_id, fingerprint, started_ts, finished_ts,
                            status, classification, fix_branch, fix_commit_sha,
                            fix_diff_lines, review_verdict, review_confidence,
                            blocked_by_gate, monitor_outcome, total_wall_seconds
                     FROM fix_attempts"""
            params: list = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY started_ts DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return {"limit": limit, "status_filter": status, "attempts": [dict(r) for r in rows]}
    return _cached(f"attempts:{status}:{limit}", 30, _q)


@router.get("/health", dependencies=[Depends(check_auth)])
def health():
    """Mirrors scripts/healthz_error_remediation.py output for dashboard."""
    def _q():
        with _get_conn() as conn:
            now_utc = datetime.now(timezone.utc)
            cutoff_24h = (now_utc - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")
            errors_24h = conn.execute("SELECT COUNT(*) FROM errors WHERE last_seen_ts >= ?", (cutoff_24h,)).fetchone()[0]
            backlog = conn.execute("SELECT COUNT(*) FROM errors WHERE classification='UNCLASSIFIED' AND remediation_status='NEW'").fetchone()[0]
            audit_24h = conn.execute("SELECT COUNT(*) FROM fix_audit_log WHERE ts >= ?", (cutoff_24h,)).fetchone()[0]
            halt_paths = [
                PROJECT_ROOT / "data" / "HALT",
                PROJECT_ROOT / ".live_halt",
                PROJECT_ROOT / "data" / "AUTO_REMEDIATION_HALT",
            ]
            halt_active = any(p.exists() for p in halt_paths)
            halt_files_present = [str(p) for p in halt_paths if p.exists()]
            backlog_ok = backlog <= 100
            return {
                "as_of": now_utc.strftime("%Y-%m-%dT%H:%M:%S"),
                "errors_last_24h": errors_24h,
                "classifier_backlog": backlog,
                "classifier_backlog_ok": backlog_ok,
                "audit_writes_24h": audit_24h,
                "halt_active": halt_active,
                "halt_files_present": halt_files_present,
                "phase": 1,
                "phase_3_enabled": False,
                "ok": backlog_ok and not halt_active,
            }
    return _cached("health", 30, _q)
