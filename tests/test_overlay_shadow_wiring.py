"""tests/test_overlay_shadow_wiring.py — M3 overlay shadow mode wiring tests.

Verifies:
  1. Shadow log emits log lines (not applied to orders)
  2. Shadow rows inserted to DB
  3. Original order size is NOT modified
  4. No shadow log when no multiplier in plan or DB
  5. EOD evaluation populates actual_outcome_pnl
  6. Shadow report JSON output structure
  7. Shadow report with empty DB returns clean result
  8. Multiplier=1.0 still logs + inserts (with diff=0.0)
  9. Multiplier=0.0 (full kill) logs warning + order still submitted
 10. Migration is idempotent
 11. FK linkage between overlay_decisions and overlay_shadow_log
 12. Shadow resolves from overlay_decisions table when plan has no overlay_context

All tests use the autouse _isolate_prod_db fixture (conftest.py) so no
writes reach production data/atlas.db.

Run:
    cd /root/atlas && python3 -m pytest tests/test_overlay_shadow_wiring.py -xvs --timeout=30
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import (
    get_db,
    init_db,
    record_overlay_decision,
    insert_overlay_shadow_event,
    get_unevaluated_shadow_events,
    update_shadow_outcome,
    get_shadow_events,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_overlay_decision(
    sizing_override: float = 0.5,
    action: str = "tighten",
    trade_date: str = "2026-04-28",
) -> int:
    """Insert a minimal overlay_decisions row and return its id."""
    ts = f"{trade_date}T10:00:00+00:00"
    return record_overlay_decision(
        timestamp=ts,
        regime_state="transition_uncertain",
        action=action,
        sizing_override=sizing_override,
        reasoning="test reasoning from overlay",
        confidence=0.7,
    )


def _minimal_executor_config(shadow_mode: bool = True) -> dict:
    """Return a minimal LiveExecutor config dict."""
    return {
        "version": "test-shadow-v1.0",
        "market": "sp500",
        "market_id": "sp500",
        "trading": {
            "mode": "live",
            "broker": "alpaca",
            "live_enabled": True,
            "live_safety": {
                "max_order_value": 50_000,
                "max_daily_orders": 50,
                "dry_run_first": False,
                "max_daily_loss_pct": 0.20,
            },
        },
        "risk": {
            "starting_equity": 10_000.0,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
        },
        "data": {"source": "alpaca", "history_years": 1},
        "overlay": {"shadow_mode": shadow_mode},
    }


def _make_plan(
    ticker: str = "AAPL",
    position_size: int = 10,
    entry_price: float = 150.0,
    sizing_override: float | None = None,
    trade_date: str = "2026-04-28",
) -> dict:
    """Build a minimal approved plan dict."""
    plan: dict = {
        "status": "APPROVED",
        "trade_date": trade_date,
        "market_id": "sp500",
        "proposed_entries": [
            {
                "ticker": ticker,
                "position_size": position_size,
                "entry_price": entry_price,
                "stop_price": entry_price * 0.95,
                "strategy": "mtf_momentum",
            }
        ],
        "proposed_exits": [],
    }
    if sizing_override is not None:
        plan["overlay_context"] = {
            "action": "tighten",
            "sizing_override": sizing_override,
            "tickers_to_avoid": [],
        }
    return plan


def _make_executor(shadow_mode: bool = True):
    """Return a minimal LiveExecutor instance with mocked broker."""
    from brokers.live_executor import LiveExecutor

    cfg = _minimal_executor_config(shadow_mode=shadow_mode)
    ex = LiveExecutor(cfg)
    ex._connected = True
    ex._halted = False
    ex._daily_date = "2026-04-28"
    ex._daily_order_count = 0
    ex._circuit_breaker_tripped = False
    ex._daily_start_equity = 10_000.0
    return ex


def _common_patches():
    """Return a list of common patches for execute_plan tests."""
    from brokers.base import OrderResult, OrderSide, OrderStatus

    def _fill(**kwargs):
        return OrderResult(
            success=True,
            order_id="ORD-TEST-001",
            ticker=kwargs.get("ticker", "TEST"),
            side=OrderSide.BUY,
            status=OrderStatus.FILLED,
            requested_qty=kwargs.get("qty", 10),
            filled_qty=kwargs.get("qty", 10),
            fill_price=kwargs.get("limit_price", 150.0) or 150.0,
            raw={},
        )

    mock_broker = MagicMock()
    mock_broker.place_order.side_effect = _fill
    mock_broker.get_account_info.return_value = {"equity": 10_000.0, "buying_power": 20_000.0}
    mock_broker.get_positions.return_value = []

    return mock_broker


# ─── Test 1: shadow log emits log line ────────────────────────────────────────

class TestShadowLogEmitsLogLine:
    """Test 1: plan with overlay_context.sizing_override=0.5 — [overlay-shadow] log line emitted."""

    def test_shadow_log_emits_log_line(self, caplog):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        plan = _make_plan(sizing_override=0.5, position_size=10, entry_price=100.0)

        with caplog.at_level(logging.INFO):
            with (
                patch("brokers.live_executor._journal_entry"),
                patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
                patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                      return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
                patch("brokers.live_executor.LiveExecutor.check_market_state",
                      return_value={"is_tradeable": True, "message": ""}),
                patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
                patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
                patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
                patch("brokers.kill_switch.is_halted", return_value=False),
                patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
                patch("regime.model.RegimeModel.classify_current",
                      side_effect=RuntimeError("no model in test")),
                patch("journal.logger.TradeLedger"),
                patch("brokers.live_portfolio.LivePortfolio.save_state"),
            ):
                result = ex.execute_plan(plan, "2026-04-28")

        shadow_lines = [r.message for r in caplog.records if "[overlay-shadow]" in r.message]
        assert len(shadow_lines) >= 1, (
            f"Expected at least one [overlay-shadow] log line, got: {[r.message for r in caplog.records]}"
        )
        line = shadow_lines[0]
        assert "AAPL" in line
        assert "original_size=10.00" in line
        assert "multiplier=0.5000" in line
        assert "NOT APPLIED" in line


# ─── Test 2: shadow log inserts DB row ────────────────────────────────────────

class TestShadowLogInsertsDbRow:
    """Test 2: plan with sizing_override=0.5 → overlay_shadow_log row inserted."""

    def test_shadow_log_inserts_db_row(self):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        plan = _make_plan(sizing_override=0.5, position_size=10, entry_price=100.0)

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
            patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model in test")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
        ):
            ex.execute_plan(plan, "2026-04-28")

        rows = get_shadow_events(days=1)
        assert len(rows) >= 1, "Expected at least 1 shadow row in DB"
        row = rows[0]
        assert row["ticker"] == "AAPL"
        assert row["original_size"] == 10.0
        assert row["overlay_size"] == 5.0
        assert row["sizing_multiplier"] == 0.5
        assert row["would_be_dollar_diff"] == pytest.approx(-500.0)  # (5-10)*100


# ─── Test 3: shadow does NOT modify order size ─────────────────────────────────

class TestShadowDoesNotModifyOrderSize:
    """Test 3: broker receives original position_size, NOT the scaled value."""

    def test_shadow_does_not_modify_order_size(self):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        execute_entry_calls: list[dict] = []

        original_execute_entry = ex._execute_entry.__func__

        def capture_entry(self_inner, entry, trade_date):
            execute_entry_calls.append(dict(entry))
            # Return a minimal success result
            return {
                "ticker": entry.get("ticker"),
                "success": True,
                "qty": entry.get("position_size"),
                "price": entry.get("entry_price"),
                "side": "BUY",
                "dry_run": False,
            }

        plan = _make_plan(sizing_override=0.5, position_size=10, entry_price=100.0)

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
            patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model in test")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
            patch.object(ex, "_execute_entry", side_effect=lambda entry, td: capture_entry(ex, entry, td)),
        ):
            result = ex.execute_plan(plan, "2026-04-28")

        assert len(execute_entry_calls) == 1, "Expected exactly 1 _execute_entry call"
        submitted_size = execute_entry_calls[0]["position_size"]
        assert submitted_size == 10, (
            f"CRITICAL SAFETY: _execute_entry must receive ORIGINAL size=10, got {submitted_size}"
        )


# ─── Test 4: no sizing_multiplier → no shadow log ────────────────────────────

class TestNoSizingMultiplierNoShadowLog:
    """Test 4: plan without overlay_context AND no overlay_decisions row → no shadow row."""

    def test_no_sizing_multiplier_no_shadow_log(self, caplog):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        plan = _make_plan(sizing_override=None)  # No overlay_context

        with caplog.at_level(logging.INFO):
            with (
                patch("brokers.live_executor._journal_entry"),
                patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
                patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                      return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
                patch("brokers.live_executor.LiveExecutor.check_market_state",
                      return_value={"is_tradeable": True, "message": ""}),
                patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
                patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
                patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
                patch("brokers.kill_switch.is_halted", return_value=False),
                patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
                patch("regime.model.RegimeModel.classify_current",
                      side_effect=RuntimeError("no model in test")),
                patch("journal.logger.TradeLedger"),
                patch("brokers.live_portfolio.LivePortfolio.save_state"),
            ):
                ex.execute_plan(plan, "2026-04-28")

        shadow_lines = [r.message for r in caplog.records if "[overlay-shadow]" in r.message]
        assert len(shadow_lines) == 0, f"Expected no shadow lines, got: {shadow_lines}"

        rows = get_shadow_events(days=1)
        assert len(rows) == 0, f"Expected no shadow DB rows, got: {rows}"


# ─── Test 5: EOD evaluation populates pnl ─────────────────────────────────────

class TestEodEvaluationPopulatesPnl:
    """Test 5: evaluate_shadow_events() matches shadow row to closed trade."""

    def test_eod_settlement_populates_pnl(self):
        from overlay.evaluator import evaluate_shadow_events  # type: ignore

        # Insert a shadow event
        shadow_id = insert_overlay_shadow_event(
            plan_id="sp500_2026-04-28",
            ticker="NVDA",
            market_id="sp500",
            original_size=10.0,
            overlay_size=5.0,
            sizing_multiplier=0.5,
            would_be_dollar_diff=-500.0,
        )
        assert shadow_id > 0, "insert_overlay_shadow_event returned -1 (failed)"

        # Insert a matching closed trade
        with get_db() as db:
            db.execute(
                """
                INSERT INTO trades
                    (ticker, strategy, universe, entry_date, exit_date, entry_price,
                     exit_price, shares, pnl, pnl_pct, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("NVDA", "mtf_momentum", "sp500",
                 "2026-04-28", "2026-05-03",
                 100.0, 110.0, 10, 100.0, 10.0, "closed"),
            )

        # Run evaluator
        stats = evaluate_shadow_events()

        assert stats["evaluated"] == 1, f"Expected 1 evaluated, got: {stats}"
        assert stats["skipped"] == 0

        # Verify the shadow row is now marked
        rows = get_unevaluated_shadow_events()
        assert len(rows) == 0, "Shadow row should be marked as evaluated"

        # Verify pnl populated
        all_rows = get_shadow_events(days=7, ticker="NVDA")
        assert len(all_rows) == 1
        assert all_rows[0]["actual_outcome_pnl"] == pytest.approx(100.0)
        assert all_rows[0]["actual_outcome_evaluated"] == 1


