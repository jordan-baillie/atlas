"""db/signals — Signals CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "record_signal",
    "get_signals",
]


def record_signal(
    timestamp: str,
    ticker: str,
    strategy: str,
    universe: str,
    entry_price: float,
    stop_price: float,
    position_size: int,
    position_value: float,
    risk_amount: float,
    confidence: float,
    action: str,
    direction: str = "long",
    take_profit: Optional[float] = None,
    rationale: Optional[str] = None,
    features: Optional[Dict] = None,
    sector: Optional[str] = None,
    regime_state: Optional[str] = None,
    action_reason: Optional[str] = None,
    config_version: Optional[str] = None,
    market_id: Optional[str] = None,
) -> None:
    """Insert a signal record."""
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT INTO signals
                (timestamp, ticker, strategy, universe, direction, entry_price,
                 stop_price, take_profit, position_size, position_value, risk_amount,
                 confidence, rationale, features, sector, regime_state, action,
                 action_reason, config_version, market_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, ticker, strategy, universe, direction, entry_price,
                stop_price, take_profit, position_size, position_value, risk_amount,
                confidence, rationale,
                json.dumps(features) if features is not None else None,
                sector, regime_state, action, action_reason, config_version, market_id,
            ),
        )


def get_signals(
    days: Optional[int] = None,
    strategy: Optional[str] = None,
    ticker: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = 10000,
) -> List[Dict]:
    """Return signals with optional filters. Most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM signals WHERE 1=1"
        params: List[Any] = []
        if days:
            query += " AND timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")
        if strategy:
            query += " AND strategy=?"
            params.append(strategy)
        if ticker:
            query += " AND ticker=?"
            params.append(ticker)
        if action:
            query += " AND action=?"
            params.append(action)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        rows = db.execute(query, params).fetchall()
        result = []
        for row in rows:
            r = dict(row)
            if r.get("features"):
                r["features"] = json.loads(r["features"])
            result.append(r)
        return result
