"""db/equity — Equity-curve CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import atlas.db as _adb

__all__ = [
    "record_equity",
    "get_equity_curve",
    "get_latest_equity",
]


def record_equity(
    date: str,
    market_id: str,
    equity: float,
    cash: Optional[float] = None,
    positions_value: Optional[float] = None,
    day_pnl: Optional[float] = None,
    regime_state: Optional[str] = None,
    broker_equity: Optional[float] = None,
    daily_pnl_pct: Optional[float] = None,
    total_pnl: Optional[float] = None,
    total_pnl_pct: Optional[float] = None,
    positions_count: Optional[int] = None,
    realized_pnl: Optional[float] = None,
) -> None:
    """Upsert an equity curve data point."""
    with _adb.get_db() as db:
        # Ensure new columns exist (idempotent migration)
        for col, ctype in [
            ("broker_equity", "REAL"),
            ("daily_pnl_pct", "REAL"),
            ("total_pnl", "REAL"),
            ("total_pnl_pct", "REAL"),
            ("positions_count", "INTEGER"),
            ("realized_pnl", "REAL"),
        ]:
            try:
                db.execute(f"ALTER TABLE equity_curve ADD COLUMN {col} {ctype}")
            except sqlite3.OperationalError:  # Column already exists
                pass
        db.execute(
            """
            INSERT OR REPLACE INTO equity_curve
                (date, market_id, equity, cash, positions_value, day_pnl, regime_state,
                 broker_equity, daily_pnl_pct, total_pnl, total_pnl_pct, positions_count, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (date, market_id, equity, cash, positions_value, day_pnl, regime_state,
             broker_equity, daily_pnl_pct, total_pnl, total_pnl_pct, positions_count, realized_pnl),
        )


def get_equity_curve(
    market_id: str, days: Optional[int] = None
) -> List[Dict]:
    """Return equity curve rows for *market_id*, oldest first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM equity_curve WHERE market_id=?"
        params: List[Any] = [market_id]
        if days:
            query += " AND date >= date('now', ?)"
            params.append(f"-{days} days")
        query += " ORDER BY date ASC"
        return [dict(r) for r in db.execute(query, params).fetchall()]


def get_latest_equity(market_id: Optional[str] = None) -> Optional[Dict]:
    """Return the most recent equity curve row, optionally filtered by market."""
    with _adb.get_db() as db:
        if market_id:
            row = db.execute(
                "SELECT * FROM equity_curve WHERE market_id=? ORDER BY date DESC LIMIT 1",
                (market_id,),
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM equity_curve ORDER BY date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None
