"""Dashboard data builder and /api/dashboard-data route.

Extracted from services/chat_server.py (Phase 8 decomposition).
Deepened by dashboard_builder extraction (candidate #8).

Routes:
  GET /api/dashboard-data  — main dashboard payload (broker + SQLite)

Thin orchestration only — all section builders live in dashboard_builder.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth

# ── Builder imports ───────────────────────────────────────────────────────────
from atlas.kernel.paths import PROJECT_ROOT
from atlas.dashboard.api.dashboard_builder import (
    fetch_broker_state,
    build_account_section,
    build_positions_section,
    build_orders_section,
    build_equity_curve_section,
    build_strategy_stats,
    build_pnl_summary,
    # Re-exported for backward compat (tests import from this module)
    _calc_alpaca_intraday_pnl,
    _calc_tiingo_daily_pnl,
    _get_portfolio_history,
)

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger("chat_server.dashboard")

_PROJECT_ROOT = PROJECT_ROOT

# ── Dashboard response cache (30-second TTL) ──────────────────────────────────
_DASHBOARD_CACHE: dict = {"ts": 0.0, "data": None}
_DASHBOARD_CACHE_TTL = 30.0  # seconds


# ── Dashboard data builder ────────────────────────────────────────────────────

def _build_dashboard_data() -> dict:
    """Build the complete dashboard data payload from SQLite + live broker.

    Thin orchestration: calls focused builders from dashboard_builder.py.
    Returns a dict that is serialised with json.dumps(..., default=str).

    Cache: 30-second in-process cache via _DASHBOARD_CACHE.
    """
    import time as _time

    # ── 30-second in-process cache ────────────────────────────────────────────
    _now = _time.monotonic()
    if (
        _DASHBOARD_CACHE["data"] is not None
        and (_now - _DASHBOARD_CACHE["ts"]) < _DASHBOARD_CACHE_TTL
    ):
        return _DASHBOARD_CACHE["data"]  # type: ignore[return-value]

    config_path = _PROJECT_ROOT / "config" / "active" / "sp500.json"
    with open(config_path) as f:
        config = json.load(f)
    market_id = config.get("market_id") or config.get("market", "sp500")
    config_dir = _PROJECT_ROOT / "config" / "active"

    result: dict = {}

    # ── 1. Broker data (parallel RPCs) ────────────────────────────────────────
    broker = None
    broker_state: dict = {"portfolio_history_raw": [], "clock": None}
    try:
        from atlas.brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if broker and broker.connect():
            # fetch_broker_state receives _get_portfolio_history from THIS module's
            # namespace so that patch.object(dash_mod, '_get_portfolio_history', ...)
            # in tests is honoured at call-time.
            broker_state = fetch_broker_state(broker, _get_portfolio_history)

            positions = build_positions_section(
                broker_state["positions_info"],
                broker_state["raw_positions"],
                broker_state["open_orders"],
            )
            account = build_account_section(
                broker_state["account_info"],
                broker_state["raw_acct"],
                positions,
                config_dir,
                broker_state["open_orders"],
            )
            orders = build_orders_section(broker_state["orders_info"])

            result["account"] = account
            result["positions"] = positions
            result["recent_orders"] = orders
            result["summary"] = {
                "equity": account.get("equity", 0),
                "total_pnl": account.get("total_pnl", 0),
                "total_pnl_pct": account.get("total_pnl_pct", 0),
                "open_positions": len(positions),
            }
    except Exception as e:  # noqa: BLE001
        logger.warning("Alpaca account data fetch failed: %s", e)
        result["account"] = {}
        result["positions"] = []
        result["recent_orders"] = []
        result["summary"] = {}

    # ── 2. Market clock ───────────────────────────────────────────────────────
    clock = broker_state.get("clock")
    try:
        if broker is not None and clock is not None:
            result["market_clock"] = {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
                "timestamp": str(clock.timestamp),
            }
        elif broker is not None:
            # broker connected but clock was not fetched — retry once
            _retry_clock = broker._broker_call(broker._trade_client.get_clock)
            result["market_clock"] = {
                "is_open": _retry_clock.is_open,
                "next_open": str(_retry_clock.next_open),
                "next_close": str(_retry_clock.next_close),
                "timestamp": str(_retry_clock.timestamp),
            }
        else:
            result["market_clock"] = {"is_open": False}
    except Exception as e:  # noqa: BLE001
        logger.warning("Market clock fetch failed: %s", e)
        result["market_clock"] = {"is_open": False}

    # ── 3. Equity curve (Alpaca history or SQLite fallback + today's point) ───
    live_equity = round(
        float((result.get("account") or {}).get("equity", 0) or 0), 2
    )
    portfolio_history = build_equity_curve_section(
        broker_state["portfolio_history_raw"],
        live_equity,
        result.get("market_clock", {}),
    )
    result["portfolio_history"] = portfolio_history

    # ── 4. Strategy stats + SPY benchmark ────────────────────────────────────
    positions = result.get("positions", [])
    stats = build_strategy_stats(positions, portfolio_history)
    result["strategy_performance"] = stats["strategy_performance"]
    result["strategy_allocation"] = stats["strategy_allocation"]
    if "benchmark" in stats:
        result["benchmark"] = stats["benchmark"]
    # Use forward-filled portfolio_history (SPY calendar alignment)
    if "_portfolio_history_filled" in stats:
        portfolio_history = stats["_portfolio_history_filled"]
        result["portfolio_history"] = portfolio_history

    # ── 5. PnL summary (also backfills positions + portfolio_history) ─────────
    # Guard: if broker failed (connect returned False without exception),
    # "summary" may not be in result yet.
    result.setdefault("summary", {})
    summary_update = build_pnl_summary(positions, market_id, portfolio_history, config)
    result["summary"].update(summary_update)

    result["timestamp"] = datetime.now().isoformat()

    # ── Write result to 30-second cache ───────────────────────────────────────
    _DASHBOARD_CACHE["data"] = result
    _DASHBOARD_CACHE["ts"] = _time.monotonic()

    return result


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/api/dashboard-data")
def dashboard_data(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/dashboard-data — main dashboard payload (replaces static JSON).

    Uses json.dumps(..., default=str) to handle enum/datetime values from
    broker dataclasses, exactly as the original handler does.
    """
    try:
        from atlas.analytics.strategy_ev import (
            get_latest_ev_stats,
            compute_all_strategies_ev,
            persist_strategy_ev,
        )
        data = _build_dashboard_data()
        # Inject EV stats into dashboard payload
        try:
            ev_stats = get_latest_ev_stats()
            if not ev_stats:
                results = compute_all_strategies_ev(min_trades=3)
                persist_strategy_ev(results)
                ev_stats = get_latest_ev_stats()
            data["ev_stats"] = ev_stats
        except Exception as e:  # noqa: BLE001
            logger.warning("EV stats failed: %s", e, exc_info=True)
            data["ev_stats"] = {}
        body = json.dumps(data, default=str)
        return Response(content=body, media_type="application/json")
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to build dashboard data")
        raise HTTPException(status_code=500, detail=str(e))
