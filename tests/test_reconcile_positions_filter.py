"""Unit tests for reconcile_positions.py universe∪state-file filter (E2 fix).

Tests verify that reconcile_positions() correctly scopes broker positions to
only those belonging to the target market — preventing cross-market UNTRACKED
false positives (e.g. commodity ETFs showing up as UNTRACKED during sp500 reconcile).
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_broker_position(ticker: str, shares: int = 5, entry_price: float = 100.0):
    """Return a simple namespace mimicking a BrokerPosition dataclass."""
    pos = types.SimpleNamespace(
        ticker=ticker,
        shares=shares,
        entry_price=entry_price,
    )
    return pos


def _make_mock_broker(positions: list) -> MagicMock:
    """Return a mock broker that returns the given positions."""
    broker = MagicMock()
    broker.connect.return_value = True
    broker.get_positions.return_value = positions
    broker.disconnect.return_value = None
    return broker


def _make_state_file(tmp_path: Path, market_id: str, tickers: list[str]) -> Path:
    """Write a minimal live_{market}.json state file."""
    state = {
        "market_id": market_id,
        "mode": "live",
        "positions": [
            {
                "ticker": t,
                "strategy": "test",
                "entry_date": "2026-04-01",
                "entry_price": 100.0,
                "shares": 1,
                "stop_price": 95.0,
                "order_id": "",
            }
            for t in tickers
        ],
        "closed_trades": [],
        "equity_history": [],
        "last_saved": "2026-04-22T00:00:00",
    }
    state_dir = tmp_path / "brokers" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"live_{market_id}.json"
    path.write_text(json.dumps(state, indent=2))
    return path


def _make_config(tmp_path: Path, market_id: str) -> Path:
    """Write a minimal active config file."""
    config_dir = tmp_path / "config" / "active"
    config_dir.mkdir(parents=True, exist_ok=True)
    cfg = {"trading": {"broker": "alpaca"}}
    path = config_dir / f"{market_id}.json"
    path.write_text(json.dumps(cfg))
    return path


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestUniverseStateFilter:
    """Tests for the universe∪state-file broker position filter."""

    def test_filter_excludes_out_of_scope_broker_positions(self, tmp_path, monkeypatch):
        """Broker positions not in universe AND not in state file are excluded.

        Scenario: reconciling sp500 market.
        - Broker holds: AMD, CHTR, GLD, UNG (sp500 tickers + commodity ETFs)
        - Universe: AMD, CHTR
        - State file: AMD, CHTR
        → GLD and UNG should be EXCLUDED from broker_map; no UNTRACKED errors.
        """
        market_id = "sp500"
        sp500_tickers = ["AMD", "CHTR"]

        # Set up state + config files in tmp_path
        _make_state_file(tmp_path, market_id, sp500_tickers)
        _make_config(tmp_path, market_id)

        # All broker positions (sp500 + commodity ETFs mixed)
        broker_positions = [
            _make_broker_position("AMD",  shares=2, entry_price=278.0),
            _make_broker_position("CHTR", shares=1, entry_price=244.0),
            _make_broker_position("GLD",  shares=2, entry_price=442.0),  # not in sp500
            _make_broker_position("UNG",  shares=54, entry_price=10.68), # not in sp500
        ]

        mock_broker = _make_mock_broker(broker_positions)

        # Patch get_universe_tickers to return sp500 tickers
        with patch("scripts.reconcile_positions.PROJECT", tmp_path), \
             patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=sp500_tickers):

            from scripts.reconcile_positions import reconcile_positions

            result = reconcile_positions(market_id=market_id, fix=False, dry_run=False)

        # Should be clean — no UNTRACKED for GLD/UNG
        untracked = [d for d in result["discrepancies"] if d["type"] == "UNTRACKED"]
        assert untracked == [], f"Expected no UNTRACKED, got: {untracked}"

        # broker_count should reflect in-scope count (2), not raw count (4)
        assert result["summary"]["broker_count"] == 2, \
            f"Expected broker_count=2, got {result['summary']['broker_count']}"

        # No errors
        assert result.get("error") == ""

    def test_filter_includes_state_tickers_outside_universe(self, tmp_path, monkeypatch):
        """State-file tickers are always in scope even if not in the universe.

        Scenario: ticker XYZ is tracked in the state file for market 'sp500'
        but does not appear in the universe definition (e.g. sector ETF added manually).
        → XYZ should be INCLUDED in broker_map (state file is source of truth).
        """
        market_id = "sp500"
        universe_tickers = ["AMD", "CHTR"]  # XYZ not in universe
        state_tickers    = ["AMD", "CHTR", "XYZ"]  # XYZ IS in state file

        _make_state_file(tmp_path, market_id, state_tickers)
        _make_config(tmp_path, market_id)

        broker_positions = [
            _make_broker_position("AMD",  shares=2,  entry_price=278.0),
            _make_broker_position("CHTR", shares=1,  entry_price=244.0),
            _make_broker_position("XYZ",  shares=10, entry_price=50.0),   # in state, not universe
            _make_broker_position("IRRELEVANT", shares=3, entry_price=99.0),  # neither
        ]

        mock_broker = _make_mock_broker(broker_positions)

        with patch("scripts.reconcile_positions.PROJECT", tmp_path), \
             patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=universe_tickers):

            from scripts.reconcile_positions import reconcile_positions

            result = reconcile_positions(market_id=market_id, fix=False, dry_run=False)

        # IRRELEVANT should be excluded (not in universe, not in state)
        untracked_tickers = {d["ticker"] for d in result["discrepancies"] if d["type"] == "UNTRACKED"}
        assert "IRRELEVANT" not in untracked_tickers, \
            "IRRELEVANT should be filtered out (not in universe or state)"

        # XYZ IS in state but broker also has it — no UNTRACKED expected for XYZ
        assert "XYZ" not in untracked_tickers, \
            "XYZ is in state file and broker — should not be UNTRACKED"

        # broker_count = 3 (AMD + CHTR + XYZ; IRRELEVANT excluded)
        assert result["summary"]["broker_count"] == 3, \
            f"Expected broker_count=3, got {result['summary']['broker_count']}"

    def test_filter_falls_back_when_no_universe_no_state(self, tmp_path, monkeypatch):
        """When universe load fails AND state file is empty, use ALL broker positions.

        The fallback path should warn but not crash. All broker positions should
        be visible as-is (original behaviour before the filter was added).
        """
        market_id = "sp500"

        # Write an empty positions state file
        _make_state_file(tmp_path, market_id, tickers=[])
        _make_config(tmp_path, market_id)

        broker_positions = [
            _make_broker_position("AMD",  shares=2, entry_price=278.0),
            _make_broker_position("GLD",  shares=2, entry_price=442.0),
        ]

        mock_broker = _make_mock_broker(broker_positions)

        def _raise_universe(*args, **kwargs):
            raise ImportError("universe module not available")

        with patch("scripts.reconcile_positions.PROJECT", tmp_path), \
             patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", side_effect=_raise_universe):

            from scripts.reconcile_positions import reconcile_positions

            result = reconcile_positions(market_id=market_id, fix=False, dry_run=False)

        # No crash
        assert result.get("error") == "", f"Unexpected error: {result.get('error')}"

        # ALL broker positions should be visible (fallback path)
        assert result["summary"]["broker_count"] == 2, \
            f"Expected broker_count=2 (fallback uses all), got {result['summary']['broker_count']}"

        # Both AMD and GLD should appear as UNTRACKED (state is empty)
        untracked_tickers = {d["ticker"] for d in result["discrepancies"] if d["type"] == "UNTRACKED"}
        assert "AMD" in untracked_tickers, "AMD should be UNTRACKED (not in empty state)"
        assert "GLD" in untracked_tickers, "GLD should be UNTRACKED (not in empty state)"


class TestCrossMarketExclusion:
    """Tests for the cross-market ticker exclusion logic.

    Tickers managed by another market's state file should be excluded from
    this market's broker_map even if they appear in this market's universe.
    This prevents the FCX-in-commodity_etfs-universe false positive.
    """

    def test_cross_market_ticker_excluded_from_universe_match(self, tmp_path):
        """FCX-like scenario: FCX in commodity_etfs universe but tracked in sp500.

        commodity_etfs universe = [FCX, GLD, UNG]
        sp500 state = [FCX]
        commodity_etfs state = [GLD, UNG]
        Broker = [FCX, GLD, UNG]

        → FCX should be excluded from commodity_etfs broker_map (managed by sp500)
        → GLD and UNG should be in scope (in commodity_etfs universe AND state)
        → No UNTRACKED for FCX
        """
        # Set up commodity_etfs state (GLD, UNG) and sp500 state (FCX)
        _make_state_file(tmp_path, "commodity_etfs", ["GLD", "UNG"])
        _make_state_file(tmp_path, "sp500", ["FCX"])
        _make_config(tmp_path, "commodity_etfs")

        broker_positions = [
            _make_broker_position("FCX", shares=5,  entry_price=68.0),
            _make_broker_position("GLD", shares=2,  entry_price=442.0),
            _make_broker_position("UNG", shares=54, entry_price=10.68),
        ]
        mock_broker = _make_mock_broker(broker_positions)

        commodity_etfs_universe = ["FCX", "GLD", "UNG"]

        with patch("scripts.reconcile_positions.PROJECT", tmp_path), \
             patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=commodity_etfs_universe):

            from scripts.reconcile_positions import reconcile_positions

            result = reconcile_positions(market_id="commodity_etfs", fix=False, dry_run=False)

        # FCX should NOT be UNTRACKED — it's managed by sp500
        untracked_tickers = {d["ticker"] for d in result["discrepancies"] if d["type"] == "UNTRACKED"}
        assert "FCX" not in untracked_tickers, \
            "FCX is managed by sp500, should not be UNTRACKED in commodity_etfs"

        # GLD and UNG are in scope (universe + state); there may be MISMATCH/DRIFT
        # because the test helper uses placeholder prices — that's OK for this test.
        # The critical assertion is that FCX produces no UNTRACKED.

        # broker_count = 2 (GLD + UNG; FCX excluded)
        assert result["summary"]["broker_count"] == 2

    def test_state_file_ticker_always_wins_over_other_market(self, tmp_path):
        """State-file tickers are always in scope, even if another market also has them.

        If a ticker somehow appears in BOTH markets' state files (duplicate state),
        the current market's state takes precedence and the position IS tracked.
        """
        # Both sp500 and commodity_etfs track FCX (degenerate case)
        _make_state_file(tmp_path, "sp500",          ["FCX", "AMD"])
        _make_state_file(tmp_path, "commodity_etfs", ["FCX", "GLD"])
        _make_config(tmp_path, "sp500")

        broker_positions = [
            _make_broker_position("FCX", shares=5, entry_price=68.0),
            _make_broker_position("AMD", shares=2, entry_price=278.0),
        ]
        mock_broker = _make_mock_broker(broker_positions)

        sp500_universe = ["AMD"]  # FCX not in sp500 universe, but in sp500 state

        with patch("scripts.reconcile_positions.PROJECT", tmp_path), \
             patch("brokers.registry.get_live_broker", return_value=mock_broker), \
             patch("universe.builder.get_universe_tickers", return_value=sp500_universe):

            from scripts.reconcile_positions import reconcile_positions

            result = reconcile_positions(market_id="sp500", fix=False, dry_run=False)

        # FCX is in sp500 state (explicit ownership) — must be in scope regardless
        # of cross-market exclusion logic
        phantom_tickers = {d["ticker"] for d in result["discrepancies"] if d["type"] == "PHANTOM"}
        untracked_tickers = {d["ticker"] for d in result["discrepancies"] if d["type"] == "UNTRACKED"}

        # FCX is in sp500 state and on broker → no PHANTOM, no UNTRACKED for FCX
        assert "FCX" not in phantom_tickers,   "FCX is in broker — should not be PHANTOM"
        assert "FCX" not in untracked_tickers, "FCX is in sp500 state — should not be UNTRACKED"

        # broker_count should include FCX (via state_tickers override)
        assert result["summary"]["broker_count"] == 2
