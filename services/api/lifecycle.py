"""Strategy Lifecycle API — promotion pipeline controls.

Routes (all require HTTP Basic Auth):
  GET  /api/strategy-lifecycle
  GET  /api/lifecycle
      — list all strategy_lifecycle rows enriched with paper/live/research metrics

  GET  /api/strategy-lifecycle/{strategy}/{universe}/history
  GET  /api/lifecycle/{strategy}/{universe}/history
      — transition history for one (strategy, universe) combo

  POST /api/strategy-lifecycle/transition
  POST /api/lifecycle/transition
      — operator-initiated state transition (graph-enforced; force=true bypasses)

  POST /api/strategy-lifecycle/promote-paper
  POST /api/lifecycle/promote-paper
      — run auto-promote gates for one PAPER combo and transition to LIVE if all pass

Both /api/strategy-lifecycle and /api/lifecycle prefixes serve identical content.
The /api/lifecycle alias was added as part of F-03 audit fix (2026-05-11).

Spec: Sub-phase 1.5 — Dashboard Controls tab backend.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from services.auth import check_auth

# F-03 audit fix: no prefix here — each endpoint carries full absolute paths so
# both /api/strategy-lifecycle (original) and /api/lifecycle (alias) are served
# from the single router that chat_server already mounts.
router = APIRouter(tags=["lifecycle"])
logger = logging.getLogger(__name__)


# ── Pydantic request models ───────────────────────────────────────────────────

class TransitionRequest(BaseModel):
    strategy: str
    universe: str
    new_state: str          # RESEARCH | PAPER | LIVE | RETIRED
    reason: str
    force: bool = False     # True → bypass graph (emergency override)


class PromotePaperRequest(BaseModel):
    strategy: str
    universe: str


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_sharpe(pnl_pcts: List[float]) -> Optional[float]:
    """Trade-level Sharpe — mirrors auto_promote_paper_to_live._compute_sharpe."""
    if len(pnl_pcts) < 2:
        return None
    mean = sum(pnl_pcts) / len(pnl_pcts)
    var = sum((x - mean) ** 2 for x in pnl_pcts) / (len(pnl_pcts) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    return round(mean / sd, 6)


def _fetch_live_trades_pnl(strategy: str, universe: str, window_days: int = 30) -> List[float]:
    """Return pnl_pct list for closed live trades within the rolling window."""
    from db.atlas_db import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        with get_db() as db:
            cur = db.execute(
                "SELECT pnl_pct FROM trades "
                "WHERE strategy = ? AND universe = ? "
                "AND status = 'closed' AND superseded = 0 "
                "AND exit_date IS NOT NULL AND DATE(exit_date) > ? "
                "AND pnl_pct IS NOT NULL",
                (strategy, universe, cutoff),
            )
            return [float(r[0]) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("_fetch_live_trades_pnl(%s, %s): %s", strategy, universe, exc)
        return []


def _fetch_paper_trades_pnl(strategy: str, universe: str, window_days: int = 30) -> List[float]:
    """Return pnl_pct list for closed paper trades within the rolling window."""
    from db.atlas_db import get_db

    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        with get_db() as db:
            cur = db.execute(
                "SELECT pnl_pct FROM paper_trades "
                "WHERE strategy = ? AND universe = ? "
                "AND status = 'closed' AND superseded = 0 "
                "AND exit_date IS NOT NULL AND DATE(exit_date) > ? "
                "AND pnl_pct IS NOT NULL",
                (strategy, universe, cutoff),
            )
            return [float(r[0]) for r in cur.fetchall()]
    except Exception as exc:
        logger.warning("_fetch_paper_trades_pnl(%s, %s): %s", strategy, universe, exc)
        return []


def _fetch_research_sharpe(strategy: str, universe: str) -> Optional[float]:
    """Return cross-regime research_best Sharpe (regime_state IS NULL) or None."""
    from db.atlas_db import get_db

    try:
        with get_db() as db:
            row = db.execute(
                "SELECT sharpe FROM research_best "
                "WHERE strategy = ? AND universe = ? AND regime_state IS NULL",
                (strategy, universe),
            ).fetchone()
            if row and row[0] is not None:
                return round(float(row[0]), 6)
            return None
    except Exception as exc:
        logger.warning("_fetch_research_sharpe(%s, %s): %s", strategy, universe, exc)
        return None


def _days_since(iso_str: Optional[str]) -> Optional[float]:
    """Return calendar days elapsed since an ISO datetime or date string."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - dt).total_seconds() / 86_400, 2)
    except Exception:
        return None


