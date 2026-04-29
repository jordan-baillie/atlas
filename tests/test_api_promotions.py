"""Tests for services/api/promotions.py — Phase 7 extraction."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from fastapi.testclient import TestClient
from fastapi import FastAPI
from services.api.promotions import router

_AUTH = ("test", "test")


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("services.auth._get_credentials", lambda: _AUTH)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestPromotionsPending:
    def test_endpoint_registered(self, client):
        """GET /api/promotions/pending returns 200."""
        with patch("research.promoter.expire_pending_promotions"), \
             patch("research.promoter._load_pending", return_value=[]):
            resp = client.get("/api/promotions/pending", auth=_AUTH)
        assert resp.status_code == 200

    def test_returns_only_pending_entries(self, client):
        """Filters to status=pending only."""
        entries = [
            {"id": "p1", "strategy": "momentum", "status": "pending"},
            {"id": "p2", "strategy": "trend", "status": "approved"},
        ]
        with patch("research.promoter.expire_pending_promotions"), \
             patch("research.promoter._load_pending", return_value=entries):
            resp = client.get("/api/promotions/pending", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["pending"][0]["id"] == "p1"

    def test_empty_pending(self, client):
        """Returns count=0 when no pending promotions."""
        with patch("research.promoter.expire_pending_promotions"), \
             patch("research.promoter._load_pending", return_value=[]):
            resp = client.get("/api/promotions/pending", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["pending"] == []


class TestPromotionsApprove:
    def test_approve_success_returns_200(self, client):
        """Returns approved=True and 200 on successful promotion."""
        result = {
            "promoted": True, "version": "1.0.3",
            "strategy": "momentum_breakout", "market": "sp500",
        }
        with patch("research.promoter.complete_pending_promotion", return_value=result):
            resp = client.post("/api/promotions/p1/approve", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["approved"] is True
        assert body["strategy"] == "momentum_breakout"

    def test_approve_not_found_returns_404(self, client):
        """Returns 404 when pending_id not found."""
        with patch("research.promoter.complete_pending_promotion",
                   return_value={"promoted": False, "reason": "not found: p999"}):
            resp = client.post("/api/promotions/p999/approve", auth=_AUTH)
        assert resp.status_code == 404

    def test_approve_already_approved_returns_409(self, client):
        """Returns 409 when promotion was already applied."""
        with patch("research.promoter.complete_pending_promotion",
                   return_value={"promoted": False, "reason": "already applied"}):
            resp = client.post("/api/promotions/p1/approve", auth=_AUTH)
        assert resp.status_code == 409


class TestPromotionsReject:
    def test_reject_success_returns_200(self, client):
        """Returns rejected=True and 200 on successful rejection."""
        with patch("research.promoter.reject_pending_promotion",
                   return_value={"rejected": True, "strategy": "momentum_breakout"}):
            resp = client.post("/api/promotions/p1/reject", auth=_AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["rejected"] is True

    def test_reject_not_found_returns_404(self, client):
        """Returns 404 when pending_id not found."""
        with patch("research.promoter.reject_pending_promotion",
                   return_value={"rejected": False, "reason": "not found"}):
            resp = client.post("/api/promotions/p999/reject", auth=_AUTH)
        assert resp.status_code == 404

    def test_reject_with_reason_query_param(self, client):
        """Accepts ?reason= query param."""
        with patch("research.promoter.reject_pending_promotion",
                   return_value={"rejected": True, "strategy": "test"}):
            resp = client.post(
                "/api/promotions/p1/reject?reason=bad+model", auth=_AUTH
            )
        assert resp.status_code == 200