# ─── Test 6: shadow report JSON output ────────────────────────────────────────

class TestShadowReportJsonOutput:
    """Test 6: insert 3 shadow rows → build_report returns valid dict with n_events=3."""

    def test_shadow_report_json_output(self):
        from scripts.overlay_shadow_report import build_report  # type: ignore

        for ticker in ["AAPL", "MSFT", "GOOGL"]:
            insert_overlay_shadow_event(
                plan_id="sp500_2026-04-28",
                ticker=ticker,
                market_id="sp500",
                original_size=10.0,
                overlay_size=7.0,
                sizing_multiplier=0.7,
                would_be_dollar_diff=-300.0,
            )

        report = build_report(days=7)

        assert isinstance(report, dict)
        assert report["n_events"] == 3
        assert "verdict" in report
        assert "events_by_day" in report
        assert "total_would_be_dollar_diff" in report
        assert report["total_would_be_dollar_diff"] == pytest.approx(-900.0)
        # Serializable as JSON
        json_str = json.dumps(report)
        assert len(json_str) > 10


# ─── Test 7: shadow report empty DB ───────────────────────────────────────────

class TestShadowReportEmptyDb:
    """Test 7: empty DB returns verdict=insufficient data."""

    def test_shadow_report_empty_clean_exit(self):
        from scripts.overlay_shadow_report import build_report  # type: ignore

        report = build_report(days=7)

        assert report["n_events"] == 0
        assert report["verdict"] == "insufficient data"
        assert report["total_would_be_dollar_diff"] == 0.0
        # Still serializable
        json_str = json.dumps(report)
        assert "insufficient data" in json_str


