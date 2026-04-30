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

# ── Dashboard response cache (30-second TTL) ──────────────────────────────────
_DASHBOARD_CACHE: dict = {"ts": 0.0, "data": None}
_DASHBOARD_CACHE_TTL = 30.0  # seconds


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
        except (ValueError, TypeError, KeyError, IndexError) as e:  # float/index errors in PnL calc
            logger.debug("per-position PnL calc failed for %s: %s", ticker, e)
            continue

    result["total_pnl"] = round(result["total_pnl"], 2)
    return result


# ── Portfolio history helper ──────────────────────────────────────────────────

def _get_portfolio_history(broker) -> list:
    """Fetch Alpaca portfolio history (1 year, daily) as a list of dicts.

    Returns [] on any error so the caller can fall back to the SQLite
    equity_curve SUM aggregate.
    """
    try:
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        from datetime import datetime as _dt_ph, timezone as _tz_ph

        _ph_req = GetPortfolioHistoryRequest(period="1A", timeframe="1D")
        _ph_resp = broker._broker_call(
            broker._trade_client.get_portfolio_history, _ph_req
        )
        _ts_list = list(getattr(_ph_resp, "timestamp", []) or [])
        _eq_list = list(getattr(_ph_resp, "equity", []) or [])
        _pl_list = list(getattr(_ph_resp, "profit_loss", []) or [])
        rows: list = []
        for _i, (_ts, _eq) in enumerate(zip(_ts_list, _eq_list)):
            if _eq is None or _eq <= 0:
                continue  # skip pre-funding zero days
            _date_str = _dt_ph.fromtimestamp(int(_ts), tz=_tz_ph.utc).strftime(
                "%Y-%m-%d"
            )
            _day_pnl = (
                float(_pl_list[_i])
                if _i < len(_pl_list) and _pl_list[_i] is not None
                else 0.0
            )
            rows.append(
                {
                    "date": _date_str,
                    "equity": round(float(_eq), 2),
                    "value": round(float(_eq), 2),
                    "day_pnl": round(_day_pnl, 2),
                }
            )
        return rows
    except Exception as _ph_err:  # noqa: BLE001
        logger.warning(
            "Alpaca portfolio_history fetch failed, will use SQLite fallback: %s",
            _ph_err,
        )
        return []


# ── Dashboard data builder ────────────────────────────────────────────────────

