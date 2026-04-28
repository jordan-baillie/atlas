#!/usr/bin/env python3
"""Atlas Dashboard Server — FastAPI port of dashboard_server.py.

Phase 2: adds headless Pi chat (WebSocket + REST) on top of the Phase 1
foundation.  All Phase 1 routes remain unchanged.

New endpoints
-------------
  GET  /api/chat/sessions          — list chat sessions
  POST /api/chat/sessions          — create a new session
  GET  /api/chat/sessions/{id}/messages — paginated history
  GET  /api/chat/token             — short-lived WS auth token
  WS   /ws/chat?token=<tok>        — streaming chat WebSocket

Credentials from ~/.atlas-secrets.json:
    dashboard_user, dashboard_pass

Run (direct):
    python3 services/chat_server.py

Run (uvicorn module):
    python3 -m uvicorn services.chat_server:app --host 127.0.0.1 --port 8899

Run (systemd):
    systemctl start atlas-dashboard
"""
# TODO: Refactor — 1369 lines. Split into: routes/, websocket/, chat/ sub-packages.
# TODO: Split into api_routes.py, auth.py, static.py using FastAPI APIRouter

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# ── Housekeeping (mirror dashboard_server.py top-level setup) ────────────────

signal.signal(signal.SIGHUP, signal.SIG_IGN)

PROJECT_ROOT = Path("/root/atlas")
SECRETS_PATH = Path(os.environ.get("ATLAS_SECRETS_PATH", str(Path.home() / ".atlas-secrets.json")))
SERVE_DIR = PROJECT_ROOT / "dashboard" / "data"
REACT_DIR = PROJECT_ROOT / "dashboard-ui" / "dist"
BIND = "127.0.0.1"
PORT = 8899

# Must be set before importing Atlas modules (same as dashboard_server.py)
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# ── FastAPI imports (after path setup) ───────────────────────────────────────

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from pydantic import BaseModel  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

# ── Chat imports ──────────────────────────────────────────────────────────────
try:
    from services.chat_db import (  # noqa: E402
        init_db as init_chat_db,
        create_session as _chat_create_session,
        get_session as _chat_get_session,
        list_sessions as _chat_list_sessions,
        add_message as _chat_add_message,
        get_messages as _chat_get_messages,
        get_latest_session as _chat_get_latest_session,
        rename_session as _chat_rename_session,
        delete_session as _chat_delete_session,
    )
    from services.pi_session import PiSessionManager  # noqa: E402
    _CHAT_AVAILABLE = True
except ImportError as _chat_import_err:
    logger_pre = logging.getLogger("chat_server")
    logger_pre.warning("Chat modules not available: %s", _chat_import_err)
    _CHAT_AVAILABLE = False

logger = logging.getLogger("chat_server")

# ── Risk / signals / regime imports (phases 7-9) ─────────────────────────────
from risk.ruin_probability import get_latest_ruin_probability, compute_for_current_portfolio, persist_ruin_probability  # noqa: E402
from signals.ev_scorer import get_latest_ev_stats, compute_all_strategies_ev, persist_strategy_ev  # noqa: E402

# ── Rate limiting (mirrors dashboard_server.py global) ───────────────────────
_last_evaluate_time = 0.0


# ── Credential management ─────────────────────────────────────────────────────

def _load_credentials() -> tuple[str, str]:
    """Load dashboard credentials from ~/.atlas-secrets.json."""
    if not SECRETS_PATH.exists():
        raise ValueError(f"Secrets file not found: {SECRETS_PATH}")
    with open(SECRETS_PATH) as f:
        s = json.load(f)
    user = s.get("dashboard_user", "")
    pw = s.get("dashboard_pass", "")
    if not user or not pw:
        raise ValueError(
            "Set dashboard_user and dashboard_pass in ~/.atlas-secrets.json"
        )
    return user, pw


# Module-level credential cache (loaded once on first request)
_CREDENTIALS: tuple[str, str] | None = None


def _get_credentials() -> tuple[str, str]:
    global _CREDENTIALS
    if _CREDENTIALS is None:
        _CREDENTIALS = _load_credentials()
    return _CREDENTIALS


# ── HTTP Basic Auth dependency ────────────────────────────────────────────────

security = HTTPBasic(realm="Atlas Dashboard")


def check_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> HTTPBasicCredentials:
    """FastAPI dependency: HTTP Basic Auth via ~/.atlas-secrets.json.

    Uses secrets.compare_digest for timing-safe comparison.
    Raises 401 with WWW-Authenticate: Basic realm="Atlas Dashboard" on failure.
    """
    expected_user, expected_pass = _get_credentials()
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_pass.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Atlas Dashboard"'},
        )
    return credentials


# ── Plan approval / execution business logic (ported verbatim) ───────────────

def _approve_and_execute(trade_date: str, market_id: str) -> dict:
    """Approve a plan and execute it via live broker. Returns result dict."""
    from utils.config import get_active_config
    from brokers.live_portfolio import LivePortfolio
    from brokers.plan import TradePlanGenerator

    config = get_active_config(market_id)
    portfolio = LivePortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)

    if not plan:
        return {"ok": False, "error": f"No plan found for {trade_date}"}

    if plan.get("status") == "EXECUTED":
        return {"ok": False, "error": "Plan already executed"}

    if plan.get("status") == "APPROVED":
        return {"ok": False, "error": "Plan already approved (awaiting execution)"}

    plan = plan_gen.approve_plan(trade_date)
    if not plan or plan.get("status") != "APPROVED":
        return {"ok": False, "error": "Failed to approve plan"}

    return _execute_live(plan, trade_date, config, market_id)


def _execute_live(plan, trade_date, config, market_id) -> dict:
    """Execute plan via live broker."""
    from brokers.live_executor import LiveExecutor
    from brokers.live_portfolio import LivePortfolio

    executor = LiveExecutor(config)
    if not executor.connect():
        return {"ok": False, "error": "Failed to connect to broker"}

    try:
        report = executor.execute_plan(plan, trade_date)

        entries_ok = sum(1 for e in report.get("entries", []) if e.get("success"))
        exits_ok = sum(1 for e in report.get("exits", []) if e.get("success"))
        total_entries = len(report.get("entries", []))
        total_exits = len(report.get("exits", []))

        if report.get("error"):
            return {"ok": False, "error": report["error"]}

        plan["status"] = "EXECUTED"
        plan["executed_at"] = datetime.now().isoformat()
        from brokers.plan import TradePlanGenerator
        tpg = TradePlanGenerator(LivePortfolio(config, market_id=market_id), config)
        tpg._save_plan(plan, trade_date)

        return {
            "ok": True,
            "mode": "live",
            "market_id": market_id,
            "entries": f"{entries_ok}/{total_entries}",
            "exits": f"{exits_ok}/{total_exits}",
            "report": report,
        }
    finally:
        executor.disconnect()


def _reject_plan(trade_date: str, market_id: str) -> dict:
    """Reject a plan (mark REJECTED, do not execute)."""
    from utils.config import get_active_config
    from brokers.live_portfolio import LivePortfolio
    from brokers.plan import TradePlanGenerator

    config = get_active_config(market_id)
    portfolio = LivePortfolio(config, market_id=market_id)
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)

    if not plan:
        return {"ok": False, "error": f"No plan found for {trade_date}"}

    plan["status"] = "REJECTED"
    plan["rejected_at"] = datetime.now().isoformat()
    plan_gen._save_plan(plan, trade_date)

    return {"ok": True, "status": "REJECTED"}



def _calc_alpaca_intraday_pnl(positions: list) -> dict:
    """Sum intraday PnL from positions already enriched with Alpaca data.

    This is the primary PnL source when Alpaca intraday enrichment succeeded.
    Returns a dict compatible with _calc_tiingo_daily_pnl for easy substitution.
    """
    per: dict = {}
    total = 0.0
    for p in positions:
        tk = p.get("ticker", "")
        if not tk:
            continue
        ipnl = float(p.get("intraday_pnl", 0) or 0)
        per[tk] = {
            "intraday_pnl": ipnl,
            "intraday_pnl_pct": p.get("intraday_pnl_pct", 0),
            "lastday_price": p.get("lastday_price", 0),
            "current_price": p.get("current_price", 0),
            # Compat fields so callers that use today_close/yesterday_close still work
            "today_close": float(p.get("current_price", 0) or 0),
            "yesterday_close": float(p.get("lastday_price", 0) or 0),
            "daily_pnl": ipnl,
            "shares": float(p.get("qty", p.get("shares", 0)) or 0),
        }
        total += ipnl
    return {"per_position": per, "total_pnl": round(total, 2)}


def _calc_tiingo_daily_pnl(positions: list, market_id: str = "sp500") -> dict:
    """Calculate per-position daily PnL from Tiingo cached parquet data.

    Returns dict with:
      - per_position: {ticker: {yesterday_close, today_close, shares, daily_pnl}}
      - total_pnl: float
    """
    import pandas as pd
    from pathlib import Path

    cache_dir = Path(__file__).parent.parent / "data" / "cache" / market_id
    result = {"per_position": {}, "total_pnl": 0.0}

    for p in positions:
        ticker = p.get("ticker", "")
        shares = float(p.get("qty", p.get("shares", 0)) or 0)
        if not ticker or shares == 0:
            continue

        parquet_path = cache_dir / f"{ticker}.parquet"
        if not parquet_path.exists():
            continue

        try:
            df = pd.read_parquet(parquet_path)
            if len(df) < 2:
                continue
            today_close = float(df["close"].iloc[-1])
            yesterday_close = float(df["close"].iloc[-2])
            daily_pnl = round(shares * (today_close - yesterday_close), 2)

            result["per_position"][ticker] = {
                "yesterday_close": yesterday_close,
                "today_close": today_close,
                "shares": shares,
                "daily_pnl": daily_pnl,
            }
            result["total_pnl"] += daily_pnl
        except Exception as e:
            logger.debug("per-position PnL calc failed for %s: %s", ticker, e)
            continue

    result["total_pnl"] = round(result["total_pnl"], 2)
    return result


# ── Dashboard data builder (ported verbatim from dashboard_server.py) ─────────

