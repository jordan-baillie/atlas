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
    python3 -m uvicorn atlas.dashboard.app:app --host 127.0.0.1 --port 8899

Run (systemd):
    systemctl start atlas-dashboard
"""
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# ── Housekeeping (mirror dashboard_server.py top-level setup) ────────────────

# SIGHUP is Unix-only; Windows dev/test environments don't have it.  Guard so
# the module imports cleanly under pytest on Windows.
if hasattr(signal, "SIGHUP"):
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

from atlas.kernel.paths import PROJECT_ROOT  # noqa: E402  (resolves /root/atlas | ATLAS_PROJECT_ROOT | repo root)

SECRETS_PATH = Path(os.environ.get("ATLAS_SECRETS_PATH", str(Path.home() / ".atlas-secrets.json")))
BIND = "127.0.0.1"
PORT = 8899

if PROJECT_ROOT.exists():
    os.chdir(PROJECT_ROOT)

# ── FastAPI imports (after path setup) ───────────────────────────────────────

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

# ── Chat DB init (lifespan only — session CRUD is in api/chat_sessions.py) ────
try:
    from atlas.dashboard.chat.db import init_db as init_chat_db  # noqa: E402
    _CHAT_AVAILABLE = True
except ImportError as _chat_import_err:
    logger_pre = logging.getLogger("chat_server")
    logger_pre.warning("Chat modules not available: %s", _chat_import_err)
    _CHAT_AVAILABLE = False

logger = logging.getLogger("chat_server")




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
from atlas.dashboard.api.portfolio import router as _portfolio_router  # noqa: E402
from atlas.dashboard.api.health import router as _health_router       # noqa: E402
from atlas.dashboard.api.dashboard import router as _dashboard_router  # noqa: E402
from atlas.dashboard.chat.sessions import router as _chat_sessions_router  # noqa: E402
from atlas.dashboard.chat.ws import router as _ws_chat_router  # noqa: E402
from atlas.dashboard.api.forge import router as _forge_router  # noqa: E402
from atlas.dashboard.api.live import router as _live_router  # noqa: E402
from atlas.dashboard.api.static_serve import router as _static_serve_router  # noqa: E402

# ── Re-export shims (backward-compat for tests importing from chat_server) ────
from atlas.dashboard.api.dashboard import (  # noqa: F401
    _calc_alpaca_intraday_pnl,
    _calc_tiingo_daily_pnl,
    _build_dashboard_data,
)
from atlas.dashboard.auth import check_auth  # noqa: F401 — re-exported for backward compat (tests)

app.include_router(_portfolio_router)
app.include_router(_health_router)
app.include_router(_dashboard_router)
app.include_router(_chat_sessions_router)
app.include_router(_ws_chat_router)
# Note: lifecycle endpoints live exclusively in services/api/lifecycle.py.
# An orphan strategy_lifecycle.py was deleted 2026-05-14 (commit edfe6efa).
# Future routers MUST be mounted here via app.include_router() and live under
# services/api/ (or services/ws/ for WebSocket routers).
app.include_router(_forge_router)
app.include_router(_live_router)
# IMPORTANT: static_serve router must be LAST — contains /{path:path} catch-all
app.include_router(_static_serve_router)

# Static serving, WS, and agent routes moved to services/ sub-routers (Phase 10)

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=BIND, port=PORT, log_level="info")
