"""25 unit tests for brokers.routing_policy.BrokerRoutingPolicy.

Spec: docs/specs/broker-routing-policy.md §3
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from brokers.routing_policy import BrokerRoutingPolicy


# ── Helper ────────────────────────────────────────────────────────────────────

def _make_config(mode: str, live_enabled: bool = True) -> dict:
    return {"trading": {"mode": mode, "live_enabled": live_enabled, "broker": "alpaca"}}


# ── §3 Tests 1-5: should_skip ─────────────────────────────────────────────────

def test_should_skip_passive():
    """mode=passive → should_skip() is True."""
    policy = BrokerRoutingPolicy(_make_config("passive"), "sp500")
    assert policy.should_skip() is True


def test_should_skip_live_disabled():
    """mode=live, live_enabled=False → should_skip() is True."""
    policy = BrokerRoutingPolicy(_make_config("live", live_enabled=False), "sp500")
    assert policy.should_skip() is True


def test_should_skip_live_enabled():
    """mode=live, live_enabled=True → should_skip() is False."""
    policy = BrokerRoutingPolicy(_make_config("live", live_enabled=True), "sp500")
    assert policy.should_skip() is False


def test_should_skip_paper_no_live_enabled():
    """mode=paper, live_enabled=False → should_skip() is False (paper runs without live_enabled)."""
    policy = BrokerRoutingPolicy(_make_config("paper", live_enabled=False), "sp500")
    assert policy.should_skip() is False


def test_should_skip_paper_with_live_enabled():
    """mode=paper, live_enabled=True → should_skip() is False."""
    policy = BrokerRoutingPolicy(_make_config("paper", live_enabled=True), "sp500")
    assert policy.should_skip() is False


# ── §3 Tests 6-11: needs_paper_pass ──────────────────────────────────────────

def test_needs_paper_pass_already_paper():
    """mode=paper, paper trades exist → needs_paper_pass() is False."""
    policy = BrokerRoutingPolicy(_make_config("paper"), "sp500")
    with patch("db.atlas_db.get_open_paper_trades",
               return_value=[{"universe": "sp500", "ticker": "AAPL"}]):
        assert policy.needs_paper_pass() is False


def test_needs_paper_pass_no_open_paper_trades():
    """mode=live, DB empty → needs_paper_pass() is False."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch("db.atlas_db.get_open_paper_trades", return_value=[]):
        assert policy.needs_paper_pass() is False


def test_needs_paper_pass_has_open_paper_trades():
    """mode=live, DB has paper trade for sp500 → needs_paper_pass() is True."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch("db.atlas_db.get_open_paper_trades",
               return_value=[{"universe": "sp500", "ticker": "AAPL"}]):
        assert policy.needs_paper_pass() is True


def test_needs_paper_pass_other_universe():
    """mode=live, DB has paper trade for commodity_etfs only → False for sp500 policy."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch("db.atlas_db.get_open_paper_trades",
               return_value=[{"universe": "commodity_etfs", "ticker": "GLD"}]):
        assert policy.needs_paper_pass() is False


def test_needs_paper_pass_db_error_non_fatal():
    """DB raises RuntimeError → needs_paper_pass() returns False (FAIL-OPEN), no exception."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch("db.atlas_db.get_open_paper_trades", side_effect=RuntimeError("db gone")):
        result = policy.needs_paper_pass()
    assert result is False


def test_needs_paper_pass_memoized():
    """Two calls to needs_paper_pass() → only ONE DB hit (cached_property on _has_open_paper_trades)."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    mock_fn = MagicMock(return_value=[{"universe": "sp500", "ticker": "AAPL"}])
    with patch("db.atlas_db.get_open_paper_trades", mock_fn):
        _ = policy.needs_paper_pass()
        _ = policy.needs_paper_pass()
    assert mock_fn.call_count == 1


# ── §3 Tests 12-14: paper_config ──────────────────────────────────────────────

def test_paper_config_patches_mode():
    """paper_config['trading']['mode'] == 'paper'."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    assert policy.paper_config["trading"]["mode"] == "paper"


def test_paper_config_preserves_other_trading_keys():
    """live_enabled and broker keys are carried through in paper_config."""
    cfg = {"trading": {"mode": "live", "live_enabled": True, "broker": "alpaca", "live_safety": "yes"}}
    policy = BrokerRoutingPolicy(cfg, "sp500")
    pc = policy.paper_config
    assert pc["trading"]["live_enabled"] is True
    assert pc["trading"]["broker"] == "alpaca"
    assert pc["trading"]["live_safety"] == "yes"


def test_paper_config_does_not_mutate_original():
    """Accessing policy.paper_config does not mutate the original config."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    _ = policy.paper_config
    assert policy.config["trading"]["mode"] == "live"
    assert policy.mode == "live"


