"""Tests for the Signal dataclass validation logic.

Run with:  python -m pytest tests/test_signal_validation.py -v
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from strategies.base import Signal  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(**overrides) -> Signal:
    """Build a minimal valid Signal, applying any keyword overrides."""
    defaults = dict(
        ticker="AAPL",
        strategy="mean_reversion",
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        take_profit=115.0,
        position_size=10,
        position_value=1000.0,
        risk_amount=50.0,
        confidence=0.75,
        rationale="Unit-test signal",
        features={"rsi": 28.0},
    )
    defaults.update(overrides)
    return Signal(**defaults)


# ---------------------------------------------------------------------------
# Valid signal creation
# ---------------------------------------------------------------------------

class TestValidSignalCreation:
    def test_basic_long_signal(self):
        sig = _make_signal()
        assert sig.ticker == "AAPL"
        assert sig.direction == "long"
        assert sig.confidence == 0.75

    def test_no_take_profit_is_valid(self):
        """take_profit=None is explicitly allowed (trailing stop exit only)."""
        sig = _make_signal(take_profit=None)
        assert sig.take_profit is None

    def test_features_dict_stored(self):
        feats = {"rsi": 27.3, "zscore": -2.8, "atr": 1.5}
        sig = _make_signal(features=feats)
        assert sig.features == feats

    def test_default_features_empty_dict(self):
        sig = Signal(
            ticker="TEST",
            strategy="test_strat",
            direction="long",
            entry_price=50.0,
            stop_price=47.0,
            take_profit=58.0,
            position_size=5,
            position_value=250.0,
            risk_amount=15.0,
            confidence=0.70,
            rationale="Default features test",
        )
        assert sig.features == {}

    def test_confidence_boundary_zero(self):
        """Confidence exactly 0.0 should be valid."""
        sig = _make_signal(confidence=0.0)
        assert sig.confidence == 0.0

    def test_confidence_boundary_one(self):
        """Confidence exactly 1.0 should be valid."""
        sig = _make_signal(confidence=1.0)
        assert sig.confidence == 1.0

    def test_timestamp_defaults_to_now(self):
        before = datetime.now()
        sig = _make_signal()
        after = datetime.now()
        assert before <= sig.timestamp <= after

    def test_market_id_default(self):
        sig = _make_signal()
        assert sig.market_id == ""

    def test_sector_default(self):
        sig = _make_signal()
        assert sig.sector == "Unknown"


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------

class TestSignalValidationErrors:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            _make_signal(direction="sideways")

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="[Cc]onfidence"):
            _make_signal(confidence=-0.01)

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="[Cc]onfidence"):
            _make_signal(confidence=1.01)

    def test_stop_equal_to_entry_raises(self):
        with pytest.raises(ValueError, match="[Ss]top"):
            _make_signal(entry_price=100.0, stop_price=100.0)

    def test_stop_above_entry_raises(self):
        with pytest.raises(ValueError, match="[Ss]top"):
            _make_signal(entry_price=100.0, stop_price=105.0)

    def test_take_profit_below_entry_raises(self):
        with pytest.raises(ValueError, match="[Tt]ake"):
            _make_signal(entry_price=100.0, stop_price=95.0, take_profit=99.0)

    def test_take_profit_equal_to_entry_raises(self):
        with pytest.raises(ValueError, match="[Tt]ake"):
            _make_signal(entry_price=100.0, stop_price=95.0, take_profit=100.0)

    def test_position_size_zero_raises(self):
        with pytest.raises(ValueError, match="[Pp]osition size"):
            _make_signal(position_size=0)

    def test_position_size_negative_raises(self):
        with pytest.raises(ValueError, match="[Pp]osition size"):
            _make_signal(position_size=-5)


# ---------------------------------------------------------------------------
# to_dict()
# ---------------------------------------------------------------------------

class TestSignalToDict:
    def test_to_dict_has_required_keys(self):
        sig = _make_signal()
        d = sig.to_dict()
        expected_keys = {
            "ticker", "strategy", "direction", "entry_price", "stop_price",
            "take_profit", "position_size", "position_value", "risk_amount",
            "confidence", "rationale", "features", "timestamp",
        }
        assert expected_keys.issubset(set(d.keys()))

    def test_to_dict_ticker(self):
        sig = _make_signal(ticker="MSFT")
        assert sig.to_dict()["ticker"] == "MSFT"

    def test_to_dict_features(self):
        feats = {"rsi": 20.1, "vol": 1.3}
        sig = _make_signal(features=feats)
        assert sig.to_dict()["features"] == feats

    def test_to_dict_take_profit_none(self):
        sig = _make_signal(take_profit=None)
        assert sig.to_dict()["take_profit"] is None

    def test_to_dict_timestamp_is_string(self):
        sig = _make_signal()
        ts = sig.to_dict()["timestamp"]
        assert isinstance(ts, str)
        # Should be ISO format parseable
        datetime.fromisoformat(ts)

    def test_to_dict_confidence_value(self):
        sig = _make_signal(confidence=0.82)
        assert sig.to_dict()["confidence"] == 0.82

    def test_repr_contains_ticker(self):
        sig = _make_signal(ticker="NVDA")
        r = repr(sig)
        assert "NVDA" in r
