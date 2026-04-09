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
from datetime import datetime
from pathlib import Path

# ── Housekeeping (mirror dashboard_server.py top-level setup) ────────────────

signal.signal(signal.SIGHUP, signal.SIG_IGN)

PROJECT_ROOT = Path("/root/atlas")
SECRETS_PATH = Path(os.environ.get("ATLAS_SECRETS_PATH", str(Path.home() / ".atlas-secrets.json")))
SERVE_DIR = PROJECT_ROOT / "dashboard" / "data"
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
            with get_db() as db:
                open_trades = db.execute(
                    "SELECT ticker, strategy, entry_date, stop_price, entry_price "
                    "FROM trades WHERE exit_date IS NULL"
                ).fetchall()
            trade_meta = {t["ticker"]: dict(t) for t in open_trades}
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
        result["portfolio_history"] = [dict(r) for r in equity_rows]

        # Strategy performance aggregated from closed trades
        trades_rows = db.execute(
            "SELECT strategy, pnl, pnl_pct FROM trades WHERE exit_date IS NOT NULL"
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
            "SELECT pnl FROM trades WHERE exit_date IS NOT NULL"
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

        # ── 3. Benchmark (SPY) curve ──────────────────────────────────────────
        spy_rows = db.execute(
            "SELECT date, close FROM ohlcv WHERE ticker = 'SPY' "
            "ORDER BY date DESC LIMIT ?",
            (max(len(equity_rows), 90),),
        ).fetchall()
        if spy_rows:
            spy_data = list(reversed(spy_rows))
            port_start = config.get("risk", {}).get("starting_equity", 1)
            spy_start = spy_data[0]["close"]
            scale = port_start / spy_start if spy_start else 1
            bench_curve = [
                {"date": r["date"], "equity": round(r["close"] * scale, 2)}
                for r in spy_data
            ]
            spy_return = ((spy_data[-1]["close"] / spy_data[0]["close"]) - 1) * 100
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
    result["summary"]["today_pnl"] = round(
        sum(
            p.get("intraday_pnl", 0) or p.get("today_pnl", 0) or 0
            for p in positions
        ),
        2,
    )
    result["summary"]["max_positions"] = config.get("risk", {}).get(
        "max_open_positions", 10
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
        return JSONResponse(rows)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/regime/current ───────────────────────────────────────────────────

@app.get("/api/regime/current")
def regime_current(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/regime/current — most recent regime state."""
    try:
        from db.atlas_db import get_current_regime
        regime = get_current_regime()
        return JSONResponse(regime if regime else {"regime_state": "unknown"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/overlay/decisions ────────────────────────────────────────────────

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
            except Exception:
                services[svc] = "unknown"

        # Data freshness from SQLite
        data_freshness = {}
        try:
            with get_db() as db:
                row = db.execute("SELECT MAX(date) as last_date FROM ohlcv").fetchone()
                data_freshness["ohlcv_last_date"] = row["last_date"] if row else None
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

        return JSONResponse({
            "services": services,
            "cron": cron_services,
            "data_freshness": data_freshness,
            "heartbeats": heartbeats,
            "timestamp": datetime.now().isoformat(),
        })
    except Exception as e:
        logger.exception("system_health failed")
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
            except Exception:
                pass

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
            except Exception:
                pass
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# POST routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── POST /api/approve ─────────────────────────────────────────────────────────

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
    model = body.get("model", "claude-opus-4-6")
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
                if not content and not images:
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
                    async for event in mgr.send_message(content, images=images):
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


# Static file catch-all  (MUST be last — fallback after all API routes)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/{path:path}")
def serve_static(
    path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve static files from dashboard/data/ with appropriate cache headers.

    Mirrors SimpleHTTPRequestHandler behaviour from the original server:
      - .json  → no-cache, no-store, must-revalidate
      - .html  → no-cache
      - other  → public, max-age=3600
    """
    if not path:
        path = "index.html"

    # Prevent path traversal
    try:
        serve_root = SERVE_DIR.resolve()
        file_path = (SERVE_DIR / path).resolve()
        if not str(file_path).startswith(str(serve_root)):
            raise HTTPException(status_code=403, detail="Forbidden")
    except (ValueError, OSError):
        raise HTTPException(status_code=403, detail="Forbidden")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Not found")

    # Cache-control matching original end_headers() logic
    if path.endswith(".json"):
        cache = "no-cache, no-store, must-revalidate"
    elif path.endswith(".html") or path in ("", "/"):
        cache = "no-cache"
    else:
        cache = "public, max-age=3600"

    return FileResponse(str(file_path), headers={"Cache-Control": cache})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