# ─── Test 8: multiplier=1.0 still logs + inserts ─────────────────────────────

class TestMultiplierOne:
    """Test 8: sizing=1.0 → still logs + inserts row, original_size == overlay_size, diff=0."""

    def test_shadow_multiplier_one_logged(self, caplog):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        plan = _make_plan(sizing_override=1.0, position_size=10, entry_price=100.0)

        with caplog.at_level(logging.INFO):
            with (
                patch("brokers.live_executor._journal_entry"),
                patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
                patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                      return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
                patch("brokers.live_executor.LiveExecutor.check_market_state",
                      return_value={"is_tradeable": True, "message": ""}),
                patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
                patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
                patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
                patch("brokers.kill_switch.is_halted", return_value=False),
                patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
                patch("regime.model.RegimeModel.classify_current",
                      side_effect=RuntimeError("no model in test")),
                patch("journal.logger.TradeLedger"),
                patch("brokers.live_portfolio.LivePortfolio.save_state"),
            ):
                ex.execute_plan(plan, "2026-04-28")

        shadow_lines = [r.message for r in caplog.records if "[overlay-shadow]" in r.message]
        assert len(shadow_lines) >= 1

        rows = get_shadow_events(days=1)
        assert len(rows) >= 1
        row = rows[0]
        assert row["original_size"] == row["overlay_size"], "multiplier=1.0 → sizes must match"
        assert row["sizing_multiplier"] == pytest.approx(1.0)
        assert row["would_be_dollar_diff"] == pytest.approx(0.0)


