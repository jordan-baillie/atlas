"""Promotions API routes — pending research promotions workflow.

Phase 7 extraction from services/chat_server.py.

Routes:
  GET  /api/promotions/pending              — list pending promotions
  POST /api/promotions/{pending_id}/approve — approve a promotion
  POST /api/promotions/{pending_id}/reject  — reject a promotion
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(prefix="/api/promotions", tags=["promotions"])
logger = logging.getLogger(__name__)


@router.get("/pending")
def promotions_pending(_auth: HTTPBasicCredentials = Depends(check_auth)):
    """List pending promotions awaiting approval."""
    try:
        from research.promoter import _load_pending, expire_pending_promotions
        # Auto-expire stale entries on every read (idempotent)
        expire_pending_promotions()
        entries = _load_pending()
        pending = [e for e in entries if e.get("status") == "pending"]
        return JSONResponse(content={"pending": pending, "count": len(pending)})
    except Exception as e:
        logger.exception("promotions_pending failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{pending_id}/approve")
def promotions_approve(
    pending_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Approve a pending promotion (executes write to config/active/)."""
    try:
        from research.promoter import complete_pending_promotion
        approver = _auth.username if _auth else "unknown"
        logger.info(
            "[audit] promotion approve: pending_id=%s approver=%s remote=%s",
            pending_id, approver, request.client.host if request.client else "?",
        )
        result = complete_pending_promotion(pending_id)
        if result.get("promoted"):
            return JSONResponse(content={
                "approved": True,
                "version": result.get("version"),
                "strategy": result.get("strategy"),
                "market": result.get("market"),
                "approver": approver,
            })
        # Surface rejections from gate failure or "already X" cleanly
        reason = result.get("reason", "promotion failed")
        if "not found" in reason.lower():
            raise HTTPException(status_code=404, detail=reason)
        if "already" in reason.lower():
            raise HTTPException(status_code=409, detail=reason)
        raise HTTPException(status_code=400, detail=reason)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("promotions_approve failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{pending_id}/reject")
def promotions_reject(
    pending_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Reject a pending promotion."""
    try:
        from research.promoter import reject_pending_promotion
        approver = _auth.username if _auth else "unknown"
        # Default reason; override from query param ?reason=foo for sync simplicity
        reason = "Rejected via dashboard"
        qreason = request.query_params.get("reason")
        if qreason:
            reason = qreason
        logger.info(
            "[audit] promotion reject: pending_id=%s approver=%s reason=%s",
            pending_id, approver, reason,
        )
        result = reject_pending_promotion(pending_id, reason)
        if result.get("rejected"):
            return JSONResponse(content={
                "rejected": True,
                "strategy": result.get("strategy"),
                "approver": approver,
            })
        raise HTTPException(status_code=404, detail=result.get("reason", "Not found"))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("promotions_reject failed")
        raise HTTPException(status_code=500, detail=str(e))
