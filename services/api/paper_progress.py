"""Paper-progress API route.

Route:
  GET /api/strategies/paper-progress
    — returns promotion gate status for all PAPER-state strategies.

No auth required for read-only metrics route (consistent with health / research
endpoints that expose non-sensitive operational data).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from services.paper_progress import compute_paper_progress

router = APIRouter(tags=["paper-progress"])
logger = logging.getLogger(__name__)


@router.get("/api/strategies/paper-progress")
def get_paper_progress() -> JSONResponse:
    """Return promotion gate metrics for all PAPER-state (strategy, universe) combos."""
    try:
        strategies = compute_paper_progress()
        return JSONResponse(
            {
                "strategies": strategies,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    except Exception:
        logger.exception("Failed to compute paper progress")
        return JSONResponse(
            {
                "strategies": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error": "Failed to compute paper progress — check server logs",
            },
            status_code=500,
        )
