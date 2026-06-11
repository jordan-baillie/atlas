"""db/system_misc — Heartbeats, system logs, and Telegram message capture.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import atlas.db as _adb

__all__ = [
    "record_heartbeat",
    "get_heartbeats",
    "record_system_log",
    "get_system_logs",
    "record_telegram_outbound",
    ]

_log = logging.getLogger(__name__)


def record_heartbeat(
    service: str,
    status: str,
    detail: Optional[Dict] = None,
) -> None:
    """Upsert a heartbeat for *service*."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO heartbeats (service, timestamp, status, detail)
            VALUES (?, datetime('now'), ?, ?)
            """,
            (service, status, json.dumps(detail) if detail is not None else None),
        )


def get_heartbeats(service: Optional[str] = None) -> List[Dict]:
    """Return heartbeats, optionally filtered by service."""
    with _adb.get_db() as db:
        query = "SELECT * FROM heartbeats"
        params: List[Any] = []
        if service:
            query += " WHERE service=?"
            params.append(service)
        query += " ORDER BY service"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


def record_system_log(
    level: str,
    service: str,
    message: Optional[str] = None,
    detail: Optional[Dict] = None,
) -> None:
    """Append a system log entry."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT INTO system_log (level, service, message, detail)
            VALUES (?, ?, ?, ?)
            """,
            (level, service, message, json.dumps(detail) if detail is not None else None),
        )


def get_system_logs(
    hours: Optional[int] = None,
    service: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 200,
) -> List[Dict]:
    """Return system log entries, most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM system_log WHERE 1=1"
        params: List[Any] = []
        if hours:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{hours} hours")
        if service:
            query += " AND service=?"
            params.append(service)
        if level:
            query += " AND level=?"
            params.append(level)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("detail"):
                try:
                    r["detail"] = json.loads(r["detail"])
                except (json.JSONDecodeError, TypeError):
                    pass
            result.append(r)
        return result


# ── Telegram message capture ────────────────────────────────────────────────

def record_telegram_outbound(
    chat_id: str,
    body: str,
    *,
    parse_mode: Optional[str] = None,
    message_id: Optional[int] = None,
    api_status: Optional[int] = None,
    api_error: Optional[str] = None,
    sent_at: Optional[str] = None,
) -> Optional[int]:
    """Persist an outbound Telegram message. Fail-open: returns None on DB error."""
    try:
        ts = sent_at or datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with _adb.get_db() as db:
            cur = db.execute(
                """
                INSERT INTO telegram_messages
                    (direction, chat_id, message_id, body, parse_mode,
                     sent_at, api_status, api_error, is_command, command_name)
                VALUES ('outbound', ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (str(chat_id), message_id, body, parse_mode, ts, api_status, api_error),
            )
            return cur.lastrowid
    except Exception as e:  # noqa: BLE001 — fail-open observability path
        _log.warning(
            "record_telegram_outbound failed (capture only — send pipeline unaffected): %s", e
        )
        return None
