"""Regression tests for execute_approved.py plan-status integrity (P1 bug fix).

Covers:
  1. all_entries_to_paper   — status=EXECUTED_PAPER, routing_summary, broker_mode tags
  2. all_entries_to_live    — status=EXECUTED, live_submitted correct
  3. broker_submit_all_rejected — status=FAILED (NOT EXECUTED)
  4. executor_returns_none  — plan.status stays APPROVED (no report → no stamp)
  5. verify_mismatch        — status=EXECUTED_VERIFY_FAILED, verify_error in report

Patch strategy mirrors test_execute_approved_paper_routing.py:
  - _run_executor is a module-level function → patch via patch.object(mod, ...)
  - _is_market_halted is module-level → patch via patch.object(mod, ...)
  - _notify_execution is module-level → suppress via patch.object(mod, ...)
  - _save_plan lives on the TradePlanGenerator instance returned by load_plan →
    inject a capturing fake via the mock.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import scripts.execute_approved as mod

# ── Patch targets ─────────────────────────────────────────────────────────────
_PATCH_CONFIG  = "utils.config.get_active_config"
_PATCH_PLAN_GEN = "brokers.plan.TradePlanGenerator"
_PATCH_IS_PAPER = "monitor.strategy_lifecycle.is_paper"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config(mode: str = "live", live_enabled: bool = True) -> dict:
    return {"trading": {"mode": mode, "live_enabled": live_enabled, "auto_approve": False}}


def _make_entry(ticker: str, strategy: str = "mean_reversion") -> dict:
    return {"ticker": ticker, "strategy": strategy, "position_size": 10}


def _approved_plan(entries: list, exits: list | None = None) -> dict:
    return {
        "status": "APPROVED",
        "proposed_entries": entries,
        "proposed_exits": exits or [],
        "overlay_context": {},
        "trade_date": "2026-05-19",
        "market_id": "sp500",
    }


def _fake_report(n_entries: int, success: bool = True) -> dict:
    """Simulate what LiveExecutor.execute_plan() returns."""
    ok = n_entries if success else 0
    entries_list = [
        {
            "ticker": f"T{i}",
            "side": "BUY",
            "qty": 2,
            "price": 100.0 + i,
            "success": success,
            "order_id": f"fake-order-{i:04d}",
            "status": "pending_new" if success else "rejected",
            "message": "" if success else "rejected by broker",
        }
        for i in range(n_entries)
    ]
    return {
        "successful_entries": ok,
        "total_entries": n_entries,
        "successful_exits": 0,
        "total_exits": 0,
        "entries": entries_list,
        "exits": [],
    }


def _plan_gen_factory(plan: dict) -> tuple[Any, dict]:
    """Return (mock_plan_gen_class, saved_plans_dict).

    saved_plans["latest"] receives the plan dict on each _save_plan call.
    """
    saved_plans: dict = {}

    pg_instance = MagicMock()
    pg_instance.load_plan.return_value = plan

    def fake_save(p, date):
        saved_plans["latest"] = json.loads(json.dumps(p))  # deep copy

    pg_instance._save_plan.side_effect = fake_save

    pg_class = MagicMock(return_value=pg_instance)
    return pg_class, saved_plans


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: all entries routed to paper
# ─────────────────────────────────────────────────────────────────────────────

def test_all_entries_to_paper_sets_executed_paper():
    """When ALL entries route to paper → plan.status=EXECUTED_PAPER."""
    n = 3
    entries = [_make_entry(f"T{i}", "mean_reversion") for i in range(n)]
    plan = _approved_plan(entries)
    pg_class, saved = _plan_gen_factory(plan)

    paper_report = _fake_report(n, success=True)

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        # Simulate: paper config returns paper_report; live would be None
        assert label == "[paper]", f"expected [paper] label, got {label!r}"
        return paper_report

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_verify_broker_submissions", return_value=(True, "no live")),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN, pg_class),
        patch(_PATCH_IS_PAPER, return_value=True),
    ):
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500"]):
            mod.main()

    assert saved, "plan was never saved"
    saved_plan = saved["latest"]

    assert saved_plan["status"] == "EXECUTED_PAPER", f"got status={saved_plan['status']!r}"

    rs = saved_plan.get("routing_summary", {})
    assert rs["paper_submitted"] == n, f"paper_submitted should be {n}, got {rs}"
    assert rs["live_submitted"] == 0, f"live_submitted should be 0, got {rs}"
    assert rs["live_total"] == 0

    er = saved_plan["execution_report"]
    assert er["live_submitted"] == 0
    assert er["paper_submitted"] == n

    entries_out = er.get("entries", [])
    assert len(entries_out) == n, f"expected {n} entries in report, got {len(entries_out)}"
    for e in entries_out:
        assert e["broker_mode"] == "paper", f"entry missing broker_mode=paper: {e}"
        assert e["order_id"] != "", f"entry missing order_id: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: all entries routed to live
# ─────────────────────────────────────────────────────────────────────────────

def test_all_entries_to_live_sets_executed():
    """When ALL entries route to live → plan.status=EXECUTED."""
    n = 2
    entries = [_make_entry(f"L{i}", "momentum_breakout") for i in range(n)]
    plan = _approved_plan(entries)
    pg_class, saved = _plan_gen_factory(plan)

    live_report = _fake_report(n, success=True)

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        assert label == "[live]", f"expected [live] label, got {label!r}"
        return live_report

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_verify_broker_submissions", return_value=(True, "verified 2 live")),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN, pg_class),
        patch(_PATCH_IS_PAPER, return_value=False),
    ):
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500"]):
            mod.main()

    assert saved, "plan was never saved"
    saved_plan = saved["latest"]

    assert saved_plan["status"] == "EXECUTED", f"got {saved_plan['status']!r}"

    rs = saved_plan.get("routing_summary", {})
    assert rs["live_submitted"] == n
    assert rs["paper_submitted"] == 0

    er = saved_plan["execution_report"]
    assert er["live_submitted"] == n
    assert er["paper_submitted"] == 0

    entries_out = er.get("entries", [])
    assert len(entries_out) == n
    for e in entries_out:
        assert e["broker_mode"] == "live", f"entry missing broker_mode=live: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: broker rejects ALL entries → status=FAILED
# ─────────────────────────────────────────────────────────────────────────────

def test_broker_submit_all_rejected_sets_failed():
    """When broker returns successful_entries=0 for all → plan.status=FAILED."""
    n = 2
    entries = [_make_entry(f"F{i}", "mean_reversion") for i in range(n)]
    plan = _approved_plan(entries)
    pg_class, saved = _plan_gen_factory(plan)

    failed_report = _fake_report(n, success=False)

    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        return failed_report

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_verify_broker_submissions", return_value=(True, "no live")),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN, pg_class),
        patch(_PATCH_IS_PAPER, return_value=True),
    ):
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500"]):
            mod.main()

    assert saved, "plan was never saved"
    saved_plan = saved["latest"]

    assert saved_plan["status"] == "FAILED", (
        f"Expected FAILED (not EXECUTED) when 0 entries succeeded, got {saved_plan['status']!r}"
    )
    er = saved_plan["execution_report"]
    assert er["live_submitted"] + er["paper_submitted"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4: executor returns None (connection failure) → plan.status stays APPROVED
# ─────────────────────────────────────────────────────────────────────────────

def test_executor_returns_none_plan_status_stays_approved():
    """If _run_executor returns None (connection failure), plan is NOT stamped EXECUTED."""
    entries = [_make_entry("X", "mean_reversion")]
    plan = _approved_plan(entries)
    pg_class, saved = _plan_gen_factory(plan)

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_run_executor", return_value=None),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN, pg_class),
        patch(_PATCH_IS_PAPER, return_value=True),
    ):
        with patch("sys.argv", ["execute_approved.py", "--market", "sp500"]):
            mod.main()

    # When both live_report and paper_report are None, `report = {}` → falsy →
    # the `if not args.dry_run and report:` block is skipped entirely.
    # _save_plan should NOT have been called.
    assert not saved, (
        "plan should NOT be saved when executor returns None (no report), "
        f"but saved keys found: {list(saved.keys())}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: verify_mismatch → status=EXECUTED_VERIFY_FAILED
# ─────────────────────────────────────────────────────────────────────────────

def test_verify_mismatch_sets_executed_verify_failed():
    """When _verify_broker_submissions returns (False, msg) → EXECUTED_VERIFY_FAILED."""
    n = 2
    entries = [_make_entry(f"V{i}", "momentum_breakout") for i in range(n)]
    plan = _approved_plan(entries)
    pg_class, saved = _plan_gen_factory(plan)

    live_report = _fake_report(n, success=True)
    verify_msg = "VERIFY MISMATCH: plan claims 2 live submissions but broker_orders has 0"

    # First _run_executor returns live_report; _verify returns mismatch
    def fake_run_executor(config, pl, entr, exts, market_id, trade_date, dry_run, label):
        return live_report

    with (
        patch.object(mod, "_is_market_halted", return_value=(False, "", "")),
        patch.object(mod, "_notify_execution"),
        patch.object(mod, "_verify_broker_submissions", return_value=(False, verify_msg)),
        patch.object(mod, "_run_executor", side_effect=fake_run_executor),
        patch(_PATCH_CONFIG, return_value=_make_config("live")),
        patch(_PATCH_PLAN_GEN, pg_class),
        patch(_PATCH_IS_PAPER, return_value=False),
    ):
        # Suppress the Telegram alert that fires on verify failure
        with (
            patch("utils.telegram.send_message"),
            patch("sys.argv", ["execute_approved.py", "--market", "sp500"]),
        ):
            mod.main()

    assert saved, "plan was never saved"
    # The _save_plan is called twice: once for normal stamp, once for verify_failed update.
    # We care about the LAST state.
    saved_plan = saved["latest"]

    assert saved_plan["status"] == "EXECUTED_VERIFY_FAILED", (
        f"Expected EXECUTED_VERIFY_FAILED, got {saved_plan['status']!r}"
    )
    er = saved_plan.get("execution_report", {})
    assert "verify_error" in er, f"verify_error missing from execution_report: {er}"
    assert verify_msg[:50] in er["verify_error"], (
        f"verify_error content mismatch: {er['verify_error']!r}"
    )
