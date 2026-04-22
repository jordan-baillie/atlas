"""Regression test: _update_state_positions() universe-membership guard (B2.1 fix).

Ensures that when self.positions contains tickers from multiple universes
(e.g. GLD valid for commodity_etfs, XLY valid only for sector_etfs, AAPL
valid only for sp500), only tickers in the market's own universe are written
to the state file.

Verifies:
  1. Only GLD ends up in commodity_etfs state after _update_state_positions()
  2. A WARNING log line is emitted for each filtered-out ticker (XLY, AAPL)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal config for LivePortfolio construction
# ---------------------------------------------------------------------------
MINIMAL_CONFIG: dict = {
    "version": "test",
    "market": "commodity_etfs",
    "risk": {
        "starting_equity": 5000,
        "leverage": 1.0,
        "max_risk_per_trade_pct": 0.005,
        "max_open_positions": 10,
        "max_sector_concentration": 2,
        "max_daily_drawdown_pct": 0.02,
    },
    "fees": {},
    "trading": {"live_enabled": False},
}

# commodity_etfs universe (confirmed via markets.get_market("commodity_etfs").get_formatted_tickers())
COMMODITY_UNIVERSE = {"GLD", "SLV", "USO", "XOP", "CORN", "DBA", "DBB", "UNG", "CCJ", "FCX"}


def _make_position(ticker: str):
    """Return a minimal Position-like object for testing."""
    pos = SimpleNamespace(
        ticker=ticker,
        strategy="momentum_breakout",
        entry_date="2026-04-22",
        entry_price=100.0,
        shares=10,
        stop_price=95.0,
        stop_order_id="",
        tp_order_id="",
        order_id="",
    )
    return pos


def _write_initial_state(state_path: Path, market_id: str, tickers: list[str]) -> None:
    """Write a minimal live_{market}.json state file to tmp path."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "market_id": market_id,
        "mode": "live",
        "positions": [
            {
                "ticker": t,
                "strategy": "momentum_breakout",
                "entry_date": "2026-04-01",
                "entry_price": 100.0,
                "shares": 10,
                "stop_price": 95.0,
                "order_id": "",
            }
            for t in tickers
        ],
        "closed_trades": [],
        "equity_history": [],
    }
    state_path.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_portfolio(tmp_path, monkeypatch):
    """Return a LivePortfolio for commodity_etfs with no broker connected."""
    from brokers.live_portfolio import LivePortfolio

    # Prevent reading/writing the real state file
    fake_state = tmp_path / "live_commodity_etfs.json"

    portfolio = LivePortfolio.__new__(LivePortfolio)
    portfolio.config = MINIMAL_CONFIG
    portfolio.market_id = "commodity_etfs"
    portfolio.starting_equity = 5000.0
    portfolio.max_risk_per_trade = 0.005
    portfolio.max_positions = 10
    portfolio.max_sector_conc = 2
    portfolio.max_daily_dd = 0.02
    portfolio.leverage = 1.0
    portfolio.commission_flat = 0
    portfolio.commission_pct = 0
    portfolio.positions = []
    portfolio.cash = 0.0
    portfolio.buying_power = 0.0
    portfolio._broker_equity = 0.0
    portfolio.broker_data_valid = True
    portfolio.closed_trades = []
    portfolio.equity_history = []
    portfolio.daily_high_water = 5000.0
    portfolio.halted = False
    portfolio.halt_reason = ""
    portfolio._broker = None
    portfolio._connected = False

    # Patch _state_path to point at tmp directory
    monkeypatch.setattr(
        portfolio, "_state_path",
        lambda: fake_state,
    )

    return portfolio, fake_state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUniverseMembershipGuard:
    """Verify _update_state_positions() filters cross-universe tickers."""

    def test_only_valid_ticker_written_to_state(self, live_portfolio, caplog):
        """GLD (in commodity_etfs universe) is written; XLY and AAPL are filtered."""
        portfolio, state_path = live_portfolio

        # Set up initial state file with GLD, XLY, AAPL
        _write_initial_state(state_path, "commodity_etfs", ["GLD", "XLY", "AAPL"])

        # self.positions has all three tickers
        portfolio.positions = [
            _make_position("GLD"),
            _make_position("XLY"),
            _make_position("AAPL"),
        ]

        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            portfolio._update_state_positions()

        # Read back state
        state = json.loads(state_path.read_text())
        written_tickers = [p["ticker"] for p in state["positions"]]

        assert written_tickers == ["GLD"], (
            f"Expected only GLD in state, got {written_tickers}"
        )

    def test_warning_emitted_for_each_filtered_ticker(self, live_portfolio, caplog):
        """A WARNING log is emitted for XLY and AAPL (out-of-universe)."""
        portfolio, state_path = live_portfolio

        _write_initial_state(state_path, "commodity_etfs", ["GLD", "XLY", "AAPL"])
        portfolio.positions = [
            _make_position("GLD"),
            _make_position("XLY"),
            _make_position("AAPL"),
        ]

        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            portfolio._update_state_positions()

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        xly_warned = any("XLY" in m for m in warning_msgs)
        aapl_warned = any("AAPL" in m for m in warning_msgs)

        assert xly_warned, f"Expected WARNING about XLY, got: {warning_msgs}"
        assert aapl_warned, f"Expected WARNING about AAPL, got: {warning_msgs}"

    def test_commodity_ticker_passes_through(self, live_portfolio):
        """Multiple valid commodity_etfs tickers all survive the filter."""
        portfolio, state_path = live_portfolio

        valid = ["GLD", "UNG", "FCX"]
        _write_initial_state(state_path, "commodity_etfs", valid)
        portfolio.positions = [_make_position(t) for t in valid]

        portfolio._update_state_positions()

        state = json.loads(state_path.read_text())
        written_tickers = sorted(p["ticker"] for p in state["positions"])
        assert written_tickers == sorted(valid)

    def test_all_filtered_out_leaves_empty_positions(self, live_portfolio):
        """If ALL self.positions are out-of-universe, state gets empty positions list."""
        portfolio, state_path = live_portfolio

        _write_initial_state(state_path, "commodity_etfs", ["XLY", "AAPL", "MSFT"])
        portfolio.positions = [
            _make_position("XLY"),
            _make_position("AAPL"),
            _make_position("MSFT"),
        ]

        portfolio._update_state_positions()

        state = json.loads(state_path.read_text())
        assert state["positions"] == []

    def test_xly_specifically_filtered_from_commodity_etfs(self, live_portfolio, caplog):
        """XLY is specifically filtered: it belongs to sector_etfs not commodity_etfs."""
        portfolio, state_path = live_portfolio

        _write_initial_state(state_path, "commodity_etfs", ["GLD", "XLY"])
        portfolio.positions = [
            _make_position("GLD"),
            _make_position("XLY"),
        ]

        with caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"):
            portfolio._update_state_positions()

        state = json.loads(state_path.read_text())
        written_tickers = [p["ticker"] for p in state["positions"]]

        assert "XLY" not in written_tickers
        assert "GLD" in written_tickers

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("XLY" in m for m in warning_msgs)

    def test_no_state_file_does_not_crash(self, live_portfolio):
        """_update_state_positions() exits gracefully when state file doesn't exist."""
        portfolio, state_path = live_portfolio
        # Don't create the state file
        assert not state_path.exists()

        portfolio.positions = [_make_position("GLD")]
        portfolio._update_state_positions()  # must not raise

    def test_idempotent_when_all_valid(self, live_portfolio):
        """Running twice with same valid positions yields same result (idempotent)."""
        portfolio, state_path = live_portfolio

        _write_initial_state(state_path, "commodity_etfs", ["GLD", "UNG"])
        portfolio.positions = [_make_position("GLD"), _make_position("UNG")]

        portfolio._update_state_positions()
        state1 = json.loads(state_path.read_text())

        portfolio._update_state_positions()
        state2 = json.loads(state_path.read_text())

        assert [p["ticker"] for p in state1["positions"]] == \
               [p["ticker"] for p in state2["positions"]]