def _build_dashboard_data() -> dict:
    """Build the complete dashboard data payload from SQLite + live broker.

    Exact port of AuthHandler._build_dashboard_data() (lines 543-744 of
    dashboard_server.py).  Returns a dict that is serialised with
    json.dumps(..., default=str) to handle enum/datetime values.
    """
    import dataclasses
    from db.atlas_db import get_db

    config_path = Path("config/active/sp500.json")
    with open(config_path) as f:
        config = json.load(f)
    # Support both 'market_id' and 'market' config keys
    market_id = config.get("market_id") or config.get("market", "sp500")

    result: dict = {}

    # ── 1. Portfolio summary from live broker ─────────────────────────────────
    positions: list = []
    try:
        from brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if broker and broker.connect():
            account_info = broker.get_account_info()
            positions_info = broker.get_positions()
            orders_info = broker.get_history_orders(days=7)

            account = dataclasses.asdict(account_info)

            # 1a. Margin usage from raw Alpaca account
            try:
                raw_acct = broker._broker_call(broker._trade_client.get_account)
                initial_margin = float(getattr(raw_acct, "initial_margin", 0) or 0)
                equity_val = float(getattr(raw_acct, "equity", 0) or 0)
                account["margin_usage_pct"] = (
                    round(initial_margin / equity_val * 100, 2) if equity_val > 0 else 0
                )
            except Exception as e:
                logger.debug("Margin usage calculation failed: %s", e)
                account["margin_usage_pct"] = 0

            positions = [dataclasses.asdict(p) for p in positions_info]
            # Override stale dataclass default — AccountInfo.num_positions is
            # never set by the Alpaca adapter, so use the actual position count.
            account["num_positions"] = len(positions)

            # 1c. Flatten orders from raw dict for dashboard compatibility
            orders = []
            for o in orders_info:
                od = dataclasses.asdict(o)
                raw = od.pop("raw", {})
                od["symbol"] = raw.get("symbol", od.get("ticker", ""))
                od["type"] = raw.get("order_type", "limit")
                od["qty"] = od.get("requested_qty", raw.get("qty", 0))
                od["submitted_at"] = raw.get("submitted_at", "")
                od["limit_price"] = float(raw.get("limit_price", 0) or 0)
                od["stop_price"] = float(raw.get("stop_price", 0) or 0)
                od["trail_price"] = float(raw.get("trail_price", 0) or 0)
                od["filled_price"] = od.get("fill_price", 0)
                od["side"] = raw.get("side", str(od.get("side", "")))
                od["status"] = raw.get("status", str(od.get("status", "")))
                orders.append(od)

            # 1. Enrich positions with Atlas trade metadata from SQLite
            #    Prefer open trades; fall back to most-recent closed trade
            #    so broker-only / orphaned positions still get strategy info.
            with get_db() as db:
                all_trades = db.execute(
                    "SELECT ticker, strategy, entry_date, stop_price, entry_price,"
                    "       (CASE WHEN exit_date IS NULL THEN 0 ELSE 1 END) AS is_closed"
                    " FROM trades"
                    " ORDER BY is_closed, id DESC"
                ).fetchall()
            # Strategies that are placeholder / uninformative — always prefer a real one
            _POISON: set = {"reconciled", "unknown", "", None}

            trade_meta: dict = {}
            for t in all_trades:
                tk = t["ticker"]
                td = dict(t)
                if tk not in trade_meta:
                    trade_meta[tk] = td
                elif (
                    trade_meta[tk].get("strategy") in _POISON
                    and td.get("strategy") not in _POISON
                ):
                    # Prefer any real strategy over a placeholder entry,
                    # regardless of which appeared first in the ORDER BY.
                    trade_meta[tk] = td
            for p in positions:
                meta = trade_meta.get(p.get("ticker", ""))
                if meta:
                    if not p.get("strategy"):
                        p["strategy"] = meta.get("strategy", "")
                    if not p.get("entry_date"):
                        p["entry_date"] = meta.get("entry_date", "")
                    if not p.get("stop_price") and meta.get("stop_price"):
                        p["stop_price"] = meta["stop_price"]

            # 1b. Enrich with Alpaca intraday fields
            try:
                raw_positions = broker._broker_call(
                    broker._trade_client.get_all_positions
                )
                alpaca_by_symbol: dict = {}
                for rp in raw_positions or []:
                    sym = str(getattr(rp, "symbol", ""))
                    alpaca_by_symbol[sym] = rp

                from brokers.alpaca import mapper
                for p in positions:
                    atlas_ticker = p.get("ticker", "")
                    alpaca_sym = mapper.to_alpaca(atlas_ticker)
                    rp = alpaca_by_symbol.get(alpaca_sym)
                    if rp:
                        p["intraday_pnl"] = round(
                            float(getattr(rp, "unrealized_intraday_pl", 0) or 0), 2
                        )
                        p["intraday_pnl_pct"] = round(
                            float(getattr(rp, "unrealized_intraday_plpc", 0) or 0) * 100,
                            4,
                        )
                        p["lastday_price"] = round(
                            float(getattr(rp, "lastday_price", 0) or 0), 4
                        )
            except Exception as e:
                logger.warning("Intraday enrichment failed: %s", e)

            # 1c. Override stop_price with broker's authoritative open-order value
            try:
                open_orders = broker.get_open_orders()
                # Build map: atlas_ticker → list of stop prices from SELL stop/trailing_stop orders
                _stop_map: dict[str, list[float]] = {}
                for od in open_orders:
                    od_d = od.asdict() if hasattr(od, "asdict") else vars(od)
                    # Flatten: use raw dict if present
                    raw = dict(od_d.pop("raw", None) or {})
                    od_d.update(raw)
                    _side = str(od_d.get("side", "")).lower()
                    _otype = str(od_d.get("order_type", od_d.get("type", ""))).lower()
                    _sym = od_d.get("symbol", od_d.get("ticker", ""))
                    _sp = od_d.get("stop_price") or od_d.get("stop_loss")
                    if _side == "sell" and _otype in ("stop", "trailing_stop") and _sp:
                        try:
                            _stop_map.setdefault(_sym, []).append(float(_sp))
                        except (TypeError, ValueError):
                            pass
                for p in positions:
                    tk = p.get("ticker", "")
                    if tk in _stop_map:
                        # Most protective stop = highest stop_price for a long
                        broker_stop = max(_stop_map[tk])
                        p["stop_price"] = broker_stop
                        p["stop_source"] = "broker"
                    else:
                        p.setdefault("stop_source", "ledger")
            except Exception as _stop_err:
                logger.warning("Broker stop_price override failed: %s", _stop_err)

            result["account"] = account
            result["positions"] = positions
            result["recent_orders"] = orders
            result["summary"] = {
                "equity": account.get("equity", 0),
                "total_pnl": account.get("total_pnl", 0),
                "total_pnl_pct": account.get("total_pnl_pct", 0),
                "open_positions": len(positions),
            }
    except Exception as e:
        logger.warning("Alpaca account data fetch failed: %s", e)
        result["account"] = {}
        result["positions"] = []
        result["recent_orders"] = []
        result["summary"] = {}

    # ── 2. Market clock ───────────────────────────────────────────────────────
    try:
        from brokers.alpaca.broker import AlpacaBroker
        ab = AlpacaBroker(config)
        if ab.connect():
            clock = ab._trade_client.get_clock()
            result["market_clock"] = {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
                "timestamp": str(clock.timestamp),
            }
    except Exception as e:
        logger.warning("Market clock fetch failed: %s", e)
        result["market_clock"] = {"is_open": False}

    # ── Equity curve + strategy performance from SQLite ───────────────────────
    with get_db() as db:
        equity_rows = db.execute(
            "SELECT date, equity, day_pnl FROM equity_curve "
            "WHERE market_id = ? ORDER BY date",
            (market_id,),
        ).fetchall()
        portfolio_history = [{**dict(r), "value": r["equity"]} for r in equity_rows]

        # Fix 1: Update today's row with live broker equity (authoritative)
        # account.equity already reflects all positions including unrealised P&L.
        live_equity = round(float((result.get("account") or {}).get("equity", 0) or 0), 2)
        if portfolio_history and live_equity:
            from datetime import datetime as _dt
            today_str = _dt.now().strftime("%Y-%m-%d")
            last_row = portfolio_history[-1]
            if last_row.get("date") == today_str:
                # Today's row exists — update it with live equity
                if abs((last_row.get("equity") or 0) - live_equity) > 0.01:
                    last_row["equity"] = round(live_equity, 2)
                    last_row["value"] = round(live_equity, 2)
                last_row["day_pnl"] = (result.get("summary") or {}).get("today_pnl", 0)
            else:
                # Check if market is open — only append a new date row
                # on trading days to avoid weekend/holiday jumps
                market_clock = result.get("market_clock") or {}
                is_trading_day = market_clock.get("is_open", False)
                # Also check weekday as fallback
                if not is_trading_day:
                    is_trading_day = _dt.now().weekday() < 5
                if is_trading_day:
                    _eq_val = round(live_equity, 2)
                    portfolio_history.append({
                        "date": today_str,
                        "equity": _eq_val,
                        "value": _eq_val,
                        "day_pnl": (result.get("summary") or {}).get("today_pnl", 0),
                    })
                else:
                    # Weekend/holiday: update the last row to reflect
                    # current (most accurate) equity
                    last_row["equity"] = round(live_equity, 2)
                    last_row["value"] = round(live_equity, 2)
        result["portfolio_history"] = portfolio_history

        # Strategy performance aggregated from closed trades (exclude phantoms/errors)
        trades_rows = db.execute(
            "SELECT strategy, pnl, pnl_pct FROM trades"
            " WHERE exit_date IS NOT NULL"
            "   AND (status IS NULL OR status != 'error')"
            "   AND (superseded=0 OR superseded IS NULL)"  -- exclude dup rows
            "   AND (exit_reason IS NULL"
            "        OR exit_reason NOT IN ('reconcile_phantom', 'reconcile_fill'))"
        ).fetchall()
        by_strategy: dict = {}
        for t in trades_rows:
            s = t["strategy"] or "unknown"
            if s not in by_strategy:
                by_strategy[s] = {"trades": 0, "pnl": 0.0, "wins": 0}
            by_strategy[s]["trades"] += 1
            by_strategy[s]["pnl"] += t["pnl"] or 0
            if (t["pnl"] or 0) > 0:
                by_strategy[s]["wins"] += 1
        result["strategy_performance"] = {"by_strategy": by_strategy}

        # ── 6. Overall performance metrics ────────────────────────────────────
        closed_rows = db.execute(
            "SELECT pnl FROM trades"
            " WHERE exit_date IS NOT NULL"
            "   AND (status IS NULL OR status != 'error')"
            "   AND (superseded=0 OR superseded IS NULL)"  -- exclude dup rows
            "   AND (exit_reason IS NULL"
            "        OR exit_reason NOT IN ('reconcile_phantom', 'reconcile_fill'))"
        ).fetchall()
        if closed_rows:
            pnls = [c["pnl"] for c in closed_rows if c["pnl"] is not None]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            loss_sum = sum(losses)
            pf = abs(sum(wins) / loss_sum) if loss_sum != 0 else 99.99
            result["strategy_performance"]["overall"] = {
                "trades": len(pnls),
                "win_rate": len(wins) / len(pnls) if pnls else 0,
                "avg_win": sum(wins) / len(wins) if wins else 0,
                "avg_loss": sum(losses) / len(losses) if losses else 0,
                "profit_factor": min(pf, 99.99),  # cap to avoid "Infinity" display
                "expectancy": sum(pnls) / len(pnls) if pnls else 0,
            }

        # ── 3. Benchmark (SPY) curve — aligned to portfolio window ────────────
        if portfolio_history:
            port_start_date = portfolio_history[0]["date"]
            port_start_equity = portfolio_history[0]["equity"] or 0
            port_end_date = portfolio_history[-1]["date"]
            spy_rows = db.execute(
                "SELECT date, close FROM ohlcv WHERE ticker = 'SPY' "
                "AND date >= ? AND date <= ? ORDER BY date",
                (port_start_date, port_end_date),
            ).fetchall()
            spy_by_date = {r["date"]: r["close"] for r in spy_rows}

            # Build full trading-day calendar = union of portfolio + SPY dates,
            # then forward-fill any gaps in portfolio equity.
            all_trading_days = sorted(
                set(p["date"] for p in portfolio_history) | set(spy_by_date.keys())
            )
            port_by_date = {p["date"]: p for p in portfolio_history}
            _last_eq: float | None = None
            filled_portfolio: list = []
            for _d in all_trading_days:
                if _d in port_by_date:
                    _row = port_by_date[_d]
                    _last_eq = _row["equity"]
                    filled_portfolio.append({
                        "date": _d,
                        "equity": _last_eq,
                        "value": _last_eq,
                        "day_pnl": _row.get("day_pnl", 0) or 0,
                    })
                elif _last_eq is not None:
                    # Forward-fill equity from previous trading day
                    filled_portfolio.append({
                        "date": _d,
                        "equity": _last_eq,
                        "value": _last_eq,
                        "day_pnl": 0.0,
                    })
            # Overwrite portfolio_history with the date-complete version
            portfolio_history = filled_portfolio
            result["portfolio_history"] = portfolio_history

            # Left-join SPY onto every portfolio date, forward-filling gaps
            if spy_rows and port_start_equity > 0:
                spy_start = spy_rows[0]["close"]
                scale = port_start_equity / spy_start if spy_start else 1
                _last_spy: float | None = None
                bench_curve = []
                for _row in portfolio_history:
                    _d = _row["date"]
                    if _d in spy_by_date:
                        _last_spy = spy_by_date[_d]
                    if _last_spy is not None:
                        _eq = round(_last_spy * scale, 2)
                        bench_curve.append({"date": _d, "equity": _eq, "value": _eq})
                spy_return = (
                    (spy_rows[-1]["close"] / spy_rows[0]["close"]) - 1
                ) * 100
                result["benchmark"] = {
                    "ticker": "SPY",
                    "curve": bench_curve,
                    "return_pct": round(spy_return, 2),
                }

    # ── 4. Strategy allocation breakdown ──────────────────────────────────────
    alloc_map: dict = {}
    total_mv = 0.0
    for p in positions:
        s = p.get("strategy") or "manual"
        if s not in alloc_map:
            alloc_map[s] = {"value": 0.0, "positions": 0}
        mv = p.get("market_value") or 0
        alloc_map[s]["value"] += mv
        alloc_map[s]["positions"] += 1
        total_mv += mv
    result["strategy_allocation"] = [
        {
            "strategy": s,
            "value": round(v["value"], 2),
            "pct": round(v["value"] / total_mv * 100, 1) if total_mv > 0 else 0,
            "positions": v["positions"],
        }
        for s, v in sorted(alloc_map.items(), key=lambda x: -x[1]["value"])
    ]

    # ── 5. Enrich summary with today_pnl + max_positions ─────────────────────
    if "summary" not in result:
        result["summary"] = {}

    # Use Alpaca intraday data if already enriched; otherwise fall back to Tiingo parquet
    if any(p.get("intraday_pnl") is not None for p in positions):
        daily_pnl = _calc_alpaca_intraday_pnl(positions)
    else:
        daily_pnl = _calc_tiingo_daily_pnl(positions, market_id=market_id)
    result["summary"]["today_pnl"] = daily_pnl["total_pnl"]
    result["summary"]["today_pnl_detail"] = daily_pnl["per_position"]

    # Also ensure each position has intraday_pnl (from whichever source won)
    for p in positions:
        ticker = p.get("ticker", "")
        if ticker in daily_pnl["per_position"]:
            tp = daily_pnl["per_position"][ticker]
            # Only overwrite if not already set by Alpaca enrichment
            if p.get("intraday_pnl") is None:
                p["intraday_pnl"] = tp["daily_pnl"]
                p["intraday_pnl_pct"] = round(
                    (tp["today_close"] - tp["yesterday_close"]) / tp["yesterday_close"] * 100, 4
                ) if tp.get("yesterday_close", 0) != 0 else 0.0
            if tp.get("today_close"):
                p["current_price_tiingo"] = tp["today_close"]
    # Backfill today's day_pnl in portfolio_history (section 2 ran before
    # Tiingo PnL was computed, so it was 0 — fix it now).
    _ph_list = result.get("portfolio_history", [])
    if _ph_list:
        from datetime import datetime as _dt2
        _today = _dt2.now().strftime("%Y-%m-%d")
        # Update today's row AND any appended row with correct day_pnl
        for _row in reversed(_ph_list):
            if _row.get("date") == _today:
                _row["day_pnl"] = daily_pnl["total_pnl"]
            else:
                break  # stop once we pass today's row(s)

    result["summary"]["max_positions"] = config.get("risk", {}).get(
        "max_open_positions", 10
    )

    # ── Add portfolio return_pct to summary ─────────────────────────────────
    _ph = result.get("portfolio_history", [])
    if _ph and len(_ph) >= 2:
        _first_eq = _ph[0].get("equity") or 0
        _last_eq_s = _ph[-1].get("equity") or 0
        if _first_eq > 0:
            result.setdefault("summary", {})["return_pct"] = round(
                (_last_eq_s / _first_eq - 1) * 100, 2
            )

    result["timestamp"] = datetime.now().isoformat()
    return result