# ─── Test 9: multiplier=0.0 full kill ─────────────────────────────────────────

class TestMultiplierZeroFullKill:
    """Test 9: sizing=0.0 → WARNING log, row inserted, ORDER STILL SUBMITTED at original size."""

    def test_shadow_multiplier_zero_full_kill(self, caplog):
        ex = _make_executor()
        mock_broker = _common_patches()
        ex._broker = mock_broker

        execute_entry_calls: list[dict] = []

        plan = _make_plan(sizing_override=0.0, position_size=10, entry_price=100.0)

        def capture_entry(entry, td):
            execute_entry_calls.append(dict(entry))
            return {
                "ticker": entry.get("ticker"),
                "success": True,
                "qty": entry.get("position_size"),
                "price": entry.get("entry_price"),
                "side": "BUY",
                "dry_run": False,
            }

        with caplog.at_level(logging.WARNING):
            with (
                patch("brokers.live_executor._journal_entry"),
                patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
                patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                      return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
                patch("brokers.live_executor.LiveExecutor.check_market_state",
                      return_value={"is_tradeable": True, "message": ""}),
                patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
                patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
                patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
                patch("brokers.kill_switch.is_halted", return_value=False),
                patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
                patch("regime.model.RegimeModel.classify_current",
                      side_effect=RuntimeError("no model in test")),
                patch("journal.logger.TradeLedger"),
                patch("brokers.live_portfolio.LivePortfolio.save_state"),
                patch.object(ex, "_execute_entry", side_effect=capture_entry),
            ):
                ex.execute_plan(plan, "2026-04-28")

        # Must emit WARNING about FULL KILL
        kill_warnings = [
            r.message for r in caplog.records
            if "FULL KILL" in r.message
        ]
        assert len(kill_warnings) >= 1, f"Expected FULL KILL warning, got: {[r.message for r in caplog.records if 'shadow' in r.message.lower()]}"

        # Order MUST still be submitted at original size
        assert len(execute_entry_calls) == 1
        assert execute_entry_calls[0]["position_size"] == 10, (
            f"CRITICAL: full-kill shadow MUST NOT suppress order; got size={execute_entry_calls[0]['position_size']}"
        )

        # DB row inserted with multiplier=0
        rows = get_shadow_events(days=1)
        assert len(rows) >= 1
        assert rows[0]["sizing_multiplier"] == pytest.approx(0.0)
        assert rows[0]["overlay_size"] == pytest.approx(0.0)


# ─── Test 10: migration is idempotent ─────────────────────────────────────────

class TestMigrationIdempotent:
    """Test 10: run migration twice → succeeds both times, table still correct."""

    def test_migration_idempotent(self):
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "migration_shadow",
            str(PROJECT / "scripts" / "migrations" / "2026-04-28-overlay-shadow-log.py"),
        )
        mig = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(mig)

        # First run
        mig.run_migration()

        # Second run — must not raise
        mig.run_migration()

        # Table must exist with correct schema
        with get_db() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='overlay_shadow_log'"
            ).fetchone()
            assert row is not None, "overlay_shadow_log table must exist after double-migration"

            # Check expected columns
            cols = {r["name"] for r in db.execute("PRAGMA table_info(overlay_shadow_log)").fetchall()}
            expected = {
                "id", "plan_id", "ticker", "market_id", "created_at",
                "original_size", "overlay_size", "sizing_multiplier",
                "would_be_dollar_diff", "overlay_decision_id", "overlay_action",
                "overlay_reasoning", "actual_outcome_pnl", "actual_outcome_evaluated",
                "evaluated_at",
            }
            assert expected.issubset(cols), f"Missing columns: {expected - cols}"


