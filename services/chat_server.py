#!/usr/bin/env python3
"""Atlas Dashboard Server — FastAPI port of dashboard_server.py.

Phase 2: adds headless Pi chat (WebSocket + REST) on top of the Phase 1
foundation.  All Phase 1 routes remain unchanged.

New endpoints
-------------
  GET  /api/chat/sessions          — list chat sessions
  POST /api/chat/sessions          — create a new session
  GET  /api/chat/sessions/{id}/messages — paginated history
  GET  /api/chat/token             — short-lived WS auth token
  WS   /ws/chat?token=<tok>        — streaming chat WebSocket

Credentials from ~/.atlas-secrets.json:
    dashboard_user, dashboard_pass

Run (direct):
    python3 services/chat_server.py

Run (uvicorn module):
    python3 -m uvicorn services.chat_server:app --host 127.0.0.1 --port 8899

Run (systemd):
    systemctl start atlas-dashboard
"""
# TODO: Refactor — 1369 lines. Split into: routes/, websocket/, chat/ sub-packages.
# TODO: Split into api_routes.py, auth.py, static.py using FastAPI APIRouter

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

# ── Housekeeping (mirror dashboard_server.py top-level setup) ────────────────

signal.signal(signal.SIGHUP, signal.SIG_IGN)

PROJECT_ROOT = Path("/root/atlas")
SECRETS_PATH = Path(os.environ.get("ATLAS_SECRETS_PATH", str(Path.home() / ".atlas-secrets.json")))
SERVE_DIR = PROJECT_ROOT / "dashboard" / "data"
REACT_DIR = PROJECT_ROOT / "dashboard-ui" / "dist"
BIND = "127.0.0.1"
PORT = 8899