# ── Pydantic request models ───────────────────────────────────────────────────

class PlanRequest(BaseModel):
    trade_date: str
    market_id: str


# ── FastAPI app + lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise chat DB and attempt to start Alpaca background poller."""
    # ── Phase 2: initialise chat persistence ────────────────────────────
    if _CHAT_AVAILABLE:
        try:
            init_chat_db()
            print("Chat DB initialised", flush=True)
        except Exception as e:
            print(f"⚠️  Chat DB init failed: {e}", flush=True)

    # P4.1: Create targets.json stub to suppress up_sync.py WARN on every /api/finance call.
    # up_sync.build_finance_payload() loads this file but degrades gracefully when missing;
    # a stub {} eliminates the WARNING noise without breaking any functionality.
    _targets_path = PROJECT_ROOT / "dashboard" / "cache" / "targets.json"
    if not _targets_path.exists():
        try:
            _targets_path.parent.mkdir(parents=True, exist_ok=True)
            _targets_path.write_text("{}")
            logger.debug("Created stub targets.json at %s", _targets_path)
        except OSError as _te:
            logger.debug("Could not create targets.json stub: %s", _te)

    # alpaca_stream removed — SSE streaming retired in Phase 5
    yield  # app runs here


app = FastAPI(
    title="Atlas Dashboard",
    description="Atlas trading system dashboard — FastAPI port of dashboard_server.py",
    version="2.0.0",
    lifespan=lifespan,
    # Disable interactive docs to match original server (no Swagger/ReDoc UI)
    docs_url=None,
    redoc_url=None,
)


# ── Security middleware ──────────────────────────────────────────────────────

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests with Content-Length exceeding 1 MB (Finding F-01)."""

    MAX_BODY = 1_048_576  # 1 MB

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.MAX_BODY:
            return JSONResponse(
                status_code=413,
                content={"error": "Request too large"},
            )
        return await call_next(request)


app.add_middleware(MaxBodySizeMiddleware)


class CSPMiddleware(BaseHTTPMiddleware):
    """Add Content-Security-Policy header to every response (P4.4)."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = self._CSP
        return response


app.add_middleware(CSPMiddleware)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to every response (Finding F-04)."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ═══════════════════════════════════════════════════════════════════════════════
# GET routes  (defined in the same priority order as dashboard_server.py do_GET)
# ═══════════════════════════════════════════════════════════════════════════════

# ── GET /api/monitor* — 410 Gone (monitor tab removed) ───────────────────────

@app.get("/api/monitor")
@app.get("/api/monitor/{monitor_path:path}")
def monitor_get_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/monitor — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ── GET /api/portfolio  +  /api/db/portfolio ──────────────────────────────────
# TODO: unused — not called by dashboard UI (data via /api/dashboard-data)

