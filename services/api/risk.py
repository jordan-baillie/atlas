"""Risk and signals API routes.

Phase 5 extraction from services/chat_server.py.

Routes:
  GET  /api/positions/risk         — position risk decomposition
  GET  /api/signals/ev             — strategy expected value scoring
  GET  /api/risk/ruin              — portfolio ruin probability
  GET  /api/signals/vix_term_structure — VIX/VIX3M ratio signal
  POST /api/risk/ruin/refresh      — trigger background ruin recompute
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(tags=["risk"])
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path("/root/atlas")

def _build_positions_array() -> list[dict]:
    """Build per-position risk decomposition for live cache-hit responses.

    Reads open trades from SQLite (superseded=0), enriches with the current
    broker price from the dashboard cache (best-effort), and computes per-position
    metrics.  Returns [] on any failure — degraded UX is better than a 500 error.
    """
    try:
        from db.atlas_db import get_db
        with get_db() as _db:
            rows = _db.execute(
                "SELECT id, ticker, strategy, universe, entry_price, shares, "
                "stop_price, take_profit, entry_date, stop_order_id, tp_order_id "
                "FROM trades WHERE status='open' AND superseded=0"
            ).fetchall()
        if not rows:
            return []

        # Best-effort live price lookup via dashboard cache
        live_prices: dict[str, float] = {}
        try:
            from services.api.dashboard import _build_dashboard_data
            _dd = _build_dashboard_data()
            for _pos in (_dd.get("positions") or []):
                _t = _pos.get("ticker")
                _cp = _pos.get("current_price")
                if _t and _cp is not None:
                    live_prices[_t] = float(_cp)
        except Exception as _live_exc:
            logger.debug("_build_positions_array: live price fetch failed: %s", _live_exc)

        positions = []
        for _r in rows:
            _rd = dict(_r)
            _ticker = _rd["ticker"]
            _entry = float(_rd["entry_price"] or 0.0)
            _shares = int(_rd["shares"] or 0)
            _stop = _rd.get("stop_price")
            _tp = _rd.get("take_profit")
            _cur = live_prices.get(_ticker, _entry)
            _mv = _cur * _shares
            _unreal = (_cur - _entry) * _shares
            _risk_to_stop = None
            if _stop is not None:
                _risk_to_stop = round((_cur - float(_stop)) * _shares, 2)
            positions.append({
                "ticker": _ticker,
                "strategy": _rd["strategy"],
                "universe": _rd["universe"],
                "entry_price": _entry,
                "current_price": _cur,
                "shares": _shares,
                "market_value": round(_mv, 2),
                "stop_price": _stop,
                "take_profit": _tp,
                "unrealized_pnl": round(_unreal, 2),
                "risk_to_stop": _risk_to_stop,
                "entry_date": _rd["entry_date"],
                "stop_order_id": _rd.get("stop_order_id") or None,
                "tp_order_id": _rd.get("tp_order_id") or None,
            })
        return positions
    except Exception as _exc:
        logger.warning("_build_positions_array failed: %s", _exc)
        return []


# ── GET /api/positions/risk ───────────────────────────────────────────────────

@router.get("/api/positions/risk")
def positions_risk(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/positions/risk — position risk decomposition."""
    import subprocess
    # === RISK CACHE (P2.7) — serve from cache, trigger bg refresh if stale ===
    try:
        from db.atlas_db import get_cached_portfolio_risk
        _cached_pr = get_cached_portfolio_risk(max_age_hours=24)
        if _cached_pr and not _cached_pr.get("stale"):
            # Audit F-01: override equity from market_equity_history (correct source)
            from db.atlas_db import get_db as _get_db
            with _get_db() as _dbx:
                _r = _dbx.execute(
                    "SELECT broker_equity FROM market_equity_history "
                    "WHERE market_id='sp500' "
                    "ORDER BY date DESC, snapshot_time DESC LIMIT 1"
                ).fetchone()
                _live_eq = float(_r[0]) if _r else float(_cached_pr.get("equity", 0))
            return JSONResponse({
                "positions": _build_positions_array(),
                "summary": {
                    "equity": _live_eq,
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
            try:
                subprocess.Popen(
                    [sys.executable, "scripts/precompute_risk.py", "--target=risk"],
                    cwd=str(_PROJECT_ROOT),
                    stdout=open("logs/risk_precompute.log", "a"),
                    stderr=subprocess.STDOUT,
                )
            except Exception as _pe:
                logger.warning("positions_risk: bg refresh failed to start: %s", _pe)
            # Audit F-01: override equity from market_equity_history (correct source)
            from db.atlas_db import get_db as _get_db
            with _get_db() as _dbx:
                _r = _dbx.execute(
                    "SELECT broker_equity FROM market_equity_history "
                    "WHERE market_id='sp500' "
                    "ORDER BY date DESC, snapshot_time DESC LIMIT 1"
                ).fetchone()
                _live_eq = float(_r[0]) if _r else float(_cached_pr.get("equity", 0))
            return JSONResponse({
                "positions": _build_positions_array(),
                "summary": {
                    "equity": _live_eq,
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

        # Build vol_cones map (ticker -> vol cone data) from per-position data
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

        # Add ruin probability summary
        ruin_summary = None
        try:
            from risk.ruin_probability import (
                get_latest_ruin_probability,
                compute_for_current_portfolio,
                persist_ruin_probability,
            )
            ruin_summary = get_latest_ruin_probability() or None
            if not ruin_summary:
                ruin_result = compute_for_current_portfolio(floor_pct=0.70)
                if ruin_result.get("status") == "ok":
                    persist_ruin_probability(ruin_result)
                    ruin_summary = get_latest_ruin_probability() or None
        except Exception as e:
            logger.warning("Ruin probability failed: %s", e)
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

@router.get("/api/signals/ev")
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
        from analytics.strategy_ev import compute_all_strategies_ev, persist_strategy_ev
        results = compute_all_strategies_ev(min_trades=3)
        try:
            persist_strategy_ev(results)
        except Exception as e:
            logger.warning("persist_strategy_ev failed: %s", e)
        return {"strategies": results, "source": "live"}
    except Exception as e:
        logger.exception("signals_ev failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /api/risk/ruin ────────────────────────────────────────────────────────

@router.get("/api/risk/ruin")
def risk_ruin(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/risk/ruin — portfolio probability of ruin.

    P2.8: response always includes ``stale`` (bool) and ``reason`` (str|None).
    """
    try:
        import json as _json
        # P2.8: use cache helper which handles portfolio-change detection
        from db.atlas_db import get_cached_ruin_probability
        cached = get_cached_ruin_probability(max_age_hours=24)
        if cached:
            cached.setdefault("status", "ok")
            cached.setdefault("prob", cached.get("horizons", {}).get("30d", {}).get("prob_ruin", 0.0))
            return cached

        # No cache or stale by age — run live compute
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
            # Audit F-01: override current_equity from market_equity_history (correct source)
            try:
                from db.atlas_db import get_db as _ruin_db
                with _ruin_db() as _rdb:
                    _rr = _rdb.execute(
                        "SELECT broker_equity FROM market_equity_history "
                        "WHERE market_id='sp500' "
                        "ORDER BY date DESC, snapshot_time DESC LIMIT 1"
                    ).fetchone()
                    if _rr:
                        current_equity = float(_rr[0])
            except Exception as _re:
                logger.debug("risk_ruin: market_equity_history lookup failed: %s", _re)
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


# ── GET /api/signals/vix_term_structure ───────────────────────────────────────

@router.get("/api/signals/vix_term_structure")
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


# ── POST /api/risk/ruin/refresh ────────────────────────────────────────────────

@router.post("/api/risk/ruin/refresh")
def risk_ruin_refresh(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """POST /api/risk/ruin/refresh — trigger a non-blocking background ruin recompute."""
    import subprocess as _sr
    from datetime import datetime, timezone
    try:
        started_at = datetime.now(timezone.utc).isoformat()
        _sr.Popen(
            [sys.executable, "scripts/precompute_risk.py", "--target=ruin"],
            cwd=str(_PROJECT_ROOT),
            stdout=open("logs/risk_precompute.log", "a"),
            stderr=_sr.STDOUT,
        )
        return JSONResponse({"ok": True, "started_at": started_at})
    except Exception as e:
        logger.exception("risk_ruin_refresh failed")
        raise HTTPException(status_code=500, detail=str(e))
