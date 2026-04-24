"""Regression tests for execute_approved.py auto-approve flow.

Tests:
  1. test_auto_approve_config_flips_pending_to_approved
  2. test_auto_approve_false_leaves_plan_pending
  3. test_manual_approve_preserves_human_approver
  4. test_auto_approve_emits_audit_log
  5. test_auto_approve_sets_plan_annotations
  6. test_auto_approve_sends_telegram_notification
  7. test_auto_approve_none_return_aborts
  8. test_approve_plan_auto_kwarg_sets_approver_auto
  9. test_approve_plan_default_kwarg_human_approver
  10. test_approve_plan_approval_reason_only_when_auto_true
"""
from __future__ import annotations

import json
import sys
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call
import pytest

# ── ensure project root is on sys.path ─────────────────────
PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))


# ─── Fixtures ────────────────────────────────────────────────────────────────

def _make_plan(status: str = "PENDING_APPROVAL") -> dict:
    return {
        "trade_date": "2026-04-24",
        "market_id": "sp500",
        "status": status,
        "proposed_entries": [
            {"ticker": "AAPL", "position_size": 10},
            {"ticker": "MSFT", "position_size": 5},
        ],
        "proposed_exits": [
            {"ticker": "NVDA"},
        ],
        "portfolio_snapshot": {
            "equity": 10000.0,
            "cash": 5000.0,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "open_positions": 1,
        },
    }


def _make_config(auto_approve: bool = True, mode: str = "live") -> dict:
    return {
        "version": "vtest",
        "market": "sp500",
        "trading": {
            "mode": mode,
            "auto_approve": auto_approve,
            "live_enabled": True,
            "broker": "alpaca",
        },
    }


# ─── Tests for brokers/plan.py::approve_plan ─────────────────────────────────

class TestApprovePlanSignature:
    """Direct tests of TradePlanGenerator.approve_plan kwargs."""

    def _make_generator(self, tmp_path: Path, plan: dict):
        """Build a TradePlanGenerator backed by a tmp plan file."""
        from brokers.plan import TradePlanGenerator

        config = _make_config()
        gen = TradePlanGenerator(None, config)
        saved = {}

        def fake_load(date, market_id=""):
            return dict(plan)

        def fake_save(p, date):
            saved["plan"] = dict(p)

        gen.load_plan = fake_load
        gen._save_plan = fake_save
        gen._saved = saved
        return gen

    def test_approve_plan_auto_kwarg_sets_approver_auto(self, tmp_path):
        plan = _make_plan("PENDING_APPROVAL")
        gen = self._make_generator(tmp_path, plan)
        result = gen.approve_plan("2026-04-24", market_id="sp500", auto=True, approver="auto")
        assert result is not None
        assert result["approver"] == "auto"
        assert result["approval_reason"] == "auto_approve_plans config flag"
        assert result["status"] == "APPROVED"

    def test_approve_plan_default_kwarg_human_approver(self, tmp_path):
        plan = _make_plan("PENDING_APPROVAL")
        gen = self._make_generator(tmp_path, plan)
        result = gen.approve_plan("2026-04-24", market_id="sp500")
        assert result is not None
        assert result["approver"] == "human"
        assert "approval_reason" not in result
        assert result["status"] == "APPROVED"

    def test_approve_plan_approval_reason_only_when_auto_true(self, tmp_path):
        """approval_reason should ONLY appear when auto=True."""
        plan = _make_plan("DRAFT")
        gen = self._make_generator(tmp_path, plan)
        # auto=False (default) → no approval_reason key
        result = gen.approve_plan("2026-04-24", market_id="sp500", auto=False)
        assert "approval_reason" not in result
        # auto=True → approval_reason present
        result2 = gen.approve_plan("2026-04-24", market_id="sp500", auto=True)
        assert result2["approval_reason"] == "auto_approve_plans config flag"

    def test_approve_plan_returns_none_when_no_plan(self, tmp_path):
        from brokers.plan import TradePlanGenerator
        config = _make_config()
        gen = TradePlanGenerator(None, config)
        gen.load_plan = lambda *a, **kw: None
        gen._save_plan = lambda *a, **kw: None
        result = gen.approve_plan("2026-04-24")
        assert result is None


# ─── Helpers for integration tests ──────────────────────────────────────────

class _FakeGen:
    """Minimal TradePlanGenerator stub for testing execute_approved.main()."""

    def __init__(self, plan: dict | None, approved_plan: dict | None = None):
        self._plan = plan
        self._approved_template = approved_plan
        self.approve_calls: list[dict] = []
        self._saved: list[dict] = []

    def load_plan(self, trade_date, market_id=""):
        return self._plan

    def approve_plan(self, trade_date, market_id="", auto=False, approver="human"):
        self.approve_calls.append({"auto": auto, "approver": approver})
        if self._approved_template is None:
            return None
        result = dict(self._approved_template)
        result["status"] = "APPROVED"
        result["approved_at"] = "2026-04-24T10:00:00"
        result["approver"] = approver
        if auto:
            result["approval_reason"] = "auto_approve_plans config flag"
        return result

    def _save_plan(self, plan, trade_date):
        self._saved.append(dict(plan))


