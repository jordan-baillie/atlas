"""Tests for LiveExecutor._get_cached_account_info() (Fix #2).

Verifies that broker.get_account_info() is called at most once per
execute_plan() invocation (regardless of how many entries/exits touch
_capture_start_equity, _check_circuit_breaker, or the leverage gate),
and that the cache is properly reset at the start of each plan execution.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from brokers.live_executor import LiveExecutor


# ── Minimal fixtures ──────────────────────────────────────────────────────────

def _minimal_config() -> dict:
    return {
        "market_id": "sp500",
        "risk": {
            "starting_equity": 5000,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 10,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": 0.02,
            "leverage": 1.0,
        },
        "fees": {"commission_per_trade": 0, "commission_pct": 0},
        "trading": {
            "live_enabled": True,
            "live_safety": {"dry_run_first": False},
        },
    }


@dataclass
class _FakeAccountInfo:
    equity: float = 5000.0
    cash: float = 1000.0
    buying_power: float = 5000.0
    portfolio_value: float = 5000.0


def _make_executor_with_mock_broker() -> tuple[LiveExecutor, MagicMock]:
    """Return (executor, mock_broker) with executor in connected state."""
    cfg = _minimal_config()
    ex = LiveExecutor(cfg)

    mock_broker = MagicMock()
    mock_broker.get_account_info.return_value = _FakeAccountInfo()
    mock_broker.get_positions.return_value = []

    ex._broker = mock_broker
    ex._connected = True
    return ex, mock_broker


def _approved_plan(n_entries: int = 3) -> dict:
    return {
        "status": "APPROVED",
        "proposed_entries": [
            {
                "ticker": f"TST{i}",
                "side": "buy",
                "qty": 1,
                "entry_price": 100.0,
                "stop_price": 90.0,
                "take_profit": 120.0,
                "strategy": "momentum",
            }
            for i in range(n_entries)
        ],
        "proposed_exits": [],
        "market_id": "sp500",
    }


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestAccountInfoCache:
    """broker.get_account_info() called at most once per execute_plan."""

    def test_cache_attribute_exists_on_init(self):
        """_account_info_cache initialised to None in __init__."""
        ex = LiveExecutor(_minimal_config())
        assert hasattr(ex, "_account_info_cache")
        assert ex._account_info_cache is None

    def test_get_cached_account_info_returns_broker_value(self):
        """_get_cached_account_info() returns broker result on first call."""
        ex, mock_broker = _make_executor_with_mock_broker()
        result = ex._get_cached_account_info()
        assert result is not None
        assert result.equity == pytest.approx(5000.0)
        mock_broker.get_account_info.assert_called_once()

    def test_second_call_uses_cache_no_second_rpc(self):
        """Second call within same context returns cached value without another RPC."""
        ex, mock_broker = _make_executor_with_mock_broker()
        r1 = ex._get_cached_account_info()
        r2 = ex._get_cached_account_info()
        assert r1 is r2  # same object
        mock_broker.get_account_info.assert_called_once()  # only one RPC

    def test_multiple_internal_calls_use_cache(self):
        """Calling _capture_start_equity + _check_circuit_breaker only fetches once."""
        ex, mock_broker = _make_executor_with_mock_broker()
        # _capture_start_equity calls _get_cached_account_info
        ex._capture_start_equity()
        # _check_circuit_breaker also calls _get_cached_account_info
        ex._check_circuit_breaker("2026-05-01")
        # Only one actual RPC should have occurred
        assert mock_broker.get_account_info.call_count == 1, (
            f"Expected 1 broker RPC, got {mock_broker.get_account_info.call_count}"
        )

    def test_get_cached_account_info_no_broker_returns_none(self):
        """Returns None gracefully when broker not set."""
        ex = LiveExecutor(_minimal_config())
        ex._broker = None
        assert ex._get_cached_account_info() is None

    def test_get_cached_account_info_broker_exception_returns_none(self):
        """Returns None when broker raises, does not propagate exception."""
        ex, mock_broker = _make_executor_with_mock_broker()
        mock_broker.get_account_info.side_effect = RuntimeError("network timeout")
        result = ex._get_cached_account_info()
        assert result is None

    def test_execute_plan_resets_stale_cache(self, monkeypatch, tmp_path):
        """execute_plan() clears any stale _account_info_cache at its start.

        Pre-condition: cache holds a sentinel object from before the plan.
        Post-condition: after plan entry, cache holds the broker's fresh value.
        """
        ex, mock_broker = _make_executor_with_mock_broker()

        # Inject a stale sentinel that should be replaced
        stale_sentinel = _FakeAccountInfo(equity=9999.0)
        ex._account_info_cache = stale_sentinel

        monkeypatch.setattr("db.atlas_db._db_path_override", str(tmp_path / "t.db"))

        with (
            patch.object(ex, "_execute_entry", return_value={"success": False, "errors": []}),
            patch.object(ex, "_execute_exit", return_value={"success": False, "errors": []}),
            patch.object(ex, "_run_volatility_gate", return_value={"action": "allow", "reason": "test"}),
            patch.object(ex, "_check_circuit_breaker", return_value=False),
            patch.object(ex, "check_market_state", return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=(["TST0"], [])),
            patch("db.atlas_db.record_system_log", return_value=None),
        ):
            ex.execute_plan(_approved_plan(n_entries=1), "2026-05-01")

        # The cache should NOT still be the stale sentinel — execute_plan reset it
        assert ex._account_info_cache is not stale_sentinel, (
            "execute_plan should have reset _account_info_cache, "
            "but stale sentinel is still present"
        )
        # And the fresh value from the broker should now be there
        if ex._account_info_cache is not None:
            assert ex._account_info_cache.equity == pytest.approx(5000.0)

    def test_cache_reset_between_two_plans_via_direct_inspection(self, monkeypatch, tmp_path):
        """The cache attribute is None at the START of the second execute_plan call.

        We verify this by patching _account_info_cache = None as a side-effect
        at the expected reset point and confirming the reset happened.
        """
        ex, mock_broker = _make_executor_with_mock_broker()
        monkeypatch.setattr("db.atlas_db._db_path_override", str(tmp_path / "t.db"))

        cache_values_at_entry: list = []

        original_capture = ex._capture_start_equity.__func__  # unbound

        def _spy_capture(self_):
            """Record cache state when _capture_start_equity is first reached per plan."""
            cache_values_at_entry.append(self_._account_info_cache)
            original_capture(self_)

        with (
            patch.object(ex, "_execute_entry", return_value={"success": False, "errors": []}),
            patch.object(ex, "_execute_exit", return_value={"success": False, "errors": []}),
            patch.object(ex, "_run_volatility_gate", return_value={"action": "allow", "reason": "test"}),
            patch.object(ex, "_check_circuit_breaker", return_value=False),
            patch.object(ex, "check_market_state", return_value={"is_tradeable": True, "message": ""}),
            patch("brokers.alpaca.tradable_assets.filter_tradable", return_value=(["TST0"], [])),
            patch("db.atlas_db.record_system_log", return_value=None),
            patch.object(ex, "_capture_start_equity", side_effect=lambda: _spy_capture(ex)),
        ):
            ex.execute_plan(_approved_plan(n_entries=1), "2026-05-01")
            ex.execute_plan(_approved_plan(n_entries=1), "2026-05-02")

        # Both plans should have entered _capture_start_equity with cache=None
        # (cache was reset to None at the top of execute_plan before the call)
        assert len(cache_values_at_entry) == 2
        assert cache_values_at_entry[0] is None, (
            "Cache should be None at start of first plan's _capture_start_equity"
        )
        assert cache_values_at_entry[1] is None, (
            "Cache should be None at start of second plan's _capture_start_equity "
            "(execute_plan must reset it)"
        )

    def test_capture_start_equity_uses_cached_method(self):
        """Source code: _capture_start_equity calls _get_cached_account_info."""
        import inspect
        src = inspect.getsource(LiveExecutor._capture_start_equity)
        assert "_get_cached_account_info" in src, (
            "_capture_start_equity should call _get_cached_account_info, "
            "not self._broker.get_account_info directly"
        )
        assert "self._broker.get_account_info" not in src

    def test_check_circuit_breaker_uses_cached_method(self):
        """Source code: _check_circuit_breaker calls _get_cached_account_info."""
        import inspect
        src = inspect.getsource(LiveExecutor._check_circuit_breaker)
        assert "_get_cached_account_info" in src, (
            "_check_circuit_breaker should call _get_cached_account_info"
        )
        assert "self._broker.get_account_info" not in src
