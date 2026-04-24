"""Regression tests for P0-4: XLK state-file / SQLite parity guardrail.

Protects against re-regression of the pattern where a trade is recorded in
SQLite (via record_trade_entry) but the broker state file for that market
is NOT updated — creating a silent divergence that makes the position
invisible to sync_protective_orders, the dashboard, and eod_settlement.

Root cause (confirmed 2026-04-23):
  sector_etfs config has ``live_enabled=False`` which gates BOTH
  eod_settlement.save_state() AND sync_protective_orders._update_state_positions().
  When XLK was executed (trades.id=180, 23:30 AEST), SQLite got the row via
  TradeLedger.record_entry → atlas_db.record_trade_entry, but neither of the
  state-file write paths fired because both are behind the live_enabled gate.

Fix: _assert_state_file_parity() helper called from record_trade_entry() after
every successful INSERT — reads the state file, logs ERROR if ticker missing,
self-heals by appending a minimal position entry.

Test layout
-----------
  TestParityGuardrailSelfHeals
    test_missing_ticker_logged_as_error      -- parity mismatch → ERROR + heal
    test_missing_ticker_healed_in_state_file -- state file patched after INSERT
    test_happy_path_no_warning               -- ticker present → silent

  TestParityGuardrailEdgeCases
    test_no_state_file_is_silent             -- missing file → no error
    test_live_enabled_false_still_guards     -- live_enabled=False does not skip
    test_parity_called_on_record_trade_entry -- integration: INSERT triggers parity

  TestStateFileSQLiteParityRegression
    test_xlk_pattern_self_heals              -- exact XLK/sector_etfs scenario
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import init_db, _assert_state_file_parity, record_trade_entry


# ─── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Redirect atlas_db to a throw-away DB and state dir so tests never touch production."""
    db_path = str(tmp_path / "test_parity.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)

    # Also redirect the state file path to tmp_path
    state_dir = tmp_path / "brokers" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_adb, "_state_dir_override", str(state_dir))

    init_db()
    yield
    monkeypatch.setattr(_adb, "_db_path_override", None)
    monkeypatch.setattr(_adb, "_state_dir_override", None)


@pytest.fixture
def sector_etfs_state_file(tmp_path) -> Path:
    """State file for sector_etfs with XLY only (pre-XLK recovery)."""
    state_dir = tmp_path / "brokers" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sf = state_dir / "live_sector_etfs.json"
    sf.write_text(json.dumps({
        "market_id": "sector_etfs",
        "mode": "live",
        "positions": [
            {
                "ticker": "XLY",
                "strategy": "momentum_breakout",
                "entry_date": "2026-04-22",
                "entry_price": 116.44,
                "shares": 10,
                "stop_price": 110.618,
                "order_id": "",
            }
        ],
        "closed_trades": [],
        "equity_history": [],
    }))
    return sf


