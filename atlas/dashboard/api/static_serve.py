"""Static file serving and SPA catch-all routes.

Extracted from services/chat_server.py (Phase 10 decomposition).

Routes (MUST be included LAST — catch-all fallback):
  GET /homerbot    — serve full-page agent chat interface
  GET /chat        — same as /homerbot
  GET /{path:path} — React SPA with fallback to legacy static files
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth
from atlas.kernel.paths import PROJECT_ROOT

router = APIRouter(tags=["static"])
logger = logging.getLogger("chat_server.static_serve")

_PROJECT_ROOT = PROJECT_ROOT
_SERVE_DIR = _PROJECT_ROOT / "services" / "static"
_REACT_DIR = _PROJECT_ROOT / "dashboard-ui" / "dist"


# ── /chat and /homerbot — full-page agent interface ──────────────────────────

@router.get("/homerbot")
@router.get("/chat")
def serve_agent_page(
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve the full-page AI agent chat interface."""
    file_path = _SERVE_DIR / "agent.html"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Agent page not found")
    return FileResponse(
        str(file_path),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


# ── Static file catch-all (MUST be last — SPA fallback after all API routes) ──

@router.get("/{path:path}")
def serve_static(
    path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve React SPA from dashboard-ui/dist/.

    - Exact file matches (JS/CSS/SVG) → serve directly with cache headers
    - Everything else → serve index.html (SPA client-side routing)
    - Fallback to legacy services/static/ for old static files
    """
    if not path:
        path = "index.html"

    # --- Try React dist first ---
    react_root = _REACT_DIR.resolve()
    try:
        react_file = (_REACT_DIR / path).resolve()
        if str(react_file).startswith(str(react_root)) and react_file.exists() and react_file.is_file():
            if path.startswith("assets/"):
                cache = "public, max-age=31536000, immutable"  # hashed filenames
            elif path.endswith(".html"):
                cache = "no-cache"
            else:
                cache = "public, max-age=3600"
            return FileResponse(str(react_file), headers={"Cache-Control": cache})
    except (ValueError, OSError):
        pass

    # --- Fallback to legacy services/static/ for old static files (.json etc) ---
    try:
        serve_root = _SERVE_DIR.resolve()
        legacy_file = (_SERVE_DIR / path).resolve()
        if str(legacy_file).startswith(str(serve_root)) and legacy_file.exists() and legacy_file.is_file():
            if path.endswith(".json"):
                cache = "no-cache, no-store, must-revalidate"
            elif path.endswith(".html"):
                cache = "no-cache"
            else:
                cache = "public, max-age=3600"
            return FileResponse(str(legacy_file), headers={"Cache-Control": cache})
    except (ValueError, OSError):
        pass

    # --- SPA fallback: serve React index.html for client-side routing ---
    index_file = _REACT_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), headers={"Cache-Control": "no-cache"})

    raise HTTPException(status_code=404, detail="Not found")
