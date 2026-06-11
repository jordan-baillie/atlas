"""WebSocket chat handler for Atlas Dashboard.

Extracted from services/chat_server.py (Phase 10 decomposition).

Routes:
  WS /ws/chat?token=<tok>  — bidirectional streaming chat

Protocol (client → server):
  {"type": "send",        "content": "...", "session_id": "uuid|null"}
  {"type": "history",    "session_id": "uuid", "limit": 50, "before_id": null}
  {"type": "cancel",     "session_id": "uuid"}
  {"type": "new_session", "name": "optional", "model": "<model id>"}  (default: central policy)
  {"type": "status",     "session_id": "uuid"}

Notes:
  - BUG FIX (Phase 10): _get_credentials was called in the Basic Auth fallback
    path but was never imported (NameError in production). Fixed here by
    importing from atlas.dashboard.auth.
"""
from __future__ import annotations

import base64
import logging
import secrets
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

# Import shared token state — REST endpoint issues tokens, WS validates them
from atlas.dashboard.chat.sessions import _ws_tokens, default_chat_model
from atlas.dashboard.auth import _get_credentials  # was missing (pre-existing bug, fixed here)

router = APIRouter()
logger = logging.getLogger("chat_server.ws")

# ── Chat module availability ───────────────────────────────────────────────────
try:
    from atlas.dashboard.chat.db import (
        create_session as _chat_create_session,
        get_session as _chat_get_session,
        get_latest_session as _chat_get_latest_session,
        add_message as _chat_add_message,
        get_messages as _chat_get_messages,
    )
    from atlas.dashboard.chat.pi_session import PiSessionManager
    _CHAT_AVAILABLE = True
except ImportError as _e:
    logger.warning("WS chat modules not available: %s", _e)
    _CHAT_AVAILABLE = False

# ── In-process PiSessionManager cache (WS-only state) ────────────────────────
# Each entry survives for the lifetime of the server process so conversations
# can be resumed after a server restart (Pi session file persisted on disk).
_pi_sessions: dict[str, "PiSessionManager"] = {}


# ── WebSocket handler ─────────────────────────────────────────────────────────