# ═══════════════════════════════════════════════════════════════════════════════
# Class 1 — Core guardrail behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestParityGuardrailSelfHeals:
    """Verify _assert_state_file_parity detects mismatch and self-heals."""

    def test_missing_ticker_logged_as_error(
        self, sector_etfs_state_file, caplog
    ):
        """When ticker missing from state file, an ERROR is logged."""
        with caplog.at_level(logging.ERROR, logger="db.atlas_db"):
            _assert_state_file_parity(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.52,
            )

        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("XLK" in m and "sector_etfs" in m for m in error_msgs), (
            f"Expected ERROR about XLK/sector_etfs, got: {error_msgs}"
        )
        assert any("MISSING" in m or "PARITY" in m for m in error_msgs), (
            f"Expected PARITY/MISSING keyword in error, got: {error_msgs}"
        )

    def test_missing_ticker_healed_in_state_file(
        self, sector_etfs_state_file
    ):
        """After _assert_state_file_parity runs, ticker is appended to state file."""
        _assert_state_file_parity(
            ticker="XLK",
            universe="sector_etfs",
            strategy="momentum_breakout",
            entry_price=156.77,
            shares=8,
            stop_price=153.52,
        )

        with open(sector_etfs_state_file) as f:
            state = json.load(f)
        tickers = [p["ticker"] for p in state["positions"]]
        assert "XLK" in tickers, f"XLK not healed into state file; tickers={tickers}"
        assert "XLY" in tickers, "XLY was clobbered during self-heal"

        xlk_entry = next(p for p in state["positions"] if p["ticker"] == "XLK")
        assert xlk_entry["strategy"] == "momentum_breakout"
        assert xlk_entry["entry_price"] == 156.77
        assert xlk_entry["shares"] == 8

    def test_happy_path_no_warning(self, tmp_path, caplog):
        """When ticker is already in state file, no warning or error is emitted."""
        sf = tmp_path / "brokers" / "state" / "live_sector_etfs.json"
        sf.write_text(json.dumps({
            "market_id": "sector_etfs",
            "mode": "live",
            "positions": [
                {"ticker": "XLK", "strategy": "momentum_breakout",
                 "entry_date": "2026-04-23", "entry_price": 156.77,
                 "shares": 8, "stop_price": 153.52, "order_id": ""},
            ],
        }))

        with caplog.at_level(logging.WARNING, logger="db.atlas_db"):
            _assert_state_file_parity(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.52,
            )

        warn_or_error = [r for r in caplog.records
                         if r.levelno >= logging.WARNING and "XLK" in r.message]
        assert not warn_or_error, (
            f"Unexpected WARNING/ERROR on happy path: {[r.message for r in warn_or_error]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Class 2 — Edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestParityGuardrailEdgeCases:
    """Edge-case coverage: missing file, live_enabled=False, integration trigger."""

    def test_no_state_file_is_silent(self, caplog):
        """When the state file does not exist, no error is emitted (paper markets)."""
        # State file deliberately NOT created (empty state_dir from _isolate_db)
        with caplog.at_level(logging.WARNING, logger="db.atlas_db"):
            _assert_state_file_parity(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.52,
            )

        warn_or_error = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not warn_or_error, (
            f"Should be silent when state file missing, got: {[r.message for r in warn_or_error]}"
        )

    def test_live_enabled_false_still_runs_parity_guard(
        self, sector_etfs_state_file, caplog
    ):
        """live_enabled=False must NOT skip the parity guardrail.

        Root cause of P0-4: the state-file write paths (eod_settlement,
        sync_protective_orders) are gated by live_enabled=False.  The parity
        guard lives in record_trade_entry (the SQLite write), which runs
        regardless of live_enabled.  This test asserts that gating.
        """
        # The parity guard reads no config — it only inspects the state file.
        # We call it directly, simulating the path after record_trade_entry
        # fires (regardless of what live_enabled is set to in config).
        with caplog.at_level(logging.ERROR, logger="db.atlas_db"):
            _assert_state_file_parity(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.52,
            )

        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("XLK" in m for m in error_msgs), (
            "Parity guard should have fired regardless of live_enabled config value. "
            f"errors={error_msgs}"
        )
        # Self-heal must also have run
        with open(sector_etfs_state_file) as f:
            state = json.load(f)
        tickers = [p["ticker"] for p in state["positions"]]
        assert "XLK" in tickers, "Self-heal should work regardless of live_enabled"

    def test_parity_called_on_record_trade_entry(
        self, sector_etfs_state_file, caplog
    ):
        """Integration: record_trade_entry triggers parity check after INSERT.

        Uses a real DB INSERT (with isolated test DB) and confirms that
        calling record_trade_entry with a missing ticker causes the parity
        guard to fire and the state file to be updated.
        """
        with caplog.at_level(logging.ERROR, logger="db.atlas_db"):
            trade_id = record_trade_entry(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.52,
                take_profit=None,
                confidence=0.75,
                regime_state="transition_uncertain",
            )

        # Trade was inserted in SQLite
        assert trade_id is not None, "record_trade_entry should have succeeded"

        # Parity guard fired
        error_msgs = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("XLK" in m for m in error_msgs), (
            f"Parity guard did not fire from record_trade_entry. errors={error_msgs}"
        )

        # State file was healed
        with open(sector_etfs_state_file) as f:
            state = json.load(f)
        tickers = [p["ticker"] for p in state["positions"]]
        assert "XLK" in tickers, f"State file not healed after record_trade_entry. tickers={tickers}"
        assert "XLY" in tickers, "XLY was clobbered during integration heal"


# ═══════════════════════════════════════════════════════════════════════════════
# Class 3 — XLK/sector_etfs exact regression scenario (P0-4)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateFileSQLiteParityRegression:
    """Reproduce the exact P0-4 XLK/sector_etfs scenario end-to-end.

    Scenario:
      1. live_sector_etfs.json exists with XLY only.
      2. XLK trade executes → record_trade_entry inserts into SQLite.
      3. live_enabled=False → eod_settlement and sync_protective_orders skip
         the state file write path.
      4. RESULT before fix: state file has XLY only; SQLite has XLK+XLY.
         RESULT after fix: parity guard detects mismatch and appends XLK.
    """

    def test_xlk_pattern_self_heals(self, sector_etfs_state_file, caplog):
        """After XLK INSERT, parity guard detects mismatch and self-heals."""
        # Before: state file has only XLY
        with open(sector_etfs_state_file) as f:
            before = json.load(f)
        assert [p["ticker"] for p in before["positions"]] == ["XLY"], (
            f"Precondition failed: expected only XLY before fix. got={before['positions']}"
        )

        # Simulate the INSERT that happened on 2026-04-23T23:30 (market open)
        with caplog.at_level(logging.ERROR, logger="db.atlas_db"):
            trade_id = record_trade_entry(
                ticker="XLK",
                universe="sector_etfs",
                strategy="momentum_breakout",
                entry_price=156.77,
                shares=8,
                stop_price=153.5191,  # exact SQLite value from trades.id=180
                take_profit=None,
                confidence=0.75,
                regime_state="transition_uncertain",
            )

        assert trade_id is not None, "record_trade_entry should have returned a valid id"

        # Parity guard fired with loud ERROR
        errors = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("XLK" in m and "sector_etfs" in m for m in errors), (
            f"Parity ERROR not emitted. errors={errors}"
        )

        # State file healed: both XLY and XLK present
        with open(sector_etfs_state_file) as f:
            after = json.load(f)
        tickers_after = [p["ticker"] for p in after["positions"]]
        assert "XLK" in tickers_after, f"XLK not self-healed. tickers={tickers_after}"
        assert "XLY" in tickers_after, f"XLY was clobbered. tickers={tickers_after}"

        # XLK entry has correct fields
        xlk = next(p for p in after["positions"] if p["ticker"] == "XLK")
        assert xlk["strategy"] == "momentum_breakout"
        assert xlk["entry_price"] == 156.77
        assert xlk["shares"] == 8

        # SQLite also has the trade
        with _adb.get_db() as db:
            row = db.execute(
                "SELECT * FROM trades WHERE ticker=? AND status='open'",
                ("XLK",),
            ).fetchone()
        assert row is not None, "XLK should be in SQLite trades table"
        assert row["strategy"] == "momentum_breakout"
        assert row["universe"] == "sector_etfs"
