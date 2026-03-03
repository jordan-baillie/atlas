"""Unit tests for the pre-market volatility gate.

Run with:
    python -m pytest tests/test_volatility_gate.py -v

Tests cover:
    - Gate disabled via config
    - No flags (all clear)
    - 1 indicator flagged → size reduction (50%)
    - 2+ indicators flagged → block all entries
    - Individual indicator checks (gap, VIX)
    - Missing / bad data handling
    - check_volatility_gate() integration
"""

import sys
import os
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
import numpy as np
from datetime import date, datetime

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.volatility_gate import (
    check_volatility_gate,
    _check_gap_indicator,
    _check_vix_indicator,
    _fetch_overnight_data,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _mock_data(ticker: str, prev_close: float, open_price: float, close: float = None) -> dict:
    """Build a mock overnight data dict as returned by _fetch_overnight_data."""
    return {
        "ticker": ticker,
        "prev_date": "2026-03-02",
        "curr_date": "2026-03-03",
        "prev_close": prev_close,
        "open": open_price,
        "high": open_price * 1.01,
        "low": open_price * 0.99,
        "close": close if close is not None else open_price,
        "volume": 1_000_000,
    }


def _config(enabled: bool = True, **threshold_overrides) -> dict:
    """Build a minimal config dict with optional threshold overrides."""
    thresholds = {
        "oil_gap_pct": 5.0,
        "gold_gap_pct": 2.0,
        "vix_level": 25.0,
        "vix_spike_pct": 20.0,
        "asx_futures_gap_pct": 1.5,
    }
    thresholds.update(threshold_overrides)
    return {
        "volatility_gate": {
            "enabled": enabled,
            "thresholds": thresholds,
            "indicators": {
                "oil": True,
                "gold": True,
                "vix": True,
                "asx_futures": True,
            },
        }
    }


# ── _check_gap_indicator ───────────────────────────────────────────────────

class TestCheckGapIndicator:

    def test_no_flag_when_gap_below_threshold(self):
        data = _mock_data("CL=F", prev_close=80.0, open_price=82.0)  # 2.5% gap
        result = _check_gap_indicator("oil", "CL=F", 5.0, data)
        assert result["flagged"] is False
        assert result["gap_pct"] == pytest.approx(2.5, abs=0.01)

    def test_flag_when_gap_exceeds_threshold(self):
        data = _mock_data("CL=F", prev_close=80.0, open_price=85.0)  # 6.25% gap
        result = _check_gap_indicator("oil", "CL=F", 5.0, data)
        assert result["flagged"] is True
        assert result["gap_pct"] == pytest.approx(6.25, abs=0.01)

    def test_flag_on_gap_down(self):
        """Negative gaps (price drops) should also be flagged."""
        data = _mock_data("CL=F", prev_close=80.0, open_price=72.0)  # -10% gap
        result = _check_gap_indicator("oil", "CL=F", 5.0, data)
        assert result["flagged"] is True
        assert result["gap_pct"] == pytest.approx(10.0, abs=0.01)

    def test_exactly_at_threshold_not_flagged(self):
        """Gap must EXCEED (not equal) the threshold to trigger."""
        data = _mock_data("CL=F", prev_close=100.0, open_price=105.0)  # exactly 5%
        result = _check_gap_indicator("oil", "CL=F", 5.0, data)
        assert result["flagged"] is False

    def test_returns_none_details_when_no_data(self):
        result = _check_gap_indicator("oil", "CL=F", 5.0, None)
        assert result["flagged"] is False
        assert result["error"] is not None
        assert result["gap_pct"] is None

    def test_zero_prev_close_handled(self):
        data = _mock_data("CL=F", prev_close=0.0, open_price=80.0)
        result = _check_gap_indicator("oil", "CL=F", 5.0, data)
        assert result["flagged"] is False
        assert result["error"] is not None


# ── _check_vix_indicator ───────────────────────────────────────────────────

class TestCheckVixIndicator:

    def test_no_flag_when_vix_ok(self):
        data = _mock_data("^VIX", prev_close=18.0, open_price=19.0, close=19.0)
        result = _check_vix_indicator(25.0, 20.0, data)
        assert result["flagged"] is False

    def test_flag_when_vix_level_high(self):
        """VIX absolute level > threshold triggers flag."""
        data = _mock_data("^VIX", prev_close=23.0, open_price=24.0, close=28.0)
        result = _check_vix_indicator(25.0, 20.0, data)
        assert result["flagged"] is True
        assert "level" in result["flag_reason"]

    def test_flag_when_vix_spikes(self):
        """VIX daily spike > threshold triggers flag even when level is low."""
        # VIX opens 25% above prev_close (big spike), level stays below 25
        data = _mock_data("^VIX", prev_close=16.0, open_price=20.0, close=19.5)
        result = _check_vix_indicator(25.0, 20.0, data)
        assert result["flagged"] is True
        assert "spike" in result["flag_reason"]

    def test_flag_when_both_vix_conditions(self):
        """Both conditions met → both should appear in flag_reason."""
        data = _mock_data("^VIX", prev_close=22.0, open_price=28.0, close=30.0)
        result = _check_vix_indicator(25.0, 20.0, data)
        assert result["flagged"] is True
        assert "level" in result["flag_reason"]
        assert "spike" in result["flag_reason"]

    def test_no_flag_when_data_none(self):
        result = _check_vix_indicator(25.0, 20.0, None)
        assert result["flagged"] is False
        assert result["error"] is not None


# ── check_volatility_gate (integration) ───────────────────────────────────

class TestCheckVolatilityGate:

    def _mock_all_clear(self):
        """Mock all indicators as safe (no flags)."""
        oil_data = _mock_data("CL=F", prev_close=80.0, open_price=81.0)
        gold_data = _mock_data("GC=F", prev_close=2000.0, open_price=2010.0)
        vix_data = _mock_data("^VIX", prev_close=15.0, open_price=16.0, close=16.0)
        asx_data = _mock_data("^AXJO", prev_close=8000.0, open_price=8010.0)
        return {
            "CL=F": oil_data,
            "GC=F": gold_data,
            "^VIX": vix_data,
            "^AXJO": asx_data,
        }

    def _mock_fetch(self, data_map):
        """Return a mock function for _fetch_overnight_data."""
        def _fetch(ticker, lookback_days=5):
            return data_map.get(ticker)
        return _fetch

    def test_disabled_gate_returns_no_action(self):
        """When gate is disabled in config, action is always 'none'."""
        config = _config(enabled=False)
        result = check_volatility_gate(config)
        assert result["gate_enabled"] is False
        assert result["action"] == "none"
        assert result["size_multiplier"] == 1.0
        assert result["triggered_count"] == 0

    def test_empty_config_uses_defaults_enabled(self):
        """No volatility_gate section → gate is enabled with defaults."""
        with patch("scripts.volatility_gate._fetch_overnight_data") as mock_fetch:
            mock_fetch.return_value = _mock_data("CL=F", prev_close=80.0, open_price=81.0)
            result = check_volatility_gate({})
        assert result["gate_enabled"] is True

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_no_flags_all_clear(self, mock_fetch):
        """No indicators triggered → action=none, multiplier=1.0."""
        data_map = self._mock_all_clear()
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert result["action"] == "none"
        assert result["size_multiplier"] == 1.0
        assert result["triggered_count"] == 0
        assert result["flags"] == []

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_one_flag_triggers_reduce(self, mock_fetch):
        """1 indicator flagged → action=reduce, multiplier=0.5."""
        data_map = self._mock_all_clear()
        # Make oil spike 10% (above 5% threshold)
        data_map["CL=F"] = _mock_data("CL=F", prev_close=80.0, open_price=88.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert result["action"] == "reduce"
        assert result["size_multiplier"] == 0.5
        assert result["triggered_count"] == 1
        assert "oil" in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_two_flags_triggers_block(self, mock_fetch):
        """2 indicators flagged → action=block, multiplier=0.0."""
        data_map = self._mock_all_clear()
        # Oil spikes 10%
        data_map["CL=F"] = _mock_data("CL=F", prev_close=80.0, open_price=88.0)
        # Gold spikes 5% (above 2% threshold)
        data_map["GC=F"] = _mock_data("GC=F", prev_close=2000.0, open_price=2100.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert result["action"] == "block"
        assert result["size_multiplier"] == 0.0
        assert result["triggered_count"] == 2
        assert "oil" in result["flags"]
        assert "gold" in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_three_flags_still_blocks(self, mock_fetch):
        """3 indicators flagged → still action=block."""
        data_map = self._mock_all_clear()
        data_map["CL=F"] = _mock_data("CL=F", prev_close=80.0, open_price=88.0)
        data_map["GC=F"] = _mock_data("GC=F", prev_close=2000.0, open_price=2100.0)
        data_map["^VIX"] = _mock_data("^VIX", prev_close=15.0, open_price=16.0, close=30.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert result["action"] == "block"
        assert result["triggered_count"] == 3

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_vix_level_flag(self, mock_fetch):
        """VIX level > 25 → flagged."""
        data_map = self._mock_all_clear()
        data_map["^VIX"] = _mock_data("^VIX", prev_close=24.0, open_price=25.5, close=27.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert "vix" in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_vix_spike_flag(self, mock_fetch):
        """VIX spike > 20% → flagged even if level < 25."""
        data_map = self._mock_all_clear()
        data_map["^VIX"] = _mock_data("^VIX", prev_close=16.0, open_price=20.0, close=19.5)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert "vix" in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_custom_thresholds_respected(self, mock_fetch):
        """Custom thresholds from config override defaults."""
        data_map = self._mock_all_clear()
        # Oil gap = 3% — below default 5% but above custom 2%
        data_map["CL=F"] = _mock_data("CL=F", prev_close=100.0, open_price=103.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        # Lower oil threshold to 2% via config
        result = check_volatility_gate(_config(oil_gap_pct=2.0))
        assert "oil" in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_indicator_disabled_via_config(self, mock_fetch):
        """Indicators disabled in config should not be fetched or checked."""
        data_map = self._mock_all_clear()
        # Oil would trigger if checked (10% gap)
        data_map["CL=F"] = _mock_data("CL=F", prev_close=80.0, open_price=88.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        config = _config()
        config["volatility_gate"]["indicators"]["oil"] = False
        result = check_volatility_gate(config)

        # Oil should not appear in flags
        assert "oil" not in result["flags"]

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_result_has_checked_at_timestamp(self, mock_fetch):
        """Result always includes a checked_at timestamp."""
        data_map = self._mock_all_clear()
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert "checked_at" in result
        assert result["checked_at"].endswith("Z")

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_result_has_details_per_indicator(self, mock_fetch):
        """Result includes per-indicator details dict."""
        data_map = self._mock_all_clear()
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert "details" in result
        details = result["details"]
        assert "oil" in details
        assert "gold" in details
        assert "vix" in details
        assert "asx_futures" in details

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_missing_data_does_not_crash(self, mock_fetch):
        """If yfinance returns None for a ticker, gate skips it gracefully."""
        # All tickers return None (yfinance unavailable)
        mock_fetch.return_value = None

        result = check_volatility_gate(_config())
        # Should not raise, should return a valid result
        assert isinstance(result, dict)
        assert result["action"] in ("none", "reduce", "block")
        assert "flags" in result

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_message_present_in_all_states(self, mock_fetch):
        """Result always has a non-empty message."""
        data_map = self._mock_all_clear()
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        for enabled in (True, False):
            result = check_volatility_gate(_config(enabled=enabled))
            assert "message" in result
            assert len(result["message"]) > 0

    @patch("scripts.volatility_gate._fetch_overnight_data")
    def test_asx_futures_gap_flag(self, mock_fetch):
        """ASX futures gap > 1.5% → flagged."""
        data_map = self._mock_all_clear()
        # 2% ASX gap (above 1.5% threshold)
        data_map["^AXJO"] = _mock_data("^AXJO", prev_close=8000.0, open_price=8160.0)
        mock_fetch.side_effect = lambda ticker, **kw: data_map.get(ticker)

        result = check_volatility_gate(_config())
        assert "asx_futures" in result["flags"]
