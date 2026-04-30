"""Plan approval and rejection API routes.

Extracted from services/chat_server.py (Phase 8 decomposition).

Routes:
  POST /api/approve  — approve + execute a trade plan (live broker)
  POST /api/reject   — reject a trade plan (mark REJECTED, no execution)
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from services.auth import check_auth

router = APIRouter(tags=["approvals"])
logger = logging.getLogger("chat_server.approvals")


# ── Pydantic request model ─────────────────────────────────────────────────────

class PlanRequest(BaseModel):
    trade_date: str
    market_id: str


# ── Business logic ────────────────────────────────────────────────────────────

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


def _execute_live(plan: dict, trade_date: str, config: dict, market_id: str) -> dict:
    """Execute plan via live broker."""
    from brokers.live_executor import LiveExecutor
    from brokers.live_portfolio import LivePortfolio
    from brokers.plan import TradePlanGenerator

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


# ── Routes ────────────────────────────────────────────────────────────────────

# TODO: unused — not called by dashboard UI (plan approval handled via Telegram bot)
@router.post("/api/approve")
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

        def _run() -> None:
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


# TODO: unused — not called by dashboard UI (plan rejection handled via Telegram bot)
@router.post("/api/reject")
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
