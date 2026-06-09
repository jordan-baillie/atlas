"""Live pipeline API — the forge->live shadow loop surfaced for the dashboard "Live" tab.

GET /api/live  — deployed strategies (live/registry), the latest daily shadow report
(data/live/daily/*.json), and the kill-switch state (core/remediation_kill_switch). Read-only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(tags=["live"])
logger = logging.getLogger("chat_server.live")

_LIVE = Path(__file__).resolve().parents[2] / "data" / "live"


@router.get("/api/live")
def live_state(_auth: HTTPBasicCredentials = Depends(check_auth)) -> dict:
    out: dict = {"deployed": [], "daily": None, "kill_switch": {"blocked": False, "reason": None}}

    # deployed strategies
    try:
        from live import registry
        out["deployed"] = [asdict(s) for s in registry.deployed()]
    except Exception as e:
        logger.warning("live: registry read failed: %s", e)

    # latest daily shadow report
    try:
        days = sorted((_LIVE / "daily").glob("*.json"))
        if days:
            out["daily"] = json.loads(days[-1].read_text())
    except Exception as e:
        logger.warning("live: daily report read failed: %s", e)

    # kill-switch state (read-only)
    try:
        from core.remediation_kill_switch import check_all_layers
        br = check_all_layers()
        if br is not None:
            reason = getattr(br, "reason", None) or getattr(br, "message", None) or str(br)
            layer = getattr(br, "layer", None)
            out["kill_switch"] = {"blocked": True, "reason": reason, "layer": layer}
    except Exception as e:
        logger.debug("live: kill-switch check skipped: %s", e)

    return out
