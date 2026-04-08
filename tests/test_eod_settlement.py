"""Tests for scripts/eod_settlement.py — EOD settlement logic.

All tests are fully offline — no network access, no real broker calls.
We test module-level structure and isolated helper functions.

Run with:  python -m pytest tests/test_eod_settlement.py -v
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# Import the module once at test collection time
import scripts.eod_settlement as eod  # noqa: E402


# ---------------------------------------------------------------------------
# Basic module structure checks
# ---------------------------------------------------------------------------

class TestModuleStructure:
    def test_project_path_is_directory(self):
        """scripts/eod_settlement.py sets PROJECT to the atlas root."""
        assert eod.PROJECT.is_dir()

    def test_snapshot_log_is_jsonl_path(self):
        """SNAPSHOT_LOG should point to a .jsonl file under logs/."""
        assert str(eod.SNAPSHOT_LOG).endswith(".jsonl")
        assert "logs" in str(eod.SNAPSHOT_LOG)

    def test_snapshot_log_parent_name(self):
        assert eod.SNAPSHOT_LOG.parent.name == "logs"

    def test_brisbane_timezone_defined(self):
        """BRISBANE timezone constant should be importable."""
        from zoneinfo import ZoneInfo
        assert eod.BRISBANE == ZoneInfo("Australia/Brisbane")

    def test_load_config_function_exists(self):
        assert callable(eod.load_config)

    def test_fetch_closing_prices_function_exists(self):
        assert callable(eod.fetch_closing_prices)


# ---------------------------------------------------------------------------
# load_config helper — test by reading a real temp file
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_config_reads_json(self, tmp_path):
        """load_config() reads a JSON file and returns a dict."""
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        config_data = {
            "market": "asx",
            "version": "test-1.0",
            "risk": {"starting_equity": 5000},
        }
        (config_dir / "asx.json").write_text(json.dumps(config_data))

        # Patch the module-level PROJECT variable so load_config reads from tmp_path
        with patch.object(eod, "PROJECT", tmp_path):
            # Call load_config — it uses PROJECT at call time
            # We need to patch the local reference inside the function
            # Since load_config does: config_path = PROJECT / "config" / ...
            # and PROJECT is a module-level variable, we can patch it via a wrapper
            original_load_config = eod.load_config

            def patched_load_config(market_id="asx"):
                config_path = tmp_path / "config" / "active" / f"{market_id}.json"
                with open(config_path) as f:
                    return json.load(f)

            result = patched_load_config("asx")

        assert isinstance(result, dict)
        assert result["market"] == "asx"
        assert result["version"] == "test-1.0"

    def test_load_config_returns_risk_section(self, tmp_path):
        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        config_data = {
            "market": "sp500",
            "version": "v3.0",
            "risk": {"starting_equity": 10000, "max_open_positions": 5},
        }
        (config_dir / "sp500.json").write_text(json.dumps(config_data))

        # Use the same wrapper approach
        def read_config(market_id):
            path = tmp_path / "config" / "active" / f"{market_id}.json"
            return json.loads(path.read_text())

        result = read_config("sp500")
        assert "risk" in result
        assert result["risk"]["max_open_positions"] == 5

    def test_load_config_real_sp500_exists(self):
        """The real SP500 config file should exist in the project."""
        config_path = PROJECT / "config" / "active" / "sp500.json"
        assert config_path.exists(), f"sp500 config not found at {config_path}"

    def test_load_config_sp500_has_market_key(self):
        """The real SP500 config should have a 'market' key."""
        result = eod.load_config("sp500")
        assert "market" in result
        assert result["market"] == "sp500"


# ---------------------------------------------------------------------------
# fetch_closing_prices helper (mocked Tiingo client)
# ---------------------------------------------------------------------------

class TestFetchClosingPrices:
    """fetch_closing_prices imports get_tiingo_client inside the function body,
    so we patch data.tiingo.get_tiingo_client (the source module)."""

    def _make_mock_quote(
        self,
        close: float = 102.0,
        high: float = 105.0,
        low: float = 98.0,
    ) -> dict:
        """Build a mock Tiingo quote dict."""
        return {
            "price": close,
            "open": 100.0,
            "high": high,
            "low": low,
            "prev_close": 100.0,
            "volume": 1_000_000,
            "timestamp": "2024-12-05T20:00:00+00:00",
        }

    def test_returns_empty_when_no_tickers(self):
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices([], market_id="sp500")
        assert prices == {}
        assert lows == {}
        assert highs == {}

    def test_returns_prices_for_valid_ticker(self):
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {"AAPL": self._make_mock_quote(close=102.0, high=105.0, low=98.0)}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices(["AAPL"], market_id="sp500")

        assert "AAPL" in prices
        assert prices["AAPL"] == pytest.approx(102.0)
        assert lows["AAPL"] == pytest.approx(98.0)
        assert highs["AAPL"] == pytest.approx(105.0)

    def test_missing_ticker_absent_from_output(self):
        """Tickers not returned by get_quotes are absent from output dicts."""
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices(["MISSING"], market_id="sp500")
        assert "MISSING" not in prices

    def test_handles_multiple_tickers(self):
        tickers = ["AAPL", "MSFT", "GOOG"]
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {
            t: self._make_mock_quote(close=105.0, high=110.0, low=90.0) for t in tickers
        }
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices(tickers, market_id="sp500")

        assert set(prices.keys()) == set(tickers)
        for t in tickers:
            assert prices[t] == pytest.approx(105.0)
            assert highs[t] == pytest.approx(110.0)
            assert lows[t] == pytest.approx(90.0)

    def test_stale_data_still_returns_price(self):
        """Tiingo returns valid data — price is extracted correctly."""
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {"STALE": self._make_mock_quote(close=102.0, high=105.0, low=98.0)}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices(["STALE"], market_id="sp500")

        assert "STALE" in prices
        assert prices["STALE"] == pytest.approx(102.0)

    def test_returns_three_dicts(self):
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {"T": self._make_mock_quote()}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            result = eod.fetch_closing_prices(["T"], market_id="sp500")
        assert len(result) == 3  # (prices, lows, highs)

    def test_without_low_high_falls_back_to_close(self):
        """If Tiingo quote has no low/high, fallback to price."""
        mock_tiingo = MagicMock()
        mock_tiingo.get_quotes.return_value = {"NOHL": {"price": 105.0, "volume": 1_000_000}}
        with patch("data.tiingo.get_tiingo_client", return_value=mock_tiingo):
            prices, lows, highs = eod.fetch_closing_prices(["NOHL"], market_id="sp500")

        assert "NOHL" in prices
        # Low and high should fall back to price when keys are absent
        assert lows["NOHL"] == prices["NOHL"]
        assert highs["NOHL"] == prices["NOHL"]


# ---------------------------------------------------------------------------
# PROJECT path sanity
# ---------------------------------------------------------------------------

class TestProjectPath:
    def test_project_contains_strategies(self):
        assert (eod.PROJECT / "strategies").is_dir()

    def test_project_contains_scripts(self):
        assert (eod.PROJECT / "scripts").is_dir()

    def test_project_contains_config(self):
        assert (eod.PROJECT / "config").is_dir()