# ── §3 Tests 15-16: for_paper ─────────────────────────────────────────────────

def test_for_paper_returns_new_policy():
    """policy.for_paper().is_paper is True; original policy.is_paper is unchanged."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    paper_policy = policy.for_paper()
    assert paper_policy.is_paper is True
    assert policy.is_paper is False


def test_for_paper_preserves_market_id():
    """policy.for_paper().market_id == policy.market_id."""
    policy = BrokerRoutingPolicy(_make_config("live"), "commodity_etfs")
    assert policy.for_paper().market_id == "commodity_etfs"


# ── §3 Tests 17-21: split_entries_by_lifecycle ────────────────────────────────

_PATCH_SPLIT = "monitor.strategy_lifecycle.split_trades_by_lifecycle"


def test_split_entries_all_live():
    """All strategies in LIVE state → (entries, [])."""
    entries = [
        {"strategy": "momentum_breakout", "ticker": "AAPL"},
        {"strategy": "connors_rsi2", "ticker": "MSFT"},
    ]
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch(_PATCH_SPLIT, return_value=(entries, [])) as mock_split:
        live, paper = policy.split_entries_by_lifecycle(entries)
    assert paper == []
    assert len(live) == 2
    mock_split.assert_called_once()


def test_split_entries_mixed_lifecycle():
    """One PAPER, one LIVE → split as expected."""
    entries = [
        {"strategy": "momentum_breakout", "ticker": "AAPL"},
        {"strategy": "short_term_mr", "ticker": "MSFT"},
    ]
    live_expected = [entries[0]]
    paper_expected = [entries[1]]
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch(_PATCH_SPLIT, return_value=(live_expected, paper_expected)):
        live, paper = policy.split_entries_by_lifecycle(entries)
    assert live == live_expected
    assert paper == paper_expected


def test_split_entries_unknown_strategy():
    """Missing/empty 'strategy' key → routed to live."""
    entries = [{"ticker": "AAPL"}, {"strategy": "", "ticker": "MSFT"}]
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    # split_trades_by_lifecycle in monitor.strategy_lifecycle routes missing strategy to live
    with patch(_PATCH_SPLIT, return_value=(entries, [])):
        live, paper = policy.split_entries_by_lifecycle(entries)
    assert paper == []
    assert len(live) == 2


def test_split_entries_empty_input():
    """Empty list → ([], [])."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch(_PATCH_SPLIT, return_value=([], [])):
        live, paper = policy.split_entries_by_lifecycle([])
    assert live == []
    assert paper == []


def test_split_entries_import_failure_routes_all_to_live():
    """monitor.strategy_lifecycle import failure → all entries to live (safe fallback)."""
    entries = [
        {"strategy": "momentum_breakout", "ticker": "AAPL"},
        {"strategy": "short_term_mr", "ticker": "MSFT"},
    ]
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    with patch.dict("sys.modules", {"monitor.strategy_lifecycle": None}):
        live, paper = policy.split_entries_by_lifecycle(entries)
    assert paper == []
    assert len(live) == 2
    assert {e["ticker"] for e in live} == {"AAPL", "MSFT"}


# ── §3 Tests 22-25: trade_table / protective_table ────────────────────────────

def test_trade_table_paper():
    """is_paper → trade_table() == 'paper_trades'."""
    policy = BrokerRoutingPolicy(_make_config("paper"), "sp500")
    assert policy.trade_table() == "paper_trades"


def test_trade_table_live():
    """is_live → trade_table() == 'trades'."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    assert policy.trade_table() == "trades"


def test_protective_table_paper():
    """is_paper → protective_table() == 'paper_position_protective_orders'."""
    policy = BrokerRoutingPolicy(_make_config("paper"), "sp500")
    assert policy.protective_table() == "paper_position_protective_orders"


def test_protective_table_live():
    """is_live → protective_table() == 'position_protective_orders'."""
    policy = BrokerRoutingPolicy(_make_config("live"), "sp500")
    assert policy.protective_table() == "position_protective_orders"
