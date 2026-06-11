"""System health and macro gauges API routes.

Phase 4 extraction from services/chat_server.py.

Routes:
  GET /api/system/health           — comprehensive system health
  GET /api/system/health/universes — per-universe status
  GET /api/macro/gauges            — macro indicator gauges + sparklines
"""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth
from atlas.kernel.paths import CONFIG_DIR, PROJECT_ROOT

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


def _load_auto_excluded() -> list[str]:
    """Load the list of auto-excluded tickers from config/auto_excluded_tickers.json.

    Supports two JSON shapes:
    - ``{"excluded": {"TICK": {...}, ...}}``  — keys of the inner dict (current format)
    - ``{"tickers": ["TICK", ...]}``           — explicit list under "tickers" key
    - ``["TICK", ...]``                        — bare list at root

    Returns an empty list on any error (graceful degradation).
    """
    try:
        path = CONFIG_DIR / "auto_excluded_tickers.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return [str(t) for t in data]
        if isinstance(data, dict):
            excl = data.get("excluded", data.get("tickers", []))
            if isinstance(excl, dict):
                # Current format: {"excluded": {"TICK": {...metadata...}, ...}}
                return list(excl.keys())
            if isinstance(excl, list):
                return [str(t) for t in excl]
    except Exception as e:
        logger.warning("Could not load auto_excluded_tickers.json: %s", e)
    return []


def _systemctl_status(svc: str) -> dict:
    """Return normalised systemd status for *svc*.

    Runs ``systemctl show`` to retrieve Type, Result, and ActiveState.
    The ``status`` key in the returned dict is the normalised string:

    * ``"active"``          — simple/notify/forking unit running normally
    * ``"oneshot-success"`` — Type=oneshot, last run succeeded, now inactive
    * ``"failed"``          — Result=failed or ActiveState=failed
    * ``"activating"``      — unit is starting up
    * ``"unknown"``         — anything else or subprocess error

    The full dict also exposes ``type``, ``result``, and ``active_state`` for
    callers that want to render richer output.
    """
    try:
        proc = subprocess.run(
            [
                "systemctl", "show", svc,
                "--property=Type,Result,ActiveState",
                "--no-pager", "--value",
            ],
            capture_output=True, text=True, timeout=5,
        )
        # ``--value`` emits each requested property on its own line, in order:
        # line 0 → Type, line 1 → Result, line 2 → ActiveState
        lines = [ln.strip() for ln in proc.stdout.strip().splitlines()]
        if len(lines) < 3:
            return {"status": "unknown", "type": "?", "result": "?", "active_state": "?"}
        unit_type, result, active_state = lines[0], lines[1], lines[2]
        if result == "failed" or active_state == "failed":
            status = "failed"
        elif active_state == "active" and result == "success":
            status = "active"
        elif unit_type == "oneshot" and active_state == "inactive" and result == "success":
            status = "oneshot-success"
        elif active_state == "activating":
            status = "activating"
        else:
            status = "unknown"
        return {
            "status": status,
            "type": unit_type,
            "result": result,
            "active_state": active_state,
        }
    except Exception as e:
        logger.debug("systemctl show %s failed: %s", svc, e)
        return {"status": "unknown", "type": "?", "result": "?", "active_state": "?"}


_PROJECT_ROOT = PROJECT_ROOT


# ── GET /api/system/health ────────────────────────────────────────────────────

@router.get("/api/system/health")
def system_health(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/system/health — comprehensive system health."""
    try:
        from atlas.db import get_heartbeats, get_db
        from datetime import datetime

        heartbeats = get_heartbeats()

        # Service status via systemd — oneshot-aware (see _systemctl_status helper).
        # The post-great-deletion unit set: the dashboard itself + the load-bearing timers.
        services: dict = {}
        for svc in (
            "atlas-dashboard",
            "atlas-live-shadow.timer",
            "atlas-backup.timer",
            "unified-healthcheck.timer",
        ):
            info = _systemctl_status(svc)
            services[svc] = info["status"]  # string for API backward-compat

        # Data freshness from SQLite
        data_freshness = {}
        try:
            excluded = _load_auto_excluded()
            data_freshness["auto_excluded_tickers"] = excluded

            with get_db() as db:
                if excluded:
                    placeholders = ",".join("?" for _ in excluded)
                    # MAX across non-excluded tickers (R-02: don't let stale excluded
                    # tickers pull down ohlcv_last_date for the whole system)
                    row = db.execute(
                        f"SELECT MAX(date) as last_date FROM ohlcv"
                        f" WHERE ticker NOT IN ({placeholders})",
                        excluded,
                    ).fetchone()
                    data_freshness["ohlcv_last_date"] = row["last_date"] if row else None
                    # Per-ticker breakdown — 10 stalest non-excluded tickers
                    ticker_rows = db.execute(
                        f"SELECT ticker, MAX(date) as last_date"
                        f" FROM ohlcv"
                        f" WHERE ticker NOT IN ({placeholders})"
                        f" GROUP BY ticker"
                        f" ORDER BY last_date ASC"
                        f" LIMIT 10",
                        excluded,
                    ).fetchall()
                else:
                    # No exclusions — use unfiltered queries
                    row = db.execute(
                        "SELECT MAX(date) as last_date FROM ohlcv"
                    ).fetchone()
                    data_freshness["ohlcv_last_date"] = row["last_date"] if row else None
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

            # R-03: weekend-aware freshness badge — compare ohlcv_last_date
            # against the last completed NYSE trading session (not wall-clock today)
            try:
                from atlas.kernel.market_hours import last_us_market_session
                data_freshness["ohlcv_last_session"] = last_us_market_session()
                last_d = data_freshness.get("ohlcv_last_date")
                last_sess = data_freshness["ohlcv_last_session"]
                data_freshness["ohlcv_is_fresh"] = bool(
                    last_d and last_sess and last_d >= last_sess
                )
            except Exception as _e:
                logger.warning("Could not compute last_us_market_session: %s", _e)
                data_freshness["ohlcv_last_session"] = None
                data_freshness["ohlcv_is_fresh"] = None

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
            from atlas.db import get_db as _ugh_db

            # Discover market IDs via glob; load effective config via canonical loader
            _universe_cfgs: list = []
            from atlas.kernel.config import get_active_config as _ghac
            for _cfg_path in sorted(Path("config/active").glob("*.json")):
                if _cfg_path.stem == "regime":
                    continue
                try:
                    _stem = _cfg_path.stem
                    try:
                        _cfg = _ghac(_stem)
                    except Exception:
                        # Fallback to raw JSON if canonical loader fails (e.g. validate_config)
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
                    # Audit F-01: read from market_equity_history.allocated_equity
                    _eq_val = None
                    try:
                        with _ugh_db() as _gheq_db:
                            _gheq_r = _gheq_db.execute(
                                "SELECT allocated_equity FROM market_equity_history "
                                "WHERE market_id=? ORDER BY date DESC LIMIT 1",
                                (_mid,),
                            ).fetchone()
                            if _gheq_r:
                                _eq_val = float(_gheq_r[0])
                    except Exception as _gheq_err:
                        logger.debug("system_health: equity lookup failed for %s: %s", _mid, _gheq_err)
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

