"""Regression tests: reconcile_ledger must not create duplicate open rows.

P0-C regression guard.

Scenarios:
  A) AMD already open in SQLite (momentum_breakout) + broker also shows AMD
     → reconcile_ledger must detect existing open row, skip INSERT, leave 1 row.

  B) AMD NOT in SQLite + broker shows AMD
     → reconcile_ledger should INSERT 1 row (strategy='reconciled' as fallback).

  C) AMD in ledger_map (primary check) + second re-entrant call
     → still exactly 1 row due to ledger_map AND pre-insert guard.

All tests use isolated tmp DB (autouse conftest fixtures) and mock broker.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import record_trade_entry, get_open_positions, get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker_position(ticker: str, entry_price: float = 100.0, shares: int = 1):
    """Return a mock Position object resembling AlpacaBroker output."""
    pos = MagicMock()
    pos.ticker = ticker
    pos.entry_price = entry_price
    pos.shares = shares
    return pos


def _make_broker_order(ticker: str, stop_price: float = 90.0, side: str = "SELL",
                        order_type: str = "stop"):
    """Return a mock OrderResult representing an open stop order."""
    order = MagicMock()
    order.ticker = ticker
    _side = MagicMock()
    _side.value = side
    order.side = _side
    _type_val = MagicMock()
    _type_val = order_type
    order.type = order_type
    order.stop_price = stop_price
    order.raw = {}
    return order


def _seed_open_trade(ticker: str, strategy: str, universe: str = "sp500",
                     stop_price: float = 90.0) -> int | None:
    """Insert an open trade and return its id."""
    return record_trade_entry(
        ticker=ticker,
        strategy=strategy,
        universe=universe,
        entry_price=100.0,
        shares=1,
        stop_price=stop_price,
        take_profit=None,
        confidence=0.5,
        regime_state=None,
        direction="long",
    )


def _open_count(ticker: str, universe: str = "sp500") -> int:
    """Count open trade rows for ticker/universe."""
    with get_db() as db:
        row = db.execute(
            "SELECT COUNT(*) FROM trades WHERE ticker=? AND universe=? AND exit_date IS NULL",
            (ticker, universe),
        ).fetchone()
    return row[0] if row else 0


def _open_strategies(ticker: str, universe: str = "sp500") -> list[str]:
    """Return all open strategy values for ticker/universe."""
    with get_db() as db:
        rows = db.execute(
            "SELECT strategy FROM trades WHERE ticker=? AND universe=? AND exit_date IS NULL",
            (ticker, universe),
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_broker(tmp_path):
    """Return a mock broker whose state file exists in tmp_path."""
    # Write a minimal live state file so the state-dir guard has a file to read
    state_dir = tmp_path / "broker_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "live_sp500.json"
    state_file.write_text(json.dumps({
        "market_id": "sp500",
        "mode": "live",
        "positions": [],
        "closed_trades": [],
    }))
    return state_dir


# ---------------------------------------------------------------------------
# Scenario A: AMD exists in SQLite → reconcile_ledger must skip INSERT
# ---------------------------------------------------------------------------

class TestScenarioA_ExistingOpenTrade:
    """reconcile_ledger skips INSERT when ticker already has an open row."""

    def _run_reconcile(self, broker_pos, broker_orders, existing_open=None):
        """Run reconcile_ledger with mocked broker returning one AMD position."""
        import importlib
        import scripts.reconcile_ledger as rl_mod

        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = broker_pos
        mock_broker.get_open_orders.return_value = broker_orders
        mock_broker.get_history_orders.return_value = []

        with patch.object(rl_mod, "AlpacaBroker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=["AMD", "AVGO"]), \
             patch("universe.membership.derive_universe", return_value="sp500"):
            result = rl_mod.reconcile_ledger(market_id="sp500", dry_run=False, broker=mock_broker)
        return result

    def test_existing_momentum_breakout_not_overwritten(self):
        """AMD open as momentum_breakout → reconciler sees it in ledger_map, skips."""
        _seed_open_trade("AMD", "momentum_breakout")
        assert _open_count("AMD") == 1

        broker_pos = [_make_broker_position("AMD", entry_price=278.25)]
        broker_orders = [_make_broker_order("AMD", stop_price=260.0)]

        self._run_reconcile(broker_pos, broker_orders)

        assert _open_count("AMD") == 1, "Expected exactly 1 open AMD row"
        assert _open_strategies("AMD") == ["momentum_breakout"], "Strategy must be preserved"

    def test_pre_insert_guard_fires_on_universe_resolved_ticker(self):
        """Pre-insert guard triggers when ledger_map misses due to universe mismatch."""
        # Seed AMD under sp500 universe
        _seed_open_trade("AMD", "momentum_breakout", universe="sp500")
        assert _open_count("AMD", "sp500") == 1

        broker_pos = [_make_broker_position("AMD", entry_price=278.25)]
        broker_orders = [_make_broker_order("AMD", stop_price=260.0)]

        # Patch derive_universe to still return "sp500" (matches existing row)
        import importlib
        import scripts.reconcile_ledger as rl_mod

        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = broker_pos
        mock_broker.get_open_orders.return_value = broker_orders
        mock_broker.get_history_orders.return_value = []

        with patch.object(rl_mod, "AlpacaBroker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=["AMD"]), \
             patch("universe.membership.derive_universe", return_value="sp500"):
            # Force ticker out of ledger_map by temporarily pretending ledger is empty
            with patch.object(rl_mod.atlas_db, "get_open_positions", return_value=[]):
                result = rl_mod.reconcile_ledger(
                    market_id="sp500", dry_run=False, broker=mock_broker
                )

        # Pre-insert guard should have caught it
        assert _open_count("AMD", "sp500") == 1, \
            "Pre-insert guard must prevent second open row even when ledger_map is empty"


# ---------------------------------------------------------------------------
# Scenario B: AMD NOT in SQLite → reconcile_ledger should INSERT exactly 1 row
# ---------------------------------------------------------------------------

class TestScenarioB_NewUnknownPosition:
    """reconcile_ledger inserts one row when ticker is untracked."""

    def test_single_insert_for_untracked_position(self):
        """AMD at broker but not in DB → exactly 1 row inserted."""
        assert _open_count("AMD") == 0

        broker_pos = [_make_broker_position("AMD", entry_price=278.25, shares=2)]
        broker_orders = [_make_broker_order("AMD", stop_price=260.0)]

        import scripts.reconcile_ledger as rl_mod
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = broker_pos
        mock_broker.get_open_orders.return_value = broker_orders
        mock_broker.get_history_orders.return_value = []

        with patch.object(rl_mod, "AlpacaBroker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=["AMD"]), \
             patch("universe.membership.derive_universe", return_value="sp500"):
            result = rl_mod.reconcile_ledger(market_id="sp500", dry_run=False, broker=mock_broker)

        assert _open_count("AMD") == 1, "Expected exactly 1 open AMD row after backfill"
        strats = _open_strategies("AMD")
        # Strategy falls through to 'reconciled' when no plan/state matches
        assert len(strats) == 1
        print(f"Backfilled strategy: {strats[0]}")

    def test_dry_run_does_not_insert(self):
        """Dry-run mode → 0 rows inserted."""
        assert _open_count("AMD") == 0

        broker_pos = [_make_broker_position("AMD", entry_price=278.25)]
        broker_orders = [_make_broker_order("AMD", stop_price=260.0)]

        import scripts.reconcile_ledger as rl_mod
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = broker_pos
        mock_broker.get_open_orders.return_value = broker_orders
        mock_broker.get_history_orders.return_value = []

        with patch.object(rl_mod, "AlpacaBroker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=["AMD"]), \
             patch("universe.membership.derive_universe", return_value="sp500"):
            result = rl_mod.reconcile_ledger(market_id="sp500", dry_run=True, broker=mock_broker)

        assert _open_count("AMD") == 0, "Dry-run must not write to DB"
        assert any("AMD" in str(b) for b in result.get("backfilled", [])), \
            "Dry-run should still list AMD in backfilled"


# ---------------------------------------------------------------------------
# Scenario C: Back-to-back calls → idempotent
# ---------------------------------------------------------------------------

class TestScenarioC_BackToBackCalls:
    """Running reconcile_ledger twice in a row produces exactly 1 row."""

    def _run_once(self, broker_pos, broker_orders):
        import scripts.reconcile_ledger as rl_mod
        mock_broker = MagicMock()
        mock_broker.get_positions.return_value = broker_pos
        mock_broker.get_open_orders.return_value = broker_orders
        mock_broker.get_history_orders.return_value = []

        with patch.object(rl_mod, "AlpacaBroker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=["AMD"]), \
             patch("universe.membership.derive_universe", return_value="sp500"):
            return rl_mod.reconcile_ledger(market_id="sp500", dry_run=False, broker=mock_broker)

    def test_two_calls_leave_one_open_row(self):
        """reconcile_ledger called twice → still exactly 1 open AMD row."""
        assert _open_count("AMD") == 0

        broker_pos = [_make_broker_position("AMD", entry_price=278.25, shares=2)]
        broker_orders = [_make_broker_order("AMD", stop_price=260.0)]

        self._run_once(broker_pos, broker_orders)
        assert _open_count("AMD") == 1, "After first call: 1 open row expected"

        self._run_once(broker_pos, broker_orders)
        assert _open_count("AMD") == 1, "After second call: still 1 open row (idempotent)"


# ---------------------------------------------------------------------------
# Unit test: pre-insert check logic in isolation
# ---------------------------------------------------------------------------

class TestPreInsertDuplicateCheck:
    """Direct unit tests for the pre-insert duplicate check in reconcile_ledger."""

    def test_record_trade_entry_returns_none_on_second_insert(self):
        """record_trade_entry returns None (not raises) on duplicate open trade."""
        first = record_trade_entry(
            ticker="AMD", strategy="momentum_breakout", universe="sp500",
            entry_price=100.0, shares=1, stop_price=90.0,
            take_profit=None, confidence=0.5, regime_state=None, direction="long",
        )
        assert first is not None, "First insert should succeed"

        second = record_trade_entry(
            ticker="AMD", strategy="reconciled", universe="sp500",
            entry_price=110.0, shares=2, stop_price=95.0,
            take_profit=None, confidence=0.0, regime_state=None, direction="long",
        )
        assert second is None, "Second insert should return None (UNIQUE constraint)"
        assert _open_count("AMD") == 1, "Still exactly 1 open AMD row"
        assert _open_strategies("AMD") == ["momentum_breakout"], \
            "Original strategy preserved"

    def test_dedup_migration_no_op_when_clean(self):
        """Migration script returns 0 (success) when no duplicates exist."""
        _seed_open_trade("AMD", "momentum_breakout")
        _seed_open_trade("AVGO", "momentum_breakout")

        import importlib
        import scripts.migrations as migs_pkg
        import importlib.util, os
        migration_path = PROJECT / "scripts" / "migrations" / "2026-04-24-dedupe-amd-reconciled.py"
        spec = importlib.util.spec_from_file_location("dedupe_amd", migration_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Patch DB_PATH to use isolated tmp DB
        original_db_path = mod.DB_PATH
        mod.DB_PATH = Path(_adb._db_path_override or _adb.DB_PATH)
        try:
            rc = mod.main(["--apply"])
        finally:
            mod.DB_PATH = original_db_path

        assert rc == 0
        assert _open_count("AMD") == 1
        assert _open_count("AVGO") == 1
