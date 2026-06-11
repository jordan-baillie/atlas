"""Unit tests for brokers/price_arbiter.py"""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is on path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from atlas.brokers import price_arbiter


@pytest.fixture(autouse=True)
def _pin_rth(monkeypatch):
    """arbitrate() only pages inside US RTH — pin it so tests are time-of-day independent."""
    from atlas.kernel import market_hours
    monkeypatch.setattr(market_hours, "is_rth", lambda *a, **k: True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset(tmp_throttle: Path) -> None:
    """Clear module-level halt set and point throttle at a temp file."""
    price_arbiter.clear_halts()
    price_arbiter._THROTTLE_PATH = tmp_throttle


# ---------------------------------------------------------------------------
# Existing behaviour — spread below warn_pct
# ---------------------------------------------------------------------------

def test_no_halt_below_warn(tmp_path):
    _reset(tmp_path / "throttle.json")
    result = price_arbiter.arbitrate("AAPL", 100.0, 100.5)
    assert result in (100.0, 100.5)
    assert not price_arbiter.is_ticker_halted("AAPL")


# ---------------------------------------------------------------------------
# Existing behaviour — halt triggered
# ---------------------------------------------------------------------------

def test_halt_added_above_halt_pct(tmp_path):
    _reset(tmp_path / "throttle.json")
    with patch.object(price_arbiter, "_send_telegram_bg") as mock_tg:
        price_arbiter.arbitrate("NFLX", 107.79, 97.0)
    assert price_arbiter.is_ticker_halted("NFLX")
    mock_tg.assert_called_once()  # first call should send


# ---------------------------------------------------------------------------
# Throttle: second call within 6 hours should NOT send Telegram
# ---------------------------------------------------------------------------

def test_throttle_suppresses_second_alert(tmp_path):
    throttle_file = tmp_path / "throttle.json"
    _reset(throttle_file)
    with patch.object(price_arbiter, "_send_telegram_bg") as mock_tg:
        price_arbiter.arbitrate("NFLX", 107.79, 97.0)
        price_arbiter.clear_halts()
        price_arbiter.arbitrate("NFLX", 107.79, 97.0)
    # First call sends, second is suppressed
    assert mock_tg.call_count == 1


# ---------------------------------------------------------------------------
# Throttle: after 6 hours elapsed, alert fires again
# ---------------------------------------------------------------------------

def test_throttle_sends_after_expiry(tmp_path):
    from datetime import datetime, timedelta, timezone

    throttle_file = tmp_path / "throttle.json"
    # Pre-seed throttle with a timestamp 7 hours ago
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    throttle_file.write_text(json.dumps({"NFLX": old_ts}))
    _reset(throttle_file)

    with patch.object(price_arbiter, "_send_telegram_bg") as mock_tg:
        price_arbiter.arbitrate("NFLX", 107.79, 97.0)
    mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# Throttle: corrupt throttle file → fail-open (send)
# ---------------------------------------------------------------------------

def test_throttle_corrupt_file_sends(tmp_path):
    throttle_file = tmp_path / "throttle.json"
    throttle_file.write_text("NOT VALID JSON{{{{")
    _reset(throttle_file)
    with patch.object(price_arbiter, "_send_telegram_bg") as mock_tg:
        price_arbiter.arbitrate("NFLX", 107.79, 97.0)
    mock_tg.assert_called_once()


# ---------------------------------------------------------------------------
# Config authority flip: alpaca wins by default
# ---------------------------------------------------------------------------

def test_authority_alpaca(tmp_path):
    _reset(tmp_path / "throttle.json")
    with patch.object(price_arbiter, "_load_config", return_value={"warn_pct": 2.0, "halt_pct": 5.0, "authority_on_mismatch": "alpaca"}):
        result = price_arbiter.arbitrate("AAPL", 107.0, 100.0)
    assert result == 100.0


def test_authority_tiingo(tmp_path):
    _reset(tmp_path / "throttle.json")
    with patch.object(price_arbiter, "_load_config", return_value={"warn_pct": 2.0, "halt_pct": 5.0, "authority_on_mismatch": "tiingo"}):
        with patch.object(price_arbiter, "_send_telegram_bg"):
            result = price_arbiter.arbitrate("AAPL", 107.0, 100.0)
    assert result == 107.0
