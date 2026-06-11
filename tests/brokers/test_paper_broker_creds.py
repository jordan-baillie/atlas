"""Unit tests for AlpacaBroker paper/live credential selection.

Verifies that:
 - mode="paper" loads ALPACA_PAPER_* credentials
 - mode="live" loads ALPACA_* credentials
 - TradingClient is called with paper=True for mode="paper"
 - name property reflects mode
 - default mode is "live"

No network calls are made — TradingClient is fully mocked.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from atlas.brokers.alpaca.broker import AlpacaBroker


# ── Fixtures ──────────────────────────────────────────────────

def _live_cfg(mode: str = "live") -> dict:
    return {
        "market": "sp500",
        "trading": {
            "broker": "alpaca",
            "live_enabled": True,
            "mode": mode,
        },
        "risk": {"starting_equity": 5000},
        "alpaca": {"feed": "iex", "tif": "day"},
    }


def _mock_account(equity: str = "100000.00") -> SimpleNamespace:
    return SimpleNamespace(
        equity=equity,
        cash="90000.00",
        buying_power="100000.00",
        portfolio_value=equity,
        status="ACTIVE",
        account_blocked=False,
        trading_blocked=False,
        account_number="PA1234567890",
        long_market_value="0.00",
    )


def _secret_provider(paper_key: str, paper_secret: str, live_key: str, live_secret: str):
    """Returns a get_secret mock that returns distinct values per key name."""
    secrets = {
        "ALPACA_PAPER_API_KEY": paper_key,
        "ALPACA_PAPER_SECRET_KEY": paper_secret,
        "ALPACA_API_KEY": live_key,
        "ALPACA_SECRET_KEY": live_secret,
    }
    return lambda key, prompt=False: secrets.get(key)


# ── Tests ─────────────────────────────────────────────────────

class TestPaperCredentials:

    def test_alpaca_broker_mode_paper_loads_paper_creds(self):
        """mode="paper" should load ALPACA_PAPER_API_KEY / ALPACA_PAPER_SECRET_KEY."""
        cfg = _live_cfg(mode="paper")
        broker = AlpacaBroker(cfg, live=True, mode="paper")

        loaded_keys = []

        def _mock_secret(key, prompt=False):
            loaded_keys.append(key)
            return f"fake_{key}"

        mock_tc = MagicMock()
        mock_tc.return_value.get_account.return_value = _mock_account()

        with patch("atlas.brokers.alpaca.broker.get_secret", side_effect=_mock_secret), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc), \
             patch("atlas.brokers.alpaca.broker.AlpacaMarketData"):
            result = broker.connect()

        assert result is True
        assert "ALPACA_PAPER_API_KEY" in loaded_keys
        assert "ALPACA_PAPER_SECRET_KEY" in loaded_keys
        # Live keys must NOT be loaded in paper mode
        assert "ALPACA_API_KEY" not in loaded_keys
        assert "ALPACA_SECRET_KEY" not in loaded_keys

    def test_alpaca_broker_mode_live_loads_live_creds(self):
        """mode="live" should load ALPACA_API_KEY / ALPACA_SECRET_KEY."""
        cfg = _live_cfg(mode="live")
        broker = AlpacaBroker(cfg, live=True, mode="live")

        loaded_keys = []

        def _mock_secret(key, prompt=False):
            loaded_keys.append(key)
            return f"fake_{key}"

        mock_tc = MagicMock()
        mock_tc.return_value.get_account.return_value = _mock_account()

        with patch("atlas.brokers.alpaca.broker.get_secret", side_effect=_mock_secret), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc), \
             patch("atlas.brokers.alpaca.broker.AlpacaMarketData"):
            result = broker.connect()

        assert result is True
        assert "ALPACA_API_KEY" in loaded_keys
        assert "ALPACA_SECRET_KEY" in loaded_keys
        # Paper keys must NOT be loaded in live mode
        assert "ALPACA_PAPER_API_KEY" not in loaded_keys
        assert "ALPACA_PAPER_SECRET_KEY" not in loaded_keys

    def test_alpaca_broker_mode_paper_sets_paper_true(self):
        """mode="paper" should call TradingClient(paper=True)."""
        cfg = _live_cfg(mode="paper")
        broker = AlpacaBroker(cfg, live=True, mode="paper")

        mock_tc = MagicMock()
        mock_tc.return_value.get_account.return_value = _mock_account()

        with patch("atlas.brokers.alpaca.broker.get_secret", return_value="test-key"), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc), \
             patch("atlas.brokers.alpaca.broker.AlpacaMarketData"):
            broker.connect()

        # Verify TradingClient was called with paper=True
        call_kwargs = mock_tc.call_args.kwargs
        assert call_kwargs.get("paper") is True, \
            f"Expected paper=True but got: {call_kwargs}"

    def test_alpaca_broker_mode_live_sets_paper_false(self):
        """mode="live" should call TradingClient(paper=False)."""
        cfg = _live_cfg(mode="live")
        # Explicit paper=False in alpaca config
        cfg["alpaca"]["paper"] = False
        broker = AlpacaBroker(cfg, live=True, mode="live")

        mock_tc = MagicMock()
        mock_tc.return_value.get_account.return_value = _mock_account()

        with patch("atlas.brokers.alpaca.broker.get_secret", return_value="test-key"), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc), \
             patch("atlas.brokers.alpaca.broker.AlpacaMarketData"):
            broker.connect()

        call_kwargs = mock_tc.call_args.kwargs
        assert call_kwargs.get("paper") is False, \
            f"Expected paper=False but got: {call_kwargs}"

    def test_broker_name_reflects_mode_paper(self):
        """mode="paper" → name should be 'AlpacaBroker[PAPER]'."""
        cfg = _live_cfg(mode="paper")
        broker = AlpacaBroker(cfg, live=True, mode="paper")
        assert broker.name == "AlpacaBroker[PAPER]"

    def test_broker_name_reflects_mode_live(self):
        """mode="live" → name should be 'AlpacaBroker[LIVE]'."""
        cfg = _live_cfg(mode="live")
        broker = AlpacaBroker(cfg, live=True, mode="live")
        assert broker.name == "AlpacaBroker[LIVE]"

    def test_default_mode_is_live(self):
        """AlpacaBroker(config) with no mode kwarg should default to mode='live'."""
        cfg = _live_cfg()
        # Explicitly pass live=True + alpaca.paper=False so self._paper=False → name=LIVE
        cfg["alpaca"]["paper"] = False
        broker = AlpacaBroker(cfg, live=True)  # no mode kwarg → defaults to "live"
        assert broker.mode == "live"
        assert broker.name == "AlpacaBroker[LIVE]"

    def test_mode_property_paper(self):
        """mode property should return the string passed at construction."""
        broker = AlpacaBroker(_live_cfg(mode="paper"), mode="paper")
        assert broker.mode == "paper"

    def test_paper_credentials_missing_returns_false(self):
        """connect() returns False when paper creds are missing."""
        cfg = _live_cfg(mode="paper")
        broker = AlpacaBroker(cfg, mode="paper")

        mock_tc = MagicMock()
        with patch("atlas.brokers.alpaca.broker.get_secret", return_value=None), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc):
            result = broker.connect()

        assert result is False
        mock_tc.assert_not_called()

    def test_account_number_populated_after_connect(self):
        """account_number property should be set from account data after connect()."""
        cfg = _live_cfg(mode="paper")
        broker = AlpacaBroker(cfg, mode="paper")

        mock_tc = MagicMock()
        mock_tc.return_value.get_account.return_value = _mock_account()

        with patch("atlas.brokers.alpaca.broker.get_secret", return_value="test-key"), \
             patch("atlas.brokers.alpaca.broker.TradingClient", mock_tc), \
             patch("atlas.brokers.alpaca.broker.AlpacaMarketData"):
            broker.connect()

        assert broker.account_number == "PA1234567890"