# ─── Test 11: FK linkage ───────────────────────────────────────────────────────

class TestForeignKeyLinkage:
    """Test 11: shadow event with overlay_decision_id FK → JOIN resolves correctly."""

    def test_foreign_key_overlay_decision(self):
        decision_id = _insert_overlay_decision(sizing_override=0.6, trade_date="2026-04-28")
        assert decision_id > 0

        shadow_id = insert_overlay_shadow_event(
            plan_id="sp500_2026-04-28",
            ticker="META",
            market_id="sp500",
            original_size=8.0,
            overlay_size=4.8,
            sizing_multiplier=0.6,
            would_be_dollar_diff=-100.0,
            overlay_decision_id=decision_id,
            overlay_action="tighten",
            overlay_reasoning="test reasoning from overlay",
        )
        assert shadow_id > 0

        with get_db() as db:
            row = db.execute(
                """
                SELECT s.ticker, s.sizing_multiplier, d.action, d.reasoning
                FROM overlay_shadow_log s
                JOIN overlay_decisions d ON d.id = s.overlay_decision_id
                WHERE s.id = ?
                """,
                (shadow_id,),
            ).fetchone()

        assert row is not None, "JOIN between overlay_shadow_log and overlay_decisions must work"
        assert row["ticker"] == "META"
        assert row["sizing_multiplier"] == pytest.approx(0.6)
        assert row["action"] == "tighten"


# ─── Test 12: shadow resolves from overlay_decisions table ────────────────────

class TestShadowResolvesFromDecisionsTable:
    """Test 12: plan WITHOUT overlay_context, but overlay_decisions row exists → shadow logged."""

    def test_shadow_resolves_from_decisions_table(self, caplog):
        # Insert an overlay_decisions row for today's trade_date
        decision_id = _insert_overlay_decision(sizing_override=0.7, trade_date="2026-04-28")
        assert decision_id > 0

        ex = _make_executor(shadow_mode=True)
        mock_broker = _common_patches()
        ex._broker = mock_broker

        # Plan WITHOUT overlay_context — but DB has a decision row
        plan = _make_plan(sizing_override=None, position_size=12, entry_price=200.0)

        execute_entry_calls: list[dict] = []

        def capture_entry(entry, td):
            execute_entry_calls.append(dict(entry))
            return {
                "ticker": entry.get("ticker"),
                "success": True,
                "qty": entry.get("position_size"),
                "price": entry.get("entry_price"),
                "side": "BUY",
                "dry_run": False,
            }

        with caplog.at_level(logging.INFO):
            with (
                patch("brokers.live_executor._journal_entry"),
                patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
                patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                      return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
                patch("brokers.live_executor.LiveExecutor.check_market_state",
                      return_value={"is_tradeable": True, "message": ""}),
                patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
                patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
                patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
                patch("brokers.kill_switch.is_halted", return_value=False),
                patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
                patch("regime.model.RegimeModel.classify_current",
                      side_effect=RuntimeError("no model in test")),
                patch("journal.logger.TradeLedger"),
                patch("brokers.live_portfolio.LivePortfolio.save_state"),
                patch.object(ex, "_execute_entry", side_effect=capture_entry),
            ):
                ex.execute_plan(plan, "2026-04-28")

        # Shadow row should be inserted with multiplier=0.7 from DB
        rows = get_shadow_events(days=1, ticker="AAPL")
        assert len(rows) >= 1, "Shadow row should be created from DB overlay_decisions lookup"
        row = rows[0]
        assert row["sizing_multiplier"] == pytest.approx(0.7), (
            f"Expected multiplier from DB=0.7, got {row['sizing_multiplier']}"
        )

        # Original order size must be unchanged
        assert len(execute_entry_calls) == 1
        assert execute_entry_calls[0]["position_size"] == 12, (
            "CRITICAL: order size must remain 12 (original), not scaled by shadow multiplier"
        )

# ─── Test 13: enforce mode uses overlay_decisions JOIN ────────────────────────