def _enrich_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich a strategy_lifecycle row with research_sharpe and state-specific metrics."""
    strategy: str = row["strategy"]
    universe: str = row["universe"]
    state: str = row.get("state", "")

    out: Dict[str, Any] = dict(row)

    # Research Sharpe — all states
    out["research_sharpe"] = _fetch_research_sharpe(strategy, universe)

    # Paper metrics — initialise to null; populated for PAPER state
    out["paper_sharpe"] = None
    out["paper_trades_count"] = 0
    out["days_in_paper"] = None
    out["gap"] = None

    # Live metrics — initialise to null; populated for LIVE state
    out["live_sharpe"] = None
    out["live_trades_count"] = 0

    if state == "PAPER":
        pnls = _fetch_paper_trades_pnl(strategy, universe, window_days=30)
        out["paper_trades_count"] = len(pnls)
        out["paper_sharpe"] = _compute_sharpe(pnls)

        # days_in_paper from paper_start_date → entered_state_at as fallback
        paper_anchor = row.get("paper_start_date") or row.get("entered_state_at")
        out["days_in_paper"] = _days_since(paper_anchor)

        # gap = |paper_sharpe − research_sharpe| / max(|research_sharpe|, 0.1)
        if out["paper_sharpe"] is not None and out["research_sharpe"] is not None:
            rs: float = out["research_sharpe"]
            ps: float = out["paper_sharpe"]
            out["gap"] = round(abs(ps - rs) / max(abs(rs), 0.1), 6)

    elif state == "LIVE":
        pnls = _fetch_live_trades_pnl(strategy, universe, window_days=30)
        out["live_trades_count"] = len(pnls)
        out["live_sharpe"] = _compute_sharpe(pnls)

    return out


# ── Endpoints ─────────────────────────────────────────────────────────────────
# F-03: Each handler is registered under BOTH the original /api/strategy-lifecycle
# path and the /api/lifecycle alias.  FastAPI supports stacked @router.get
# decorators natively — both are live from a single router mounted in chat_server.

@router.get("/api/strategy-lifecycle")
@router.get("/api/lifecycle")
def get_lifecycle_list(
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """Return all strategy_lifecycle rows enriched with paper/live/research metrics."""
    try:
        from db.atlas_db import list_lifecycle_states

        rows = list_lifecycle_states(state=None)
        enriched = [_enrich_row(r) for r in rows]
        return JSONResponse(content={"rows": enriched})
    except Exception as exc:
        logger.exception("get_lifecycle_list failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/api/strategy-lifecycle/{strategy}/{universe}/history")
@router.get("/api/lifecycle/{strategy}/{universe}/history")
def get_lifecycle_history(
    strategy: str,
    universe: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """Return transition history for one (strategy, universe) combo, newest first."""
    try:
        from db.atlas_db import get_db

        with get_db() as db:
            rows = db.execute(
                """
                SELECT from_state, to_state, transitioned_at, reason,
                       auto_promotion_id, operator
                FROM strategy_lifecycle_history
                WHERE strategy = ? AND universe = ?
                ORDER BY transitioned_at DESC
                """,
                (strategy, universe),
            ).fetchall()
            history = [
                {
                    "from_state": r["from_state"],
                    "to_state": r["to_state"],
                    "transitioned_at": r["transitioned_at"],
                    "reason": r["reason"],
                    "operator": r["operator"],
                    "auto_promotion_id": r["auto_promotion_id"],
                }
                for r in rows
            ]
        return JSONResponse(content={"history": history})
    except Exception as exc:
        logger.exception("get_lifecycle_history(%s, %s) failed", strategy, universe)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/api/strategy-lifecycle/transition")
@router.post("/api/lifecycle/transition")
def post_lifecycle_transition(
    body: TransitionRequest,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """Operator-initiated state transition.

    force=false (default): graph-enforced (system operator); returns 400 if disallowed.
    force=true:            graph-bypassed with WARNING; uses auth.username as operator.
    """
    from monitor.strategy_lifecycle import (
        ALLOWED_TRANSITIONS,
        PromotionState,
        get_state,
        transition,
    )

    operator: str = _auth.username if _auth else "system"

    # ── Validate new_state ────────────────────────────────────────────────────
    try:
        new_state_enum = PromotionState[body.new_state]
    except KeyError:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid state {body.new_state!r}. "
                f"Valid states: RESEARCH, PAPER, LIVE, RETIRED"
            ),
        )

    # ── Audit log ─────────────────────────────────────────────────────────────
    logger.info(
        "[audit] lifecycle transition: %s/%s %s by %s reason=%r force=%s",
        body.strategy, body.universe, body.new_state, operator,
        body.reason, body.force,
    )

    # ── Determine effective operator ──────────────────────────────────────────
    # operator='system' → graph-enforced (raises ValueError on disallowed)
    # operator=<username> → graph-bypassed with WARNING (any other string)
    effective_operator = operator if body.force else "system"

    current = get_state(body.strategy, body.universe)
    transitioned_at = datetime.now(timezone.utc).isoformat()

    try:
        transition(
            strategy=body.strategy,
            universe=body.universe,
            new_state=new_state_enum,
            reason=body.reason,
            operator=effective_operator,
        )
    except ValueError:
        # Graph-disallowed transition (operator='system')
        allowed = ALLOWED_TRANSITIONS.get(current, set())
        allowed_names = sorted(s.value for s in allowed)
        current_val = current.value if current else None
        raise HTTPException(
            status_code=400,
            detail=(
                f"Disallowed system transition {current_val!r} → {body.new_state!r} "
                f"for ({body.strategy}, {body.universe}). "
                f"Allowed from {current_val!r}: {allowed_names}. "
                f"Use force=true to override."
            ),
        )

    # ── Telegram alert (non-fatal) ────────────────────────────────────────────
    try:
        from utils.telegram import notify

        notify(
            f"🔄 <b>{body.strategy}/{body.universe}</b> transitioned to "
            f"<b>{body.new_state}</b> by {operator} "
            f"(reason: {body.reason})",
            category="lifecycle_transition",
        )
    except Exception as tg_exc:
        logger.warning("lifecycle transition Telegram alert failed: %s", tg_exc)

    return JSONResponse(content={
        "transitioned": True,
        "strategy": body.strategy,
        "universe": body.universe,
        "from_state": current.value if current else None,
        "to_state": body.new_state,
        "operator": operator,
        "transitioned_at": transitioned_at,
    })


@router.post("/api/strategy-lifecycle/promote-paper")
@router.post("/api/lifecycle/promote-paper")
def post_promote_paper(
    body: PromotePaperRequest,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """Run auto-promote gate evaluation for one PAPER combo.

    Equivalent to: ``scripts/auto_promote_paper_to_live.py --force strategy:universe``

    All nine gates are evaluated.  If all pass, the combo is transitioned to LIVE
    and a promotion_log.json entry is appended.  If any gate fails, the response
    includes the per-gate breakdown so the operator can see exactly what is missing.
    """
    operator: str = _auth.username if _auth else "system"

    logger.info(
        "[audit] lifecycle promote-paper: %s/%s by %s",
        body.strategy, body.universe, operator,
    )

    try:
        from scripts.auto_promote_paper_to_live import evaluate_and_promote

        result = evaluate_and_promote(
            body.strategy,
            body.universe,
            force=True,
            dry_run=False,
            no_telegram=False,
        )
        return JSONResponse(content=result)
    except Exception as exc:
        logger.exception("post_promote_paper(%s, %s) failed", body.strategy, body.universe)
        raise HTTPException(status_code=500, detail=str(exc))
