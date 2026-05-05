"""Admin API — universe and strategy override management.

Routes (all require HTTP Basic Auth):
  GET  /api/admin/universes                          — all 8 universes with effective state
  GET  /api/admin/strategies                         — all (universe × strategy) pairs
  GET  /api/admin/override-audit                     — paginated audit log
  POST /api/admin/universe/{market_id}/state         — set universe state override
  POST /api/admin/strategy/{market_id}/{strategy}/state — set strategy state override
  POST /api/admin/override/{override_id}/revert      — soft-revert an override

Spec: docs/specs/dashboard-universe-strategy-toggles.md §7
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel, field_validator

from services.auth import check_auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ACTIVE_DIR = PROJECT_ROOT / "config" / "active"

# ── Pydantic request models ───────────────────────────────────────────────────

class UniverseStateRequest(BaseModel):
    state: Literal["live", "passive", "disabled"]
    reason: str
    expires_at: Optional[str] = None  # ISO 8601, None = default 30d, null in JSON = permanent
    confirm_token: Optional[str] = None
    i_understand: bool = False

    @field_validator("reason")
    @classmethod
    def reason_length(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("reason must be at least 10 characters")
        if len(v) > 500:
            raise ValueError("reason must be at most 500 characters")
        return v


class StrategyStateRequest(BaseModel):
    state: Literal["enabled", "disabled"]
    reason: str
    expires_at: Optional[str] = None  # ISO 8601, None = default 30d, null in JSON = permanent
    i_understand: bool = False

    @field_validator("reason")
    @classmethod
    def reason_length(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("reason must be at least 10 characters")
        if len(v) > 500:
            raise ValueError("reason must be at most 500 characters")
        return v


class RevertRequest(BaseModel):
    reason: str

    @field_validator("reason")
    @classmethod
    def reason_length(cls, v: str) -> str:
        if len(v) < 10:
            raise ValueError("reason must be at least 10 characters")
        return v


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_market_ids() -> list[str]:
    """Return all market IDs from config/active/*.json, excluding 'regime'."""
    return sorted(
        p.stem for p in _ACTIVE_DIR.glob("*.json") if p.stem != "regime"
    )


def _effective_state_for_universe(market_id: str, cfg: dict) -> str:
    """Derive single-string effective state from (mode, live_enabled)."""
    mode = cfg.get("trading", {}).get("mode", "live")
    live_enabled = bool(cfg.get("trading", {}).get("live_enabled", False))
    if live_enabled and mode == "live":
        return "live"
    if live_enabled and mode != "live":
        return "passive"
    # live_enabled=False
    return "disabled"


def _config_state_for_universe(market_id: str) -> str:
    """Derive single-string state from raw JSON config (no overrides)."""
    from utils.config import get_raw_config
    try:
        raw = get_raw_config(market_id)
        return _effective_state_for_universe(market_id, raw)
    except Exception:
        return "unknown"


def _get_active_override(scope: str, key: str) -> Optional[dict]:
    """Return the active, non-expired override row for (scope, key) or None."""
    from db.atlas_db import get_db
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT id, scope, key, state, reason, created_by, created_at,
                          expires_at, prev_state, active
                   FROM config_overrides
                   WHERE scope=? AND key=? AND active=1
                     AND (expires_at IS NULL OR expires_at > datetime('now'))
                   LIMIT 1""",
                (scope, key),
            ).fetchone()
        return dict(row) if row else None
    except Exception as e:
        logger.warning("Override lookup failed for %s/%s: %s", scope, key, e)
        return None


def _get_current_effective_universe_state(market_id: str) -> str:
    """Get current effective universe state string (from overridden config)."""
    from utils.config import get_active_config, clear_config_cache
    try:
        cfg = get_active_config(market_id)
        return _effective_state_for_universe(market_id, cfg)
    except Exception:
        return "unknown"


def _is_production(market_id: str) -> bool:
    """A universe is 'production' if its current effective state is live+live_enabled."""
    from utils.config import get_active_config
    try:
        cfg = get_active_config(market_id)
        return (
            cfg.get("trading", {}).get("mode") == "live"
            and bool(cfg.get("trading", {}).get("live_enabled", False))
        )
    except Exception:
        return False


def _default_expires_at() -> str:
    """Default expiry: 30 days from now (ISO 8601 UTC)."""
    return (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")


def _open_position_count(market_id: str) -> int:
    """Count open trades for a market from SQLite."""
    from db.atlas_db import get_db
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE exit_date IS NULL AND universe=?",
                (market_id,),
            ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _open_positions_by_strategy(market_id: str, strategy: str) -> int:
    """Count open trades for a specific (market, strategy) pair."""
    from db.atlas_db import get_db
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM trades "
                "WHERE exit_date IS NULL AND universe=? AND strategy=?",
                (market_id, strategy),
            ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _trades_30d_and_pnl(market_id: str, strategy: str) -> tuple[int, float]:
    """Count closed trades and sum PnL for a strategy in last 30 days."""
    from db.atlas_db import get_db
    try:
        cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0) AS pnl "
                "FROM trades "
                "WHERE exit_date IS NOT NULL AND universe=? AND strategy=? "
                "  AND exit_date >= ?",
                (market_id, strategy, cutoff),
            ).fetchone()
        return int(row["n"]), float(row["pnl"])
    except Exception:
        return 0, 0.0


def _last_trade_at(market_id: str) -> Optional[str]:
    """Return ISO timestamp of most recent trade entry for a market."""
    from db.atlas_db import get_db
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT MAX(entry_date) AS d FROM trades WHERE universe=?",
                (market_id,),
            ).fetchone()
        return row["d"] if row and row["d"] else None
    except Exception:
        return None


def _supersede_active_override(
    conn, scope: str, key: str, actor: str, reason: str
) -> Optional[int]:
    """Mark any active override for (scope, key) as superseded.

    Returns the ID of the superseded row, or None if no active row existed.
    Must be called inside an open get_db() context.
    """
    row = conn.execute(
        "SELECT id, state FROM config_overrides "
        "WHERE scope=? AND key=? AND active=1 LIMIT 1",
        (scope, key),
    ).fetchone()
    if row is None:
        return None
    override_id = row["id"]
    old_state = row["state"]
    conn.execute(
        "UPDATE config_overrides SET active=0, ended_at=datetime('now'), "
        "ended_reason='superseded' WHERE id=?",
        (override_id,),
    )
    conn.execute(
        "INSERT INTO config_override_audit "
        "(override_id, scope, key, action, from_state, to_state, reason, actor, source) "
        "VALUES (?, ?, ?, 'supersede', ?, NULL, ?, ?, 'dashboard')",
        (override_id, scope, key, old_state, reason, actor),
    )
    return override_id


# ── GET /api/admin/universes ──────────────────────────────────────────────────

@router.get("/api/admin/universes")
def admin_get_universes(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/admin/universes — all universes with effective and config state."""
    from utils.config import get_active_config, get_raw_config
    from db.atlas_db import get_db, get_latest_equity

    market_ids = _list_market_ids()

    # Batch open-position count
    open_pos_by_market: dict[str, int] = {}
    if market_ids:
        ph = ",".join("?" * len(market_ids))
        try:
            with get_db() as conn:
                rows = conn.execute(
                    f"SELECT universe, COUNT(*) AS n FROM trades "
                    f"WHERE exit_date IS NULL AND universe IN ({ph}) "
                    f"GROUP BY universe",
                    market_ids,
                ).fetchall()
            for r in rows:
                open_pos_by_market[r["universe"]] = r["n"]
        except Exception as e:
            logger.warning("Batch open-pos query failed: %s", e)

    universes = []
    for mid in market_ids:
        try:
            effective_cfg = get_active_config(mid)
            raw_cfg = get_raw_config(mid)
        except Exception as e:
            logger.warning("Config load failed for %s: %s", mid, e)
            continue

        effective_state = _effective_state_for_universe(mid, effective_cfg)
        config_state = _effective_state_for_universe(mid, raw_cfg)
        override = _get_active_override("universe", mid)

        # Equity
        eq_row = None
        try:
            eq_row = get_latest_equity(market_id=mid)
        except Exception:
            pass
        current_equity = (eq_row or {}).get("equity")
        starting_equity = effective_cfg.get("risk", {}).get("starting_equity")

        universes.append({
            "market_id": mid,
            "effective_state": effective_state,
            "config_state": config_state,
            "override": override,
            "open_positions": open_pos_by_market.get(mid, 0),
            "last_trade_at": _last_trade_at(mid),
            "starting_equity": starting_equity,
            "current_equity": current_equity,
            "version": effective_cfg.get("version"),
        })

    return JSONResponse({"universes": universes})


# ── GET /api/admin/strategies ─────────────────────────────────────────────────

@router.get("/api/admin/strategies")
def admin_get_strategies(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """GET /api/admin/strategies — all (universe, strategy) pairs with effective state."""
    from utils.config import get_active_config, get_raw_config

    market_ids = _list_market_ids()
    strategies = []

    for mid in market_ids:
        try:
            effective_cfg = get_active_config(mid)
            raw_cfg = get_raw_config(mid)
        except Exception:
            continue

        raw_strats = raw_cfg.get("strategies", {})
        for strat_name, strat_cfg in raw_strats.items():
            config_enabled = bool(strat_cfg.get("enabled", False))
            effective_enabled = bool(
                effective_cfg.get("strategies", {}).get(strat_name, {}).get("enabled", config_enabled)
            )
            weight = strat_cfg.get("weight", 0.0)
            override = _get_active_override("strategy", f"{mid}.{strat_name}")
            open_pos = _open_positions_by_strategy(mid, strat_name)
            trades_30d, pnl_30d = _trades_30d_and_pnl(mid, strat_name)

            strategies.append({
                "market_id": mid,
                "strategy": strat_name,
                "effective_enabled": effective_enabled,
                "config_enabled": config_enabled,
                "weight": weight,
                "override": override,
                "open_positions": open_pos,
                "trades_30d": trades_30d,
                "pnl_30d": round(pnl_30d, 2),
                "lifecycle": "UNKNOWN",  # §11.5 decision: skip lifecycle integration
            })

    return JSONResponse({"strategies": strategies})


# ── GET /api/admin/override-audit ────────────────────────────────────────────

@router.get("/api/admin/override-audit")
def admin_get_audit(
    since: Optional[str] = Query(None, description="ISO 8601 — only events after this timestamp"),
    scope: Optional[str] = Query(None, description="Filter by scope: universe | strategy"),
    key: Optional[str] = Query(None, description="Filter by override key"),
    limit: int = Query(100, ge=1, le=500),
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/admin/override-audit — paginated audit log."""
    from db.atlas_db import get_db

    conditions = []
    params: list = []

    if since:
        conditions.append("ts > ?")
        params.append(since)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if key:
        conditions.append("key = ?")
        params.append(key)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    try:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT id, ts, override_id, scope, key, action, from_state, to_state, "
                f"reason, actor, source, remote_ip "
                f"FROM config_override_audit {where} "
                f"ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
        audit = [dict(r) for r in rows]
    except Exception as e:
        logger.exception("override-audit query failed")
        raise HTTPException(status_code=500, detail=str(e))

    return JSONResponse({"audit": audit, "next_cursor": None})


# ── POST /api/admin/universe/{market_id}/state ────────────────────────────────

@router.post("/api/admin/universe/{market_id}/state")
async def admin_set_universe_state(
    market_id: str,
    body: UniverseStateRequest,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/admin/universe/{market_id}/state — set universe state override."""
    from utils.config import get_active_config, get_raw_config, clear_config_cache
    from db.atlas_db import get_db

    market_id = market_id.lower().strip()

    # Validate market exists
    if not (_ACTIVE_DIR / f"{market_id}.json").exists():
        raise HTTPException(status_code=404, detail=f"Unknown market: {market_id}")

    # i_understand gate
    if not body.i_understand:
        raise HTTPException(
            status_code=400,
            detail="i_understand must be True — confirm you understand this affects live trading",
        )

    # Production-universe type-to-confirm
    if _is_production(market_id):
        if body.confirm_token != market_id:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"'{market_id}' is a production universe (live+live_enabled). "
                    f"Set confirm_token='{market_id}' to proceed."
                ),
            )

    # Disabled guard: reject if open positions exist
    if body.state == "disabled":
        count = _open_position_count(market_id)
        if count > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot disable {market_id} — {count} open position(s). "
                    f"Set state=passive first, close positions, then disable."
                ),
            )

    # Same-state rejection (409 Conflict)
    current_state = _get_current_effective_universe_state(market_id)
    if current_state == body.state:
        raise HTTPException(
            status_code=409,
            detail=f"Universe {market_id} is already in state '{body.state}' — no-op rejected",
        )

    # Resolve expiry: distinguish omitted (default 30d) from explicit null (permanent)
    # Pydantic v2: model_fields_set contains keys explicitly provided in the request body.
    if 'expires_at' in body.model_fields_set:
        # Explicitly provided in JSON (may be None=permanent or ISO string)
        expires_at = body.expires_at
    else:
        # Omitted from request body → default 30 days
        expires_at = _default_expires_at()

    actor = f"human:{_auth.username}"
    remote_ip = request.client.host if request.client else None
    payload_json = body.model_dump_json()

    try:
        with get_db() as conn:
            # 1. Supersede prior active override (if any)
            _supersede_active_override(conn, "universe", market_id, actor, body.reason)

            # 2. Insert new override
            conn.execute(
                "INSERT INTO config_overrides "
                "(scope, key, state, reason, created_by, expires_at, prev_state) "
                "VALUES ('universe', ?, ?, ?, ?, ?, ?)",
                (market_id, body.state, body.reason, actor, expires_at, current_state),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            # 3. Write audit row
            conn.execute(
                "INSERT INTO config_override_audit "
                "(override_id, scope, key, action, from_state, to_state, reason, "
                " actor, source, remote_ip, payload_json) "
                "VALUES (?, 'universe', ?, 'create', ?, ?, ?, ?, 'dashboard', ?, ?)",
                (new_id, market_id, current_state, body.state,
                 body.reason, actor, remote_ip, payload_json),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to set universe state for %s", market_id)
        raise HTTPException(status_code=500, detail=str(e))

    # 4. Invalidate config cache
    clear_config_cache()

    return JSONResponse({
        "ok": True,
        "override_id": new_id,
        "market_id": market_id,
        "from_state": current_state,
        "to_state": body.state,
        "expires_at": expires_at,
    })


# ── POST /api/admin/strategy/{market_id}/{strategy}/state ─────────────────────

@router.post("/api/admin/strategy/{market_id}/{strategy}/state")
async def admin_set_strategy_state(
    market_id: str,
    strategy: str,
    body: StrategyStateRequest,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/admin/strategy/{market_id}/{strategy}/state — set strategy state override."""
    from utils.config import get_raw_config, get_active_config, clear_config_cache
    from db.atlas_db import get_db

    market_id = market_id.lower().strip()

    # Validate market exists
    if not (_ACTIVE_DIR / f"{market_id}.json").exists():
        raise HTTPException(status_code=404, detail=f"Unknown market: {market_id}")

    # Validate strategy exists in raw config
    try:
        raw_cfg = get_raw_config(market_id)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Config not found for {market_id}")

    if strategy not in raw_cfg.get("strategies", {}):
        raise HTTPException(
            status_code=404,
            detail=f"Strategy '{strategy}' not found in {market_id} config",
        )

    # i_understand gate
    if not body.i_understand:
        raise HTTPException(
            status_code=400,
            detail="i_understand must be True — confirm you understand this affects live trading",
        )

    key = f"{market_id}.{strategy}"

    # Determine current effective enabled state
    effective_cfg = get_active_config(market_id)
    current_enabled = bool(
        effective_cfg.get("strategies", {}).get(strategy, {}).get("enabled", False)
    )
    current_state = "enabled" if current_enabled else "disabled"

    # Same-state rejection (409)
    if current_state == body.state:
        raise HTTPException(
            status_code=409,
            detail=f"Strategy {key} is already '{body.state}' — no-op rejected",
        )

    # Resolve expiry: distinguish omitted (default 30d) from explicit null (permanent)
    if 'expires_at' in body.model_fields_set:
        expires_at = body.expires_at
    else:
        expires_at = _default_expires_at()

    actor = f"human:{_auth.username}"
    remote_ip = request.client.host if request.client else None
    payload_json = body.model_dump_json()

    try:
        with get_db() as conn:
            _supersede_active_override(conn, "strategy", key, actor, body.reason)
            conn.execute(
                "INSERT INTO config_overrides "
                "(scope, key, state, reason, created_by, expires_at, prev_state) "
                "VALUES ('strategy', ?, ?, ?, ?, ?, ?)",
                (key, body.state, body.reason, actor, expires_at, current_state),
            )
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO config_override_audit "
                "(override_id, scope, key, action, from_state, to_state, reason, "
                " actor, source, remote_ip, payload_json) "
                "VALUES (?, 'strategy', ?, 'create', ?, ?, ?, ?, 'dashboard', ?, ?)",
                (new_id, key, current_state, body.state,
                 body.reason, actor, remote_ip, payload_json),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to set strategy state for %s/%s", market_id, strategy)
        raise HTTPException(status_code=500, detail=str(e))

    clear_config_cache()

    return JSONResponse({
        "ok": True,
        "override_id": new_id,
        "market_id": market_id,
        "strategy": strategy,
        "from_state": current_state,
        "to_state": body.state,
        "expires_at": expires_at,
    })


# ── POST /api/admin/override/{override_id}/revert ─────────────────────────────

@router.post("/api/admin/override/{override_id}/revert")
async def admin_revert_override(
    override_id: int,
    body: RevertRequest,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/admin/override/{override_id}/revert — soft-revert an override.

    Marks the override active=0, ended_reason='reverted'. Next read falls back
    to raw JSON config. Does NOT chain back through prior overrides.
    """
    from utils.config import clear_config_cache
    from db.atlas_db import get_db

    actor = f"human:{_auth.username}"
    remote_ip = request.client.host if request.client else None

    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT id, scope, key, state, active FROM config_overrides WHERE id=?",
                (override_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail=f"Override {override_id} not found")
            if not row["active"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"Override {override_id} is already inactive (ended_reason already set)",
                )

            scope = row["scope"]
            key = row["key"]
            from_state = row["state"]

            # Mark override inactive
            conn.execute(
                "UPDATE config_overrides SET active=0, ended_at=datetime('now'), "
                "ended_reason='reverted' WHERE id=?",
                (override_id,),
            )

            # Determine "to_state" after revert (falls back to raw config)
            from utils.config import get_raw_config
            if scope == "universe":
                try:
                    raw = get_raw_config(key)
                    to_state = _effective_state_for_universe(key, raw)
                    source_label = "config"
                except Exception:
                    to_state = "unknown"
                    source_label = "config"
            else:
                # strategy key = "market_id.strategy_name"
                parts = key.split(".", 1)
                mid, strat = (parts[0], parts[1]) if len(parts) == 2 else (key, key)
                try:
                    raw = get_raw_config(mid)
                    raw_en = bool(raw.get("strategies", {}).get(strat, {}).get("enabled", False))
                    to_state = "enabled" if raw_en else "disabled"
                    source_label = "config"
                except Exception:
                    to_state = "unknown"
                    source_label = "config"

            # Write audit row
            conn.execute(
                "INSERT INTO config_override_audit "
                "(override_id, scope, key, action, from_state, to_state, reason, "
                " actor, source, remote_ip) "
                "VALUES (?, ?, ?, 'revert', ?, ?, ?, ?, 'dashboard', ?)",
                (override_id, scope, key, from_state, to_state,
                 body.reason, actor, remote_ip),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Failed to revert override %d", override_id)
        raise HTTPException(status_code=500, detail=str(e))

    clear_config_cache()

    return JSONResponse({
        "ok": True,
        "reverted_override_id": override_id,
        "scope": scope,
        "key": key,
        "from_state": from_state,
        "to_state": to_state,
        "source": source_label,
    })
