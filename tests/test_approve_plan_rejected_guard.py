"""Regression tests for plan-approval guards.

Ensures approve_plan() refuses to re-approve a REJECTED plan,
preventing silent override of explicit rejections.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brokers.plan import TradePlanGenerator, PlanAlreadyRejectedError


class _StubPortfolio:
    """Minimal portfolio stub for TradePlanGenerator construction."""
    market_id = "sp500"
    starting_equity = 1000.0
    cash = 1000.0
    positions: list = []


def _make_plan_file(plans_dir: Path, trade_date: str, status: str, market_id: str = "sp500") -> Path:
    """Write a minimal plan JSON file to plans_dir."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "trade_date": trade_date,
        "market_id": market_id,
        "status": status,
        "portfolio_snapshot": {"equity": 1000.0, "cash": 1000.0, "total_pnl": 0.0,
                                "total_pnl_pct": 0.0, "open_positions": 0},
        "proposed_entries": [],
        "proposed_exits": [],
        "rejected_entries": [],
        "risk_summary": {"total_proposed_cost": 0.0, "total_proposed_risk": 0.0},
    }
    fname = f"plan_{market_id}_{trade_date}.json"
    path = plans_dir / fname
    path.write_text(json.dumps(plan))
    return path


@pytest.fixture
def plan_gen(tmp_path, monkeypatch):
    """Create a TradePlanGenerator with PROJECT_ROOT pointing to tmp_path.

    Both _save_plan() and load_plan() resolve paths as
    PROJECT_ROOT / self.PLANS_DIR, so patching the module-level
    PROJECT_ROOT constant is the correct way to redirect file I/O.
    """
    import brokers.plan as plan_mod

    # Redirect all plan file I/O to tmp_path
    monkeypatch.setattr(plan_mod, "PROJECT_ROOT", tmp_path)

    # Ensure plans subdir exists so mkdir(exist_ok=True) is a no-op
    plans_dir = tmp_path / TradePlanGenerator.PLANS_DIR
    plans_dir.mkdir(parents=True, exist_ok=True)

    portfolio = _StubPortfolio()
    config: dict = {}
    gen = TradePlanGenerator(portfolio, config)
    return gen, plans_dir


def test_approve_rejected_plan_raises(plan_gen):
    """approve_plan() must raise PlanAlreadyRejectedError on a REJECTED plan."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "REJECTED", market_id="sp500")
    with pytest.raises(PlanAlreadyRejectedError, match="REJECTED"):
        gen.approve_plan("2026-05-01", market_id="sp500")


def test_approve_pending_plan_succeeds(plan_gen):
    """approve_plan() must approve a PENDING plan and persist status=APPROVED."""
    gen, plans_dir = plan_gen
    path = _make_plan_file(plans_dir, "2026-05-01", "PENDING", market_id="sp500")
    result = gen.approve_plan("2026-05-01", market_id="sp500")
    assert result is not None
    assert result["status"] == "APPROVED"
    on_disk = json.loads(path.read_text())
    assert on_disk["status"] == "APPROVED"
    assert "approved_at" in on_disk
    assert on_disk["approver"] == "human"


def test_approve_already_approved_idempotent(plan_gen):
    """Re-approving an APPROVED plan is idempotent -- same state, no error."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "APPROVED", market_id="sp500")
    result = gen.approve_plan("2026-05-01", market_id="sp500")
    assert result is not None
    assert result["status"] == "APPROVED"


def test_approve_missing_plan_returns_none(plan_gen):
    """approve_plan() must return None when no plan file is found."""
    gen, _ = plan_gen
    result = gen.approve_plan("2026-05-99", market_id="sp500")
    assert result is None


def test_approve_lowercase_rejected_also_blocked(plan_gen):
    """Status comparison must be case-insensitive -- 'rejected' is also blocked."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "rejected", market_id="sp500")
    with pytest.raises(PlanAlreadyRejectedError):
        gen.approve_plan("2026-05-01", market_id="sp500")


def test_auto_approve_rejected_also_blocked(plan_gen):
    """Auto-approval (auto=True) must also be blocked on a REJECTED plan."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "REJECTED", market_id="sp500")
    with pytest.raises(PlanAlreadyRejectedError):
        gen.approve_plan("2026-05-01", market_id="sp500", auto=True, approver="auto")


def test_error_message_contains_date_and_market(plan_gen):
    """Exception message must include trade_date and market_id for diagnostics."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "REJECTED", market_id="commodity_etfs")
    with pytest.raises(PlanAlreadyRejectedError) as exc_info:
        gen.approve_plan("2026-05-01", market_id="commodity_etfs")
    msg = str(exc_info.value)
    assert "2026-05-01" in msg
    assert "commodity_etfs" in msg


def test_error_message_default_market(plan_gen):
    """When market_id='', error message shows 'default' as market placeholder."""
    gen, plans_dir = plan_gen
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "trade_date": "2026-05-01",
        "market_id": "",
        "status": "REJECTED",
        "portfolio_snapshot": {},
        "proposed_entries": [],
        "proposed_exits": [],
        "rejected_entries": [],
        "risk_summary": {},
    }
    (plans_dir / "plan_2026-05-01.json").write_text(json.dumps(plan))
    with pytest.raises(PlanAlreadyRejectedError) as exc_info:
        gen.approve_plan("2026-05-01", market_id="")
    assert "default" in str(exc_info.value)


def test_pending_approval_status_succeeds(plan_gen):
    """PENDING_APPROVAL status (common generated status) must not be blocked."""
    gen, plans_dir = plan_gen
    _make_plan_file(plans_dir, "2026-05-01", "PENDING_APPROVAL", market_id="sp500")
    result = gen.approve_plan("2026-05-01", market_id="sp500")
    assert result is not None
    assert result["status"] == "APPROVED"


def test_auto_approve_annotation_written(plan_gen):
    """When auto=True, approval_reason must be written to the plan."""
    gen, plans_dir = plan_gen
    path = _make_plan_file(plans_dir, "2026-05-01", "PENDING", market_id="sp500")
    result = gen.approve_plan("2026-05-01", market_id="sp500", auto=True, approver="auto")
    assert result is not None
    assert result["approval_reason"] == "auto_approve_plans config flag"
    assert result["approver"] == "auto"
    on_disk = json.loads(path.read_text())
    assert on_disk["approval_reason"] == "auto_approve_plans config flag"


def test_plan_already_rejected_error_is_exception_subclass():
    """PlanAlreadyRejectedError must be a proper Exception subclass."""
    assert issubclass(PlanAlreadyRejectedError, Exception)


def test_rejected_guard_does_not_mutate_plan_file(plan_gen):
    """When approve_plan raises, the plan file must remain unchanged (REJECTED)."""
    gen, plans_dir = plan_gen
    path = _make_plan_file(plans_dir, "2026-05-01", "REJECTED", market_id="sp500")
    original_content = path.read_text()

    with pytest.raises(PlanAlreadyRejectedError):
        gen.approve_plan("2026-05-01", market_id="sp500")

    # File must be exactly unchanged
    assert path.read_text() == original_content
