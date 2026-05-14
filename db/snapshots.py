"""db/snapshots — Portfolio and position snapshot CRUD.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "record_position_snapshots",
    "get_position_snapshots",
    "record_snapshot",
    "record_all_markets_snapshot",
    "get_latest_snapshot",
    "get_snapshots",
    "_decode_snapshot",
]


def record_position_snapshots(
    date: str,
    market_id: str,
    positions: List[Dict],
) -> None:
    """Write per-position snapshots for historical tracking.

    Deletes existing snapshots for this date/market before inserting
    (idempotent — safe to re-run).
    """
    with _adb.get_db() as db:
        # Create table if not exists
        db.execute("""
            CREATE TABLE IF NOT EXISTS position_snapshots (
                date TEXT NOT NULL,
                market_id TEXT NOT NULL,
                ticker TEXT NOT NULL,
                strategy TEXT,
                entry_date TEXT,
                entry_price REAL,
                close_price REAL,
                shares INTEGER,
                unrealized_pnl REAL,
                unrealized_pnl_pct REAL,
                stop_price REAL,
                take_profit REAL,
                mae_pct REAL,
                mfe_pct REAL,
                holding_days INTEGER,
                sector TEXT,
                PRIMARY KEY (date, market_id, ticker)
            )
        """)
        # Delete existing for idempotent re-runs
        db.execute("DELETE FROM position_snapshots WHERE date=? AND market_id=?", (date, market_id))
        for pos in positions:
            db.execute(
                """INSERT INTO position_snapshots
                    (date, market_id, ticker, strategy, entry_date, entry_price, close_price,
                     shares, unrealized_pnl, unrealized_pnl_pct, stop_price, take_profit,
                     mae_pct, mfe_pct, holding_days, sector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date, market_id, pos.get("ticker"), pos.get("strategy"),
                 pos.get("entry_date"), pos.get("entry_price"), pos.get("close_price"),
                 pos.get("shares"), pos.get("unrealized_pnl"), pos.get("unrealized_pnl_pct"),
                 pos.get("stop_price"), pos.get("take_profit"),
                 pos.get("mae_pct"), pos.get("mfe_pct"), pos.get("holding_days"),
                 pos.get("sector")),
            )


def get_position_snapshots(date: str, market_id: Optional[str] = None) -> List[Dict]:
    """Return position snapshots for a given date."""
    with _adb.get_db() as db:
        if market_id:
            rows = db.execute(
                "SELECT * FROM position_snapshots WHERE date=? AND market_id=?",
                (date, market_id),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM position_snapshots WHERE date=?", (date,)
            ).fetchall()
        return [dict(r) for r in rows]


def record_snapshot(
    timestamp: str,
    total_equity: Optional[float] = None,
    cash: Optional[float] = None,
    positions: Optional[List[Dict]] = None,
    exposure_by_universe: Optional[Dict] = None,
    exposure_by_sector: Optional[Dict] = None,
    regime_state: Optional[str] = None,
    source: str = "eod",
    market_id: str = "sp500",
) -> None:
    """Insert a per-market portfolio snapshot.

    Automatically computes and stores ``daily_pnl_pct`` as
    ``(total_equity - prev_total_equity) / prev_total_equity * 100``.
    """
    with _adb.get_db() as db:
        # Compute daily_pnl_pct from previous snapshot for this market.
        daily_pnl_pct: Optional[float] = None
        if total_equity is not None:
            prev_row = db.execute(
                """SELECT total_equity FROM portfolio_snapshots
                   WHERE market_id = ?
                   ORDER BY timestamp DESC LIMIT 1""",
                (market_id,),
            ).fetchone()
            if prev_row is not None:
                prev_eq = prev_row[0]
                if prev_eq is not None and prev_eq != 0.0:
                    daily_pnl_pct = round(
                        (total_equity - prev_eq) / prev_eq * 100.0, 4
                    )

        db.execute(
            """
            INSERT INTO portfolio_snapshots
                (timestamp, total_equity, cash, positions, exposure_by_universe,
                 exposure_by_sector, regime_state, source, market_id, daily_pnl_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp, total_equity, cash,
                json.dumps(positions) if positions is not None else None,
                json.dumps(exposure_by_universe) if exposure_by_universe is not None else None,
                json.dumps(exposure_by_sector) if exposure_by_sector is not None else None,
                regime_state, source, market_id, daily_pnl_pct,
            ),
        )


def record_all_markets_snapshot(
    timestamp: str,
    broker_equity: float,
    broker_cash: float,
    source: str = "eod",
) -> None:
    """Write one aggregate snapshot row (market_id='ALL') per EOD cycle."""
    record_snapshot(
        timestamp=timestamp,
        total_equity=broker_equity,
        cash=broker_cash,
        positions=None,
        regime_state=None,
        source=source,
        market_id="ALL",
    )


def get_latest_snapshot(market_id: str = "ALL") -> Optional[Dict]:
    """Return the most recent portfolio snapshot for a given market."""
    with _adb.get_db() as db:
        if market_id is None:
            row = db.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        else:
            row = db.execute(
                "SELECT * FROM portfolio_snapshots"
                " WHERE market_id=?"
                " ORDER BY timestamp DESC LIMIT 1",
                (market_id,),
            ).fetchone()
        if row:
            return _decode_snapshot(dict(row))
        return None


def get_snapshots(
    days: Optional[int] = None,
    limit: int = 10000,
    market_id: Optional[str] = "ALL",
) -> List[Dict]:
    """Return portfolio snapshots, most recent first."""
    with _adb.get_db() as db:
        query = "SELECT * FROM portfolio_snapshots"
        params: List[Any] = []
        conditions: List[str] = []
        if market_id is not None:
            conditions.append("market_id=?")
            params.append(market_id)
        if days:
            conditions.append("timestamp >= datetime('now', ?)")
            params.append(f"-{days} days")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += f" ORDER BY timestamp DESC LIMIT {int(limit)}"
        return [
            _decode_snapshot(dict(r)) for r in db.execute(query, params).fetchall()
        ]


def _decode_snapshot(row: Dict) -> Dict:
    for field in ("positions", "exposure_by_universe", "exposure_by_sector"):
        if row.get(field):
            try:
                row[field] = json.loads(row[field])
            except (json.JSONDecodeError, TypeError):
                pass
    return row
