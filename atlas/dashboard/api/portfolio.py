"""Portfolio, trades, and equity API routes.

Phase 3 extraction from services/chat_server.py.

Routes:
  GET /api/portfolio         — open positions + latest equity (SQLite)
  GET /api/db/portfolio      — alias
  GET /api/trades            — closed trades with P&L slicers
  GET /api/db/trades         — alias
  GET /api/pnl_filter_options — dropdown values for P&L slicers
  GET /api/performance       — performance summary
  GET /api/db/performance    — alias
  GET /api/equity-curve      — historical equity data
  GET /api/market_equity_history — per-market virtual equity history
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth

router = APIRouter(tags=["portfolio"])
logger = logging.getLogger(__name__)


# ── GET /api/portfolio  +  /api/db/portfolio ──────────────────────────────────
# TODO: unused — not called by dashboard UI (data via /api/dashboard-data)

@router.get("/api/portfolio")
@router.get("/api/db/portfolio")
def db_portfolio(
    universe: str | None = None,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/portfolio — open positions + latest equity from market_equity_history.

    Equity source is market_equity_history (broker_equity == total account equity;
    allocated_equity == per-market virtual slice). This replaced the corrupt
    equity_curve table which had broken per-universe attribution (negative cash/
    positions_value were possible — audit finding F-01/F-04).
    """
    try:
        from atlas import db as atlas_db
        positions = atlas_db.get_open_positions()
        regime = atlas_db.get_current_regime()
        market_id = universe or "sp500"
        with atlas_db.get_db() as db:
            row = db.execute(
                "SELECT date, market_id, broker_equity, allocated_equity, "
                "cash_attributed, position_mv, broker_cash "
                "FROM market_equity_history "
                "WHERE market_id=? ORDER BY date DESC LIMIT 1",
                (market_id,),
            ).fetchone()
            if row:
                rd = dict(row)
                # Backward-compat shape: callers expect 'equity', 'cash', 'positions_value'
                equity = {
                    "date": rd["date"],
                    "market_id": rd["market_id"],
                    "equity": rd["broker_equity"],           # account total (F-01 fix)
                    "allocated_equity": rd["allocated_equity"],
                    "cash": rd["cash_attributed"],
                    "broker_cash": rd["broker_cash"],
                    "positions_value": rd["position_mv"],
                }
            else:
                equity = None
        return JSONResponse({"positions": positions, "regime": regime, "equity": equity})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@router.get("/api/performance")
@router.get("/api/db/performance")
def db_performance(
    days: int = 0,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/performance?days=30 — performance summary from SQLite."""
    try:
        from atlas import db as atlas_db
        days_or_none = days if days > 0 else None
        summary = atlas_db.performance_summary(days=days_or_none)
        return JSONResponse(summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/equity-curve ─────────────────────────────────────────────────────
# TODO: unused — not called by dashboard UI (equity data via /api/dashboard-data)

@router.get("/api/equity-curve")
def equity_curve(
    market: str = "sp500",
    days: int = 90,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/equity-curve?market=sp500&days=90 — equity history from SQLite.

    Returns oldest-first list (same as original handler which reversed the rows).
    """
    try:
        from atlas.db import get_equity_curve
        rows = get_equity_curve(market_id=market, days=days)
        # get_equity_curve returns oldest-first; reverse so most recent is first
        rows.reverse()
        return JSONResponse(rows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/market_equity_history ───────────────────────────────────────────

@router.get("/api/market_equity_history")
def market_equity_history(
    days: int = 90,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/market_equity_history?days=90 — per-market virtual equity history.

    Returns markets list and history rows oldest-first, suitable for charting.
    """
    try:
        from atlas.db import get_db
        _KNOWN_MARKETS = ["sp500", "commodity_etfs", "sector_etfs"]
        with get_db() as conn:
            rows = conn.execute(
                """SELECT date, market_id, allocated_equity, position_mv, cash_attributed,
                          broker_equity, broker_cash, snapshot_time
                   FROM market_equity_history
                   WHERE date >= DATE('now', ? || ' days')
                   ORDER BY date ASC, market_id ASC""",
                (f"-{days}",),
            ).fetchall()
        history = [dict(r) for r in rows]
        markets_seen = sorted({r["market_id"] for r in history}) or _KNOWN_MARKETS
        return JSONResponse({"markets": markets_seen, "history": history})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
