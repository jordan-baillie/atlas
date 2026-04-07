#!/usr/bin/env python3
"""Atlas Dashboard Server (FastAPI) — HTTP Basic Auth protected.

1:1 port of dashboard_server.py using FastAPI + Uvicorn.
Phase 1 of dashboard migration — NO chat features, drop-in replacement.

Credentials from ~/.atlas-secrets.json:
    dashboard_user, dashboard_pass

Run:
    python3 services/chat_server.py                    # foreground (uvicorn)
    uvicorn services.chat_server:app --host 127.0.0.1 --port 8899
    systemctl start atlas-dashboard                    # systemd (after updating unit file)
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

signal.signal(signal.SIGHUP, signal.SIG_IGN)

PROJECT_ROOT = Path("/root/atlas")
SECRETS_PATH = Path.home() / ".atlas-secrets.json"
SERVE_DIR = PROJECT_ROOT / "dashboard" / "data"
BIND = "127.0.0.1"
PORT = 8899

# Must happen before any Atlas module imports
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("chat_server")

# Rate limiting for expensive endpoints
_last_evaluate_time = 0.0

# Thread pool for blocking broker / DB I/O
_thread_pool = ThreadPoolExecutor(max_workers=4)

# ── Credentials ───────────────────────────────────────────────────────────────


def _load_credentials() -> tuple[str, str]:
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


try:
    EXPECTED_USER, EXPECTED_PASS = _load_credentials()
except ValueError as e:
    print(f"❌ {e}", file=sys.stderr)
    EXPECTED_USER = ""
    EXPECTED_PASS = ""

security = HTTPBasic()


def verify_credentials(
    credentials: HTTPBasicCredentials = Depends(security),
) -> HTTPBasicCredentials:
    """Timing-safe HTTP Basic Auth check."""
    user_ok = secrets.compare_digest(
        credentials.username.encode(), EXPECTED_USER.encode()
    )
    pw_ok = secrets.compare_digest(
        credentials.password.encode(), EXPECTED_PASS.encode()
    )
    if not (user_ok and pw_ok):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": 'Basic realm="Atlas Dashboard"'},
        )
    return credentials


# ── Plan helpers (ported verbatim from dashboard_server.py) ──────────────────


def _approve_and_execute(trade_date: str, market_id: str) -> dict:
    """Approve a plan and execute it. Returns result dict."""
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)

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
    """Execute via live broker."""
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
    """Reject a plan (mark as REJECTED, don't execute)."""
    sys.path.insert(0, str(PROJECT_ROOT))
    os.chdir(PROJECT_ROOT)

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


def _build_dashboard_data() -> dict:
    """Build the complete dashboard data payload from SQLite + broker.

    Copied verbatim from dashboard_server.py _build_dashboard_data().
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
            except Exception:
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

            # 1. Enrich positions with Atlas trade metadata
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
    except Exception:
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
    except Exception:
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
            spy_return = (
                (spy_data[-1]["close"] / spy_data[0]["close"]) - 1
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

    # ── 5. Enrich summary with today_pnl + max_positions ──────────────────────
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


# ── Lifespan (startup / shutdown) ─────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: attempt to start Alpaca background poller for SSE streaming
    # alpaca_stream retired in Phase 5 — skipped gracefully if module missing
    try:
        from dashboard.alpaca_stream import start as start_stream
        start_stream(interval_open=10, interval_closed=60)
        print("Alpaca live poller started", flush=True)
    except ImportError:
        print(
            "ℹ️  alpaca_stream retired — SSE stream and snapshot endpoints will return 503",
            flush=True,
        )
    except Exception as e:
        print(f"⚠️ Alpaca poller failed to start: {e}", flush=True)
        print("  Dashboard will serve static JSON only", flush=True)
    yield
    # Shutdown: nothing special needed


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Atlas Dashboard",
    description="Atlas trading system dashboard API",
    lifespan=lifespan,
)


# ── Cache-control middleware for static files ─────────────────────────────────


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    """Add appropriate Cache-Control headers based on file extension."""
    response = await call_next(request)
    path = request.url.path.split("?")[0]
    # Only modify static-file paths (not API routes)
    if not path.startswith("/api/"):
        if path.endswith(".json"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
        elif path.endswith(".html") or path in ("/", ""):
            response.headers["Cache-Control"] = "no-cache"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ── GET routes ────────────────────────────────────────────────────────────────


@app.get("/api/stream")
async def sse_stream(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/stream — Server-Sent Events stream of live Alpaca data.

    alpaca_stream module retired in Phase 5. Returns 503 until a
    replacement real-time feed is wired in.
    """
    try:
        from dashboard.alpaca_stream import get_state, get_seq
    except ImportError:
        return JSONResponse(
            status_code=503,
            content={"error": "live stream unavailable (alpaca_stream retired)"},
        )

    async def event_generator():
        last_seq = -1
        try:
            while True:
                current_seq = get_seq()
                if current_seq != last_seq:
                    state = get_state()
                    last_seq = current_seq
                    payload = json.dumps(state, default=str)
                    yield f"event: snapshot\ndata: {payload}\n\n"
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("SSE stream error: %s", e)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/snapshot")
async def snapshot(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/snapshot — One-shot JSON of current Alpaca state.

    alpaca_stream module retired in Phase 5. Returns 503 until replaced.
    """
    try:
        from dashboard.alpaca_stream import get_state
    except ImportError:
        return JSONResponse(
            status_code=503,
            content={"error": "snapshot unavailable (alpaca_stream retired)"},
        )
    try:
        state = get_state()
        return Response(
            content=json.dumps(state, default=str),
            media_type="application/json",
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/prices")
async def prices(
    tickers: str = Query(default=""),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/prices — pre-computed P&L for open positions.

    live_prices module retired in Phase 5. Returns 503 until replaced.

    Query params:
      ?tickers=AAPL,REH.AX  — legacy: fall back to raw quotes
    """
    try:
        if tickers:
            # Legacy mode: specific tickers requested → return raw quotes
            try:
                from dashboard.live_prices import fetch_prices, get_cache_stats
            except ImportError:
                return JSONResponse(
                    status_code=503,
                    content={"error": "live prices unavailable (live_prices retired)"},
                )
            ticker_list = [t.strip() for t in tickers.split(",") if t.strip()]
            quotes = fetch_prices(ticker_list)
            response_data = {
                "ok": True,
                "timestamp": datetime.now().isoformat(),
                "quotes": quotes,
                "cache": get_cache_stats(),
                "ticker_count": len(quotes),
            }
        else:
            # New mode: pre-computed P&L for all positions
            try:
                from dashboard.live_prices import get_live_prices_with_pnl
            except ImportError:
                return JSONResponse(
                    status_code=503,
                    content={"error": "live prices unavailable (live_prices retired)"},
                )
            simple_path = str(SERVE_DIR / "simple-dashboard-data.json")
            response_data = get_live_prices_with_pnl(simple_path)

        return Response(
            content=json.dumps(response_data, default=str),
            media_type="application/json",
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/monitor")
async def monitor_get(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/monitor — returns 410 (Monitor tab removed)."""
    return JSONResponse(status_code=410, content={"error": "Monitor tab removed"})


@app.get("/api/monitor/{path:path}")
async def monitor_get_sub(
    path: str,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/monitor/* — returns 410 (Monitor tab removed)."""
    return JSONResponse(status_code=410, content={"error": "Monitor tab removed"})


@app.get("/api/portfolio")
@app.get("/api/db/portfolio")
async def db_portfolio(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/portfolio or /api/db/portfolio — positions + equity from SQLite."""
    try:
        from db import atlas_db
        positions = atlas_db.get_open_positions()
        regime = atlas_db.get_current_regime()
        with atlas_db.get_db() as db:
            row = db.execute(
                "SELECT * FROM equity_curve ORDER BY date DESC LIMIT 1"
            ).fetchone()
            equity = dict(row) if row else None
        return JSONResponse(
            content={
                "positions": positions,
                "regime": regime,
                "equity": equity,
            }
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/trades")
@app.get("/api/db/trades")
async def db_trades(
    days: int = Query(default=0),
    strategy: Optional[str] = Query(default=None),
    universe: Optional[str] = Query(default=None),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/trades or /api/db/trades — closed trades from SQLite.

    Query params:
      ?days=30&strategy=mean_reversion&universe=sp500
    """
    try:
        from db import atlas_db
        days_val = days or None
        trades = atlas_db.get_closed_trades(
            days=days_val, strategy=strategy, universe=universe
        )
        return JSONResponse(content={"trades": trades, "count": len(trades)})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/performance")
@app.get("/api/db/performance")
async def db_performance(
    days: int = Query(default=0),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/performance or /api/db/performance — performance summary."""
    try:
        from db import atlas_db
        days_val = days or None
        summary = atlas_db.performance_summary(days=days_val)
        return JSONResponse(content=summary)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/equity-curve")
async def equity_curve(
    market: str = Query(default="sp500"),
    days: int = Query(default=90),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/equity-curve?market=sp500&days=90 — equity history from SQLite."""
    try:
        from db.atlas_db import get_equity_curve
        rows = get_equity_curve(market_id=market, days=days)
        # get_equity_curve returns oldest-first; reverse so most recent is first
        rows.reverse()
        return JSONResponse(content=rows)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/regime/history")
async def regime_history(
    days: int = Query(default=90),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/regime/history?days=90 — regime classification history."""
    try:
        from db.atlas_db import get_regime_history
        rows = get_regime_history(days=days)
        # get_regime_history returns most-recent-first with JSON decoded
        return JSONResponse(content=rows)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/regime/current")
async def regime_current(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/regime/current — most recent regime state."""
    try:
        from db.atlas_db import get_current_regime
        regime = get_current_regime()
        if regime:
            return JSONResponse(content=regime)
        return JSONResponse(content={"regime_state": "unknown"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/overlay/decisions")
async def overlay_decisions(
    days: int = Query(default=30),
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """GET /api/overlay/decisions?days=30 — AI overlay decisions."""
    try:
        from db.atlas_db import get_overlay_decisions
        decisions = get_overlay_decisions(days=days)
        return JSONResponse(content=decisions)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/system/health")
async def system_health(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/system/health — heartbeat status for all services."""
    try:
        from db.atlas_db import get_heartbeats
        heartbeats = get_heartbeats()
        return JSONResponse(
            content={
                "heartbeats": heartbeats,
                "timestamp": datetime.now().isoformat(),
            }
        )
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/dashboard-data")
async def dashboard_data(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """GET /api/dashboard-data — complete dashboard payload.

    Replaces static simple-dashboard-data.json.
    Uses default=str to handle enum values from broker dataclasses.
    Runs in thread pool to avoid blocking the event loop during broker I/O.
    """
    try:
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(_thread_pool, _build_dashboard_data)
        return Response(
            content=json.dumps(data, default=str),
            media_type="application/json",
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})


# ── POST routes ───────────────────────────────────────────────────────────────


@app.post("/api/approve")
async def approve(
    request: Request,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """POST /api/approve — approve + execute trade plan.

    Body: {"trade_date": "YYYY-MM-DD", "market_id": "sp500"}
    Runs in thread pool with 60-second timeout for broker I/O.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    trade_date = body.get("trade_date", "")
    market_id = body.get("market_id", "")
    if not trade_date or not market_id:
        return JSONResponse(
            status_code=400,
            content={"error": "trade_date and market_id required"},
        )

    loop = asyncio.get_running_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                _thread_pool,
                lambda: _approve_and_execute(trade_date, market_id),
            ),
            timeout=60.0,
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "Execution timed out (still running in background)"},
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

    return Response(
        content=json.dumps(result, default=str),
        media_type="application/json",
        status_code=200 if result.get("ok") else 400,
    )


@app.post("/api/reject")
async def reject(
    request: Request,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """POST /api/reject — reject trade plan.

    Body: {"trade_date": "YYYY-MM-DD", "market_id": "sp500"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON body"})

    trade_date = body.get("trade_date", "")
    market_id = body.get("market_id", "")
    if not trade_date or not market_id:
        return JSONResponse(
            status_code=400,
            content={"error": "trade_date and market_id required"},
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            _thread_pool,
            lambda: _reject_plan(trade_date, market_id),
        )
    except Exception as e:
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

    return JSONResponse(
        status_code=200 if result.get("ok") else 400,
        content=result,
    )


@app.post("/api/monitor")
async def monitor_post_root(_: HTTPBasicCredentials = Depends(verify_credentials)):
    """POST /api/monitor — returns 410 (Monitor tab removed)."""
    return JSONResponse(status_code=410, content={"error": "Monitor tab removed"})


@app.post("/api/monitor/{path:path}")
async def monitor_post_sub(
    path: str,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """POST /api/monitor/* — returns 410 (Monitor tab removed)."""
    return JSONResponse(status_code=410, content={"error": "Monitor tab removed"})


# ── DELETE routes ─────────────────────────────────────────────────────────────


@app.delete("/api/monitor/positions/{pos_id}")
async def delete_monitor_position(
    pos_id: str,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """DELETE /api/monitor/positions/{id} — delete a monitor position."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_position(pos_id)
    return JSONResponse(status_code=200 if ok else 404, content={"ok": ok})


@app.delete("/api/monitor/templates/{tmpl_id}")
async def delete_monitor_template(
    tmpl_id: str,
    _: HTTPBasicCredentials = Depends(verify_credentials),
):
    """DELETE /api/monitor/templates/{id} — delete a monitor template."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_template(tmpl_id)
    return JSONResponse(status_code=200 if ok else 404, content={"ok": ok})


# ── Static file serving — MUST be mounted last so API routes take priority ────

if SERVE_DIR.exists():
    app.mount(
        "/",
        StaticFiles(directory=str(SERVE_DIR), html=True),
        name="static",
    )
else:
    logger.warning("Static files directory not found: %s", SERVE_DIR)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
