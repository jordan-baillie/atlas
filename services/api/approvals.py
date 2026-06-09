"""Plan approval API routes — RETIRED 2026-06-09.

The Tier-2 entry+stop trade-plan flow (brokers/plan.py + brokers/live_executor.py) has been retired and
replaced by the forge->live shadow loop (live/daily.py + brokers/target_executor.py). These routes were already
unused by the dashboard UI (approval handled elsewhere); they now return a retirement notice. The symbols below
are kept so services/chat_server.py imports remain satisfied.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from pydantic import BaseModel

from services.auth import check_auth

router = APIRouter(tags=["approvals"])
logger = logging.getLogger("chat_server.approvals")

_TIER2_RETIRED = ("Tier-2 entry+stop plan flow retired (2026-06-09). Replaced by the forge->live shadow loop "
                  "(live/daily.py); approve deployed strategies with `python3 -m live.registry approve NAME`.")


class PlanRequest(BaseModel):
    trade_date: str
    market_id: str


# ── Retirement stubs (kept for import compatibility) ───────────────────────────
def _approve_and_execute(trade_date: str, market_id: str) -> dict:
    return {"ok": False, "error": _TIER2_RETIRED}


def _execute_live(plan: dict, trade_date: str, config: dict, market_id: str) -> dict:
    return {"ok": False, "error": _TIER2_RETIRED}


def _reject_plan(trade_date: str, market_id: str) -> dict:
    return {"ok": False, "error": _TIER2_RETIRED}


# ── Routes (return 410 Gone) ───────────────────────────────────────────────────
@router.post("/api/approve")
def approve_plan(body: PlanRequest, _auth: HTTPBasicCredentials = Depends(check_auth)):
    return JSONResponse({"ok": False, "error": _TIER2_RETIRED}, status_code=410)


@router.post("/api/reject")
def reject_plan(body: PlanRequest, _auth: HTTPBasicCredentials = Depends(check_auth)):
    return JSONResponse({"ok": False, "error": _TIER2_RETIRED}, status_code=410)
