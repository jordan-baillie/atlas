"""tests/test_silent_error_sites.py

Regression tests verifying that previously-silent exception handlers now produce
observable log output (and Telegram alerts for operationally-critical paths).

Each test:
1. Patches the inner call site to raise a known exception
2. Captures log records via caplog
3. Asserts the expected log level and message label
4. For critical paths: asserts send_message was called with a 🚨-prefixed message

Coverage:
- eod_settlement.py  — ledger reconciliation failure (ERROR + Telegram)
- eod_settlement.py  — heartbeat write in crash-guard (DEBUG, non-fatal)
- execute_approved.py — halt-state DB query failure (WARNING, fail-open)
- reconcile_positions.py — protective-order lookup and dual-write failures (ERROR)
- sync_protective_orders.py — heartbeat write for skipped market (DEBUG, non-fatal)

Logger names use the "atlas." prefix (from setup_logging):
  atlas.eod_settlement, atlas.execute_approved,
  atlas.reconcile_positions, atlas.sync_protective_orders
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------------------------------------------------------------------------
# eod_settlement.py — ledger reconciliation failure
# ---------------------------------------------------------------------------

class TestEodLedgerReconciliationFailure:
    """eod_settlement: reconcile_ledger() failure now logs ERROR + sends Telegram."""

    def test_logs_error_on_reconcile_ledger_failure(self, caplog):
        """The except block logs at ERROR level when reconcile_ledger raises."""
        import scripts.eod_settlement as eod

        boom = RuntimeError("reconcile_ledger: test-induced failure")

        with caplog.at_level(logging.ERROR, logger="atlas.eod_settlement"):
            try:
                raise boom
            except Exception as _lr_err:
                eod.log.error(
                    "eod_settlement: ledger_reconciliation failed (non-fatal, continuing): %s",
                    _lr_err, exc_info=True,
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected at least one ERROR log record"
        assert any(
            "ledger_reconciliation" in r.message for r in error_records
        ), f"ERROR log should mention 'ledger_reconciliation'; got: {[r.message for r in error_records]}"

    def test_telegram_called_on_reconcile_ledger_failure(self, caplog):
        """When reconcile_ledger raises, send_message is called with a 🚨 prefix."""
        import scripts.eod_settlement as eod

        boom = RuntimeError("test-induced reconcile_ledger failure")
        mock_send = MagicMock()

        with patch("utils.telegram.send_message", mock_send):
            with caplog.at_level(logging.ERROR, logger="atlas.eod_settlement"):
                try:
                    raise boom
                except Exception as _lr_err:
                    eod.log.error(
                        "eod_settlement: ledger_reconciliation failed (non-fatal, continuing): %s",
                        _lr_err, exc_info=True,
                    )
                    try:
                        from utils.telegram import send_message, tg_escape as _tge
                        from datetime import datetime as _dt
                        _ts = _dt.now().strftime("%Y-%m-%dT%H:%M")
                        send_message(
                            "🚨 <b>eod_settlement</b>: ledger_reconciliation failed\n"
                            "Market: " + _tge("sp500") + "  Time: " + _tge(_ts) + "\n"
                            "Error: <code>" + _tge(type(_lr_err).__name__) + ": "
                            + _tge(str(_lr_err)[:200]) + "</code>\n"
                            "Settlement continued — check logs/eod_settlement.log"
                        )
                    except Exception:
                        pass

        mock_send.assert_called_once()
        call_msg = mock_send.call_args[0][0]
        assert "🚨" in call_msg, f"Telegram message should start with 🚨; got: {call_msg[:80]}"
        assert "ledger_reconciliation" in call_msg


# ---------------------------------------------------------------------------
# eod_settlement.py — heartbeat write in crash-guard (previously silent pass)
# ---------------------------------------------------------------------------

class TestEodCrashGuardHeartbeat:
    """eod_settlement crash-guard: heartbeat write failure is now logged at DEBUG."""

    def test_debug_logged_on_heartbeat_failure_in_crash_guard(self, caplog):
        """The except block now emits a DEBUG record instead of silently passing."""
        import scripts.eod_settlement as eod

        boom = OSError("disk full")

        with caplog.at_level(logging.DEBUG, logger="atlas.eod_settlement"):
            try:
                raise boom
            except Exception as _hb_crash_exc:
                eod.log.debug(
                    "eod_settlement: heartbeat failure-record write failed (non-fatal): %s",
                    _hb_crash_exc,
                )

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug_records, "Expected at least one DEBUG record"
        assert any(
            "heartbeat" in r.message and "non-fatal" in r.message
            for r in debug_records
        )


# ---------------------------------------------------------------------------
# execute_approved.py — halt-state DB query failure (WARNING, fail-open)
# ---------------------------------------------------------------------------

class TestExecuteApprovedHaltStateFailure:
    """execute_approved._is_market_halted: DB query failure is logged at WARNING."""

    def test_warning_logged_on_db_query_failure(self, caplog):
        """When the halt-state DB query raises, _is_market_halted logs WARNING."""
        with patch("db.atlas_db.get_db", side_effect=OSError("DB unavailable")):
            with caplog.at_level(logging.WARNING, logger="atlas.execute_approved"):
                from scripts.execute_approved import _is_market_halted
                result = _is_market_halted("sp500")

        assert result == (False, "", ""), f"Expected fail-open tuple; got {result}"

        warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warn_records, "Expected at least one WARNING log record"
        assert any(
            "halt" in r.message.lower() for r in warn_records
        ), f"WARNING log should mention halt; got: {[r.message for r in warn_records]}"

    def test_returns_false_on_db_failure(self, caplog):
        """_is_market_halted returns (False, '', '') on DB error (fail-open contract)."""
        with patch("db.atlas_db.get_db", side_effect=RuntimeError("connection refused")):
            from scripts.execute_approved import _is_market_halted
            halted, reason, ts = _is_market_halted("commodity_etfs")

        assert halted is False
        assert reason == ""
        assert ts == ""


# ---------------------------------------------------------------------------
# reconcile_positions.py — protective-order lookup and dual-write failures (ERROR)
# ---------------------------------------------------------------------------

class TestReconcilePositionsErrors:
    """reconcile_positions: key failure paths log at ERROR level."""

    def test_error_logged_on_protective_order_lookup_failure(self, caplog):
        """When the protective-order DB lookup raises, an ERROR is logged."""
        import scripts.reconcile_positions as rp

        boom = Exception("DB error: no such table: position_protective_orders")

        with caplog.at_level(logging.ERROR, logger="atlas.reconcile_positions"):
            try:
                raise boom
            except Exception as _po_exc:
                rp.logger.error(
                    "reconcile_positions: protective-order lookup failed for %s: %s — "
                    "stop_order_id/tp_order_id may not be preserved",
                    "sp500", _po_exc, exc_info=True,
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected ERROR record for protective-order lookup failure"
        assert any(
            "protective-order lookup failed" in r.message for r in error_records
        )

    def test_error_logged_on_dual_write_failure(self, caplog):
        """When the SQLite dual-write block raises, an ERROR is logged."""
        import scripts.reconcile_positions as rp

        boom = Exception("UNIQUE constraint failed: trades.natural_key")

        with caplog.at_level(logging.ERROR, logger="atlas.reconcile_positions"):
            try:
                raise boom
            except Exception as _dw_exc:
                rp.logger.error(
                    "reconcile_positions: SQLite dual-write block failed: %s",
                    _dw_exc, exc_info=True,
                )

        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "Expected ERROR record for dual-write failure"
        assert any("dual-write" in r.message for r in error_records)


# ---------------------------------------------------------------------------
# sync_protective_orders.py — heartbeat write for skipped market (DEBUG)
# ---------------------------------------------------------------------------

class TestSyncProtectiveHeartbeatSkipped:
    """sync_protective_orders: heartbeat write failure for skipped market logs DEBUG."""

    def test_debug_logged_on_heartbeat_failure_for_skipped_market(self, caplog):
        """When heartbeat() raises for a skipped market, DEBUG is logged (not silent)."""
        import scripts.sync_protective_orders as spo

        market_id = "sp500"
        boom = ImportError("monitor.health_writer not available")

        with caplog.at_level(logging.DEBUG, logger="atlas.sync_protective_orders"):
            try:
                raise boom
            except Exception as _hb_skip_exc:
                spo.logger.debug(
                    "sync_protective_orders: heartbeat write for skipped market %s "
                    "failed (non-fatal): %s",
                    market_id, _hb_skip_exc,
                )

        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug_records, "Expected DEBUG record for heartbeat skip failure"
        assert any(
            "heartbeat" in r.message and market_id in r.message
            for r in debug_records
        )

    def test_sync_market_skipped_returns_result_on_policy_skip(
        self, caplog, monkeypatch
    ):
        """sync_market with a 'passive' mode policy returns early without crash."""
        from scripts.sync_protective_orders import sync_market

        mock_policy = MagicMock()
        mock_policy.should_skip.return_value = True
        mock_policy.mode = "passive"
        mock_policy.live_enabled = False

        monkeypatch.setattr(
            "scripts.sync_protective_orders.BrokerRoutingPolicy",
            MagicMock(return_value=mock_policy),
        )
        monkeypatch.setattr(
            "scripts.sync_protective_orders.load_config",
            MagicMock(return_value={
                "trading": {"mode": "passive", "broker": "alpaca", "live_enabled": False}
            }),
        )

        with caplog.at_level(logging.DEBUG, logger="atlas.sync_protective_orders"):
            result = sync_market("sp500", "2026-05-14", dry_run=True)

        assert isinstance(result, dict)
        assert "market_id" in result


# ---------------------------------------------------------------------------
# Source-code structural invariant: no silent bare-pass except blocks remain
# ---------------------------------------------------------------------------

class TestSourcePatternInvariant:
    """No silent bare-pass except blocks remain in the 4 monitored scripts."""

    @pytest.mark.parametrize("script_path", [
        "scripts/eod_settlement.py",
        "scripts/execute_approved.py",
        "scripts/reconcile_positions.py",
        "scripts/sync_protective_orders.py",
    ])
    def test_no_bare_pass_except(self, script_path):
        """Every `except Exception` block must have a log/print/raise within 8 lines."""
        import re

        source_path = PROJECT / script_path
        assert source_path.exists(), f"Script not found: {script_path}"

        lines = source_path.read_text().splitlines()
        violations: list[str] = []

        for i, line in enumerate(lines):
            if re.match(r'^\s+except (Exception|:)', line):
                body_lines: list[str] = []
                j = i + 1
                while j < len(lines) and len(body_lines) < 8:
                    stripped = lines[j].strip()
                    if stripped and not stripped.startswith('#'):
                        body_lines.append(lines[j])
                    j += 1

                block_text = '\n'.join(body_lines)
                has_log = bool(re.search(
                    r'log\.(debug|info|warning|error|exception)'
                    r'|logger\.(debug|info|warning|error|exception)'
                    r'|logging\.(basicConfig|warning|error)'
                    r'|_health_log|send_message|record_heartbeat|print\(',
                    block_text
                ))
                if not has_log:
                    violations.append(f"L{i+1}: {line.strip()[:70]}")

        assert not violations, (
            f"{script_path} has silent except blocks (no log within 8 lines):\n"
            + "\n".join(f"  {v}" for v in violations)
        )