class TestEnforceModeUsesOverlayDecisionsJoin:
    """Test 13: shadow_mode=False, plan has empty overlay_context, but overlay_decisions
    has a row with sizing_override=0.5 → executor halves qty (50, not 100).
    NO row inserted into overlay_shadow_log."""

    def test_enforce_mode_uses_overlay_decisions_join(self):
        # Insert an overlay_decisions row for trade_date
        decision_id = _insert_overlay_decision(sizing_override=0.5, trade_date="2026-04-28")
        assert decision_id > 0

        ex = _make_executor(shadow_mode=False)
        mock_broker = _common_patches()
        ex._broker = mock_broker

        # Plan WITHOUT overlay_context — must resolve from DB
        plan = _make_plan(sizing_override=None, position_size=100, entry_price=100.0)

        execute_entry_calls: list[dict] = []

        def capture_entry(entry, td):
            execute_entry_calls.append(dict(entry))
            return {
                "ticker": entry.get("ticker"),
                "success": True,
                "qty": entry.get("position_size"),
                "price": entry.get("entry_price"),
                "side": "BUY",
                "dry_run": False,
            }

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
            patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model in test")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
            patch.object(ex, "_execute_entry", side_effect=capture_entry),
        ):
            ex.execute_plan(plan, "2026-04-28")

        # _execute_entry must receive HALVED size (100 * 0.5 = 50)
        assert len(execute_entry_calls) == 1
        assert execute_entry_calls[0]["position_size"] == 50, (
            f"Expected position_size=50 (halved from 100 via DB resolution), "
            f"got {execute_entry_calls[0]['position_size']}"
        )

        # NO shadow row in DB (enforce mode does not log shadow)
        rows = get_shadow_events(days=1)
        assert len(rows) == 0, (
            f"Enforce mode must NOT insert shadow rows, got {len(rows)} rows"
        )


# ─── Test 14: enforce mode plan.overlay_context wins over DB ──────────────────

class TestEnforceModePlanOverlayContextPriority:
    """Test 14: shadow_mode=False, plan.overlay_context.sizing_override=0.3,
    overlay_decisions row also exists with sizing_override=0.5 → plan wins (qty=30).
    Confirms resolution priority: plan first, DB fallback."""

    def test_enforce_mode_with_plan_overlay_context_priority(self):
        # Insert an overlay_decisions row with DIFFERENT multiplier
        decision_id = _insert_overlay_decision(sizing_override=0.5, trade_date="2026-04-28")
        assert decision_id > 0

        ex = _make_executor(shadow_mode=False)
        mock_broker = _common_patches()
        ex._broker = mock_broker

        # Plan WITH overlay_context — must take priority over DB
        plan = _make_plan(sizing_override=0.3, position_size=100, entry_price=100.0)

        execute_entry_calls: list[dict] = []

        def capture_entry(entry, td):
            execute_entry_calls.append(dict(entry))
            return {
                "ticker": entry.get("ticker"),
                "success": True,
                "qty": entry.get("position_size"),
                "price": entry.get("entry_price"),
                "side": "BUY",
                "dry_run": False,
            }

        with (
            patch("brokers.live_executor._journal_entry"),
            patch("brokers.live_executor.LiveExecutor._cancel_open_orders_for_ticker", return_value=0),
            patch("brokers.live_executor.LiveExecutor._run_volatility_gate",
                  return_value={"action": "allow", "reason": "ok", "size_multiplier": 1.0}),
            patch("brokers.live_executor.LiveExecutor.check_market_state",
                  return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.live_executor.LiveExecutor._capture_start_equity"),
            patch("brokers.live_executor.LiveExecutor._check_circuit_breaker", return_value=False),
            patch("brokers.live_executor.LiveExecutor.place_stops_for_plan", return_value=[]),
            patch("brokers.kill_switch.is_halted", return_value=False),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=([], [])),
            patch("regime.model.RegimeModel.classify_current",
                  side_effect=RuntimeError("no model in test")),
            patch("journal.logger.TradeLedger"),
            patch("brokers.live_portfolio.LivePortfolio.save_state"),
            patch.object(ex, "_execute_entry", side_effect=capture_entry),
        ):
            ex.execute_plan(plan, "2026-04-28")

        # _execute_entry must receive 30 (100 * 0.3 from plan, NOT 100*0.5 from DB)
        assert len(execute_entry_calls) == 1
        assert execute_entry_calls[0]["position_size"] == 30, (
            f"Expected position_size=30 (plan.overlay_context.sizing_override=0.3 "
            f"wins over DB sizing_override=0.5), got {execute_entry_calls[0]['position_size']}"
        )

        # NO shadow row (enforce mode)
        rows = get_shadow_events(days=1)
        assert len(rows) == 0
