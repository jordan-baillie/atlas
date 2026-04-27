"""Regression tests for scripts/check_fred_health.py — spec-named test IDs.

These 7 tests cover the exact scenarios listed in the A1.3 acceptance criteria.
Complements the broader test_check_fred_health.py suite.

All tests are fully mocked — no live API calls, no DB writes.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from scripts.check_fred_health import _check_key, _check_series, main, run_checks


# ── Helpers ────────────────────────────────────────────────────────────────────

def _series_with_lag(lag_days: int, n: int = 10) -> pd.Series:
    """Return a Series whose latest index is ``lag_days`` before today (UTC)."""
    today = datetime.now(tz=timezone.utc).date()
    latest = today - timedelta(days=lag_days)
    dates = pd.date_range(end=str(latest), periods=n, freq="D")
    return pd.Series([1.0] * n, index=dates)


def _fresh_client_mock() -> MagicMock:
    """Return a mock FREDClient that returns fresh series for all 3 FRED methods."""
    client = MagicMock()
    client.api_key = "test_key_123"
    client.get_yield_curve_slope.return_value = _series_with_lag(1)
    client.get_credit_oas.return_value = _series_with_lag(1)
    # FEDFUNDS: monthly, 60d threshold — 30d lag is safely within threshold
    client.get_fed_funds_rate.return_value = _series_with_lag(30)
    return client


# ── 1 ─────────────────────────────────────────────────────────────────────────

def test_check_key_missing_returns_failure_cleanly(tmp_path):
    """_check_key returns False cleanly (no exception) when api_key is None.

    run_checks() must also return (False, [...]) with results[0]['ok'] == False
    and reason containing 'missing'.
    """
    mock_client = MagicMock()
    mock_client.api_key = None

    with patch("scripts.check_fred_health.FREDClient", return_value=mock_client):
        all_ok, results = run_checks(tmp_path)

    assert all_ok is False
    assert len(results) >= 1
    assert results[0]["ok"] is False
    assert "missing" in results[0]["reason"].lower()


# ── 2 ─────────────────────────────────────────────────────────────────────────

def test_check_key_present_returns_true(tmp_path):
    """run_checks returns all_ok=True when key='test_key_123' and all series fresh."""
    mock_client = _fresh_client_mock()

    with patch("scripts.check_fred_health.FREDClient", return_value=mock_client):
        all_ok, results = run_checks(tmp_path)

    assert all_ok is True
    assert results[0]["name"] == "API Key"
    assert results[0]["ok"] is True
    assert results[0]["reason"] == "present"


# ── 3 ─────────────────────────────────────────────────────────────────────────

def test_check_series_handles_exception_gracefully():
    """_check_series catches RuntimeError and returns ok=False with 'exception' in reason."""
    mock_client = MagicMock()
    mock_client.get_yield_curve_slope.side_effect = RuntimeError("simulated")
    mock_client.api_key = "key"

    with patch("scripts.check_fred_health.FREDClient", return_value=mock_client):
        result = _check_series(
            "get_yield_curve_slope",
            "Yield Curve (T10Y2Y)",
            max_lag_days=5,
            logger=MagicMock(),
        )

    assert result["ok"] is False
    assert "exception" in result["reason"].lower()
    assert "simulated" in result["reason"]


# ── 4 ─────────────────────────────────────────────────────────────────────────

def test_check_series_handles_empty_series():
    """_check_series returns ok=False with reason='empty series returned' for pd.Series()."""
    mock_client = MagicMock()
    mock_client.get_yield_curve_slope.return_value = pd.Series(dtype=float)
    mock_client.api_key = "key"

    with patch("scripts.check_fred_health.FREDClient", return_value=mock_client):
        result = _check_series(
            "get_yield_curve_slope",
            "Yield Curve (T10Y2Y)",
            max_lag_days=5,
            logger=MagicMock(),
        )

    assert result["ok"] is False
    assert result["reason"] == "empty series returned"


# ── 5 ─────────────────────────────────────────────────────────────────────────

def test_check_series_flags_stale_data():
    """Series whose latest date is 30d ago fails a 5-day max_lag check with 'stale'."""
    mock_client = MagicMock()
    # 30 days old — well beyond the 5-day max_lag
    mock_client.get_yield_curve_slope.return_value = _series_with_lag(lag_days=30)
    mock_client.api_key = "key"

    with patch("scripts.check_fred_health.FREDClient", return_value=mock_client):
        result = _check_series(
            "get_yield_curve_slope",
            "Yield Curve (T10Y2Y)",
            max_lag_days=5,
            logger=MagicMock(),
        )

    assert result["ok"] is False
    assert "stale" in result["reason"].lower()


# ── 6 ─────────────────────────────────────────────────────────────────────────

def test_main_exits_1_on_failure(tmp_path):
    """main([]) returns 1 when run_checks returns (False, [...])."""
    failing_results = [
        {"ok": False, "name": "API Key", "latest_date": None, "n_obs": None,
         "reason": "missing from ~/.atlas-secrets.json"},
    ]
    with patch("scripts.check_fred_health.run_checks", return_value=(False, failing_results)), \
         patch("scripts.check_fred_health._send_telegram"):
        rc = main(["--log-dir", str(tmp_path)])

    assert rc == 1


# ── 7 ─────────────────────────────────────────────────────────────────────────

def test_main_exits_0_on_success(tmp_path):
    """main([]) returns 0 when run_checks returns (True, [...])."""
    passing_results = [
        {"ok": True, "name": "API Key",               "latest_date": None,         "n_obs": None, "reason": "present"},
        {"ok": True, "name": "Yield Curve (T10Y2Y)",  "latest_date": "2026-04-24", "n_obs": 10,   "reason": "OK (3d lag, 10 obs)"},
        {"ok": True, "name": "Credit OAS (BAMLC0A0CM)","latest_date": "2026-04-23","n_obs": 10,   "reason": "OK (4d lag, 10 obs)"},
        {"ok": True, "name": "Fed Funds (FEDFUNDS)",  "latest_date": "2026-03-01", "n_obs": 10,   "reason": "OK (57d lag, 10 obs)"},
    ]
    with patch("scripts.check_fred_health.run_checks", return_value=(True, passing_results)):
        rc = main(["--log-dir", str(tmp_path)])

    assert rc == 0
