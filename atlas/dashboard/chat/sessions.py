"""Chat session REST endpoints and shared WebSocket token state.

Extracted from services/chat_server.py (Phase 9 decomposition).

Routes:
  GET    /api/chat/token                        — issue short-lived WS auth token
  GET    /api/chat/sessions                     — list sessions
  POST   /api/chat/sessions                     — create session
  GET    /api/chat/sessions/{id}                — get session
  PUT    /api/chat/sessions/{id}                — rename session
  DELETE /api/chat/sessions/{id}                — delete session
  GET    /api/chat/sessions/{id}/messages       — paginated message history

Shared state exported for WebSocket handler:
  _ws_tokens      — token_str -> (expires_epoch, username)
  _WS_TOKEN_TTL   — token lifetime in seconds
  _MAX_WS_TOKENS  — max simultaneous live tokens
  _CHAT_AVAILABLE — True if chat_db / pi_session imports succeeded
"""
from __future__ import annotations

import json
import logging
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials

from atlas.dashboard.auth import check_auth

router = APIRouter(tags=["chat"])
logger = logging.getLogger("chat_server.chat_sessions")


def default_chat_model(tier: str = "frontier", failsafe: str = "claude-opus-4-8") -> str:
    """Default chat model from the central policy (/root/.pi/model-policy.json).

    Read per-call (cheap) so policy changes apply without a service restart.
    Failsafe is a $0-Max model — never accidentally paid.
    """
    try:
        with open("/root/.pi/model-policy.json") as fh:
            return json.load(fh)["tiers"][tier]
    except Exception:
        return failsafe

# ── Chat module availability ───────────────────────────────────────────────────
try:
    from atlas.dashboard.chat.db import (
        create_session as _chat_create_session,
        get_session as _chat_get_session,
        list_sessions as _chat_list_sessions,
        add_message as _chat_add_message,
        get_messages as _chat_get_messages,
        get_latest_session as _chat_get_latest_session,
        rename_session as _chat_rename_session,
        delete_session as _chat_delete_session,
    )
    _CHAT_AVAILABLE = True
except ImportError as _chat_import_err:
    logging.getLogger("chat_server.chat_sessions").warning(
        "Chat modules not available: %s", _chat_import_err
    )
    _CHAT_AVAILABLE = False

# ── Shared WebSocket token state (also consumed by ws/chat.py handler) ─────────
# Short-lived WebSocket auth tokens: token_str -> (expires_epoch, username)
_ws_tokens: dict[str, tuple[float, str]] = {}
_WS_TOKEN_TTL = 300  # seconds (5 minutes)
_MAX_WS_TOKENS = 1000


def _require_chat() -> None:
    """Raise 503 if chat modules failed to import."""
    if not _CHAT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Chat modules unavailable")


# ── Token endpoint (HTTP Basic → short-lived WS token) ────────────────────────

@router.get("/api/chat/token")
def chat_get_token(
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/token — exchange HTTP Basic Auth for a short-lived WS token.

    The browser calls this once (via XMLHttpRequest with cached Basic Auth
    credentials) and stores the returned token in sessionStorage.  The
    WebSocket upgrade then passes it as ``?token=<value>``.
    """
    _require_chat()
    # Purge expired tokens first (Finding F-07)
    now = time.time()
    stale = [k for k, (exp, _) in _ws_tokens.items() if exp < now]
    for k in stale:
        _ws_tokens.pop(k, None)
    # Reject if still over capacity
    if len(_ws_tokens) >= _MAX_WS_TOKENS:
        return JSONResponse(
            status_code=429,
            content={"error": "Too many active tokens"},
        )
    token = secrets.token_urlsafe(32)
    expires = now + _WS_TOKEN_TTL
    _ws_tokens[token] = (expires, _auth.username)
    return JSONResponse({"token": token, "expires_in": _WS_TOKEN_TTL})


# ── Chat session REST endpoints ────────────────────────────────────────────────

@router.get("/api/chat/sessions")
def chat_list_sessions(
    limit: int = 20,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions — list active sessions, newest first."""
    _require_chat()
    return JSONResponse(_chat_list_sessions(limit))


@router.post("/api/chat/sessions")
async def chat_create_session_endpoint(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """POST /api/chat/sessions — create a new chat session.

    Body (JSON): {"name": "optional name", "model": "<model id>"} (default: central policy)
    """
    _require_chat()
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:  # HTTP request body parse
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name")
    model = body.get("model") or default_chat_model()
    session = _chat_create_session(name=name, model=model)
    return JSONResponse(session)


@router.get("/api/chat/sessions/{session_id}")
def chat_get_session_endpoint(
    session_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions/{id} — get single session details."""
    _require_chat()
    session = _chat_get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse(session)


@router.put("/api/chat/sessions/{session_id}")
async def chat_rename_session_endpoint(
    session_id: str,
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """PUT /api/chat/sessions/{id} — rename a chat session.

    Body: {"name": "new session name"}
    """
    _require_chat()
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:  # HTTP request body parse
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    ok = _chat_rename_session(session_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"ok": True, "id": session_id, "name": name})


@router.delete("/api/chat/sessions/{session_id}")
def chat_delete_session_endpoint(
    session_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """DELETE /api/chat/sessions/{id} — soft-delete a chat session."""
    _require_chat()
    ok = _chat_delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"ok": True, "id": session_id})


@router.get("/api/chat/sessions/{session_id}/messages")
def chat_get_messages_endpoint(
    session_id: str,
    limit: int = 50,
    before_id: int = None,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions/{id}/messages — paginated message history."""
    _require_chat()
    msgs = _chat_get_messages(session_id, limit=limit, before_id=before_id)
    return JSONResponse(msgs)