class _FakeExecutor:
    def __init__(self):
        self.is_dry_run = False
        self.execute_plan_calls = 0

    def connect(self):
        return True

    def disconnect(self):
        pass

    def execute_plan(self, plan, trade_date):
        self.execute_plan_calls += 1
        return {
            "successful_entries": 2,
            "successful_exits": 1,
            "total_entries": 2,
            "total_exits": 1,
            "entries": [],
        }


def _run_main(
    market: str = "sp500",
    auto_approve: bool = True,
    plan_status: str = "PENDING_APPROVAL",
    mode: str = "live",
):
    """
    Run execute_approved.main() with mocked dependencies.
    Returns (fake_gen, fake_executor, mock_notify_auto_approve).

    Note: execute_approved.py uses local imports inside main(), so we patch
    at the SOURCE module level (utils.config, brokers.plan, brokers.live_executor).
    """
    # Force re-import since the module may be cached with prior patches
    import importlib
    import scripts.execute_approved as ea
    importlib.reload(ea)

    plan = _make_plan(plan_status)
    approved_plan = _make_plan("APPROVED")
    config = _make_config(auto_approve=auto_approve, mode=mode)
    gen = _FakeGen(plan, approved_plan)
    executor = _FakeExecutor()

    with (
        patch("utils.config.get_active_config", return_value=config),
        patch("brokers.plan.TradePlanGenerator", return_value=gen),
        patch("brokers.live_executor.LiveExecutor", return_value=executor),
        patch("utils.telegram.send_message"),
        patch.object(ea, "_notify_execution"),
        patch.object(ea, "_notify_auto_approve") as mock_notify_auto,
    ):
        old_argv = sys.argv
        sys.argv = ["execute_approved.py", "-m", market]
        try:
            ea.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv

    return gen, executor, mock_notify_auto


# ─── Integration tests for execute_approved.py auto-approve logic ─────────────

class TestAutoApproveFlow:

    def test_auto_approve_config_flips_pending_to_approved(self):
        """auto_approve=True + PENDING_APPROVAL → approve_plan called with auto=True."""
        gen, executor, _ = _run_main(auto_approve=True, plan_status="PENDING_APPROVAL")
        assert len(gen.approve_calls) == 1
        assert gen.approve_calls[0]["auto"] is True
        assert gen.approve_calls[0]["approver"] == "auto"
        # Execution should have proceeded
        assert executor.execute_plan_calls == 1

    def test_auto_approve_false_leaves_plan_pending(self):
        """auto_approve=False + PENDING_APPROVAL → approve_plan NOT called, no execution."""
        gen, executor, _ = _run_main(auto_approve=False, plan_status="PENDING_APPROVAL")
        assert len(gen.approve_calls) == 0, "approve_plan must NOT be called when auto_approve=False"
        assert executor.execute_plan_calls == 0

    def test_auto_approve_sets_plan_annotations(self):
        """After auto-approve, plan dict must have auto_approved=True + approval_source."""
        import importlib
        import scripts.execute_approved as ea
        importlib.reload(ea)

        plan = _make_plan("PENDING_APPROVAL")
        approved_plan = _make_plan("APPROVED")
        config = _make_config(auto_approve=True)
        gen = _FakeGen(plan, approved_plan)
        executor = _FakeExecutor()

        # Intercept what plan is passed to execute_plan
        captured_plan = {}
        real_execute = executor.execute_plan

        def tracking_execute(p, date):
            captured_plan.update(p)
            return real_execute(p, date)

        executor.execute_plan = tracking_execute

        with (
            patch("utils.config.get_active_config", return_value=config),
            patch("brokers.plan.TradePlanGenerator", return_value=gen),
            patch("brokers.live_executor.LiveExecutor", return_value=executor),
            patch("utils.telegram.send_message"),
            patch.object(ea, "_notify_execution"),
            patch.object(ea, "_notify_auto_approve"),
        ):
            old_argv = sys.argv
            sys.argv = ["execute_approved.py", "-m", "sp500"]
            try:
                ea.main()
            except SystemExit:
                pass
            finally:
                sys.argv = old_argv

        assert captured_plan.get("auto_approved") is True, (
            "plan must have auto_approved=True when passed to execute_plan"
        )
        assert captured_plan.get("approval_source") == "auto_approve_config_flag"

    def test_auto_approve_emits_audit_log(self, caplog):
        """AUTO_APPROVE warning with n_entries/n_exits must be logged."""
        import importlib
        import scripts.execute_approved as ea
        importlib.reload(ea)

        plan = _make_plan("PENDING_APPROVAL")
        approved_plan = _make_plan("APPROVED")
        config = _make_config(auto_approve=True)
        gen = _FakeGen(plan, approved_plan)
        executor = _FakeExecutor()

        with (
            patch("utils.config.get_active_config", return_value=config),
            patch("brokers.plan.TradePlanGenerator", return_value=gen),
            patch("brokers.live_executor.LiveExecutor", return_value=executor),
            patch("utils.telegram.send_message"),
            patch.object(ea, "_notify_execution"),
            patch.object(ea, "_notify_auto_approve"),
            caplog.at_level(logging.WARNING),
        ):
            old_argv = sys.argv
            sys.argv = ["execute_approved.py", "-m", "sp500"]
            try:
                ea.main()
            except SystemExit:
                pass
            finally:
                sys.argv = old_argv

        audit_lines = [r.message for r in caplog.records if "AUTO_APPROVE" in r.message]
        assert audit_lines, "Expected at least one AUTO_APPROVE audit log line"
        assert "n_entries=2" in audit_lines[0]
        assert "n_exits=1" in audit_lines[0]

    def test_auto_approve_sends_telegram_notification(self):
        """_notify_auto_approve must be called once with correct args."""
        gen, executor, mock_notify = _run_main(auto_approve=True, plan_status="PENDING_APPROVAL")
        assert mock_notify.call_count == 1
        pos_args = mock_notify.call_args[0]
        assert pos_args[0] == "sp500"   # market_id
        assert pos_args[2] == 2         # n_entries
        assert pos_args[3] == 1         # n_exits

    def test_auto_approve_none_return_aborts(self):
        """If approve_plan returns None, execution must abort (no orders)."""
        import importlib
        import scripts.execute_approved as ea
        importlib.reload(ea)

        plan = _make_plan("PENDING_APPROVAL")
        config = _make_config(auto_approve=True)
        gen = _FakeGen(plan, approved_plan=None)  # approve_plan → None
        executor = _FakeExecutor()

        with (
            patch("utils.config.get_active_config", return_value=config),
            patch("brokers.plan.TradePlanGenerator", return_value=gen),
            patch("brokers.live_executor.LiveExecutor", return_value=executor),
            patch("utils.telegram.send_message"),
            patch.object(ea, "_notify_auto_approve"),
        ):
            old_argv = sys.argv
            sys.argv = ["execute_approved.py", "-m", "sp500"]
            try:
                ea.main()
            except SystemExit:
                pass
            finally:
                sys.argv = old_argv

        assert executor.execute_plan_calls == 0, (
            "execute_plan must NOT be called when approve_plan returns None"
        )


