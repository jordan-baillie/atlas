"""Tests for the SKIP logic in scripts/health_check.py.

Covers the 4 scenarios in the A3.3 acceptance criteria:
  1. Passive config (live_enabled=false) → status=SKIPPED, exit 0
  2. Active config + sufficient data → status in [HEALTHY, DEGRADED], not SKIPPED
  3. Active config + NO data → status=ERROR, exit 1
  4. Config without live_enabled field + known-passive universe → SKIPPED

All tests mock load_data_recent and BacktestEngine to avoid hitting the real cache.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from scripts.health_check import _is_inactive, main


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_config(tmp_path: Path, market: str, live_enabled=None, include_live_enabled: bool = True) -> Path:
    """Write a minimal config JSON and return its path."""
    trading: dict = {"mode": "live" if live_enabled else "passive"}
    if include_live_enabled:
        trading["live_enabled"] = live_enabled
    cfg = {
        "version": "test-v1.0",
        "market": market,
        "trading": trading,
        "strategies": {},
    }
    p = tmp_path / f"{market}_test.json"
    p.write_text(json.dumps(cfg))
    return p


def _mock_backtest_result(cagr: float = 12.0, sharpe: float = 0.85,
                          pf: float = 1.5, maxdd: float = 5.0,
                          trades: int = 100) -> MagicMock:
    """Return a mock BacktestEngine result with healthy metrics."""
    result = MagicMock()
    result.metrics = {
        "cagr": cagr,
        "sharpe": sharpe,
        "profit_factor": pf,
        "max_drawdown": maxdd,
        "total_trades": trades,
    }
    return result


def _dummy_data(n: int = 15) -> dict:
    """Return a dict of n minimal DataFrames — enough to pass the < 10 gate."""
    idx = pd.date_range("2025-01-01", periods=100, freq="D")
    df = pd.DataFrame({
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1_000_000,
    }, index=idx)
    return {f"TICK{i}": df for i in range(n)}


# ── Test 1 ─────────────────────────────────────────────────────────────────────

def test_passive_universe_writes_skipped_status(tmp_path):
    """live_enabled=false in config → status='SKIPPED', exit 0."""
    cfg_path = _make_config(tmp_path, market="asx", live_enabled=False)
    report_path = tmp_path / "report.json"

    with pytest.raises(SystemExit) as exc_info:
        main([
            "--config-path", str(cfg_path),
            "--report-path", str(report_path),
        ])

    assert exc_info.value.code == 0
    assert report_path.exists(), "SKIP report must be written"
    report = json.loads(report_path.read_text())
    assert report["status"] == "SKIPPED"
    assert "inactive" in report["message"].lower()
    assert report["config_path"] == str(cfg_path)


# ── Test 2 ─────────────────────────────────────────────────────────────────────

def test_active_universe_with_data_runs_full_check(tmp_path):
    """live_enabled=true + sufficient data → full backtest runs, status not SKIPPED."""
    cfg_path = _make_config(tmp_path, market="sp500", live_enabled=True)
    report_path = tmp_path / "report.json"

    mock_result = _mock_backtest_result()

    with patch("scripts.health_check.load_data_recent", return_value=_dummy_data(15)), \
         patch("scripts.health_check.BacktestEngine") as mock_engine_cls, \
         patch("scripts.health_check.build_strategies", return_value=[]):
        mock_engine_cls.return_value.run_walkforward.return_value = mock_result
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--config-path", str(cfg_path),
                "--report-path", str(report_path),
            ])

    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["status"] in ("HEALTHY", "DEGRADED"), (
        f"Expected HEALTHY or DEGRADED, got {report['status']!r}"
    )
    assert report["status"] != "SKIPPED"


# ── Test 3 ─────────────────────────────────────────────────────────────────────

def test_active_universe_no_data_still_errors(tmp_path):
    """live_enabled=true but zero tickers → status=ERROR, exit 1 (active universe broken data)."""
    cfg_path = _make_config(tmp_path, market="sp500", live_enabled=True)
    report_path = tmp_path / "report.json"

    with patch("scripts.health_check.load_data_recent", return_value={}):
        with pytest.raises(SystemExit) as exc_info:
            main([
                "--config-path", str(cfg_path),
                "--report-path", str(report_path),
            ])

    assert exc_info.value.code == 1
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["status"] == "ERROR"
    assert "Insufficient" in report["message"]


# ── Test 4 ─────────────────────────────────────────────────────────────────────

def test_missing_live_enabled_field_treated_as_inactive_for_known_passive(tmp_path):
    """Config WITHOUT live_enabled field + universe in PASSIVE_UNIVERSES → SKIPPED.

    Covers the fallback path for configs that pre-date the live_enabled field.
    """
    cfg_path = _make_config(tmp_path, market="asx", include_live_enabled=False)
    report_path = tmp_path / "report.json"

    with pytest.raises(SystemExit) as exc_info:
        main([
            "--config-path", str(cfg_path),
            "--report-path", str(report_path),
        ])

    assert exc_info.value.code == 0
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert report["status"] == "SKIPPED"


# ── Unit tests for _is_inactive helper ────────────────────────────────────────

class TestIsInactive:
    """Direct unit tests for the _is_inactive() helper."""

    def test_live_enabled_false_is_inactive(self):
        assert _is_inactive({"trading": {"live_enabled": False}}) is True

    def test_live_enabled_true_is_active(self):
        assert _is_inactive({"trading": {"live_enabled": True}}) is False

    def test_live_enabled_missing_asx_is_inactive(self):
        assert _is_inactive({"market": "asx", "trading": {}}) is True

    def test_live_enabled_missing_crypto_is_inactive(self):
        assert _is_inactive({"market": "crypto", "trading": {}}) is True

    def test_live_enabled_missing_sp500_is_active(self):
        assert _is_inactive({"market": "sp500", "trading": {}}) is False

    def test_live_enabled_missing_sector_etfs_is_active(self):
        # sector_etfs has live_enabled=True in prod; without field defaults to active
        assert _is_inactive({"market": "sector_etfs", "trading": {}}) is False
