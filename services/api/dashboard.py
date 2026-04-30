"""Dashboard data builder and /api/dashboard-data route.

Extracted from services/chat_server.py (Phase 8 decomposition).

Routes:
  GET /api/dashboard-data  — main dashboard payload (broker + SQLite)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(tags=["dashboard"])
logger = logging.getLogger("chat_server.dashboard")

_PROJECT_ROOT = Path("/root/atlas")


# ── PnL helpers ───────────────────────────────────────────────────────────────

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

    cache_dir = _PROJECT_ROOT / "data" / "cache" / market_id
    result: dict = {"per_position": {}, "total_pnl": 0.0}

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


# ── Dashboard data builder ────────────────────────────────────────────────────

def _build_dashboard_data() -> dict:
    """Build the complete dashboard data payload from SQLite + live broker.

    Exact port of AuthHandler._build_dashboard_data() (lines 543-744 of
    dashboard_server.py).  Returns a dict that is serialised with
    json.dumps(..., default=str) to handle enum/datetime values.
    """
    import dataclasses
    from db.atlas_db import get_db

    config_path = _PROJECT_ROOT / "config" / "active" / "sp500.json"
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
            "   AND (superseded=0 OR superseded IS NULL)"  # -- exclude dup rows
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
            "   AND (superseded=0 OR superseded IS NULL)"  # -- exclude dup rows
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


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/api/dashboard-data")
def dashboard_data(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/dashboard-data — main dashboard payload (replaces static JSON).

    Uses json.dumps(..., default=str) to handle enum/datetime values from
    broker dataclasses, exactly as the original handler does.
    """
    try:
        from signals.ev_scorer import (
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
        except Exception as e:
            logger.warning("EV stats failed: %s", e)
            data["ev_stats"] = {}
        body = json.dumps(data, default=str)
        return Response(content=body, media_type="application/json")
    except Exception as e:
        logger.exception("Failed to build dashboard data")
        raise HTTPException(status_code=500, detail=str(e))