class TestManualApprove:

    def test_manual_approve_preserves_human_approver(self, tmp_path):
        """cli.py cmd_approve calls approve_plan with default kwargs → approver='human'."""
        from brokers.plan import TradePlanGenerator

        plan = _make_plan("PENDING_APPROVAL")
        config = _make_config()
        gen = TradePlanGenerator(None, config)
        gen.load_plan = lambda *a, **kw: dict(plan)
        gen._save_plan = lambda *a, **kw: None

        # Simulate cli.py: plan_gen.approve_plan(trade_date) — no auto/approver kwargs
        result = gen.approve_plan("2026-04-24", market_id="sp500")

        assert result is not None
        assert result["approver"] == "human", (
            "cli.py calls approve_plan() without auto= kwargs — must default to 'human'"
        )
        assert "approval_reason" not in result

    def test_telegram_approve_button_preserves_human_approver(self, tmp_path):
        """telegram_bot.py calls approve_plan(trade_date, market_id=x) → approver='human'."""
        from brokers.plan import TradePlanGenerator

        plan = _make_plan("GENERATED")
        config = _make_config()
        gen = TradePlanGenerator(None, config)
        gen.load_plan = lambda *a, **kw: dict(plan)
        gen._save_plan = lambda *a, **kw: None

        # telegram_bot.py: plan_gen.approve_plan(trade_date, market_id=market_id)
        result = gen.approve_plan("2026-04-24", market_id="sp500")
        assert result["approver"] == "human"
        assert result.get("auto_approved") is None


class TestAutoApproveAllStatuses:
    """All eligible statuses should trigger auto-approve."""

    @pytest.mark.parametrize("status", ["", "PENDING", "PENDING_APPROVAL", "GENERATED", "DRAFT"])
    def test_eligible_statuses_auto_approve(self, status):
        gen, executor, _ = _run_main(auto_approve=True, plan_status=status)
        assert len(gen.approve_calls) == 1, f"approve_plan must be called for status={status!r}"
        assert executor.execute_plan_calls == 1

    def test_already_approved_skips_auto_approve(self):
        """APPROVED plans must NOT re-trigger approve_plan."""
        gen, executor, _ = _run_main(auto_approve=True, plan_status="APPROVED")
        assert len(gen.approve_calls) == 0, "approve_plan must NOT be called for already-APPROVED plan"
        # But execution should still happen
        assert executor.execute_plan_calls == 1
