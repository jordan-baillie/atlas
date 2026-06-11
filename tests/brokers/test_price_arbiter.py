"""Tests for price_arbiter RTH gating + throttle behaviour."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from atlas.brokers import price_arbiter


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Reset halted set + redirect throttle file to a tmp path before each test."""
    price_arbiter.clear_halts()
    monkeypatch.setattr(price_arbiter, "_THROTTLE_PATH", tmp_path / "throttle.json")
    yield
    price_arbiter.clear_halts()


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# 03:00 ET weekday = 08:00 UTC (during EST) or 07:00 UTC (during EDT)
# Use Jan (EST): 03:00 ET Tue Jan 14 2025 = 08:00 UTC
_PRE_MARKET_UTC = _utc(2025, 1, 14, 8, 0)        # 03:00 ET Tue (pre-market)
_RTH_UTC        = _utc(2025, 1, 14, 15, 0)       # 10:00 ET Tue (RTH)
_SUNDAY_UTC     = _utc(2025, 1, 12, 15, 0)       # 10:00 ET Sun (closed)


def test_outside_rth_no_telegram_logs_warning(caplog):
    """Pre-market divergence: no Telegram, WARNING logged, ticker still halted."""
    sent = []
    with patch("atlas.brokers.price_arbiter._send_telegram_bg", side_effect=lambda m: sent.append(m)), \
         patch("atlas.kernel.market_hours.is_rth", return_value=False):
        with caplog.at_level("WARNING"):
            price = price_arbiter.arbitrate("TEST", tiingo_price=100.0, alpaca_price=110.0)
    assert price == 100.0  # Tiingo authority (Wave B #265)
    assert price_arbiter.is_ticker_halted("TEST")
    assert sent == []
    assert any("outside RTH" in rec.message and "TEST" in rec.message for rec in caplog.records)


def test_inside_rth_telegram_fires_then_throttled():
    """During RTH: first call sends Telegram; second within 6h throttled."""
    sent = []
    with patch("atlas.brokers.price_arbiter._send_telegram_bg", side_effect=lambda m: sent.append(m)), \
         patch("atlas.kernel.market_hours.is_rth", return_value=True):
        # First call
        price_arbiter.arbitrate("TEST", tiingo_price=100.0, alpaca_price=110.0)
        # Second call same throttle window
        price_arbiter.arbitrate("TEST", tiingo_price=100.0, alpaca_price=110.0)
    assert len(sent) == 1, f"expected 1 telegram call, got {len(sent)}: {sent}"


def test_sunday_no_alert_regardless():
    """Weekend: divergence never alerts, regardless of size."""
    sent = []
    with patch("atlas.brokers.price_arbiter._send_telegram_bg", side_effect=lambda m: sent.append(m)), \
         patch("atlas.kernel.market_hours.is_rth", return_value=False):
        price_arbiter.arbitrate("TEST", tiingo_price=100.0, alpaca_price=110.0)
    assert sent == []
    assert price_arbiter.is_ticker_halted("TEST")  # still flagged for safety


def test_warn_band_does_not_alert():
    """spread between warn_pct and halt_pct: no halt, no Telegram."""
    sent = []
    with patch("atlas.brokers.price_arbiter._send_telegram_bg", side_effect=lambda m: sent.append(m)):
        # 3% spread is above warn_pct (2%) but below halt_pct (5%)
        price = price_arbiter.arbitrate("TEST", tiingo_price=100.0, alpaca_price=103.0)
    assert price == 100.0  # Tiingo authority
    assert not price_arbiter.is_ticker_halted("TEST")
    assert sent == []

def test_default_authority_is_tiingo():
    """Lock-in test: post-Wave-B (#265, commit a445662b), default authority_on_mismatch is 'tiingo'."""
    cfg = price_arbiter._load_config()
    assert cfg["authority_on_mismatch"] == "tiingo", (
        f"Default authority should be 'tiingo' per Wave B #265 (commit a445662b); "
        f"got {cfg['authority_on_mismatch']!r}"
    )
