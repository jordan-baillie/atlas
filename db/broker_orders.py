"""db/broker_orders — Broker orders, fill-price oracle, and position protective records.

All public functions are re-exported through db.atlas_db for backward compat.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import db.atlas_db as _adb

__all__ = [
    "get_broker_fill_price",
    "get_broker_orders",
    "get_fill_price",
    "get_protective_record",
    "upsert_protective_record",
    "close_protective_record",
    "list_active_protective_records",
    "list_protective_gaps",
]

_log = logging.getLogger(__name__)


def get_broker_fill_price(
    symbol: str,
    side: str = "buy",
    after: Optional[str] = None,
    order_id: Optional[str] = None,
) -> Optional[float]:
    """Return the most recent fill price from broker_orders for *symbol*.

    Returns None if no matching filled order found or if broker_orders
    table does not exist (graceful degradation).
    """
    try:
        with _adb.get_db() as db:
            if order_id:
                row = db.execute(
                    "SELECT fill_price FROM broker_orders "
                    "WHERE order_id=? AND fill_price IS NOT NULL AND fill_price > 0",
                    (order_id,),
                ).fetchone()
            elif after:
                row = db.execute(
                    "SELECT fill_price FROM broker_orders "
                    "WHERE symbol=? AND side=? AND status='filled' "
                    "AND fill_price IS NOT NULL AND fill_price > 0 "
                    "AND submitted_at >= ? "
                    "ORDER BY filled_at DESC NULLS LAST, submitted_at DESC LIMIT 1",
                    (symbol, side.lower(), after),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT fill_price FROM broker_orders "
                    "WHERE symbol=? AND side=? AND status='filled' "
                    "AND fill_price IS NOT NULL AND fill_price > 0 "
                    "ORDER BY filled_at DESC NULLS LAST, submitted_at DESC LIMIT 1",
                    (symbol, side.lower()),
                ).fetchone()
            return float(row[0]) if row and row[0] is not None else None
    except Exception as exc:
        _log.debug(
            "get_broker_fill_price(%s, %s): %s (non-fatal)", symbol, side, exc
        )
        return None


def get_broker_orders(
    symbol: Optional[str] = None,
    side: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[Dict]:
    """Return broker_orders rows, optionally filtered.

    Returns empty list if table does not exist (graceful degradation).
    """
    try:
        with _adb.get_db() as db:
            query = "SELECT * FROM broker_orders WHERE 1=1"
            params: List[Any] = []
            if symbol:
                query += " AND symbol=?"
                params.append(symbol)
            if side:
                query += " AND side=?"
                params.append(side.lower())
            if status:
                query += " AND status=?"
                params.append(status.lower())
            query += " ORDER BY submitted_at DESC LIMIT ?"
            params.append(limit)
            rows = db.execute(query, params).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _log.debug("get_broker_orders failed: %s (non-fatal)", exc)
        return []


def get_fill_price(order_id: str, *, after: Optional[str] = None) -> Optional[float]:
    """Return the broker-confirmed fill price for a specific order_id.

    Reads from broker_orders table (Priority 1 — authoritative oracle).
    Returns None if order_id not in broker_orders OR not yet filled.
    """
    if not order_id:
        return None
    try:
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT fill_price, filled_at, status FROM broker_orders WHERE order_id = ?",
                (order_id,),
            ).fetchone()
        if not row:
            return None
        if row["status"] != "filled":
            return None
        if row["fill_price"] is None:
            return None
        if after is not None and row["filled_at"] is not None:
            if row["filled_at"] < after:
                return None
        return float(row["fill_price"])
    except Exception as exc:
        _log.debug("get_fill_price(%s): %s (non-fatal)", order_id, exc)
        return None


# ── Position Protective Orders ────────────────────────────────────────────────

def get_protective_record(market_id: str, ticker: str) -> Optional[Dict[str, Any]]:
    """Fetch the active protective record for a position. Returns None if missing."""
    try:
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT * FROM position_protective_orders "
                "WHERE market_id=? AND ticker=? AND status='active'",
                (market_id, ticker),
            ).fetchone()
            return dict(row) if row else None
    except Exception as exc:
        _log.warning(
            "get_protective_record(%s, %s) failed: %s", market_id, ticker, exc
        )
        return None


def upsert_protective_record(
    market_id: str,
    ticker: str,
    trade_id: Optional[int],
    position_qty: float,
    stop_order_id: Optional[str] = None,
    stop_price: Optional[float] = None,
    tp_order_id: Optional[str] = None,
    tp_price: Optional[float] = None,
    oco_class: Optional[str] = None,
) -> None:
    """Insert or update protective record. Always sets last_synced_at=now and status='active'."""
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with _adb.get_db() as db:
        db.execute(
            """
            INSERT INTO position_protective_orders
                (market_id, ticker, trade_id, position_qty,
                 stop_order_id, stop_price,
                 tp_order_id, tp_price,
                 oco_class, last_synced_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,'active')
            ON CONFLICT(market_id, ticker) DO UPDATE SET
                trade_id       = excluded.trade_id,
                position_qty   = excluded.position_qty,
                stop_order_id  = excluded.stop_order_id,
                stop_price     = excluded.stop_price,
                tp_order_id    = excluded.tp_order_id,
                tp_price       = excluded.tp_price,
                oco_class      = excluded.oco_class,
                last_synced_at = excluded.last_synced_at,
                status         = 'active'
            """,
            (
                market_id, ticker, trade_id, float(position_qty),
                stop_order_id, float(stop_price) if stop_price is not None else None,
                tp_order_id, float(tp_price) if tp_price is not None else None,
                oco_class, now,
            ),
        )


def close_protective_record(market_id: str, ticker: str) -> None:
    """Mark protective record as 'closed' when position exits. Idempotent."""
    try:
        with _adb.get_db() as db:
            db.execute(
                "UPDATE position_protective_orders "
                "SET status='closed', last_synced_at=? "
                "WHERE market_id=? AND ticker=?",
                (datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), market_id, ticker),
            )
    except Exception as exc:
        _log.warning(
            "close_protective_record(%s, %s) failed: %s", market_id, ticker, exc
        )


def list_active_protective_records(
    market_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List all status='active' records. Optionally filter by market."""
    try:
        with _adb.get_db() as db:
            if market_id:
                rows = db.execute(
                    "SELECT * FROM position_protective_orders "
                    "WHERE status='active' AND market_id=? "
                    "ORDER BY market_id, ticker",
                    (market_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM position_protective_orders "
                    "WHERE status='active' "
                    "ORDER BY market_id, ticker",
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _log.warning("list_active_protective_records failed: %s", exc)
        return []


def list_protective_gaps(
    market_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return open trades that have NO active protective record."""
    try:
        with _adb.get_db() as db:
            if market_id:
                rows = db.execute(
                    """
                    SELECT
                        t.id            AS trade_id,
                        t.ticker        AS ticker,
                        t.universe      AS market_id,
                        t.entry_date    AS entry_date,
                        CAST(julianday('now') - julianday(t.entry_date) AS INTEGER) AS days_open
                    FROM trades t
                    WHERE t.status = 'open'
                      AND t.superseded = 0
                      AND t.universe = ?
                      AND NOT EXISTS (
                          SELECT 1 FROM position_protective_orders p
                          WHERE p.market_id = t.universe
                            AND p.ticker    = t.ticker
                            AND p.status    = 'active'
                      )
                    ORDER BY t.entry_date
                    """,
                    (market_id,),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT
                        t.id            AS trade_id,
                        t.ticker        AS ticker,
                        t.universe      AS market_id,
                        t.entry_date    AS entry_date,
                        CAST(julianday('now') - julianday(t.entry_date) AS INTEGER) AS days_open
                    FROM trades t
                    WHERE t.status = 'open'
                      AND t.superseded = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM position_protective_orders p
                          WHERE p.market_id = t.universe
                            AND p.ticker    = t.ticker
                            AND p.status    = 'active'
                      )
                    ORDER BY t.entry_date
                    """,
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        _log.warning("list_protective_gaps failed: %s", exc)
        return []
