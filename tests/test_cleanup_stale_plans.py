"""Tests for scripts/cleanup_stale_plans.py.

All tests use the autouse _isolate_prod_db fixture from conftest.py so they
operate on a throw-away tmp SQLite DB and never touch data/atlas.db.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_plan(status: str, created_at: str) -> int:
    """Insert a minimal test plan row; return its id."""
    with get_db() as db:
        cur = db.execute(
            """
            INSERT INTO plans (date, market_id, plan_data, status, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("2024-01-01", "sp500", "{}", status, created_at),
        )
        return cur.lastrowid


def _recent_ts() -> str:
    """Timestamp 5 days ago — inside the 14-day window."""
    return (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _stale_ts() -> str:
    """Timestamp 20 days ago — outside the 14-day window."""
    return (datetime.now(timezone.utc) - timedelta(days=20)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _get_status(plan_id: int) -> str:
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM plans WHERE id=?", (plan_id,)
        ).fetchone()
    assert row is not None, f"Plan {plan_id} not found"
    return row["status"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExpireStale:
    """Core expiry logic."""

    def test_recent_plans_stay_pending(self):
        """Plans < 14 days old must remain pending after cleanup."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        plan_id = _insert_plan("pending", _recent_ts())
        expire_stale_plans()
        assert _get_status(plan_id) == "pending"

    def test_stale_plans_become_expired(self):
        """Plans > 14 days old with status='pending' must become 'expired'."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        plan_id = _insert_plan("pending", _stale_ts())
        count = expire_stale_plans()
        assert _get_status(plan_id) == "expired"
        assert count >= 1

    def test_count_reflects_affected_rows(self):
        """Return value equals the number of rows actually changed."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        _insert_plan("pending", _stale_ts())
        _insert_plan("pending", _stale_ts())
        count = expire_stale_plans()
        assert count == 2

    def test_boundary_exactly_14_days_not_expired(self):
        """A plan created exactly 14 days ago (not older) stays pending."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        # datetime('now', '-14 days') excludes equal — use 13d 23h to be safe
        ts = (datetime.now(timezone.utc) - timedelta(days=13, hours=23)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        plan_id = _insert_plan("pending", ts)
        expire_stale_plans()
        assert _get_status(plan_id) == "pending"


class TestNonPendingUntouched:
    """Only 'pending' rows are eligible for expiry."""

    @pytest.mark.parametrize("status", ["approved", "executed", "rejected", "expired"])
    def test_non_pending_untouched(self, status):
        """Stale plans with non-pending status must not be modified."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        plan_id = _insert_plan(status, _stale_ts())
        expire_stale_plans()
        assert _get_status(plan_id) == status, (
            f"status='{status}' was mutated — should be untouched"
        )


class TestIdempotent:
    """Running twice produces zero changes on the second run."""

    def test_idempotent(self):
        """Second call returns count=0 when no new stale plans exist."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        _insert_plan("pending", _stale_ts())

        first = expire_stale_plans()
        second = expire_stale_plans()

        assert first >= 1, "First run must expire at least one plan"
        assert second == 0, "Second run must find nothing to expire"

    def test_idempotent_mixed_pool(self):
        """With a mix of recent + stale plans, only stale ones expire once."""
        from scripts.cleanup_stale_plans import expire_stale_plans

        recent_id = _insert_plan("pending", _recent_ts())
        stale_id = _insert_plan("pending", _stale_ts())

        first = expire_stale_plans()
        second = expire_stale_plans()

        assert _get_status(recent_id) == "pending"
        assert _get_status(stale_id) == "expired"
        assert first == 1
        assert second == 0