@app.get("/api/portfolio")
@app.get("/api/db/portfolio")
def db_portfolio(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/portfolio — open positions + latest equity from SQLite."""
    try:
        from db import atlas_db
        positions = atlas_db.get_open_positions()
        regime = atlas_db.get_current_regime()
        with atlas_db.get_db() as db:
            row = db.execute(
                "SELECT * FROM equity_curve ORDER BY date DESC LIMIT 1"
            ).fetchone()
            equity = dict(row) if row else None
        return JSONResponse({"positions": positions, "regime": regime, "equity": equity})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/trades  +  /api/db/trades ───────────────────────────────────────
# TODO: unused — not called by dashboard UI

@app.get("/api/trades")
@app.get("/api/db/trades")
def db_trades(
    days: int = 0,
    strategy: str | None = None,
    universe: str | None = None,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/trades?days=30&strategy=mean_reversion&universe=sp500"""
    try:
        from db import atlas_db
        days_or_none = days if days > 0 else None
        trades = atlas_db.get_closed_trades(
            days=days_or_none, strategy=strategy, universe=universe
        )
        return JSONResponse({"trades": trades, "count": len(trades)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/performance  +  /api/db/performance ─────────────────────────────
# TODO: unused — not called by dashboard UI

@app.get("/api/performance")
@app.get("/api/db/performance")
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

@app.get("/api/equity-curve")
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


# ── GET /api/regime/history ───────────────────────────────────────────────────

@app.get("/api/regime/history")
def regime_history(
    days: int = 90,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/regime/history?days=90 — regime classification history."""
    try:
        from db.atlas_db import get_regime_history
        rows = get_regime_history(days=days)
        # get_regime_history already returns most-recent-first
        # Normalise: rename regime_state → state for consistent API field naming
        normalised = [
            {**r, "state": r["regime_state"]} if "regime_state" in r and "state" not in r else r
            for r in rows
        ]
        return JSONResponse(normalised)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/regime/current ───────────────────────────────────────────────────

@app.get("/api/regime/current")
def regime_current(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/regime/current — most recent regime state."""
    try:
        from db.atlas_db import get_current_regime
        regime = get_current_regime()
        if regime:
            # Normalise: rename regime_state → state so the API field is consistent
            if "regime_state" in regime and "state" not in regime:
                regime["state"] = regime.pop("regime_state")
            return JSONResponse(regime)
        return JSONResponse({"state": "unknown"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/overlay/decisions ────────────────────────────────────────────────
# TODO: unused — not called by dashboard UI (OverlayDecisions component not rendered)

@app.get("/api/overlay/decisions")
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


# ── GET /api/system/health ────────────────────────────────────────────────────

@app.get("/api/system/health")
def system_health(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/system/health — comprehensive system health."""
    import subprocess
    try:
        from db.atlas_db import get_heartbeats, get_db

        heartbeats = get_heartbeats()

        # Service status via systemd
        services = {}
        for svc in ("atlas-dashboard", "atlas-telegram-bot"):
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5,
                )
                services[svc] = result.stdout.strip()
            except Exception as e:
                logger.debug("systemctl is-active %s failed: %s", svc, e)
                services[svc] = "unknown"

        # Data freshness from SQLite
        data_freshness = {}
        try:
            with get_db() as db:
                # MAX across all tickers = most recent data available (freshness indicator)
                row = db.execute("SELECT MAX(date) as last_date FROM ohlcv").fetchone()
                data_freshness["ohlcv_last_date"] = row["last_date"] if row else None
                # Per-ticker breakdown — 10 stalest tickers (most useful for diagnostics)
                ticker_rows = db.execute(
                    "SELECT ticker, MAX(date) as last_date"
                    " FROM ohlcv"
                    " GROUP BY ticker"
                    " ORDER BY last_date ASC"
                    " LIMIT 10"
                ).fetchall()
                data_freshness["ohlcv_per_ticker"] = [
                    {"ticker": r["ticker"], "last_date": r["last_date"]}
                    for r in ticker_rows
                ]
                row = db.execute("SELECT MAX(date) as last_date FROM equity_curve").fetchone()
                data_freshness["equity_last_date"] = row["last_date"] if row else None
                row = db.execute("SELECT COUNT(*) as cnt FROM overlay_decisions").fetchone()
                data_freshness["overlay_decisions_count"] = row["cnt"] if row else 0
        except Exception as exc:
            data_freshness["error"] = str(exc)

        # Cron heartbeats — extract key services
        cron_services = {}
        for hb in heartbeats:
            name = hb.get("service", "")
            if name in ("premarket", "postclose", "sync_protective"):
                cron_services[name] = {
                    "last_run": hb.get("timestamp"),
                    "status": hb.get("status"),
                }

        # P4.2 — universe health surface
        universes_data = []
        try:
            import json as _uj
            from db.atlas_db import get_db as _ugh_db, get_latest_equity as _ugh_eq
            for _cfg_path in sorted(Path("config/active").glob("*.json")):
                if _cfg_path.stem == "regime":
                    continue
                try:
                    _cfg = _uj.loads(_cfg_path.read_text())
                    _mid = _cfg.get("market", _cfg_path.stem)
                    _mode = _cfg.get("trading", {}).get("mode", "unknown")
                    _approval = bool(_cfg.get("trading", {}).get("live_enabled", False))
                    _starting_eq = _cfg.get("risk", {}).get("starting_equity")
                    with _ugh_db() as _db:
                        _op = _db.execute(
                            "SELECT COUNT(*) AS n FROM trades "
                            "WHERE exit_date IS NULL AND (universe=? OR universe IS NULL)",
                            (_mid,),
                        ).fetchone()
                    _open_pos = _op["n"] if _op else 0
                    _eq_row = _ugh_eq(market_id=_mid)
                    _eq_val = (_eq_row or {}).get("equity")
                    universes_data.append({
                        "market_id": _mid,
                        "mode": _mode,
                        "approval": _approval,
                        "open_positions": _open_pos,
                        "equity": _eq_val,
                        "starting_equity": _starting_eq,
                    })
                except Exception as _ue:
                    logger.debug("universes: error reading %s: %s", _cfg_path.name, _ue)
        except Exception as _uge:
            logger.warning("universes health failed: %s", _uge)

        return JSONResponse({
            "services": services,
            "cron": cron_services,
            "data_freshness": data_freshness,
            "heartbeats": heartbeats,
            "timestamp": datetime.now().isoformat(),
            "universes": universes_data,
        })
    except Exception as e:
        logger.exception("system_health failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/macro/gauges ────────────────────────────────────────────────────

@app.get("/api/macro/gauges")
def macro_gauges(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/macro/gauges — macro indicator gauges with scores and sparklines."""
    try:
        import json as _json
        from db.atlas_db import get_db
        from regime.indicators import compute_all_scores

        config_path = Path("config/active/regime.json")
        with open(config_path) as f:
            regime_config = _json.load(f)

        with get_db() as db:
            # Latest macro row
            latest = db.execute(
                "SELECT * FROM macro_indicators ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if not latest:
                return JSONResponse({"dimensions": [], "date": None})

            latest_dict = dict(latest)
            scores = compute_all_scores(latest_dict, regime_config)

            # 90-day history for sparklines
            history = db.execute(
                "SELECT date, vix, credit_oas, yield_curve_10y2y, dxy, gold_copper_ratio, "
                "spy_above_200dma, spy_200dma_slope "
                "FROM macro_indicators ORDER BY date DESC LIMIT 90"
            ).fetchall()
            history = [dict(r) for r in reversed(history)]

        # Build dimension data
        dimensions = [
            {
                "name": "trend",
                "label": "Trend",
                "score": round(scores.get("trend", 0), 3),
                "raw_label": "SPY vs 200-DMA",
                "raw_value": "Above" if latest_dict.get("spy_above_200dma") else "Below",
                "raw_detail": f"Slope: {(latest_dict.get('spy_200dma_slope') or 0):.4f}",
                "sparkline": [h.get("spy_200dma_slope") for h in history if h.get("spy_200dma_slope") is not None],
                "weight": regime_config["weights"]["trend"],
            },
            {
                "name": "risk",
                "label": "Risk (VIX)",
                "score": round(scores.get("risk", 0), 3),
                "raw_label": "VIX",
                "raw_value": f"{latest_dict.get('vix', 0):.1f}",
                "raw_detail": f"Term ratio: {(latest_dict.get('vix_term_ratio') or 0):.3f}",
                "sparkline": [h.get("vix") for h in history if h.get("vix") is not None],
                "weight": regime_config["weights"]["risk"],
            },
            {
                "name": "credit",
                "label": "Credit",
                "score": round(scores.get("credit", 0), 3),
                "raw_label": "IG OAS",
                "raw_value": f"{latest_dict.get('credit_oas', 0):.2f}" if latest_dict.get("credit_oas") else "N/A",
                "raw_detail": "",
                "sparkline": [h.get("credit_oas") for h in history if h.get("credit_oas") is not None],
                "weight": regime_config["weights"]["credit"],
            },
            {
                "name": "yield_curve",
                "label": "Yield Curve",
                "score": round(scores.get("yield_curve", 0), 3),
                "raw_label": "10Y-2Y Spread",
                "raw_value": f"{latest_dict.get('yield_curve_10y2y', 0):.3f}" if latest_dict.get("yield_curve_10y2y") is not None else "N/A",
                "raw_detail": f"10Y-3M: {(latest_dict.get('yield_curve_10y3m') or 0):.3f}" if latest_dict.get("yield_curve_10y3m") is not None else "",
                "sparkline": [h.get("yield_curve_10y2y") for h in history if h.get("yield_curve_10y2y") is not None],
                "weight": regime_config["weights"]["yield_curve"],
            },
            {
                "name": "dollar",
                "label": "Dollar (DXY)",
                "score": round(scores.get("dollar", 0), 3),
                "raw_label": "DXY",
                "raw_value": f"{latest_dict.get('dxy', 0):.1f}" if latest_dict.get("dxy") else "N/A",
                "raw_detail": "",
                "sparkline": [h.get("dxy") for h in history if h.get("dxy") is not None],
                "weight": regime_config["weights"]["dollar"],
            },
            {
                "name": "commodity",
                "label": "Gold/Copper",
                "score": round(scores.get("commodity", 0), 3),
                "raw_label": "Gold/Copper Ratio",
                "raw_value": f"{latest_dict.get('gold_copper_ratio', 0):.1f}" if latest_dict.get("gold_copper_ratio") else "N/A",
                "raw_detail": "",
                "sparkline": [h.get("gold_copper_ratio") for h in history if h.get("gold_copper_ratio") is not None],
                "weight": regime_config["weights"]["commodity"],
            },
        ]

        return JSONResponse({
            "dimensions": dimensions,
            "composite": round(scores.get("composite", 0), 3),
            "available_weight": round(scores.get("available_weight", 0), 3),
            "date": latest_dict.get("date"),
        })
    except Exception as e:
        logger.exception("macro_gauges failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/positions/risk ───────────────────────────────────────────────────

@app.get("/api/positions/risk")
def positions_risk(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/positions/risk — position risk decomposition."""
    # === RISK CACHE (P2.7) — serve from cache, trigger bg refresh if stale ===
    try:
        from db.atlas_db import get_cached_portfolio_risk
        _cached_pr = get_cached_portfolio_risk(max_age_hours=24)
        if _cached_pr and not _cached_pr.get("stale"):
            return JSONResponse({
                "positions": [],
                "summary": {
                    "equity": _cached_pr.get("equity", 0),
                    "positions_count": _cached_pr.get("positions_count", 0),
                    "tickers": _cached_pr.get("tickers", []),
                },
                "portfolio_risk": {
                    "method": _cached_pr.get("method"),
                    "var_1d_95": _cached_pr.get("var_1d_95"),
                    "cvar_1d_95": _cached_pr.get("cvar_1d_95"),
                    "effective_bets": _cached_pr.get("effective_bets"),
                    "correlation_avg": _cached_pr.get("correlation_avg"),
                },
                "as_of": _cached_pr.get("as_of"),
                "stale": False,
                "source": "cache",
            })
        if _cached_pr:
            # Stale — kick off background refresh, return stale cache
            import subprocess as _sp
            try:
                _sp.Popen(
                    [sys.executable, "scripts/precompute_risk.py", "--target=risk"],
                    cwd=str(PROJECT_ROOT),
                    stdout=open("logs/risk_precompute.log", "a"),
                    stderr=subprocess.STDOUT,
                )
            except Exception as _pe:
                logger.warning("positions_risk: bg refresh failed to start: %s", _pe)
            return JSONResponse({
                "positions": [],
                "summary": {
                    "equity": _cached_pr.get("equity", 0),
                    "positions_count": _cached_pr.get("positions_count", 0),
                    "tickers": _cached_pr.get("tickers", []),
                },
                "portfolio_risk": {
                    "method": _cached_pr.get("method"),
                    "var_1d_95": _cached_pr.get("var_1d_95"),
                    "cvar_1d_95": _cached_pr.get("cvar_1d_95"),
                    "effective_bets": _cached_pr.get("effective_bets"),
                    "correlation_avg": _cached_pr.get("correlation_avg"),
                },
                "as_of": _cached_pr.get("as_of"),
                "stale": True,
                "source": "cache",
            })
    except Exception as _ce:
        logger.warning("positions_risk: cache lookup failed: %s", _ce)
    # === END RISK CACHE ===
    try:
        import json as _json
        from db.atlas_db import get_db

        config_path = Path("config/active/sp500.json")
        with open(config_path) as f:
            config = _json.load(f)

        max_risk_pct = config.get("risk", {}).get("max_risk_per_trade_pct", 2.0)

        # Get equity and current prices from broker (single connection)
        equity = 0.0
        current_prices = {}
        try:
            from brokers.registry import get_live_broker
            import dataclasses
            broker = get_live_broker(config)
            if broker and broker.connect():
                account_info = broker.get_account_info()
                equity = float(account_info.equity or 0)
                positions_info = broker.get_positions()
                for p in positions_info:
                    pd = dataclasses.asdict(p)
                    current_prices[pd.get("ticker", "")] = float(pd.get("current_price", 0) or 0)
        except Exception as e:
            logger.warning("positions_risk: broker fetch failed: %s", e)

        # Get open trades from SQLite
        with get_db() as db:
            trades = db.execute(
                "SELECT ticker, strategy, entry_price, stop_price, shares "
                "FROM trades WHERE exit_date IS NULL"
            ).fetchall()

        position_risks = []
        total_risk_dollars = 0.0
        stops_missing = 0

        for t in trades:
            td = dict(t)
            ticker = td["ticker"]
            entry = float(td["entry_price"] or 0)
            stop = float(td["stop_price"] or 0) if td["stop_price"] else None
            shares = int(td["shares"] or 0)
            current = current_prices.get(ticker, entry)
            strategy = td.get("strategy", "unknown")

            position_value = current * shares

            if stop and stop > 0:
                distance_pct = round(((current - stop) / current) * 100, 2) if current > 0 else 0
                distance_dollars = round((current - stop) * shares, 2)
                max_loss = round((entry - stop) * shares, 2) if entry > stop else 0
                risk_pct_equity = round((max_loss / equity) * 100, 2) if equity > 0 else 0
                has_stop = True
            else:
                distance_pct = None
                distance_dollars = None
                max_loss = position_value  # entire position at risk
                risk_pct_equity = round((position_value / equity) * 100, 2) if equity > 0 else 0
                has_stop = False
                stops_missing += 1

            total_risk_dollars += max_loss

            # Risk status: green/yellow/red
            if not has_stop:
                risk_status = "critical"
            elif risk_pct_equity > max_risk_pct:
                risk_status = "high"
            elif risk_pct_equity > max_risk_pct * 0.7:
                risk_status = "warning"
            else:
                risk_status = "normal"

            # Phase 3: volatility cone data
            vol_cone_data = None
            try:
                from indicators.vol_cones import compute_vol_cone, REGIME_MULTIPLIERS, _percentile_position
                vc = compute_vol_cone(ticker)
                if not vc.get("error") and 20 in vc.get("cone", {}):
                    c20 = vc["cone"][20]
                    regime = vc["current_regime"]
                    k = REGIME_MULTIPLIERS.get(regime, 2.0)
                    import math as _math
                    vol_daily = c20["current"] / _math.sqrt(252)
                    vol_cone_data = {
                        "vol_20d_annual": round(c20["current"], 4),
                        "regime": regime,
                        "percentile": _percentile_position(c20["current"], c20),
                        "multiplier": k,
                        "suggested_stop_distance_pct": round(k * vol_daily, 4),
                    }
            except Exception as vc_err:
                logger.warning("vol_cone lookup failed for %s: %s", ticker, vc_err)

            position_risks.append({
                "ticker": ticker,
                "strategy": strategy,
                "shares": shares,
                "entry_price": entry,
                "current_price": current,
                "stop_price": stop,
                "has_stop": has_stop,
                "distance_pct": distance_pct,
                "distance_dollars": distance_dollars,
                "max_loss": round(max_loss, 2),
                "risk_pct_equity": risk_pct_equity,
                "position_value": round(position_value, 2),
                "risk_status": risk_status,
                "vol_cone": vol_cone_data,
            })

        # Sort by risk (highest first)
        position_risks.sort(key=lambda x: x["max_loss"], reverse=True)

        # Portfolio summary
        num_positions = len(position_risks)
        avg_distance = None
        distances = [p["distance_pct"] for p in position_risks if p["distance_pct"] is not None]
        if distances:
            avg_distance = round(sum(distances) / len(distances), 2)

        # Phase 4: portfolio-level VaR/CVaR via regime-conditional MC
        portfolio_risk = None
        try:
            from risk.portfolio_var import compute_portfolio_var_regime_aware
            from db.atlas_db import get_current_regime

            # Get current regime state
            current_regime_data = get_current_regime() or {}
            current_regime = (
                current_regime_data.get("regime_state")
                or current_regime_data.get("state")
                or "transition_uncertain"
            )

            # Build positions list in expected shape
            var_positions = [
                {
                    "ticker": p["ticker"],
                    "shares": p["shares"],
                    "current_price": p["current_price"],
                    "entry_price": p["entry_price"],
                }
                for p in position_risks
            ]

            if var_positions and equity > 0:
                var_result = compute_portfolio_var_regime_aware(
                    positions=var_positions,
                    current_regime=current_regime,
                    lookback_days=60,
                    n_paths=10000,
                    horizons=(1, 5),
                    seed=42,
                    equity=equity,
                )
                portfolio_risk = {
                    "method": var_result.get("method"),
                    "current_regime": var_result.get("regime_state"),
                    "effective_bets": var_result.get("effective_bets"),
                    "correlation_avg": var_result.get("correlation_avg"),
                    "correlation_max": var_result.get("correlation_max"),
                    "horizons": var_result.get("horizons", {}),
                    "n_paths": var_result.get("n_paths"),
                    "warnings": var_result.get("warnings", []),
                }
        except Exception as pr_err:
            logger.warning("portfolio_risk computation failed: %s", pr_err)
            portfolio_risk = None

        # Build vol_cones map (ticker → vol cone data) from per-position data
        vol_cones_map = {
            p["ticker"]: p["vol_cone"]
            for p in position_risks
            if p.get("vol_cone")
        }

        # Stop probability analysis
        try:
            from risk.stop_probability import analyze_all_open_positions as _analyze_stops
            stop_results = _analyze_stops(horizons=(1, 5, 10, 20))
            stop_probability = {}
            for r in stop_results:
                stop_probability[r["ticker"]] = {
                    "vol_annual": r["vol_annual"],
                    "stop_distance_pct": r["stop_distance_pct"],
                    "horizons": {k: v["prob_touch"] for k, v in r["horizons"].items()},
                    "expected_loss_20d": r["loss"]["expected_loss"],
                    "max_loss": r["loss"]["max_loss"],
                }
        except Exception as e:
            logger.warning("stop_probability computation failed: %s", e)
            stop_probability = {}

        # Add ruin probability summary — uses get_latest_ruin_probability() helper (phase 9)
        ruin_summary = None
        try:
            ruin_summary = get_latest_ruin_probability() or None
            if not ruin_summary:
                # Compute fresh if no cached data in DB
                ruin_result = compute_for_current_portfolio(floor_pct=0.70)
                if ruin_result.get('status') == 'ok':
                    persist_ruin_probability(ruin_result)
                    ruin_summary = get_latest_ruin_probability() or None
        except Exception as e:
            logger.warning(f"Ruin probability failed: {e}")
            ruin_summary = None

        return JSONResponse({
            "positions": position_risks,
            "summary": {
                "total_risk_dollars": round(total_risk_dollars, 2),
                "total_risk_pct": round((total_risk_dollars / equity) * 100, 2) if equity > 0 else 0,
                "equity": round(equity, 2),
                "num_positions": num_positions,
                "avg_distance_to_stop": avg_distance,
                "positions_without_stops": stops_missing,
                "max_risk_per_trade_pct": max_risk_pct,
            },
            "portfolio_risk": portfolio_risk,
            "vol_cones": vol_cones_map,
            "stop_probability": stop_probability,
            "ruin_probability": ruin_summary,
        })
    except Exception as e:
        logger.exception("positions_risk failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/signals/ev ───────────────────────────────────────────────────────

@app.get("/api/signals/ev")
def signals_ev(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/signals/ev — strategy expected value scoring."""
    try:
        from db.atlas_db import get_db
        # Try cached DB row first (today's compute)
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM signal_ev WHERE as_of = (SELECT MAX(as_of) FROM signal_ev) ORDER BY ev_per_trade DESC"
            ).fetchall()
        if rows:
            return {"strategies": [dict(r) for r in rows], "source": "cached"}
        
        # Fallback: live compute
        from signals.ev_scorer import compute_all_strategies_ev, persist_strategy_ev
        results = compute_all_strategies_ev(min_trades=3)
        try:
            persist_strategy_ev(results)
        except Exception as e:
            logger.warning("persist_strategy_ev failed: %s", e)
        return {"strategies": results, "source": "live"}
    except Exception as e:
        logger.exception("signals_ev failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/regime/forecast ──────────────────────────────────────────────────

@app.get("/api/regime/forecast")
def regime_forecast(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/regime/forecast — regime forward Monte Carlo forecast."""
    try:
        import json as _json
        from db.atlas_db import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM regime_forecast WHERE as_of = (SELECT MAX(as_of) FROM regime_forecast) ORDER BY horizon_days"
            ).fetchall()
        if rows:
            horizons = {}
            current = None
            n_paths = None
            as_of = None
            for r in rows:
                rd = dict(r)
                current = rd["current_regime"]
                n_paths = rd["n_paths"]
                as_of = rd["as_of"]
                state_probs = {}
                try:
                    state_probs = _json.loads(rd.get("state_probabilities") or "{}")
                except Exception as e:
                    logger.debug("state_probs JSON parse failed: %s", e)
                horizons[f"{rd['horizon_days']}d"] = {
                    "days": rd["horizon_days"],
                    "expected_return": rd["expected_return"],
                    "median_return": rd["median_return"],
                    "std": rd["std"],
                    "var_5": rd["var_5"],
                    "var_1": rd["var_1"],
                    "cvar_5": rd["cvar_5"],
                    "cvar_1": rd["cvar_1"],
                    "p95": rd["p95"],
                    "p75": rd["p75"],
                    "p25": rd["p25"],
                    "prob_positive": rd["prob_positive"],
                    "state_probabilities": state_probs,
                }
            return {
                "current_regime": current,
                "n_paths": n_paths,
                "as_of": as_of,
                "horizons": horizons,
                "source": "cached",
            }
        
        # Fallback: live compute
        from regime.forward_mc import simulate_return_paths_from_regime, persist_forecast, get_current_regime
        cur = get_current_regime()
        result = simulate_return_paths_from_regime(cur, n_paths=5000, n_days=90, seed=42)
        try:
            persist_forecast(result)
        except Exception as e:
            logger.warning("persist_forecast failed: %s", e)
        result["source"] = "live"
        return result
    except Exception as e:
        logger.exception("regime_forecast failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/risk/ruin ────────────────────────────────────────────────────────

@app.get("/api/risk/ruin")
def risk_ruin(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/risk/ruin — portfolio probability of ruin.

    P2.8: response always includes ``stale`` (bool) and ``reason`` (str|None).
    - ``stale=False`` — cache is fresh and portfolio unchanged
    - ``stale=True, reason="portfolio_changed"`` — cached tickers differ from
      current open positions; frontend shows "PORTFOLIO CHANGED — recomputing"
    """
    try:
        # P2.8: use cache helper which handles portfolio-change detection
        from db.atlas_db import get_cached_ruin_probability
        cached = get_cached_ruin_probability(max_age_hours=24)
        if cached:
            # Surface canonical prob at top level for convenience
            cached.setdefault("status", "ok")
            cached.setdefault("prob", cached.get("horizons", {}).get("30d", {}).get("prob_ruin", 0.0))
            return cached

        # No cache or stale by age — run live compute
        import json as _json
        from db.atlas_db import get_db
        with get_db() as db:
            rows = db.execute(
                "SELECT * FROM ruin_probability WHERE as_of = (SELECT MAX(as_of) FROM ruin_probability) ORDER BY horizon_days"
            ).fetchall()
        if rows:
            horizons = {}
            current_equity = 0.0
            floor = 0.0
            floor_pct = 0.0
            n_paths = 0
            as_of = None
            tickers = []
            for r in rows:
                rd = dict(r)
                current_equity = rd["current_equity"]
                floor = rd["floor"]
                floor_pct = rd["floor_pct"]
                n_paths = rd["n_paths"]
                as_of = rd["as_of"]
                try:
                    tickers = _json.loads(rd.get("tickers") or "[]")
                except Exception as e:
                    logger.debug("tickers JSON parse failed: %s", e)
                horizons[f"{rd['horizon_days']}d"] = {
                    "days": rd["horizon_days"],
                    "prob_ruin": rd["prob_ruin"],
                    "worst_case_equity": rd["worst_case_equity"],
                    "worst_5pct_equity": rd["worst_5pct_equity"],
                    "median_end_equity": rd["median_end_equity"],
                }
            prob = horizons.get("30d", {}).get("prob_ruin", 0.0)
            return {
                "current_equity": current_equity,
                "floor": floor,
                "floor_pct": floor_pct,
                "n_paths": n_paths,
                "as_of": as_of,
                "prob": prob,
                "tickers": tickers,
                "horizons": horizons,
                "stale": False,
                "reason": None,
                "status": "ok",
                "source": "db",
            }

        # Fallback: live compute
        from risk.ruin_probability import compute_for_current_portfolio, persist_ruin_probability
        result = compute_for_current_portfolio(floor_pct=0.70)
        try:
            persist_ruin_probability(result)
        except Exception as e:
            logger.warning("persist_ruin_probability failed: %s", e)
        result["source"] = "live"
        result.setdefault("stale", False)
        result.setdefault("reason", None)
        result.setdefault("prob", result.get("horizons", {}).get("30d", {}).get("prob_ruin", 0.0))
        return result
    except Exception as e:
        logger.exception("risk_ruin failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/regime/distributions ─────────────────────────────────────────────

_regime_dist_cache: dict = {"as_of": None, "data": None}


@app.get("/api/regime/distributions")
def regime_distributions(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/regime/distributions — return distribution stats for all 6 regime states. Cached daily."""
    try:
        from datetime import date
        today = date.today().isoformat()
        if _regime_dist_cache["as_of"] == today and _regime_dist_cache["data"] is not None:
            return JSONResponse(_regime_dist_cache["data"])

        from regime.distributions import RegimeDistributions
        rd = RegimeDistributions()
        rd.fit(lookback_years=10)
        all_stats = rd.all_regime_stats()

        # Reshape to spec — rename n_samples → n
        distributions = {}
        for state, stats in all_stats.items():
            distributions[state] = {
                "n": stats.get("n_samples", 0),
                "mean": round(stats.get("mean", 0.0), 6),
                "vol": round(stats.get("vol", 0.0), 6),
                "skew": round(stats.get("skew", 0.0), 4),
                "kurt": round(stats.get("kurt", 0.0), 4),
                "var_5": round(stats.get("var_5", 0.0), 6),
                "var_1": round(stats.get("var_1", 0.0), 6),
                "cvar_5": round(stats.get("cvar_5", 0.0), 6),
                "cvar_1": round(stats.get("cvar_1", 0.0), 6),
                "fallback": bool(stats.get("fallback", False)),
            }

        result = {"as_of": today, "distributions": distributions}
        _regime_dist_cache["as_of"] = today
        _regime_dist_cache["data"] = result
        return JSONResponse(result)
    except Exception as e:
        logger.exception("regime_distributions failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/signals/vix_term_structure ───────────────────────────────────────

@app.get("/api/signals/vix_term_structure")
def vix_term_structure_signal(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/signals/vix_term_structure — VIX/VIX3M ratio signal with persistence + action."""
    try:
        from signals.vix_term_structure import get_current_signal
        signal = get_current_signal()
        if "error" in signal:
            return JSONResponse(signal, status_code=503)
        return JSONResponse(signal)
    except Exception as e:
        logger.exception("vix_term_structure_signal failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/regime/transitions ───────────────────────────────────────────────

@app.get("/api/regime/transitions")
def regime_transitions(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/regime/transitions — regime transition probability matrix."""
    # === REGIME CACHE (P2.7) ===
    try:
        from db.atlas_db import get_cached_regime_transitions
        _cached_rt = get_cached_regime_transitions(max_age_hours=24)
        if _cached_rt:
            return JSONResponse({
                "matrix": _cached_rt.get("matrix", {}),
                "durations": {},
                "states": list(_cached_rt.get("matrix", {}).keys()),
                "current_state": None,
                "total_days": _cached_rt.get("n_observations", 0),
                "as_of": _cached_rt.get("as_of"),
                "stale": False,
                "source": "cache",
            })
    except Exception as _rce:
        logger.warning("regime_transitions: cache lookup failed: %s", _rce)

    # Cache absent — kick off background refresh (non-blocking)
    try:
        import subprocess as _sp2
        _sp2.Popen(
            [sys.executable, "scripts/precompute_risk.py", "--target=regime"],
            cwd=str(PROJECT_ROOT),
            stdout=open("logs/risk_precompute.log", "a"),
            stderr=_sp2.STDOUT,
        )
    except Exception as _rpe:
        logger.warning("regime_transitions: bg refresh failed to start: %s", _rpe)
    # === END REGIME CACHE ===
    try:
        from db.atlas_db import get_db

        STATES = [
            "bull_risk_on", "bull_risk_off", "transition_uncertain",
            "bear_risk_off", "bear_capitulation", "recovery_early"
        ]

        with get_db() as db:
            rows = db.execute(
                "SELECT date, regime_state FROM regime_history ORDER BY date ASC"
            ).fetchall()

        if not rows:
            return JSONResponse({"matrix": {}, "durations": {}, "total_days": 0, "states": STATES})

        history = [dict(r) for r in rows]

        # Count transitions between consecutive days
        transition_counts: dict = {s: {t: 0 for t in STATES} for s in STATES}
        from_counts: dict = {s: 0 for s in STATES}

        for i in range(len(history) - 1):
            from_state = history[i]["regime_state"]
            to_state = history[i + 1]["regime_state"]
            if from_state in transition_counts and to_state in transition_counts[from_state]:
                transition_counts[from_state][to_state] += 1
                from_counts[from_state] += 1

        # Convert to probabilities
        matrix: dict = {}
        for from_s in STATES:
            matrix[from_s] = {}
            total = from_counts[from_s]
            for to_s in STATES:
                if total > 0:
                    matrix[from_s][to_s] = round(transition_counts[from_s][to_s] / total * 100, 1)
                else:
                    matrix[from_s][to_s] = 0.0

        # Calculate average duration in each state (consecutive day runs)
        durations: dict = {s: [] for s in STATES}
        if history:
            current_state = history[0]["regime_state"]
            run_length = 1
            for i in range(1, len(history)):
                if history[i]["regime_state"] == current_state:
                    run_length += 1
                else:
                    if current_state in durations:
                        durations[current_state].append(run_length)
                    current_state = history[i]["regime_state"]
                    run_length = 1
            # Don't forget the last run
            if current_state in durations:
                durations[current_state].append(run_length)

        avg_durations = {}
        for s in STATES:
            runs = durations[s]
            if runs:
                avg_durations[s] = {
                    "avg_days": round(sum(runs) / len(runs), 1),
                    "max_days": max(runs),
                    "occurrences": len(runs),
                    "total_days": sum(runs),
                }
            else:
                avg_durations[s] = {"avg_days": 0, "max_days": 0, "occurrences": 0, "total_days": 0}

        # Current state
        current = history[-1]["regime_state"] if history else None

        return JSONResponse({
            "matrix": matrix,
            "durations": avg_durations,
            "states": STATES,
            "current_state": current,
            "total_days": len(history),
        })
    except Exception as e:
        logger.exception("regime_transitions failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/dashboard-data ───────────────────────────────────────────────────

@app.get("/api/dashboard-data")
def dashboard_data(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/dashboard-data — main dashboard payload (replaces static JSON).

    Uses json.dumps(..., default=str) to handle enum/datetime values from
    broker dataclasses, exactly as the original handler does.
    """
    try:
        data = _build_dashboard_data()
        # Phase 7: inject EV stats into dashboard payload
        try:
            ev_stats = get_latest_ev_stats()
            if not ev_stats:
                results = compute_all_strategies_ev(min_trades=3)
                persist_strategy_ev(results)
                ev_stats = get_latest_ev_stats()
            data['ev_stats'] = ev_stats
        except Exception as e:
            logger.warning(f"EV stats failed: {e}")
            data['ev_stats'] = {}
        body = json.dumps(data, default=str)
        return Response(content=body, media_type="application/json")
    except Exception as e:
        logger.exception("Failed to build dashboard data")
        raise HTTPException(status_code=500, detail=str(e))



_finance_cache: dict = {"data": None, "ts": 0.0}
_FINANCE_CACHE_TTL = 60  # seconds


@app.get("/api/finance")
def finance_data(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/finance — personal finance data from Up Bank SQLite + Atlas DB.

    Queries /root/up-bank/up_bank.db directly (same pattern as trading tab
    querying Atlas SQLite). Caches result for 60 seconds. Falls back to
    static finance-data.json if the DB query fails.
    """
    import time as _time
    now = _time.time()
    if _finance_cache["data"] and (now - _finance_cache["ts"]) < _FINANCE_CACHE_TTL:
        return JSONResponse(content=_finance_cache["data"])

    try:
        import sys
        import sqlite3 as _sqlite3
        if "/root/up-bank" not in sys.path:
            sys.path.insert(0, "/root/up-bank")
        from up_sync import build_finance_payload

        # Open Up Bank DB read-only
        up_conn = _sqlite3.connect("file:///root/up-bank/up_bank.db?mode=ro", uri=True)
        up_conn.row_factory = _sqlite3.Row

        # Get Atlas equity from Atlas SQLite (equity_curve table)
        atlas_eq = 0.0
        atlas_pnl = 0.0
        portfolio_history: list = []
        try:
            from db.atlas_db import get_db
            with get_db() as atlas_conn:
                rows = atlas_conn.execute(
                    "SELECT * FROM equity_curve WHERE market_id='sp500' "
                    "ORDER BY date DESC LIMIT 60"
                ).fetchall()
                if rows:
                    atlas_eq = float(rows[0]["equity"] or 0)
                    atlas_pnl = float(rows[0]["day_pnl"] or 0)
                    portfolio_history = [dict(r) for r in reversed(rows)]
        except Exception as e:
            logger.warning("Atlas equity lookup failed: %s", e)

        # Moomoo data (manual JSON, if available)
        moomoo_data: dict = {}
        moomoo_path = Path("/root/atlas/dashboard/cache/moomoo_manual.json")
        if moomoo_path.exists():
            try:
                with open(moomoo_path) as f:
                    moomoo_data = json.load(f)
            except Exception as e:
                logger.warning("Moomoo manual cache parse failed: %s", e)

        payload = build_finance_payload(
            up_conn, atlas_eq, atlas_pnl, portfolio_history, moomoo_data
        )
        up_conn.close()

        _finance_cache["data"] = payload
        _finance_cache["ts"] = now
        return JSONResponse(content=payload)

    except Exception as e:
        logger.exception("Finance API SQLite query failed, falling back to JSON")
        # Fallback to static JSON file
        finance_path = SERVE_DIR / "finance-data.json"
        if finance_path.exists():
            try:
                with open(finance_path) as f:
                    return JSONResponse(content=json.load(f))
            except Exception as e2:
                logger.warning("Finance cache fallback parse failed: %s", e2)
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# POST routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── POST /api/approve ─────────────────────────────────────────────────────────
# TODO: unused — not called by dashboard UI (plan approval handled via Telegram bot)

@app.post("/api/approve")
def approve_plan(
    body: PlanRequest,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/approve — approve + execute a trade plan.

    Runs in a thread to avoid blocking (broker I/O can be slow).
    60-second timeout; returns 504 if execution is still pending.
    """
    try:
        trade_date = body.trade_date
        market_id = body.market_id
        if not trade_date or not market_id:
            raise HTTPException(
                status_code=400, detail="trade_date and market_id required"
            )

        result: dict = {"pending": True}

        def _run():
            nonlocal result
            try:
                result = _approve_and_execute(trade_date, market_id)
            except Exception as exc:
                logger.exception("Plan approval/execution failed")
                result = {"ok": False, "error": str(exc)}

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=60)  # 60s max for broker execution (same as original)

        if result.get("pending"):
            return JSONResponse(
                {"error": "Execution timed out (still running in background)"},
                status_code=504,
            )

        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Approve endpoint failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── POST /api/reject ──────────────────────────────────────────────────────────
# TODO: unused — not called by dashboard UI (plan rejection handled via Telegram bot)

@app.post("/api/reject")
def reject_plan(
    body: PlanRequest,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/reject — reject a trade plan (mark REJECTED, no execution)."""
    try:
        trade_date = body.trade_date
        market_id = body.market_id
        if not trade_date or not market_id:
            raise HTTPException(
                status_code=400, detail="trade_date and market_id required"
            )

        result = _reject_plan(trade_date, market_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Reject endpoint failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── POST /api/monitor* — 410 Gone ────────────────────────────────────────────

@app.post("/api/monitor")
@app.post("/api/monitor/{monitor_path:path}")
def monitor_post_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/monitor* — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── DELETE /api/monitor/positions/{id} ───────────────────────────────────────

@app.delete("/api/monitor/positions/{pos_id}")
def delete_position(
    pos_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """DELETE /api/monitor/positions/{id} — delete a monitor position."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_position(pos_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ── DELETE /api/monitor/templates/{id} ───────────────────────────────────────

@app.delete("/api/monitor/templates/{tmpl_id}")
def delete_template(
    tmpl_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """DELETE /api/monitor/templates/{id} — delete a monitor template."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_template(tmpl_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Chat REST + WebSocket endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# In-process cache of PiSessionManager instances (one per chat session).
# These survive for the lifetime of the server process; each manager
# maintains the pi_session_path on disk so the conversation can be resumed
# after a server restart.
_pi_sessions: dict[str, "PiSessionManager"] = {}

# Short-lived WebSocket auth tokens: token_str -> (expires_epoch, username)
_ws_tokens: dict[str, tuple[float, str]] = {}
_WS_TOKEN_TTL = 300  # seconds (5 minutes)
_MAX_WS_TOKENS = 1000


def _require_chat() -> None:
    """Raise 503 if chat modules failed to import."""
    if not _CHAT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Chat modules unavailable")


# ── Token endpoint (HTTP Basic → short-lived WS token) ────────────────────

@app.get("/api/chat/token")
def chat_get_token(
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/token — exchange HTTP Basic Auth for a short-lived WS token.

    The browser calls this once (via XMLHttpRequest with cached Basic Auth
    credentials) and stores the returned token in sessionStorage.  The
    WebSocket upgrade then passes it as ``?token=<value>``.
    """
    _require_chat()
    # Purge expired tokens first (Finding F-07)
    now = time.time()
    stale = [k for k, (exp, _) in _ws_tokens.items() if exp < now]
    for k in stale:
        _ws_tokens.pop(k, None)
    # Reject if still over capacity
    if len(_ws_tokens) >= _MAX_WS_TOKENS:
        return JSONResponse(
            status_code=429,
            content={"error": "Too many active tokens"},
        )
    token = secrets.token_urlsafe(32)
    expires = now + _WS_TOKEN_TTL
    _ws_tokens[token] = (expires, _auth.username)
    return JSONResponse({"token": token, "expires_in": _WS_TOKEN_TTL})


# ── Chat session REST endpoints ────────────────────────────────────────────

@app.get("/api/chat/sessions")
def chat_list_sessions(
    limit: int = 20,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions — list active sessions, newest first."""
    _require_chat()
    return JSONResponse(_chat_list_sessions(limit))


@app.post("/api/chat/sessions")
async def chat_create_session_endpoint(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """POST /api/chat/sessions — create a new chat session.

    Body (JSON): {"name": "optional name", "model": "claude-sonnet-4-6"}
    """
    _require_chat()
    try:
        body = await request.json()
    except Exception as e:
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name")
    model = body.get("model", "claude-opus-4-7")
    session = _chat_create_session(name=name, model=model)
    return JSONResponse(session)


@app.get("/api/chat/sessions/{session_id}")
def chat_get_session_endpoint(
    session_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions/{id} — get single session details."""
    _require_chat()
    session = _chat_get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(session)


@app.put("/api/chat/sessions/{session_id}")
async def chat_rename_session_endpoint(
    session_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """PUT /api/chat/sessions/{id} — rename a chat session.

    Body: {"name": "new session name"}
    """
    _require_chat()
    try:
        body = await request.json()
    except Exception as e:
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    ok = _chat_rename_session(session_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"ok": True, "id": session_id, "name": name})


@app.delete("/api/chat/sessions/{session_id}")
def chat_delete_session_endpoint(
    session_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """DELETE /api/chat/sessions/{id} — soft-delete a chat session."""
    _require_chat()
    ok = _chat_delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"ok": True, "id": session_id})


@app.get("/api/chat/sessions/{session_id}/messages")
def chat_get_messages_endpoint(
    session_id: str,
    limit: int = 50,
    before_id: int = None,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions/{id}/messages — paginated message history."""
    _require_chat()
    msgs = _chat_get_messages(session_id, limit=limit, before_id=before_id)
    return JSONResponse(msgs)


# ── WebSocket chat endpoint ──────────────────────────────────────────────────

@app.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket) -> None:  # noqa: C901
    """WS /ws/chat?token=<tok> — bidirectional streaming chat.

    Auth
    ----
    Pass the token from ``GET /api/chat/token`` as a query parameter::

        ws://host/ws/chat?token=<value>

    Alternatively pass ``Authorization: Basic <b64>`` as a WS header
    (supported by some clients; browsers cannot set custom WS headers).

    Protocol (client → server)
    --------------------------
    {"type": "send",        "content": "...", "session_id": "uuid|null"}
    {"type": "history",    "session_id": "uuid", "limit": 50, "before_id": null}
    {"type": "cancel",     "session_id": "uuid"}
    {"type": "new_session", "name": "optional", "model": "claude-sonnet-4-6"}
    {"type": "status",     "session_id": "uuid"}

    Protocol (server → client) — see PiEvent.to_dict() for streaming events.
    """
    # ── Auth: token query param takes priority, then Basic Auth header ──
    token_param = ws.query_params.get("token", "")
    authed = False

    if token_param:
        entry = _ws_tokens.get(token_param)
        if entry:
            expires, _username = entry
            if time.time() < expires:
                authed = True

    if not authed:
        # Fall back: check Authorization header
        auth_header = ws.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                uname, pw = decoded.split(":", 1)
                exp_user, exp_pass = _get_credentials()
                user_ok = secrets.compare_digest(uname.encode(), exp_user.encode())
                pass_ok = secrets.compare_digest(pw.encode(), exp_pass.encode())
                authed = user_ok and pass_ok
            except Exception as e:
                logger.debug("WebSocket auth decode failed: %s", e)

    if not authed:
        await ws.close(code=1008, reason="Unauthorized")
        return

    if not _CHAT_AVAILABLE:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "Chat modules unavailable"})
        await ws.close()
        return

    await ws.accept()

    # Guard: prevent overlapping send_message calls on the same session.
    # If user sends a second message while the first is still streaming,
    # we reject it rather than corrupt the Pi session file.
    _generating = False

    try:
        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                break

            msg_type = data.get("type", "")

            # ---- send: user sends a chat message --------------------------
            if msg_type == "send":
                if _generating:
                    await ws.send_json({"type": "error", "message": "Already generating a response. Wait for it to finish or cancel first."})
                    continue
                content = data.get("content", "").strip()
                images = data.get("images")  # [{data, mime}, ...]
                attachments = data.get("attachments")  # [{name, data, mime}, ...]
                if not content and not images and not attachments:
                    continue

                session_id = data.get("session_id")

                # Resolve or create session
                if not session_id:
                    latest = _chat_get_latest_session()
                    if latest:
                        session_id = latest["id"]
                    else:
                        new_sess = _chat_create_session()
                        session_id = new_sess["id"]

                # Persist user message
                msg_id = _chat_add_message(session_id, "user", content)
                await ws.send_json({
                    "type": "user_message_saved",
                    "id": msg_id,
                    "session_id": session_id,
                })

                # Get or create PiSessionManager for this session
                # Allow per-message team mode toggle
                use_teams = bool(data.get("use_teams", False))

                if session_id not in _pi_sessions:
                    sess_rec = _chat_get_session(session_id)
                    model = (
                        sess_rec.get("model", "claude-sonnet-4-6")
                        if sess_rec
                        else "claude-sonnet-4-6"
                    )
                    _pi_sessions[session_id] = PiSessionManager(
                        session_id, model=model, use_teams=use_teams
                    )
                else:
                    # Update teams mode if changed
                    _pi_sessions[session_id].use_teams = use_teams

                mgr = _pi_sessions[session_id]

                # Warn if session is getting heavy (>60K tokens estimated)
                if mgr.pi_session_path.exists():
                    session_size = mgr.pi_session_path.stat().st_size
                    if session_size > 200_000:  # ~60K tokens
                        await ws.send_json({
                            "type": "warning",
                            "message": f"Session history is large ({session_size // 1024}KB). Consider starting a new session for faster responses.",
                        })

                # Stream response events back to client
                _generating = True
                full_text = ""
                try:
                    async for event in mgr.send_message(content, images=images, attachments=attachments):
                        await ws.send_json(event.to_dict())
                        if event.type == "text_delta":
                            full_text += event.data.get("delta", "")
                        elif event.type == "done":
                            full_text = event.data.get("full_text") or full_text
                except WebSocketDisconnect:
                    # Client left mid-stream; Pi keeps running, we save what we have
                    break
                finally:
                    _generating = False

                # Persist assistant reply
                if full_text:
                    _chat_add_message(session_id, "assistant", full_text)

            # ---- history: load stored messages ----------------------------
            elif msg_type == "history":
                session_id = data.get("session_id")
                limit = int(data.get("limit", 50))
                before_id = data.get("before_id")
                if session_id:
                    msgs = _chat_get_messages(
                        session_id, limit=limit, before_id=before_id
                    )
                    await ws.send_json({
                        "type": "history",
                        "messages": msgs,
                        "session_id": session_id,
                    })

            # ---- cancel: kill running Pi subprocess -----------------------
            elif msg_type == "cancel":
                session_id = data.get("session_id")
                if session_id and session_id in _pi_sessions:
                    await _pi_sessions[session_id].cancel()
                _generating = False
                await ws.send_json({"type": "cancelled"})

            # ---- new_session: create a fresh conversation -----------------
            elif msg_type == "new_session":
                name = data.get("name")
                model = data.get("model", "claude-sonnet-4-6")
                sess = _chat_create_session(name=name, model=model)
                await ws.send_json({"type": "session_created", "session": sess})

            # ---- status: is Pi running? -----------------------------------
            elif msg_type == "status":
                session_id = data.get("session_id")
                mgr = _pi_sessions.get(session_id) if session_id else None
                await ws.send_json({
                    "type": "status",
                    "pi_running": mgr.is_running if mgr else False,
                    "session_id": session_id,
                })

    except WebSocketDisconnect:
        pass  # Normal: client closed tab / navigated away
    except Exception as exc:
        logger.exception("WebSocket chat error: %s", exc)
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception as e:
            logger.debug("Could not send error to WebSocket client: %s", e)



# ═══════════════════════════════════════════════════════════════════════════════
# ── Research Tab API — Overview, Leaderboard, Controls ───────────────────────

@app.get("/api/research/overview")
def research_overview(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Comprehensive research overview: universes, engine status, daily metrics."""
    try:
        import json as _json
        from db.atlas_db import get_db

        # Load priorities config
        priorities_path = PROJECT_ROOT / "config" / "research_priorities.json"
        priorities = {}
        if priorities_path.exists():
            with open(priorities_path) as f:
                pdata = _json.load(f)
                priorities = pdata.get("research_priorities", {})

        # Load research_best for best sharpes per strategy/universe
        with get_db() as db:
            # Per-universe stats from research_experiments
            universe_stats = {}
            for r in db.execute("""
                SELECT universe,
                       COUNT(*) as total_experiments,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept,
                       MAX(sharpe) as best_sharpe,
                       MAX(created_at) as last_experiment
                FROM research_experiments
                GROUP BY universe
            """).fetchall():
                d = dict(r)
                universe_stats[d["universe"]] = d

            # Today's stats per universe
            today_stats = {}
            for r in db.execute("""
                SELECT universe,
                       COUNT(*) as experiments_today,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept_today
                FROM research_experiments
                WHERE created_at >= date('now')
                GROUP BY universe
            """).fetchall():
                d = dict(r)
                today_stats[d["universe"]] = d

            # Best per strategy per universe from research_best
            best_by_universe = {}
            for r in db.execute("SELECT strategy, universe, sharpe, trades FROM research_best WHERE sharpe > 0 ORDER BY sharpe DESC").fetchall():
                d = dict(r)
                uni = d["universe"]
                if uni not in best_by_universe:
                    best_by_universe[uni] = []
                best_by_universe[uni].append({"strategy": d["strategy"], "best_sharpe": d["sharpe"], "trades": d["trades"]})

            # Strategy breakdown per universe from experiments
            strat_breakdown = {}
            for r in db.execute("""
                SELECT universe, strategy, COUNT(*) as experiments,
                       MAX(sharpe) as best_sharpe,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                GROUP BY universe, strategy
            """).fetchall():
                d = dict(r)
                uni = d["universe"]
                if uni not in strat_breakdown:
                    strat_breakdown[uni] = {}
                strat_breakdown[uni][d["strategy"]] = {
                    "best_sharpe": d["best_sharpe"],
                    "experiments": d["experiments"],
                    "kept": d["kept"]
                }

            # Build universe list
            all_universes = set(list(priorities.keys()) + list(universe_stats.keys()))
            universes = []
            for uid in sorted(all_universes):
                pri = priorities.get(uid, {})
                stats = universe_stats.get(uid, {})
                today = today_stats.get(uid, {})
                total_exp = stats.get("total_experiments", 0)
                kept_total = stats.get("kept", 0)
                exp_today = today.get("experiments_today", 0)
                kept_today_val = today.get("kept_today", 0)

                universes.append({
                    "id": uid,
                    "mode": pri.get("mode", "passive"),
                    "priority": pri.get("priority", "low"),
                    "best_sharpe": stats.get("best_sharpe", 0) or 0,
                    "total_experiments": total_exp,
                    "experiments_today": exp_today,
                    "kept_today": kept_today_val,
                    "keep_rate": round(kept_total / total_exp * 100, 1) if total_exp > 0 else 0,
                    "strategies": strat_breakdown.get(uid, {}),
                    "top_strategies": best_by_universe.get(uid, [])[:5],
                    "last_experiment": stats.get("last_experiment"),
                    "windows_per_day": pri.get("windows_per_day", 0),
                })

            # Engine status
            import subprocess
            try:
                result = subprocess.run(["systemctl", "is-active", "atlas-research-window"],
                                       capture_output=True, text=True, timeout=5)
                engine_status = result.stdout.strip()
                if engine_status == "active":
                    engine_status = "running"
                elif engine_status == "inactive":
                    engine_status = "idle"
                else:
                    engine_status = "idle"
            except Exception as e:
                logger.debug("engine_status subprocess failed: %s", e)
                engine_status = "unknown"

            # Total all-time and daily aggregates
            totals = db.execute("""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN created_at >= date('now') THEN 1 ELSE 0 END) as today,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept_all
                FROM research_experiments
            """).fetchone()

            # Experiments per day for last 14 days (sparkline data)
            daily_counts = [dict(r) for r in db.execute("""
                SELECT date(created_at) as date, COUNT(*) as count,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                WHERE created_at >= date('now', '-14 days')
                GROUP BY date(created_at)
                ORDER BY date
            """).fetchall()]

            return JSONResponse(content={
                "universes": universes,
                "engine": {
                    "status": engine_status,
                    "total_experiments_all_time": totals["total"],
                    "experiments_today": totals["today"],
                    "kept_all_time": totals["kept_all"],
                    "daily_counts": daily_counts,
                },
            })
    except Exception as e:
        logger.exception("research_overview failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/leaderboard")
def research_leaderboard(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Best strategy/universe combos ranked by Sharpe from research_best table."""
    try:
        import json as _json
        from db.atlas_db import get_db
        with get_db() as db:
            rows = []
            for r in db.execute("""
                SELECT rb.strategy, rb.universe, rb.sharpe, rb.trades, rb.max_dd_pct, rb.updated_at,
                       (SELECT COUNT(*) FROM research_experiments re
                        WHERE re.strategy = rb.strategy AND re.universe = rb.universe) as total_experiments
                FROM research_best rb
                WHERE rb.sharpe > 0
                ORDER BY rb.sharpe DESC
            """).fetchall():
                d = dict(r)
                rows.append(d)
            return JSONResponse(content={"leaderboard": rows})
    except Exception as e:
        logger.exception("research_leaderboard failed")
        raise HTTPException(status_code=500, detail=str(e))


# TODO: unused — not called by dashboard UI (admin-only endpoint)
@app.post("/api/research/prioritize")
async def research_prioritize(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Update priority for a universe in research_priorities.json."""
    import json as _json
    try:
        body = await request.json()
        universe = body.get("universe")
        priority = body.get("priority")  # high, medium, low
        action = body.get("action")  # pause, resume, or None

        if not universe:
            raise HTTPException(status_code=400, detail="universe required")

        priorities_path = PROJECT_ROOT / "config" / "research_priorities.json"
        with open(priorities_path) as f:
            pdata = _json.load(f)

        rp = pdata.get("research_priorities", {})
        if universe not in rp:
            raise HTTPException(status_code=404, detail=f"Universe {universe} not found")

        if priority and priority in ("high", "medium", "low"):
            rp[universe]["priority"] = priority

        if action == "pause":
            rp[universe]["paused"] = True
        elif action == "resume":
            rp[universe].pop("paused", None)

        pdata["research_priorities"] = rp
        pdata["_updated"] = __import__("datetime").date.today().isoformat()

        with open(priorities_path, "w") as f:
            _json.dump(pdata, f, indent=2)

        return JSONResponse(content={"ok": True, "universe": universe, "updated": rp[universe]})
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("research_prioritize failed")
        raise HTTPException(status_code=500, detail=str(e))



# ── Research Dashboard API ────────────────────────────────────────────────────

@app.get("/api/research/summary")
def research_summary(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Research overview: total experiments, keep rate, by strategy, by source."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            # Total experiments
            total = db.execute("SELECT COUNT(*) as c FROM research_experiments").fetchone()["c"]
            kept = db.execute("SELECT COUNT(*) as c FROM research_experiments WHERE status='kept'").fetchone()["c"]

            # Last 7 days
            recent = db.execute(
                "SELECT COUNT(*) as c FROM research_experiments WHERE created_at >= datetime('now', '-7 days')"
            ).fetchone()["c"]

            # By strategy
            by_strategy = [dict(r) for r in db.execute("""
                SELECT strategy, COUNT(*) as total,
                       SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept,
                       MAX(sharpe) as best_sharpe
                FROM research_experiments
                GROUP BY strategy ORDER BY total DESC
            """).fetchall()]

            # By source (experiment_type)
            by_source = [dict(r) for r in db.execute("""
                SELECT experiment_type as source, COUNT(*) as total
                FROM research_experiments
                GROUP BY experiment_type ORDER BY total DESC
            """).fetchall()]

            # Last research timestamp
            last_ts = db.execute(
                "SELECT MAX(created_at) as ts FROM research_experiments"
            ).fetchone()["ts"]

            # Distinct strategies
            strat_count = db.execute(
                "SELECT COUNT(DISTINCT strategy) as c FROM research_experiments"
            ).fetchone()["c"]

            return JSONResponse(content={
                "total_experiments": total,
                "kept_count": kept,
                "keep_rate": round(kept / total * 100, 1) if total > 0 else 0,
                "experiments_7d": recent,
                "strategies_count": strat_count,
                "last_research_ts": last_ts,
                "by_strategy": by_strategy,
                "by_source": by_source,
            })
    except Exception as e:
        logger.exception("research_summary failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/experiments")
def research_experiments(
    strategy: str = None,
    status: str = None,
    source: str = None,
    regime: str = None,
    limit: int = 50,
    offset: int = 0,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Paginated experiment list with filters."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            query = "SELECT * FROM research_experiments WHERE 1=1"
            count_query = "SELECT COUNT(*) as c FROM research_experiments WHERE 1=1"
            params = []

            if strategy:
                query += " AND strategy=?"
                count_query += " AND strategy=?"
                params.append(strategy)
            if status:
                query += " AND status=?"
                count_query += " AND status=?"
                params.append(status)
            if source:
                query += " AND experiment_type=?"
                count_query += " AND experiment_type=?"
                params.append(source)
            if regime:
                query += " AND regime_state=?"
                count_query += " AND regime_state=?"
                params.append(regime)

            total = db.execute(count_query, params).fetchone()["c"]

            query += f" ORDER BY created_at DESC LIMIT {int(limit)} OFFSET {int(offset)}"
            rows = []
            for r in db.execute(query, params).fetchall():
                d = dict(r)
                if d.get("params_changed"):
                    try:
                        d["params_changed"] = _json.loads(d["params_changed"])
                    except (ValueError, TypeError):
                        pass
                rows.append(d)

            return JSONResponse(content={"experiments": rows, "total": total})
    except Exception as e:
        logger.exception("research_experiments failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/strategies")
def research_strategies(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Per-strategy stats with best params from research_best."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            strategies = [dict(r) for r in db.execute("""
                SELECT
                    e.strategy,
                    COUNT(*) as total_experiments,
                    SUM(CASE WHEN e.status='kept' THEN 1 ELSE 0 END) as kept_count,
                    MAX(e.sharpe) as best_sharpe,
                    MAX(e.cagr_pct) as best_cagr,
                    MAX(CASE WHEN e.status='kept' THEN e.created_at END) as last_improvement
                FROM research_experiments e
                GROUP BY e.strategy
                ORDER BY best_sharpe DESC
            """).fetchall()]

            # Enrich with best params
            best_rows = {r["strategy"]: dict(r) for r in db.execute(
                "SELECT * FROM research_best"
            ).fetchall()}

            for s in strategies:
                best = best_rows.get(s["strategy"])
                if best and best.get("params"):
                    try:
                        s["best_params"] = _json.loads(best["params"])
                    except (ValueError, TypeError):
                        s["best_params"] = best["params"]
                else:
                    s["best_params"] = None

            return JSONResponse(content={"strategies": strategies})
    except Exception as e:
        logger.exception("research_strategies failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/timeline")
def research_timeline(days: int = 30, _auth: HTTPBasicCredentials = Depends(check_auth)):
    """Daily experiment counts and running best Sharpe per strategy."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            rows = [dict(r) for r in db.execute("""
                SELECT
                    DATE(created_at) as date,
                    strategy,
                    COUNT(*) as experiments,
                    MAX(sharpe) as best_sharpe,
                    SUM(CASE WHEN status='kept' THEN 1 ELSE 0 END) as kept
                FROM research_experiments
                WHERE created_at >= datetime('now', ?)
                GROUP BY DATE(created_at), strategy
                ORDER BY date
            """, (f"-{int(days)} days",)).fetchall()]

            # Organize into series by strategy
            series = {}
            dates = sorted(set(r["date"] for r in rows if r["date"]))
            for r in rows:
                strat = r["strategy"]
                if strat not in series:
                    series[strat] = []
                series[strat].append({
                    "date": r["date"],
                    "experiments": r["experiments"],
                    "best_sharpe": r["best_sharpe"],
                    "kept": r["kept"],
                })

            return JSONResponse(content={"dates": dates, "series": series})
    except Exception as e:
        logger.exception("research_timeline failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/discoveries")
def research_discoveries(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Discovery pipeline runs."""
    try:
        from db.atlas_db import get_db
        import json as _json
        with get_db() as db:
            rows = []
            for r in db.execute("""
                SELECT * FROM research_discoveries ORDER BY created_at DESC LIMIT 50
            """).fetchall():
                d = dict(r)
                if d.get("paper_titles"):
                    try:
                        d["paper_titles"] = _json.loads(d["paper_titles"])
                    except (ValueError, TypeError):
                        pass
                rows.append(d)
            return JSONResponse(content={"discoveries": rows})
    except Exception as e:
        logger.exception("research_discoveries failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/research/brain")
def research_brain(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Brain knowledge entries — params and patterns."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            # Param summaries: aggregate by title (param name)
            params = [dict(r) for r in db.execute("""
                SELECT title as param_name,
                       COUNT(*) as tests,
                       COUNT(DISTINCT strategy) as strategies_tested,
                       SUM(CASE WHEN sharpe_delta > 0 THEN 1 ELSE 0 END) as improved,
                       AVG(sharpe_delta) as avg_sharpe_delta
                FROM research_brain
                WHERE entry_type='param'
                GROUP BY title
                ORDER BY tests DESC
            """).fetchall()]

            # Patterns
            patterns = [dict(r) for r in db.execute("""
                SELECT title as name, content as summary, source_file, updated_at
                FROM research_brain
                WHERE entry_type='pattern'
                ORDER BY updated_at DESC
            """).fetchall()]

            return JSONResponse(content={"params": params, "patterns": patterns})
    except Exception as e:
        logger.exception("research_brain failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ── C4: Research coverage matrix ─────────────────────────────────────────────

@app.get("/api/research/coverage")
def research_coverage(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """Coverage matrix: strategies × universes → last-promotion-date + sharpe."""
    try:
        from db.atlas_db import get_db
        from datetime import datetime, timezone

        with get_db() as db:
            rows = [dict(r) for r in db.execute(
                "SELECT strategy, universe, sharpe, trades, updated_at "
                "FROM research_best ORDER BY strategy, universe"
            ).fetchall()]

        strategies = sorted({r["strategy"] for r in rows})
        universes = sorted({r["universe"] for r in rows})

        now = datetime.now(timezone.utc)
        matrix: dict = {s: {u: None for u in universes} for s in strategies}
        for r in rows:
            updated_at_str = r.get("updated_at")
            age_days = None
            status = "never"
            if updated_at_str:
                try:
                    # SQLite datetime('now') format: 'YYYY-MM-DD HH:MM:SS' in UTC
                    ts = datetime.fromisoformat(updated_at_str.replace(" ", "T"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    age_days = (now - ts).total_seconds() / 86400
                    if age_days < 7:
                        status = "fresh"
                    elif age_days < 14:
                        status = "stale"
                    else:
                        status = "very_stale"
                except (ValueError, TypeError):
                    pass
            matrix[r["strategy"]][r["universe"]] = {
                "sharpe": r["sharpe"],
                "trades": r["trades"],
                "updated_at": updated_at_str,
                "age_days": round(age_days, 1) if age_days is not None else None,
                "status": status,
            }

        return JSONResponse(content={
            "strategies": strategies,
            "universes": universes,
            "matrix": matrix,
            "generated_at": now.isoformat(),
        })
    except Exception as e:
        logger.exception("research_coverage failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ── C5: Pending promotions endpoints ─────────────────────────────────────────

@app.get("/api/promotions/pending")
def promotions_pending(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """List pending promotions awaiting approval."""
    try:
        from research.promoter import _load_pending, expire_pending_promotions
        # Auto-expire stale entries on every read (idempotent)
        expire_pending_promotions()
        entries = _load_pending()
        pending = [e for e in entries if e.get("status") == "pending"]
        return JSONResponse(content={"pending": pending, "count": len(pending)})
    except Exception as e:
        logger.exception("promotions_pending failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/promotions/{pending_id}/approve")
def promotions_approve(
    pending_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Approve a pending promotion (executes write to config/active/)."""
    try:
        from research.promoter import complete_pending_promotion
        approver = _auth.username if _auth else "unknown"
        logger.info(
            "[audit] promotion approve: pending_id=%s approver=%s remote=%s",
            pending_id, approver, request.client.host if request.client else "?",
        )
        result = complete_pending_promotion(pending_id)
        if result.get("promoted"):
            return JSONResponse(content={
                "approved": True,
                "version": result.get("version"),
                "strategy": result.get("strategy"),
                "market": result.get("market"),
                "approver": approver,
            })
        # Surface rejections from gate failure or "already X" cleanly
        reason = result.get("reason", "promotion failed")
        if "not found" in reason.lower():
            raise HTTPException(status_code=404, detail=reason)
        if "already" in reason.lower():
            raise HTTPException(status_code=409, detail=reason)
        raise HTTPException(status_code=400, detail=reason)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("promotions_approve failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/promotions/{pending_id}/reject")
def promotions_reject(
    pending_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Reject a pending promotion."""
    try:
        from research.promoter import reject_pending_promotion
        approver = _auth.username if _auth else "unknown"
        # Default reason; override from query param ?reason=foo for sync simplicity
        reason = "Rejected via dashboard"
        qreason = request.query_params.get("reason")
        if qreason:
            reason = qreason
        logger.info(
            "[audit] promotion reject: pending_id=%s approver=%s reason=%s",
            pending_id, approver, reason,
        )
        result = reject_pending_promotion(pending_id, reason)
        if result.get("rejected"):
            return JSONResponse(content={
                "rejected": True,
                "strategy": result.get("strategy"),
                "approver": approver,
            })
        raise HTTPException(status_code=404, detail=result.get("reason", "Not found"))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("promotions_reject failed")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# ── /chat route — full-page agent interface ──────────────────────────────────

@app.get("/homerbot")
@app.get("/chat")
def serve_agent_page(
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve the full-page AI agent chat interface."""
    file_path = SERVE_DIR / "agent.html"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Agent page not found")
    return FileResponse(
        str(file_path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# === RISK CACHE ENDPOINTS (P2.7/P2.8) ===
# New endpoints added at EOF (before SPA catch-all) per coordination rules.
# The other backend worker owns P2.1-P2.6 blocks; we stay strictly here.

# ── POST /api/risk/ruin/refresh ────────────────────────────────────────────────

@app.post("/api/risk/ruin/refresh")
def risk_ruin_refresh(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """POST /api/risk/ruin/refresh — trigger a non-blocking background ruin recompute.

    Kicks off precompute_risk.py --target=ruin in the background and returns
    immediately.  Used by the frontend PORTFOLIO CHANGED banner to request fresh data.
    """
    import subprocess as _sr
    try:
        started_at = datetime.now(timezone.utc).isoformat()
        _sr.Popen(
            [sys.executable, "scripts/precompute_risk.py", "--target=ruin"],
            cwd=str(PROJECT_ROOT),
            stdout=open("logs/risk_precompute.log", "a"),
            stderr=_sr.STDOUT,
        )
        return JSONResponse({"ok": True, "started_at": started_at})
    except Exception as e:
        logger.exception("risk_ruin_refresh failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/risk/ruin — patched to include stale flag (P2.8) ─────────────────
# The existing /api/risk/ruin handler above returns from the ruin_probability
# table without portfolio-change detection.  We shadow it here with a wrapper
# that reads get_cached_ruin_probability() first (which carries stale/reason).
# FastAPI processes routes in registration order so the FIRST matching route
# wins; therefore we cannot shadow by re-registering the same path.
#
# Instead, we patch the existing risk_ruin handler inside /api/system/health
# inline and expose stale semantics via get_cached_ruin_probability().
# The existing /api/risk/ruin endpoint already returns {as_of, tickers, ...};
# we only need to inject stale+reason which is done in the GET /api/risk/ruin
# block above via the cache lookup.  No duplicate registration needed.


# ── GET /api/system/health/universes (P4.2) ───────────────────────────────────

def _build_universes_list() -> list:
    """Build the universe health list from config/active/*.json + SQLite."""
    import json as _uj
    from db.atlas_db import get_db as _udb, get_latest_equity as _ueq

    universes = []
    for cfg_path in sorted(Path("config/active").glob("*.json")):
        if cfg_path.stem == "regime":
            continue
        try:
            cfg = _uj.loads(cfg_path.read_text())
            market_id = cfg.get("market", cfg_path.stem)
            mode = cfg.get("trading", {}).get("mode", "unknown")
            approval = bool(cfg.get("trading", {}).get("live_enabled", False))
            starting_equity = cfg.get("risk", {}).get("starting_equity")
            with _udb() as db:
                op_row = db.execute(
                    "SELECT COUNT(*) AS n FROM trades "
                    "WHERE exit_date IS NULL AND (universe=? OR universe IS NULL)",
                    (market_id,),
                ).fetchone()
            open_positions = op_row["n"] if op_row else 0
            eq_row = _ueq(market_id=market_id)
            equity = (eq_row or {}).get("equity")
            universes.append({
                "market_id": market_id,
                "mode": mode,
                "approval": approval,
                "open_positions": open_positions,
                "equity": equity,
                "starting_equity": starting_equity,
            })
        except Exception as ue:
            logger.debug("universes: error reading %s: %s", cfg_path.name, ue)
    return universes


@app.get("/api/system/health/universes")
def system_health_universes(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/system/health/universes — per-universe status from config/active/*.json.

    Returns live/passive/paper mode, approval flag, open position count, and
    equity for every configured market universe.  ASX (approval=false) is
    deliberately included — the frontend uses this to render universe toggles.

    Example response::

        {
          "universes": [
            {"market_id": "sp500", "mode": "live", "approval": true,
             "open_positions": 7, "equity": 5334.25, "starting_equity": 5000},
            {"market_id": "asx", "mode": "passive", "approval": false,
             "open_positions": 0, "equity": null, "starting_equity": 0},
            ...
          ]
        }
    """
    try:
        universes = _build_universes_list()
        return JSONResponse({"universes": universes})
    except Exception as e:
        logger.exception("system_health_universes failed")
        raise HTTPException(status_code=500, detail=str(e))

# === END RISK CACHE ENDPOINTS (P2.7/P2.8) ===

# Static file catch-all  (MUST be last — fallback after all API routes)
# Serves React SPA from dashboard-ui/dist/ with fallback to index.html
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/{path:path}")
def serve_static(
    path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve React SPA from dashboard-ui/dist/.

    - Exact file matches (JS/CSS/SVG) → serve directly with cache headers
    - Everything else → serve index.html (SPA client-side routing)
    - Fallback to legacy dashboard/data/ for old static files
    """
    if not path:
        path = "index.html"

    # --- Try React dist first ---
    react_root = REACT_DIR.resolve()
    try:
        react_file = (REACT_DIR / path).resolve()
        if str(react_file).startswith(str(react_root)) and react_file.exists() and react_file.is_file():
            if path.startswith("assets/"):
                cache = "public, max-age=31536000, immutable"  # hashed filenames
            elif path.endswith(".html"):
                cache = "no-cache"
            else:
                cache = "public, max-age=3600"
            return FileResponse(str(react_file), headers={"Cache-Control": cache})
    except (ValueError, OSError):
        pass

    # --- Fallback to legacy dashboard/data/ for old static files (.json etc) ---
    try:
        serve_root = SERVE_DIR.resolve()
        legacy_file = (SERVE_DIR / path).resolve()
        if str(legacy_file).startswith(str(serve_root)) and legacy_file.exists() and legacy_file.is_file():
            if path.endswith(".json"):
                cache = "no-cache, no-store, must-revalidate"
            elif path.endswith(".html"):
                cache = "no-cache"
            else:
                cache = "public, max-age=3600"
            return FileResponse(str(legacy_file), headers={"Cache-Control": cache})
    except (ValueError, OSError):
        pass

    # --- SPA fallback: serve React index.html for client-side routing ---
    index_file = REACT_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), headers={"Cache-Control": "no-cache"})

    raise HTTPException(status_code=404, detail="Not found")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