# Must be set before importing Atlas modules (same as dashboard_server.py)
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# ── FastAPI imports (after path setup) ───────────────────────────────────────

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import (  # noqa: E402
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

# ── Chat imports ──────────────────────────────────────────────────────────────
try:
    from services.chat_db import (  # noqa: E402
        init_db as init_chat_db,
        create_session as _chat_create_session,
        get_session as _chat_get_session,
        list_sessions as _chat_list_sessions,
        add_message as _chat_add_message,
        get_messages as _chat_get_messages,
        get_latest_session as _chat_get_latest_session,
        rename_session as _chat_rename_session,
        delete_session as _chat_delete_session,
    )
    from services.pi_session import PiSessionManager  # noqa: E402
    _CHAT_AVAILABLE = True
except ImportError as _chat_import_err:
    logger_pre = logging.getLogger("chat_server")
    logger_pre.warning("Chat modules not available: %s", _chat_import_err)
    _CHAT_AVAILABLE = False

logger = logging.getLogger("chat_server")

# ── Credential management ─────────────────────────────────────────────────────

# ── HTTP Basic Auth dependency (moved to services/auth.py for router sharing) ─
from services.auth import check_auth, security  # noqa: E402






# ── FastAPI app + lifespan ────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialise chat DB and attempt to start Alpaca background poller."""
    # ── Phase 2: initialise chat persistence ────────────────────────────
    if _CHAT_AVAILABLE:
        try:
            init_chat_db()
            print("Chat DB initialised", flush=True)
        except Exception as e:  # noqa: BLE001 — startup hook; DB/FS failure must not crash server
            print(f"⚠️  Chat DB init failed: {e}", flush=True)

    # P4.1: Create targets.json stub to suppress up_sync.py WARN on every /api/finance call.
    # up_sync.build_finance_payload() loads this file but degrades gracefully when missing;
    # a stub {} eliminates the WARNING noise without breaking any functionality.
    _targets_path = PROJECT_ROOT / "dashboard" / "cache" / "targets.json"
    if not _targets_path.exists():
        try:
            _targets_path.parent.mkdir(parents=True, exist_ok=True)
            _targets_path.write_text("{}")
            logger.debug("Created stub targets.json at %s", _targets_path)
        except OSError as _te:
            logger.debug("Could not create targets.json stub: %s", _te)

    # alpaca_stream removed — SSE streaming retired in Phase 5
    yield  # app runs here


app = FastAPI(
    title="Atlas Dashboard",
    description="Atlas trading system dashboard — FastAPI port of dashboard_server.py",
    version="2.0.0",
    lifespan=lifespan,
    # Disable interactive docs to match original server (no Swagger/ReDoc UI)
    docs_url=None,
    redoc_url=None,
)


# ── Security middleware ──────────────────────────────────────────────────────

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests with Content-Length exceeding 1 MB (Finding F-01)."""

    MAX_BODY = 1_048_576  # 1 MB

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self.MAX_BODY:
            return JSONResponse(
                status_code=413,
                content={"error": "Request too large"},
            )
        return await call_next(request)


app.add_middleware(MaxBodySizeMiddleware)


class CSPMiddleware(BaseHTTPMiddleware):
    """Add Content-Security-Policy header to every response (P4.4)."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = self._CSP
        return response


app.add_middleware(CSPMiddleware)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    """Add security headers to every response (Finding F-04)."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


# ── Sub-package routers (Phase 1 extraction — docs/phase-c-god-file-decomposition.md) ──
from services.api.finance import router as _finance_router  # noqa: E402
from services.api.regime import router as _regime_router   # noqa: E402
from services.api.error_remediation import router as _error_remediation_router  # noqa: E402
from services.api.portfolio import router as _portfolio_router  # noqa: E402
from services.api.health import router as _health_router       # noqa: E402
from services.api.risk import router as _risk_router           # noqa: E402
from services.api.research import router as _research_router   # noqa: E402
from services.api.promotions import router as _promotions_router  # noqa: E402
from services.api.dashboard import router as _dashboard_router  # noqa: E402
from services.api.approvals import router as _approvals_router  # noqa: E402

# ── Re-export shims (backward-compat for tests importing from chat_server) ────
from services.api.dashboard import (  # noqa: F401
    _calc_alpaca_intraday_pnl,
    _calc_tiingo_daily_pnl,
    _build_dashboard_data,
)
from services.api.approvals import (  # noqa: F401
    _approve_and_execute,
    _execute_live,
    _reject_plan,
    PlanRequest,
)
app.include_router(_finance_router)
app.include_router(_regime_router)
app.include_router(_error_remediation_router)
app.include_router(_portfolio_router)
app.include_router(_health_router)
app.include_router(_risk_router)
app.include_router(_research_router)
app.include_router(_promotions_router)
app.include_router(_dashboard_router)
app.include_router(_approvals_router)

# ═══════════════════════════════════════════════════════════════════════════════
# GET routes  (defined in the same priority order as dashboard_server.py do_GET)
# ═══════════════════════════════════════════════════════════════════════════════

# ── GET /api/monitor* — 410 Gone (monitor tab removed) ───────────────────────

@app.get("/api/monitor")
@app.get("/api/monitor/{monitor_path:path}")
def monitor_get_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """GET /api/monitor — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ── Portfolio, trades, and equity routes moved to services/api/portfolio.py ──

# ── System health and macro routes moved to services/api/health.py ─────────

# ── Risk and signals routes moved to services/api/risk.py ──────────────────

# ── /api/regime/transitions — moved to services/api/regime.py ──────────────


# ── POST /api/monitor* — 410 Gone ────────────────────────────────────────────

@app.post("/api/monitor")
@app.post("/api/monitor/{monitor_path:path}")
def monitor_post_gone(
    monitor_path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """POST /api/monitor* — monitor tab removed, returns 410."""
    return JSONResponse({"error": "Monitor tab removed"}, status_code=410)


# ═══════════════════════════════════════════════════════════════════════════════
# DELETE routes
# ═══════════════════════════════════════════════════════════════════════════════

# ── DELETE /api/monitor/positions/{id} ───────────────────────────────────────

@app.delete("/api/monitor/positions/{pos_id}")
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

@app.delete("/api/monitor/templates/{tmpl_id}")
def delete_template(
    tmpl_id: str,
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """DELETE /api/monitor/templates/{id} — delete a monitor template."""
    from monitor.models import PositionStore
    store = PositionStore()
    ok = store.delete_template(tmpl_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


# ═══════════════════════════════════════════════════════════════════════════════
# Phase 2: Chat REST + WebSocket endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# In-process cache of PiSessionManager instances (one per chat session).
# These survive for the lifetime of the server process; each manager
# maintains the pi_session_path on disk so the conversation can be resumed
# after a server restart.
_pi_sessions: dict[str, "PiSessionManager"] = {}

# Short-lived WebSocket auth tokens: token_str -> (expires_epoch, username)
_ws_tokens: dict[str, tuple[float, str]] = {}
_WS_TOKEN_TTL = 300  # seconds (5 minutes)
_MAX_WS_TOKENS = 1000


def _require_chat() -> None:
    """Raise 503 if chat modules failed to import."""
    if not _CHAT_AVAILABLE:
        raise HTTPException(status_code=503, detail="Chat modules unavailable")


# ── Token endpoint (HTTP Basic → short-lived WS token) ────────────────────

@app.get("/api/chat/token")
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


# ── Chat session REST endpoints ────────────────────────────────────────────

@app.get("/api/chat/sessions")
def chat_list_sessions(
    limit: int = 20,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """GET /api/chat/sessions — list active sessions, newest first."""
    _require_chat()
    return JSONResponse(_chat_list_sessions(limit))


@app.post("/api/chat/sessions")
async def chat_create_session_endpoint(
    request: Request,
    _auth: HTTPBasicCredentials = Depends(check_auth),
) -> JSONResponse:
    """POST /api/chat/sessions — create a new chat session.

    Body (JSON): {"name": "optional name", "model": "claude-sonnet-4-6"}
    """
    _require_chat()
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name")
    model = body.get("model", "claude-opus-4-7")
    session = _chat_create_session(name=name, model=model)
    return JSONResponse(session)


@app.get("/api/chat/sessions/{session_id}")
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


@app.put("/api/chat/sessions/{session_id}")
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
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        logger.debug("Could not parse request body: %s", e)
        body = {}
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    ok = _chat_rename_session(session_id, name)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return JSONResponse({"ok": True, "id": session_id, "name": name})


@app.delete("/api/chat/sessions/{session_id}")
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


@app.get("/api/chat/sessions/{session_id}/messages")
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


# ── WebSocket chat endpoint ──────────────────────────────────────────────────

@app.websocket("/ws/chat")
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
    {"type": "new_session", "name": "optional", "model": "claude-sonnet-4-6"}
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
            except (ValueError, UnicodeDecodeError, OSError, KeyError) as e:  # base64/split/credentials errors
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
                        sess_rec.get("model", "claude-sonnet-4-6")
                        if sess_rec
                        else "claude-sonnet-4-6"
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
                model = data.get("model", "claude-sonnet-4-6")
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
    except Exception as exc:  # noqa: BLE001 — WebSocket handler; any exception must be reported to client
        logger.exception("WebSocket chat error: %s", exc)
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except (RuntimeError, WebSocketDisconnect, OSError) as e:  # WS already closed or disconnected
            logger.debug("Could not send error to WebSocket client: %s", e)



# ── Research routes moved to services/api/research.py ───────────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# ── Promotion routes moved to services/api/promotions.py ───────────────────

# ═══════════════════════════════════════════════════════════════════════════════
# ── /chat route — full-page agent interface ──────────────────────────────────

@app.get("/homerbot")
@app.get("/chat")
def serve_agent_page(
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve the full-page AI agent chat interface."""
    file_path = SERVE_DIR / "agent.html"
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


# ── Risk ruin refresh moved to services/api/risk.py ─────────────────────────


# ── System health/universes routes moved to services/api/health.py ─────────

# === END RISK CACHE ENDPOINTS (P2.7/P2.8) ===

# Static file catch-all  (MUST be last — fallback after all API routes)
# Serves React SPA from dashboard-ui/dist/ with fallback to index.html
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/{path:path}")
def serve_static(
    path: str = "",
    _auth: HTTPBasicCredentials = Depends(check_auth),
):
    """Serve React SPA from dashboard-ui/dist/.

    - Exact file matches (JS/CSS/SVG) → serve directly with cache headers
    - Everything else → serve index.html (SPA client-side routing)
    - Fallback to legacy dashboard/data/ for old static files
    """
    if not path:
        path = "index.html"

    # --- Try React dist first ---
    react_root = REACT_DIR.resolve()
    try:
        react_file = (REACT_DIR / path).resolve()
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

    # --- Fallback to legacy dashboard/data/ for old static files (.json etc) ---
    try:
        serve_root = SERVE_DIR.resolve()
        legacy_file = (SERVE_DIR / path).resolve()
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
    index_file = REACT_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), headers={"Cache-Control": "no-cache"})

    raise HTTPException(status_code=404, detail="Not found")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
