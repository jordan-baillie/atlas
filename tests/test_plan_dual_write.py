"""Regression tests for _save_plan UPDATE-or-INSERT idempotency.

Bug fixed: previously every call to _save_plan() did a raw INSERT, resulting
in 3 rows per (date, market_id) per trading day:
  1. generate_plan()          -> status=pending_approval
  2. _run_regime_aware_plan() -> status=pending_approval (re-persist after regime)
  3. execute_approved.py      -> status=executed

Fix: _save_plan() now does get_plan() first; if a row exists it calls
update_plan() to mutate in place; only inserts on the very first call.

Run with:  python -m pytest tests/test_plan_dual_write.py -v --timeout=30
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.plan import TradePlanGenerator  # noqa: E402
from db import atlas_db  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = datetime.today().strftime("%Y-%m-%d")
YESTERDAY = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def _make_generator(tmp_path: Path) -> TradePlanGenerator:
    """Minimal TradePlanGenerator - no broker calls needed."""
    portfolio = MagicMock()
    config = {
        "market": "sp500",
        "version": "test-v1.0",
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.01,
            "min_confidence": 0.65,
            "max_open_positions": 5,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": 0.05,
            "require_stop_loss": True,
            "require_planned_exit": True,
        },
        "fees": {
            "commission_per_trade": 0,
            "commission_pct": 0.0,
            "slippage_pct": 0.0005,
            "min_position_value": 100.0,
            "flat_fee_threshold": 0,
        },
        "trading": {
            "mode": "paper",
            "broker": "alpaca",
            "live_enabled": False,
        },
    }
    return TradePlanGenerator(portfolio, config)


def _minimal_plan(trade_date: str = TODAY, status: str = "PENDING_APPROVAL") -> dict:
    return {
        "trade_date": trade_date,
        "market_id": "sp500",
        "status": status,
        "entries": [],
        "exits": [],
        "rejected_entries": [],
    }


def _count_plan_rows(trade_date: str, market_id: str = "sp500") -> int:
    """Count actual DB rows for (date, market_id) - bypasses get_plan() ORDER BY."""
    with atlas_db.get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM plans WHERE date=? AND market_id=?",
            (trade_date, market_id),
        ).fetchone()
        return row["n"] if row else 0


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlanDualWriteIdempotency:
    """Verify _save_plan never inserts more than one row per (date, market_id)."""

    def test_save_plan_first_call_inserts_pending_approval(self, tmp_path):
        """Initial save creates exactly 1 row with status='pending_approval'."""
        gen = _make_generator(tmp_path)
        plan = _minimal_plan()

        with patch("brokers.plan.PROJECT_ROOT", tmp_path):
            gen._save_plan(plan, TODAY)

        assert _count_plan_rows(TODAY) == 1
        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing is not None
        assert existing["status"] == "pending_approval"

    def test_save_plan_second_call_updates_not_inserts(self, tmp_path):
        """Calling _save_plan twice for same (date, market) -> exactly 1 row."""
        gen = _make_generator(tmp_path)
        plan = _minimal_plan()

        with patch("brokers.plan.PROJECT_ROOT", tmp_path):
            gen._save_plan(plan, TODAY)
            gen._save_plan(plan, TODAY)  # regime re-persist (2nd call same status)

        assert _count_plan_rows(TODAY) == 1

    def test_approve_plan_updates_same_row(self, tmp_path):
        """pending_approval -> approved still results in exactly 1 row,
        with the approved status and approved_at timestamp set."""
        gen = _make_generator(tmp_path)
        approved_at = datetime.now().isoformat()

        plan_pending = _minimal_plan(status="PENDING_APPROVAL")
        plan_approved = _minimal_plan(status="APPROVED")
        plan_approved["approved_at"] = approved_at

        with patch("brokers.plan.PROJECT_ROOT", tmp_path):
            gen._save_plan(plan_pending, TODAY)    # insert (pending_approval)
            gen._save_plan(plan_approved, TODAY)   # update (approved)

        assert _count_plan_rows(TODAY) == 1
        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing is not None
        assert existing["status"] == "approved"
        assert existing["approved_at"] == approved_at

    def test_execute_plan_updates_same_row(self, tmp_path):
        """Full lifecycle: pending_approval -> approved -> executed -> exactly 1 row
        with status='executed' and executed_at set."""
        gen = _make_generator(tmp_path)
        approved_at = datetime.now().isoformat()
        executed_at = datetime.now().isoformat()

        plan_pending = _minimal_plan(status="PENDING_APPROVAL")
        plan_approved = _minimal_plan(status="APPROVED")
        plan_approved["approved_at"] = approved_at
        plan_executed = _minimal_plan(status="EXECUTED")
        plan_executed["approved_at"] = approved_at
        plan_executed["executed_at"] = executed_at

        with patch("brokers.plan.PROJECT_ROOT", tmp_path):
            gen._save_plan(plan_pending, TODAY)    # 1st call -> INSERT
            gen._save_plan(plan_approved, TODAY)   # 2nd call -> UPDATE (approved)
            gen._save_plan(plan_executed, TODAY)   # 3rd call -> UPDATE (executed)

        assert _count_plan_rows(TODAY) == 1
        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing is not None
        assert existing["status"] == "executed"
        assert existing["approved_at"] == approved_at
        assert existing["executed_at"] == executed_at

    def test_save_plan_different_date_inserts_new_row(self, tmp_path):
        """Two different trade_dates produce two separate DB rows."""
        gen = _make_generator(tmp_path)

        plan_today = _minimal_plan(trade_date=TODAY)
        plan_yesterday = _minimal_plan(trade_date=YESTERDAY)

        with patch("brokers.plan.PROJECT_ROOT", tmp_path):
            gen._save_plan(plan_today, TODAY)
            gen._save_plan(plan_yesterday, YESTERDAY)

        assert _count_plan_rows(TODAY) == 1
        assert _count_plan_rows(YESTERDAY) == 1


class TestUpdatePlanFunction:
    """Unit tests for the new atlas_db.update_plan() helper."""

    def test_update_status_only(self):
        """update_plan with only status updates just the status field."""
        plan_id = atlas_db.record_plan(
            date=TODAY,
            market_id="sp500",
            plan_data={"trade_date": TODAY},
            status="pending_approval",
        )
        atlas_db.update_plan(plan_id, status="approved")

        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing["status"] == "approved"
        assert existing["approved_at"] is None  # not set - COALESCE kept None

    def test_update_plan_data_overwrites(self):
        """update_plan with plan_data replaces the stored JSON."""
        plan_id = atlas_db.record_plan(
            date=TODAY,
            market_id="sp500",
            plan_data={"v": 1, "trade_date": TODAY},
            status="pending_approval",
        )
        atlas_db.update_plan(plan_id, plan_data={"v": 2, "trade_date": TODAY})

        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing["plan_data"]["v"] == 2

    def test_update_plan_none_fields_not_overwritten(self):
        """Passing None for a field leaves the existing value unchanged."""
        plan_id = atlas_db.record_plan(
            date=TODAY,
            market_id="sp500",
            plan_data={"trade_date": TODAY},
            status="approved",
        )
        # Update only executed_at; status should remain 'approved'
        atlas_db.update_plan(plan_id, executed_at="2099-01-01T00:00:00")

        existing = atlas_db.get_plan(TODAY, "sp500")
        assert existing["status"] == "approved"         # unchanged
        assert existing["executed_at"] == "2099-01-01T00:00:00"
