"""Monitor API legacy stubs — returns 410 Gone (monitor tab removed).

Extracted from services/chat_server.py (Phase 9 decomposition).

Routes:
  GET    /api/monitor             — 410 Gone
  GET    /api/monitor/{path}      — 410 Gone
  POST   /api/monitor             — 410 Gone
  POST   /api/monitor/{path}      — 410 Gone
  DELETE /api/monitor/positions/{id}  — delete monitor position
  DELETE /api/monitor/templates/{id}  — delete monitor template
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from services.auth import check_auth

router = APIRouter(tags=["monitor"])
logger = logging.getLogger("chat_server.monitor_legacy")


# ── GET /api/monitor* — 410 Gone (monitor tab removed) ───────────────────────

@router.get("/api/monitor")
@router.get("/api/monitor/{monitor_path:path}")
def monitor_get_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/monitor — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ── POST /api/monitor* — 410 Gone ────────────────────────────────────────────

@router.post("/api/monitor")
@router.post("/api/monitor/{monitor_path:path}")
def monitor_post_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/monitor* — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ── DELETE /api/monitor/positions/{id} ───────────────────────────────────────

@router.delete("/api/monitor/positions/{pos_id}")
def delete_position(
    pos_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """DELETE /api/monitor/positions/{id} — delete a monitor position."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_position(pos_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ── DELETE /api/monitor/templates/{id} ───────────────────────────────────────

@router.delete("/api/monitor/templates/{tmpl_id}")
def delete_template(
    tmpl_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """DELETE /api/monitor/templates/{id} — delete a monitor template."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_template(tmpl_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)
