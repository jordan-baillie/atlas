"""Shared HTTP Basic Auth dependency for Atlas dashboard API.

Extracted from services/chat_server.py to allow circular-import-free
use in services/api/* routers (per docs/phase-c-god-file-decomposition.md).

Usage:
    from atlas.dashboard.auth import check_auth
    @router.get("/")
    def my_route(_auth: HTTPBasicCredentials = Depends(check_auth)):
        ...
"""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from fastapi import Depends, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

SECRETS_PATH = Path(os.environ.get("ATLAS_SECRETS_PATH", str(Path.home() / ".atlas-secrets.json")))

security = HTTPBasic(realm="Atlas Dashboard")

# Module-level credential cache (loaded once on first request)
_CREDENTIALS: tuple[str, str] | None = None


def _load_credentials() -> tuple[str, str]:
    """Load dashboard credentials from ~/.atlas-secrets.json."""
    if not SECRETS_PATH.exists():
        raise ValueError(f"Secrets file not found: {SECRETS_PATH}")
    with open(SECRETS_PATH) as f:
        s = json.load(f)
    user = s.get("dashboard_user", "")
    pw = s.get("dashboard_pass", "")
    if not user or not pw:
        raise ValueError(
            "Set dashboard_user and dashboard_pass in ~/.atlas-secrets.json"
        )
    return user, pw


def _get_credentials() -> tuple[str, str]:
    global _CREDENTIALS
    if _CREDENTIALS is None:
        _CREDENTIALS = _load_credentials()
    return _CREDENTIALS


def check_auth(
    credentials: HTTPBasicCredentials = Depends(security),
) -> HTTPBasicCredentials:
    """FastAPI dependency: HTTP Basic Auth via ~/.atlas-secrets.json.

    Uses secrets.compare_digest for timing-safe comparison.
    Raises 401 with WWW-Authenticate: Basic realm="Atlas Dashboard" on failure.
    """
    expected_user, expected_pass = _get_credentials()
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        expected_user.encode("utf-8"),
    )
    pass_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        expected_pass.encode("utf-8"),
    )
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Atlas Dashboard"'},
        )
    return credentials
