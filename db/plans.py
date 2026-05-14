"""db/plans — Trade plans CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "record_plan",
    "get_plan",
    "get_plans",
    "update_plan_status",
    "update_plan",
    "_validate_plan_date",
    "_decode_plan",
]


def _validate_plan_date(date: str) -> None:
    """Raise ValueError if plan date is suspiciously far from today (>30d or year mismatch)."""
    if not date:
        return
    import logging as _plan_log
    from datetime import date as _date
    _log = _plan_log.getLogger(__name__)
    try:
        plan_d = _date.fromisoformat(date[:10])
    except ValueError:
        return  # Non-standard date string — skip silently
    today = _date.today()
    delta_days = abs((plan_d - today).days)
    if delta_days > 30 or plan_d.year != today.year:
        raise ValueError(
            f"plan.date={date!r} is {delta_days}d from today (today year={today.year}, "
            f"plan year={plan_d.year}) — likely hardcoded test date leaking into production. "
            "Fix the call site or use datetime.today().strftime('%Y-%m-%d')."
        )


def record_plan(
    date: str,
    market_id: str,
    plan_data: Dict,
    regime_state: Optional[str] = None,
    active_universes: Optional[List[str]] = None,
    sizing_multiplier: Optional[float] = None,
    overlay_applied: bool = False,
    overlay_adjustments: Optional[Dict] = None,
    status: str = "pending_approval",
) -> int:
    """Insert a new plan. Returns the new plan id."""
    _validate_plan_date(date)
    with _adb.get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO plans
                (date, market_id, regime_state, active_universes, sizing_multiplier,
                 overlay_applied, overlay_adjustments, plan_data, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, market_id, regime_state,
                json.dumps(active_universes) if active_universes is not None else None,
                sizing_multiplier,
                1 if overlay_applied else 0,
                json.dumps(overlay_adjustments) if overlay_adjustments is not None else None,
                json.dumps(plan_data),
                status,
            ),
        )
        return cursor.lastrowid


def get_plan(date: str, market_id: str) -> Optional[Dict]:
    """Return the most recent plan for (date, market_id), or None."""
    with _adb.get_db() as db:
        row = db.execute(
            "SELECT * FROM plans WHERE date=? AND market_id=? ORDER BY id DESC LIMIT 1",
            (date, market_id),
        ).fetchone()
        if row:
            return _decode_plan(dict(row))
        return None


def get_plans(
    days: Optional[int] = None,
    status: Optional[str] = None,
    market_id: Optional[str] = None,
) -> List[Dict]:
    """Return plans with optional filters. Most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM plans WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        if status:
            query += " AND status=?"
            params.append(status)
        if market_id:
            query += " AND market_id=?"
            params.append(market_id)
        query += " ORDER BY id DESC"
        return [_decode_plan(dict(r)) for r in db.execute(query, params).fetchall()]


def update_plan_status(
    plan_id: int,
    status: str,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
) -> None:
    """Update a plan's status (and optional timestamps)."""
    with _adb.get_db() as db:
        db.execute(
            """
            UPDATE plans
            SET status      = ?,
                approved_at = COALESCE(?, approved_at),
                executed_at = COALESCE(?, executed_at)
            WHERE id = ?
            """,
            (status, approved_at, executed_at, plan_id),
        )


def update_plan(
    plan_id: int,
    status: Optional[str] = None,
    approved_at: Optional[str] = None,
    executed_at: Optional[str] = None,
    plan_data: Optional[Dict] = None,
) -> None:
    """Update an existing plan row. All args except plan_id are optional."""
    with _adb.get_db() as db:
        db.execute(
            """
            UPDATE plans
            SET status      = COALESCE(?, status),
                approved_at = COALESCE(?, approved_at),
                executed_at = COALESCE(?, executed_at),
                plan_data   = COALESCE(?, plan_data)
            WHERE id = ?
            """,
            (
                status,
                approved_at,
                executed_at,
                json.dumps(plan_data) if plan_data is not None else None,
                plan_id,
            ),
        )


def _decode_plan(row: Dict) -> Dict:
    """Deserialize JSON columns in a plan row."""
    if row.get("plan_data"):
        try:
            row["plan_data"] = json.loads(row["plan_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("active_universes"):
        try:
            row["active_universes"] = json.loads(row["active_universes"])
        except (json.JSONDecodeError, TypeError):
            pass
    if row.get("overlay_adjustments"):
        try:
            row["overlay_adjustments"] = json.loads(row["overlay_adjustments"])
        except (json.JSONDecodeError, TypeError):
            pass
    return row
