"""db/regime — Regime-history CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import atlas.db as _adb

__all__ = [
    "record_regime",
    "get_current_regime",
    "get_current_regime_state",
    "get_regime_history",
]


def record_regime(
    date: str,
    state: str,
    trend_score: float,
    risk_score: float,
    active_universes: List[str],
    sizing_multiplier: float,
    reasoning: str = "",
    enabled_strategies: Optional[List[str]] = None,
    model_version: str = "v1",
    pending_state: Optional[str] = None,
) -> None:
    """Insert or replace a regime classification for a date."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO regime_history
                (date, regime_state, trend_score, risk_score, active_universes,
                 sizing_multiplier, enabled_strategies, reasoning, model_version,
                 pending_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                date, state, trend_score, risk_score,
                json.dumps(active_universes), sizing_multiplier,
                json.dumps(enabled_strategies) if enabled_strategies is not None else None,
                reasoning, model_version, pending_state,
            ),
        )


def get_current_regime() -> Optional[Dict]:
    """Return the most recent regime record, or None if table is empty."""
    with _adb.get_db() as db:
        row = db.execute(
            "SELECT * FROM regime_history ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if row:
            r = dict(row)
            r["active_universes"] = json.loads(r["active_universes"] or "[]")
            if r.get("enabled_strategies"):
                r["enabled_strategies"] = json.loads(r["enabled_strategies"])
            return r
        return None


def get_current_regime_state() -> Optional[str]:
    """Return only the regime_state string from the most recent regime_history row."""
    try:
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT regime_state FROM regime_history ORDER BY date DESC LIMIT 1"
            ).fetchone()
            return row["regime_state"] if row else None
    except Exception:
        return None


def get_regime_history(days: Optional[int] = None, limit: int = 10000) -> List[Dict]:
    """Return regime history, optionally limited to recent *days*."""
    with _adb.get_db() as db:
        query = "SELECT * FROM regime_history"
        params: List[Any] = []
        if days:
            query += " WHERE date >= date('now', ?)"
            params.append(f"-{days} days")
        query += f" ORDER BY date DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            r["active_universes"] = json.loads(r["active_universes"] or "[]")
            if r.get("enabled_strategies"):
                r["enabled_strategies"] = json.loads(r["enabled_strategies"])
            result.append(r)
        return result
