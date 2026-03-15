"""Tests for universe/builder.py — universe construction logic.

All tests are fully offline: no yfinance calls, no network access.
We patch network-bound functions and use tmp_path for file I/O.

Run with:  python -m pytest tests/test_universe_builder.py -v
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from tests.conftest import MINIMAL_CONFIG  # noqa: E402
import copy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config():
    return copy.deepcopy(MINIMAL_CONFIG)


def _make_universe_json(
    tickers: list[str],
    market_id: str = "sp500",
    top_n: int | None = None,
) -> dict:
    """Build a universe.json payload matching the expected schema."""
    top_n = top_n or len(tickers)
    return {
        "metadata": {
            "built_at": datetime.now().isoformat(),
            "config_version": "test-v1.0",
            "candidates_evaluated": len(tickers),
            "candidates_valid": len(tickers),
            "candidates_failed": 0,
            "filters": {
                "min_price": 5.0,
                "min_median_daily_value": 5_000_000,
                "min_market_cap": 2_000_000_000,
                "top_n": top_n,
                "exclusions": [],
            },
            "filtered_out": {
                "by_price": 0,
                "by_daily_value": 0,
                "by_market_cap": 0,
            },
            "final_count": len(tickers),
        },
        "tickers": tickers,
        "details": [
            {
                "ticker": t,
                "last_close": 100.0,
                "median_daily_value": 10_000_000.0,
                "avg_daily_value": 10_000_000.0,
                "market_cap": 5_000_000_000.0,
            }
            for t in tickers
        ],
    }


# ---------------------------------------------------------------------------
# load_universe
# ---------------------------------------------------------------------------

class TestLoadUniverse:
    def test_load_returns_dict(self, tmp_path):
        tickers = ["AAPL", "MSFT", "GOOG"]
        universe_data = _make_universe_json(tickers)
        # Write universe.json to expected path
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        assert isinstance(result, dict)

    def test_load_has_tickers_key(self, tmp_path):
        tickers = ["AAPL", "MSFT"]
        universe_data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        assert "tickers" in result

    def test_load_has_metadata_key(self, tmp_path):
        tickers = ["AAPL", "MSFT"]
        universe_data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        assert "metadata" in result

    def test_load_tickers_count_matches_saved(self, tmp_path):
        tickers = [f"TICK{i}" for i in range(50)]
        universe_data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        assert len(result["tickers"]) == 50

    def test_load_tickers_are_strings(self, tmp_path):
        tickers = ["AAPL", "MSFT", "GOOG"]
        universe_data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        for t in result["tickers"]:
            assert isinstance(t, str)

    def test_load_raises_file_not_found_when_missing(self, tmp_path):
        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            with pytest.raises(FileNotFoundError):
                load_universe("nonexistent_market")

    def test_load_metadata_has_final_count(self, tmp_path):
        tickers = ["A", "B", "C"]
        universe_data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(universe_data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import load_universe
            result = load_universe("sp500")

        assert result["metadata"]["final_count"] == 3


# ---------------------------------------------------------------------------
# build_universe — filter logic (fully mocked, no network)
# ---------------------------------------------------------------------------

class TestBuildUniverseFilters:
    """Test that build_universe filter logic works correctly.

    We patch out all network calls (_get_market_cap, _compute_daily_value_stats,
    get_market_tickers) so tests run offline.
    """

    def _make_dv_stats(
        self,
        last_close: float = 100.0,
        median_dv: float = 10_000_000.0,
        avg_dv: float = 10_000_000.0,
    ) -> dict:
        return {
            "median_daily_value": median_dv,
            "avg_daily_value": avg_dv,
            "last_close": last_close,
            "avg_volume": 100_000.0,
            "trading_days": 60,
        }

    def test_price_filter_removes_cheap_tickers(self, tmp_path, mock_config):
        """Tickers with last_close < min_price should be filtered out."""
        mock_config["universe"]["min_price"] = 10.0
        mock_config["universe"]["min_market_cap"] = 0  # disable
        mock_config["universe"]["top_n"] = 10

        tickers = ["CHEAP", "OK"]
        # CHEAP: price < 10, OK: price >= 10
        dv_stats = {
            "CHEAP": self._make_dv_stats(last_close=3.0),
            "OK": self._make_dv_stats(last_close=50.0),
        }

        def fake_dv(ticker, lookback_days=60):
            return dv_stats.get(ticker.split(".")[0], self._make_dv_stats())

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder._get_market_cap", return_value=5_000_000_000.0), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(mock_config, save=False, verbose=False)

        assert "CHEAP" not in result
        assert "OK" in result

    def test_daily_value_filter_removes_illiquid_tickers(self, tmp_path, mock_config):
        """Tickers with median_daily_value < min_median_daily_value are removed."""
        mock_config["universe"]["min_median_daily_value"] = 5_000_000
        mock_config["universe"]["min_market_cap"] = 0  # disable
        mock_config["universe"]["top_n"] = 10

        tickers = ["ILLIQUID", "LIQUID"]
        dv_stats = {
            "ILLIQUID": self._make_dv_stats(median_dv=1_000_000),   # < 5M
            "LIQUID":   self._make_dv_stats(median_dv=20_000_000),  # >= 5M
        }

        def fake_dv(ticker, lookback_days=60):
            return dv_stats.get(ticker.split(".")[0], self._make_dv_stats())

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder._get_market_cap", return_value=5_000_000_000.0), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(mock_config, save=False, verbose=False)

        assert "ILLIQUID" not in result
        assert "LIQUID" in result

    def test_exclusions_remove_tickers(self, tmp_path, mock_config):
        """Tickers in exclusions list should not appear in output."""
        mock_config["universe"]["exclusions"] = ["BAD"]
        mock_config["universe"]["min_market_cap"] = 0
        mock_config["universe"]["top_n"] = 10

        tickers = ["BAD", "GOOD"]

        def fake_dv(ticker, lookback_days=60):
            return self._make_dv_stats()

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder._get_market_cap", return_value=5_000_000_000.0), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(mock_config, save=False, verbose=False)

        assert "BAD" not in result
        assert "GOOD" in result

    def test_top_n_limits_output_count(self, tmp_path, mock_config):
        """build_universe should return at most top_n tickers."""
        mock_config["universe"]["top_n"] = 3
        mock_config["universe"]["min_market_cap"] = 0

        tickers = [f"T{i}" for i in range(10)]

        def fake_dv(ticker, lookback_days=60):
            return self._make_dv_stats()

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder._get_market_cap", return_value=5_000_000_000.0), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(mock_config, save=False, verbose=False)

        assert len(result) <= 3

    def test_save_writes_universe_json(self, tmp_path, mock_config):
        """When save=True, universe.json is written to disk."""
        mock_config["universe"]["min_market_cap"] = 0
        mock_config["universe"]["top_n"] = 5
        tickers = ["AAPL", "MSFT", "GOOG"]

        def fake_dv(ticker, lookback_days=60):
            return self._make_dv_stats()

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder._get_market_cap", return_value=5_000_000_000.0), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            build_universe(mock_config, save=True, verbose=False)

        market_id = mock_config.get("market", "asx")
        output = tmp_path / market_id / "universe.json"
        assert output.exists()
        with open(output) as f:
            data = json.load(f)
        assert "tickers" in data
        assert "metadata" in data

    def test_empty_candidate_list_returns_empty(self, tmp_path, mock_config):
        """No candidates → empty universe, no crash."""
        mock_config["universe"]["min_market_cap"] = 0
        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(
                mock_config, candidate_tickers=[], save=False, verbose=False
            )
        assert result == []

    def test_all_tickers_fail_dv_returns_empty(self, tmp_path, mock_config):
        """If _compute_daily_value_stats returns last_close=None, universe is empty."""
        mock_config["universe"]["min_market_cap"] = 0
        mock_config["universe"]["top_n"] = 10
        tickers = ["FAIL1", "FAIL2"]

        def fake_dv(ticker, lookback_days=60):
            return {
                "median_daily_value": None,
                "avg_daily_value": None,
                "last_close": None,
                "avg_volume": None,
                "trading_days": 0,
            }

        with patch("universe.builder._compute_daily_value_stats", side_effect=fake_dv), \
             patch("universe.builder.get_market_tickers", return_value=tickers), \
             patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import build_universe
            result = build_universe(mock_config, save=False, verbose=False)

        assert result == []


# ---------------------------------------------------------------------------
# get_universe_tickers convenience wrapper
# ---------------------------------------------------------------------------

class TestGetUniverseTickers:
    def test_returns_list_of_strings(self, tmp_path):
        tickers = ["AAPL", "MSFT", "AMZN"]
        data = _make_universe_json(tickers)
        proc_dir = tmp_path / "sp500"
        proc_dir.mkdir(parents=True)
        (proc_dir / "universe.json").write_text(json.dumps(data))

        with patch("universe.builder.PROCESSED_DIR", tmp_path):
            from universe.builder import get_universe_tickers
            result = get_universe_tickers("sp500")

        assert isinstance(result, list)
        assert all(isinstance(t, str) for t in result)
        assert result == tickers
