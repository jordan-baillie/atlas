"""System health and macro gauges API routes.

Phase 4 extraction from services/chat_server.py.

Routes:
  GET /api/system/health           — comprehensive system health
  GET /api/system/health/universes — per-universe status
  GET /api/macro/gauges            — macro indicator gauges + sparklines
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path("/root/atlas")


# ── GET /api/system/health ────────────────────────────────────────────────────

@router.get("/api/system/health")
def system_health(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/system/health — comprehensive system health."""
    try:
        from db.atlas_db import get_heartbeats, get_db
        from datetime import datetime

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

        # P4.2 — universe health surface (batch DB query to avoid N+1 connections)
        universes_data = []
        try:
            import json as _uj
            from db.atlas_db import get_db as _ugh_db, get_latest_equity as _ugh_eq

            # Read all universe configs first (file I/O only, no DB yet)
            _universe_cfgs: list = []
            for _cfg_path in sorted(Path("config/active").glob("*.json")):
                if _cfg_path.stem == "regime":
                    continue
                try:
                    _cfg = _uj.loads(_cfg_path.read_text())
                    _universe_cfgs.append((_cfg_path, _cfg))
                except Exception as _ue:
                    logger.debug("universes: error reading %s: %s", _cfg_path.name, _ue)

            _market_ids = [_c.get("market", _cp.stem) for _cp, _c in _universe_cfgs]

            # ONE connection, batch query for open-position counts across all universes
            _open_pos_by_market: dict = {}
            if _market_ids:
                _ph = ",".join("?" * len(_market_ids))
                with _ugh_db() as _db:
                    _op_rows = _db.execute(
                        f"SELECT universe, COUNT(*) AS n FROM trades "
                        f"WHERE exit_date IS NULL AND universe IN ({_ph}) "
                        f"GROUP BY universe",
                        _market_ids,
                    ).fetchall()
                    for _r in _op_rows:
                        _open_pos_by_market[_r["universe"]] = _r["n"]

            for _cfg_path, _cfg in _universe_cfgs:
                try:
                    _mid = _cfg.get("market", _cfg_path.stem)
                    _mode = _cfg.get("trading", {}).get("mode", "unknown")
                    _approval = bool(_cfg.get("trading", {}).get("live_enabled", False))
                    _starting_eq = _cfg.get("risk", {}).get("starting_equity")
                    _open_pos = _open_pos_by_market.get(_mid, 0)
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

@router.get("/api/macro/gauges")
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


# ── GET /api/system/health/universes ─────────────────────────────────────────

def _build_universes_list() -> list:
    """Build the universe health list from config/active/*.json + SQLite.

    Uses a single batched COUNT query across all universes instead of
    opening one DB connection per universe (eliminates N+1 pattern).
    """
    import json as _uj
    from db.atlas_db import get_db as _udb, get_latest_equity as _ueq

    # Read all configs (file I/O only)
    universe_cfgs: list = []
    for cfg_path in sorted(Path("config/active").glob("*.json")):
        if cfg_path.stem == "regime":
            continue
        try:
            cfg = _uj.loads(cfg_path.read_text())
            universe_cfgs.append((cfg_path, cfg))
        except Exception as ue:
            logger.debug("universes: error reading %s: %s", cfg_path.name, ue)

    market_ids = [cfg.get("market", p.stem) for p, cfg in universe_cfgs]

    # ONE connection for all open-position counts
    open_pos_by_market: dict = {}
    if market_ids:
        ph = ",".join("?" * len(market_ids))
        try:
            with _udb() as db:
                rows = db.execute(
                    f"SELECT universe, COUNT(*) AS n FROM trades "
                    f"WHERE exit_date IS NULL AND universe IN ({ph}) "
                    f"GROUP BY universe",
                    market_ids,
                ).fetchall()
                for r in rows:
                    open_pos_by_market[r["universe"]] = r["n"]
        except Exception as db_exc:
            logger.debug("universes batch open-pos query failed: %s", db_exc)

    universes = []
    for cfg_path, cfg in universe_cfgs:
        try:
            market_id = cfg.get("market", cfg_path.stem)
            mode = cfg.get("trading", {}).get("mode", "unknown")
            approval = bool(cfg.get("trading", {}).get("live_enabled", False))
            starting_equity = cfg.get("risk", {}).get("starting_equity")
            open_positions = open_pos_by_market.get(market_id, 0)
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


@router.get("/api/system/health/universes")
def system_health_universes(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/system/health/universes — per-universe status from config/active/*.json.

    Returns live/passive/paper mode, approval flag, open position count, and
    equity for every configured market universe.
    """
    try:
        universes = _build_universes_list()
        return JSONResponse({"universes": universes})
    except Exception as e:
        logger.exception("system_health_universes failed")
        raise HTTPException(status_code=500, detail=str(e))
