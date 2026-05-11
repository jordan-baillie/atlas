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
  GET /api/overlay/decisions — AI overlay decisions
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

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
        from db import atlas_db
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


# ── GET /api/trades  +  /api/db/trades ───────────────────────────────────────
# TODO: unused — not called by dashboard UI

@router.get("/api/trades")
@router.get("/api/db/trades")
def db_trades(
    days: int = 0,
    strategy: str | None = None,
    universe: str | None = None,
    market_id: str | None = None,
    sector: str | None = None,
    limit: int = 100,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/trades — closed trades with optional P&L slicers (RCA #4E).

    Query params:
      days       — lookback window in days (0 = all time)
      strategy   — filter by strategy name
      universe   — filter by market universe (alias: market_id)
      market_id  — alias for universe
      sector     — filter by sector via JOIN against signals table
      limit      — max rows returned (default 100)
    """
    try:
        from db.atlas_db import get_db
        # market_id is a frontend-friendly alias for universe
        effective_universe = universe or market_id
        with get_db() as db:
            _cols = {r[1] for r in db.execute("PRAGMA table_info(trades)").fetchall()}
            _sup_clause = " AND superseded=0" if "superseded" in _cols else ""
            sql = f"SELECT * FROM trades WHERE status='closed'{_sup_clause}"
            params: list = []
            if days > 0:
                sql += " AND exit_date >= date('now', ?)"
                params.append(f"-{days} days")
            if strategy:
                sql += " AND strategy = ?"
                params.append(strategy)
            if effective_universe:
                sql += " AND universe = ?"
                params.append(effective_universe)
            if sector:
                sql += (
                    " AND ticker IN"
                    " (SELECT DISTINCT ticker FROM signals WHERE sector = ?)"
                )
                params.append(sector)
            sql += " ORDER BY exit_date DESC LIMIT ?"
            params.append(int(limit))
            trades = [dict(r) for r in db.execute(sql, params).fetchall()]
        return JSONResponse(trades)  # flat array — matches PnlTrade[] in frontend usePnlTrades
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/pnl_filter_options ───────────────────────────────────────────────

@router.get("/api/pnl_filter_options")
def pnl_filter_options(
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/pnl_filter_options — distinct dropdown values for P&L slicers (RCA #4E).

    Returns:
      markets    — distinct universe values from closed trades
      strategies — distinct strategy values from closed trades
      sectors    — distinct sector values from signals table
    """
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            markets = [
                r[0] for r in db.execute(
                    "SELECT DISTINCT universe FROM trades"
                    " WHERE status='closed' AND universe IS NOT NULL ORDER BY universe"
                ).fetchall()
            ]
            strategies = [
                r[0] for r in db.execute(
                    "SELECT DISTINCT strategy FROM trades"
                    " WHERE status='closed' AND strategy IS NOT NULL ORDER BY strategy"
                ).fetchall()
            ]
            sectors = [
                r[0] for r in db.execute(
                    "SELECT DISTINCT sector FROM signals"
                    " WHERE sector IS NOT NULL ORDER BY sector"
                ).fetchall()
            ]
        return JSONResponse({"markets": markets, "strategies": strategies, "sectors": sectors})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/performance  +  /api/db/performance ─────────────────────────────
# TODO: unused — not called by dashboard UI

@router.get("/api/performance")
@router.get("/api/db/performance")
def db_performance(
    days: int = 0,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/performance?days=30 — performance summary from SQLite."""
    try:
        from db import atlas_db
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
        from db.atlas_db import get_equity_curve
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
        from db.atlas_db import get_db
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


# ── GET /api/overlay/decisions ────────────────────────────────────────────────
# TODO: unused — not called by dashboard UI (OverlayDecisions component not rendered)

@router.get("/api/overlay/decisions")
def overlay_decisions(
    days: int = 30,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/overlay/decisions?days=30 — AI overlay decisions from SQLite."""
    try:
        from db.atlas_db import get_overlay_decisions
        decisions = get_overlay_decisions(days=days)
        return JSONResponse(decisions)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