@router.websocket("/ws/chat")
async def websocket_chat(ws: WebSocket) -> None:  # noqa: C901
    """WS /ws/chat?token=<tok> — bidirectional streaming chat.

    Auth
    ----
    Pass the token from ``GET /api/chat/token`` as a query parameter::

        ws://host/ws/chat?token=<value>

    Alternatively pass ``Authorization: Basic <b64>`` as a WS header
    (supported by some clients; browsers cannot set custom WS headers).

    Protocol (client → server)
    --------------------------
    {"type": "send",        "content": "...", "session_id": "uuid|null"}
    {"type": "history",    "session_id": "uuid", "limit": 50, "before_id": null}
    {"type": "cancel",     "session_id": "uuid"}
    {"type": "new_session", "name": "optional", "model": "<model id>"}  (default: central policy)
    {"type": "status",     "session_id": "uuid"}

    Protocol (server → client) — see PiEvent.to_dict() for streaming events.
    """
    # ── Auth: token query param takes priority, then Basic Auth header ──
    token_param = ws.query_params.get("token", "")
    authed = False

    if token_param:
        entry = _ws_tokens.get(token_param)
        if entry:
            expires, _username = entry
            if time.time() < expires:
                authed = True

    if not authed:
        # Fall back: check Authorization header
        auth_header = ws.headers.get("authorization", "")
        if auth_header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode()
                uname, pw = decoded.split(":", 1)
                exp_user, exp_pass = _get_credentials()
                user_ok = secrets.compare_digest(uname.encode(), exp_user.encode())
                pass_ok = secrets.compare_digest(pw.encode(), exp_pass.encode())
                authed = user_ok and pass_ok
            except (ValueError, UnicodeDecodeError, OSError, KeyError) as e:
                logger.debug("WebSocket auth decode failed: %s", e)

    if not authed:
        await ws.close(code=1008, reason="Unauthorized")
        return

    if not _CHAT_AVAILABLE:
        await ws.accept()
        await ws.send_json({"type": "error", "message": "Chat modules unavailable"})
        await ws.close()
        return

    await ws.accept()

    # Guard: prevent overlapping send_message calls on the same session.
    # If user sends a second message while the first is still streaming,
    # we reject it rather than corrupt the Pi session file.
    _generating = False

    try:
        while True:
            try:
                data = await ws.receive_json()
            except WebSocketDisconnect:
                break

            msg_type = data.get("type", "")

            # ---- send: user sends a chat message --------------------------
            if msg_type == "send":
                if _generating:
                    await ws.send_json({"type": "error", "message": "Already generating a response. Wait for it to finish or cancel first."})
                    continue
                content = data.get("content", "").strip()
                images = data.get("images")  # [{data, mime}, ...]
                attachments = data.get("attachments")  # [{name, data, mime}, ...]
                if not content and not images and not attachments:
                    continue

                session_id = data.get("session_id")

                # Resolve or create session
                if not session_id:
                    latest = _chat_get_latest_session()
                    if latest:
                        session_id = latest["id"]
                    else:
                        new_sess = _chat_create_session()
                        session_id = new_sess["id"]

                # Persist user message
                msg_id = _chat_add_message(session_id, "user", content)
                await ws.send_json({
                    "type": "user_message_saved",
                    "id": msg_id,
                    "session_id": session_id,
                })

                # Get or create PiSessionManager for this session
                # Allow per-message team mode toggle
                use_teams = bool(data.get("use_teams", False))

                if session_id not in _pi_sessions:
                    sess_rec = _chat_get_session(session_id)
                    model = (
                        sess_rec.get("model") or default_chat_model()
                        if sess_rec
                        else default_chat_model()
                    )
                    _pi_sessions[session_id] = PiSessionManager(
                        session_id, model=model, use_teams=use_teams
                    )
                else:
                    # Update teams mode if changed
                    _pi_sessions[session_id].use_teams = use_teams

                mgr = _pi_sessions[session_id]

                # Warn if session is getting heavy (>60K tokens estimated)
                if mgr.pi_session_path.exists():
                    session_size = mgr.pi_session_path.stat().st_size
                    if session_size > 200_000:  # ~60K tokens
                        await ws.send_json({
                            "type": "warning",
                            "message": f"Session history is large ({session_size // 1024}KB). Consider starting a new session for faster responses.",
                        })

                # Stream response events back to client
                _generating = True
                full_text = ""
                try:
                    async for event in mgr.send_message(content, images=images, attachments=attachments):
                        await ws.send_json(event.to_dict())
                        if event.type == "text_delta":
                            full_text += event.data.get("delta", "")
                        elif event.type == "done":
                            full_text = event.data.get("full_text") or full_text
                except WebSocketDisconnect:
                    # Client left mid-stream; Pi keeps running, we save what we have
                    break
                finally:
                    _generating = False

                # Persist assistant reply
                if full_text:
                    _chat_add_message(session_id, "assistant", full_text)

            # ---- history: load stored messages ----------------------------
            elif msg_type == "history":
                session_id = data.get("session_id")
                limit = int(data.get("limit", 50))
                before_id = data.get("before_id")
                if session_id:
                    msgs = _chat_get_messages(
                        session_id, limit=limit, before_id=before_id
                    )
                    await ws.send_json({
                        "type": "history",
                        "messages": msgs,
                        "session_id": session_id,
                    })

            # ---- cancel: kill running Pi subprocess -----------------------
            elif msg_type == "cancel":
                session_id = data.get("session_id")
                if session_id and session_id in _pi_sessions:
                    await _pi_sessions[session_id].cancel()
                _generating = False
                await ws.send_json({"type": "cancelled"})

            # ---- new_session: create a fresh conversation -----------------
            elif msg_type == "new_session":
                name = data.get("name")
                model = data.get("model") or default_chat_model()
                sess = _chat_create_session(name=name, model=model)
                await ws.send_json({"type": "session_created", "session": sess})

            # ---- status: is Pi running? -----------------------------------
            elif msg_type == "status":
                session_id = data.get("session_id")
                mgr = _pi_sessions.get(session_id) if session_id else None
                await ws.send_json({
                    "type": "status",
                    "pi_running": mgr.is_running if mgr else False,
                    "session_id": session_id,
                })

    except WebSocketDisconnect:
        pass  # Normal: client closed tab / navigated away
    except Exception as exc:
        logger.exception("WebSocket chat error: %s", exc)
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect, OSError) as e:
            logger.debug("Could not send error to WebSocket client: %s", e)
