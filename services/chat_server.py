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
BIND = "127.0.0.1"
PORT = 8899

# Must be set before importing Atlas modules (same as dashboard_server.py)
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# ── FastAPI imports (after path setup) ───────────────────────────────────────

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402
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
from services.api.chat_sessions import router as _chat_sessions_router  # noqa: E402
from services.api.monitor_legacy import router as _monitor_legacy_router  # noqa: E402
from services.ws.chat import router as _ws_chat_router  # noqa: E402
from services.api.static_serve import router as _static_serve_router  # noqa: E402

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
app.include_router(_chat_sessions_router)
app.include_router(_monitor_legacy_router)
app.include_router(_ws_chat_router)
# IMPORTANT: static_serve router must be LAST — contains /{path:path} catch-all
app.include_router(_static_serve_router)

# Static serving, WS, and agent routes moved to services/ sub-routers (Phase 10)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
