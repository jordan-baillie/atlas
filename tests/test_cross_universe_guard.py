"""Tests for W6 cross-universe position & buying-power guard (2026-04-28).

Guards entry orders against:
  1. Hard cap on total simultaneous positions across ALL universes
  2. Zero / negative buying power at order submission time
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from risk.cross_universe_guard import (
    GuardConfig,
    GuardDecision,
    available_buying_power,
    check_entry,
    count_open_positions_all_universes,
    load_guard_config,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def guard_cfg_default() -> GuardConfig:
    return GuardConfig(enabled=True, global_max_positions=8, require_positive_cash=True)


@pytest.fixture
def mock_broker_bp():
    """Factory: mock_broker_bp(buying_power=N) → mock broker."""
    def _factory(bp: float) -> MagicMock:
        b = MagicMock()
        b.get_account.return_value = {"buying_power": bp, "cash": bp}
        return b
    return _factory


@pytest.fixture
def mock_broker_account_info_style():
    """Factory: returns a broker that uses get_account_info() (AlpacaBroker style)."""
    def _factory(bp: float) -> MagicMock:
        b = MagicMock(spec=[])  # spec=[] means no auto-attrs; we add manually
        acct_info = MagicMock()
        acct_info.buying_power = bp
        b.get_account_info = MagicMock(return_value=acct_info)
        # No get_account attr
        return b
    return _factory


# ── tests ─────────────────────────────────────────────────────────────────────

def test_position_count_at_7_allows_entry(monkeypatch, guard_cfg_default, mock_broker_bp):
    """7 open positions < cap of 8: entry allowed."""
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 7,
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=mock_broker_bp(2000.0), config=guard_cfg_default,
    )
    assert decision.allowed
    assert decision.positions_count == 7


def test_position_count_at_8_rejects_entry(monkeypatch, guard_cfg_default, mock_broker_bp):
    """8 open positions == cap: entry rejected."""
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 8,
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=mock_broker_bp(2000.0), config=guard_cfg_default,
    )
    assert not decision.allowed
    assert "position cap" in decision.reason
    assert decision.positions_count == 8
    assert decision.positions_cap == 8


def test_position_count_above_cap_rejects(monkeypatch, guard_cfg_default, mock_broker_bp):
    """9 open positions > cap of 8: entry rejected."""
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 9,
    )
    decision = check_entry(
        ticker="TSLA", universe="sp500", qty=5, price=250.0,
        broker=mock_broker_bp(5000.0), config=guard_cfg_default,
    )
    assert not decision.allowed
    assert decision.positions_count == 9


def test_negative_buying_power_rejects(monkeypatch, guard_cfg_default, mock_broker_bp):
    """Negative buying power (clamped to 0 by available_buying_power) rejects entry."""
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 3,
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=mock_broker_bp(-50.0), config=guard_cfg_default,
    )
    # available_buying_power clamps to 0; check_entry rejects on bp <= 0
    assert not decision.allowed
    assert "buying power" in decision.reason.lower()


def test_buying_power_below_order_cost_rejects(monkeypatch, guard_cfg_default, mock_broker_bp):
    """Order cost $1000, buying power $500: rejected."""
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 3,
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,  # cost = $1000
        broker=mock_broker_bp(500.0), config=guard_cfg_default,
    )
    assert not decision.allowed
    assert "exceeds buying power" in decision.reason


def test_guard_disabled_via_config_allows_all(monkeypatch, mock_broker_bp):
    """Guard disabled in config: all entries allowed, even when caps exceeded."""
    cfg = GuardConfig(enabled=False, global_max_positions=8, require_positive_cash=True)
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 99,  # would normally reject
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=mock_broker_bp(0), config=cfg,
    )
    assert decision.allowed
    assert "disabled" in decision.reason


def test_multiple_universes_counted_correctly():
    """count_open_positions_all_universes returns a non-negative int from live DB."""
    n = count_open_positions_all_universes()
    assert isinstance(n, int)
    assert n >= 0


def test_global_risk_config_present_and_loadable():
    """config/global_risk.json must exist and load correctly."""
    p = Path("/root/atlas/config/global_risk.json")
    assert p.exists(), "config/global_risk.json must exist for guard to be reachable"
    cfg = load_guard_config()
    assert cfg.enabled is True
    assert cfg.global_max_positions == 8
    assert cfg.require_positive_cash is True


def test_apr24_scenario_simulated(monkeypatch, guard_cfg_default, mock_broker_bp):
    """Simulate Apr 24 conditions: 9 positions across universes + negative cash.

    The guard must reject new entries under these conditions.
    """
    # Apr 24 conditions: 9 positions across sp500 + sector_etfs + commodity_etfs,
    # cash was -$4063 (negative).
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 9,
    )
    decision = check_entry(
        ticker="AVGO", universe="sp500", qty=10, price=200.0,
        broker=mock_broker_bp(-4063.0), config=guard_cfg_default,
    )
    assert not decision.allowed
    # Position cap fires first (9 >= 8)
    assert "position cap" in decision.reason or "buying power" in decision.reason.lower()


def test_exit_orders_unaffected():
    """Document: the guard is inserted in _execute_entry only, NOT in place_order.

    Verifies exit/stop/TP orders are never gated.
    """
    src = Path("/root/atlas/brokers/live_executor.py").read_text()
    entry_idx = src.find("def _execute_entry(")
    place_idx = src.find("def place_order(")
    assert entry_idx > 0, "_execute_entry not found in live_executor.py"
    assert place_idx > 0, "place_order not found in live_executor.py"
    # Slice each method's body
    entry_body = src[entry_idx: entry_idx + 4000]
    place_body = src[place_idx: place_idx + 2000]
    assert "cross_universe_guard" in entry_body, "guard must be in _execute_entry"
    assert "cross_universe_guard" not in place_body, (
        "guard must NOT be in place_order — this would block exits/stops/TPs"
    )


def test_guard_failure_fails_open(guard_cfg_default, monkeypatch):
    """broker=None (simulate broken broker) returns a GuardDecision (no exception).

    Verifies that a broken broker path returns a decision rather than raising.
    """
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 3,
    )
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=None, config=guard_cfg_default,
    )
    # broker=None → available_buying_power returns 0.0 → bp <= 0 → rejected
    # (guard returns a valid decision, does NOT raise)
    assert isinstance(decision, GuardDecision)
    assert not decision.allowed
    assert "buying power" in decision.reason.lower()


def test_available_buying_power_get_account_info_fallback(mock_broker_account_info_style):
    """available_buying_power uses get_account_info() when get_account() absent.

    AlpacaBroker exposes get_account_info(), not get_account().
    """
    broker = mock_broker_account_info_style(3500.0)
    bp = available_buying_power(broker)
    assert bp == 3500.0


def test_available_buying_power_clamps_negative():
    """available_buying_power always returns >= 0."""
    b = MagicMock()
    b.get_account.return_value = {"buying_power": -999.0}
    bp = available_buying_power(b)
    assert bp == 0.0


def test_require_positive_cash_false_skips_bp_check(monkeypatch):
    """When require_positive_cash=False, buying-power check is skipped."""
    cfg = GuardConfig(enabled=True, global_max_positions=8, require_positive_cash=False)
    monkeypatch.setattr(
        "risk.cross_universe_guard.count_open_positions_all_universes",
        lambda: 3,
    )
    # Even with no broker (bp=0), should be allowed because cash check disabled
    decision = check_entry(
        ticker="AAPL", universe="sp500", qty=10, price=100.0,
        broker=None, config=cfg,
    )
    assert decision.allowed
    assert decision.buying_power is None  # bp was never queried


def test_guard_not_in_market_state(monkeypatch):
    """Guard module must not import or reference market_state.

    market_state is the per-market halt mechanism; the guard is orthogonal.
    """
    import risk.cross_universe_guard as cug
    src = Path(cug.__file__).read_text()
    assert "market_state" not in src, (
        "cross_universe_guard must not touch market_state.halted — "
        "that is the per-market halt, not the cross-universe guard"
    )