def _build_dashboard_data() -> dict:
    """Build the complete dashboard data payload from SQLite + live broker.

    Exact port of AuthHandler._build_dashboard_data() (lines 543-744 of
    dashboard_server.py).  Returns a dict that is serialised with
    json.dumps(..., default=str) to handle enum/datetime values.
    """
    import time as _time

    # ── 30-second in-process cache ────────────────────────────────────────────
    _now = _time.monotonic()
    if (
        _DASHBOARD_CACHE["data"] is not None
        and (_now - _DASHBOARD_CACHE["ts"]) < _DASHBOARD_CACHE_TTL
    ):
        return _DASHBOARD_CACHE["data"]  # type: ignore[return-value]

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
    broker = None
    clock = None
    portfolio_history_raw: list = []
    try:
        from brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if broker and broker.connect():
            # ── Parallelize all independent broker RPCs ───────────────────────
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=8) as _exec:
                _f_account = _exec.submit(broker.get_account_info)
                _f_positions = _exec.submit(broker.get_positions)
                _f_orders = _exec.submit(broker.get_history_orders, 7)
                _f_raw_acct = _exec.submit(
                    broker._broker_call, broker._trade_client.get_account
                )
                _f_raw_pos = _exec.submit(
                    broker._broker_call, broker._trade_client.get_all_positions
                )
                _f_open = _exec.submit(broker.get_open_orders)
                _f_clock = _exec.submit(
                    broker._broker_call, broker._trade_client.get_clock
                )
                _f_phist = _exec.submit(_get_portfolio_history, broker)

                # Required — propagate failure to outer try/except
                account_info = _f_account.result()
                positions_info = _f_positions.result()
                orders_info = _f_orders.result()

                # Optional — individual fallbacks
                try:
                    raw_acct_result = _f_raw_acct.result()
                except Exception as _e:  # noqa: BLE001
                    raw_acct_result = None
                    logger.debug("raw account fetch failed: %s", _e)
                try:
                    raw_positions_result = _f_raw_pos.result()
                except Exception as _e:  # noqa: BLE001
                    raw_positions_result = None
                    logger.debug("raw positions fetch failed: %s", _e)
                try:
                    open_orders_result = _f_open.result()
                except Exception as _e:  # noqa: BLE001
                    open_orders_result = []
                    logger.debug("open orders fetch failed: %s", _e)
                try:
                    clock = _f_clock.result()
                except Exception as _e:  # noqa: BLE001
                    clock = None
                    logger.debug("clock fetch failed: %s", _e)
                portfolio_history_raw = _f_phist.result()  # always returns list

            account = dataclasses.asdict(account_info)

            # 1a. Margin usage from raw Alpaca account (already fetched in parallel)
            try:
                initial_margin = float(
                    getattr(raw_acct_result, "initial_margin", 0) or 0
                )
                equity_val = float(getattr(raw_acct_result, "equity", 0) or 0)
                account["margin_usage_pct"] = (
                    round(initial_margin / equity_val * 100, 2) if equity_val > 0 else 0
                )
            except Exception as e:  # noqa: BLE001 — broker RPC can raise any SDK exception
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

            # 1b. Enrich with Alpaca intraday fields (already fetched in parallel)
            try:
                alpaca_by_symbol: dict = {}
                for rp in raw_positions_result or []:
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
            except Exception as e:  # noqa: BLE001 — broker call + attr access can raise any exception
                logger.warning("Intraday enrichment failed: %s", e)

            # 1c. Override stop_price with broker's authoritative open-order value
            try:
                _stop_map: dict[str, list[float]] = {}
                for od in open_orders_result:
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
            except Exception as _stop_err:  # noqa: BLE001 — get_open_orders() can raise any exception
                logger.warning("Broker stop_price override failed: %s", _stop_err)

            # ── Bug 1 fix: Aggregate starting_equity across ALL enabled markets ─
            # Alpaca is a single account; starting_equity is per-market in config.
            import glob as _glob
            _total_starting = 0.0
            for _f in sorted(
                _glob.glob(str(_PROJECT_ROOT / "config" / "active" / "*.json"))
            ):
                try:
                    with open(_f) as _fh:
                        _cfg = json.load(_fh)
                    _se = float(_cfg.get("risk", {}).get("starting_equity", 0) or 0)
                    if _se > 0:
                        _total_starting += _se
                except (OSError, json.JSONDecodeError, ValueError) as _e:
                    logger.debug(
                        "Could not read starting_equity from %s: %s", _f, _e
                    )
                    continue
            _equity = float(account.get("equity", 0) or 0)
            if _total_starting > 0:
                account["total_pnl"] = round(_equity - _total_starting, 2)
                account["total_pnl_pct"] = round(
                    (_equity - _total_starting) / _total_starting * 100, 2
                )
            account["starting_equity_total"] = round(_total_starting, 2)

            result["account"] = account
            result["positions"] = positions
            result["recent_orders"] = orders
            result["summary"] = {
                "equity": account.get("equity", 0),
                "total_pnl": account.get("total_pnl", 0),
                "total_pnl_pct": account.get("total_pnl_pct", 0),
                "open_positions": len(positions),
            }
    except Exception as e:  # noqa: BLE001 — full broker init+connect can raise any SDK exception
        logger.warning("Alpaca account data fetch failed: %s", e)
        result["account"] = {}
        result["positions"] = []
        result["recent_orders"] = []
        result["summary"] = {}

    # ── 2. Market clock — reuse existing broker (no second AlpacaBroker) ──────
    try:
        if broker is not None and clock is not None:
            result["market_clock"] = {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
                "timestamp": str(clock.timestamp),
            }
        elif broker is not None:
            # broker connected but clock was not fetched in parallel — retry once
            _retry_clock = broker._broker_call(broker._trade_client.get_clock)
            result["market_clock"] = {
                "is_open": _retry_clock.is_open,
                "next_open": str(_retry_clock.next_open),
                "next_close": str(_retry_clock.next_close),
                "timestamp": str(_retry_clock.timestamp),
            }
        else:
            result["market_clock"] = {"is_open": False}
    except Exception as e:  # noqa: BLE001 — broker clock call can raise any SDK exception
        logger.warning("Market clock fetch failed: %s", e)
        result["market_clock"] = {"is_open": False}

    # ── Equity curve: Alpaca portfolio_history (single-account source of truth) ─
    # The per-market equity_curve table has discontinuities from the 2026-04-29
    # per-market attribution refactor. Alpaca's portfolio_history is the single
    # source of truth for the WHOLE account equity over time.
    portfolio_history: list = portfolio_history_raw  # from parallel fetch above
    if not portfolio_history:
        # Fallback: SUM across markets per date (better than single market_id slice)
        try:
            with get_db() as db:
                _eq_rows = db.execute(
                    "SELECT date, SUM(equity) AS equity, SUM(day_pnl) AS day_pnl "
                    "FROM equity_curve GROUP BY date ORDER BY date"
                ).fetchall()
                portfolio_history = [
                    {
                        "date": r["date"],
                        "equity": r["equity"],
                        "value": r["equity"],
                        "day_pnl": r["day_pnl"] or 0,
                    }
                    for r in _eq_rows
                ]
        except Exception as _fb_err:  # noqa: BLE001
            logger.warning("Equity curve SQLite fallback failed: %s", _fb_err)
            portfolio_history = []

    # Append/update today's live equity if not already in the series
    live_equity = round(
        float((result.get("account") or {}).get("equity", 0) or 0), 2
    )
    if portfolio_history and live_equity:
        from datetime import datetime as _dt
        today_str = _dt.now().strftime("%Y-%m-%d")
        last_row = portfolio_history[-1]
        if last_row.get("date") == today_str:
            # Today's row exists — update it with live equity
            if abs((last_row.get("equity") or 0) - live_equity) > 0.01:
                last_row["equity"] = live_equity
                last_row["value"] = live_equity
            last_row["day_pnl"] = (result.get("summary") or {}).get("today_pnl", 0)
        else:
            market_clock = result.get("market_clock") or {}
            is_trading_day = (
                market_clock.get("is_open", False) or _dt.now().weekday() < 5
            )
            if is_trading_day:
                portfolio_history.append(
                    {
                        "date": today_str,
                        "equity": live_equity,
                        "value": live_equity,
                        "day_pnl": (result.get("summary") or {}).get("today_pnl", 0),
                    }
                )
            else:
                last_row["equity"] = live_equity
                last_row["value"] = live_equity
    result["portfolio_history"] = portfolio_history

    # ── Strategy performance, benchmark (SQLite) ──────────────────────────────
    with get_db() as db:
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
        except Exception as e:  # noqa: BLE001 — optional stats injection must not crash dashboard
            logger.warning("EV stats failed: %s", e, exc_info=True)
            data["ev_stats"] = {}
        body = json.dumps(data, default=str)
        return Response(content=body, media_type="application/json")
    except Exception as e:  # noqa: BLE001 — HTTP handler catch-all; converts unknown exceptions to 500
        logger.exception("Failed to build dashboard data")
        raise HTTPException(status_code=500, detail=str(e))
