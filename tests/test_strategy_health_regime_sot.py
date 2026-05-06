"""Tests for monitor.strategy_health._load_backtest_metrics — regime SOT consolidation.

Items 2 + 3 (audit 2026-05-06):
- research_best SQLite is the canonical SOT for backtest metrics
- _load_backtest_metrics reads regime-conditioned row when regime is known
- Falls back: regime row → cross-regime row → legacy JSON file

Run with:
    python -m pytest tests/test_strategy_health_regime_sot.py -v --timeout=30
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db.atlas_db as _adb
from monitor.strategy_health import (
    DEGRADED,
    HEALTHY,
    INSUFFICIENT_DATA,
    WARNING,
    StrategyHealthMonitor,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path):
    """Point atlas_db at a temp database so tests don't touch production."""
    db_path = str(tmp_path / "test_regime_sot.db")
    _adb._db_path_override = db_path
    _adb.init_db()
    yield
    _adb._db_path_override = None


def _make_monitor(market_id: str = "sp500") -> StrategyHealthMonitor:
    """Build a minimal StrategyHealthMonitor for testing."""
    config = {
        "risk": {
            "starting_equity": 10000,
            "max_open_positions": 10,
            "max_risk_per_trade_pct": 1.0,
        },
        "fees": {"commission_per_trade": 0},
        "strategies": {"mean_reversion": {"enabled": True}},
    }
    return StrategyHealthMonitor(config, market_id)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestLoadBacktestMetricsRegimeSOT:
    """Tests for the new regime-aware _load_backtest_metrics implementation."""

    def test_cross_regime_fallback_when_no_regime_data(self, tmp_path):
        """When _safe_current_regime returns None, load_best called with regime_state=None."""
        mock_metrics = {"sharpe": 0.85, "total_trades": 200}

        with patch("monitor.strategy_health._safe_current_regime", return_value=None), \
             patch("research.loop.load_best",
                   return_value={"metrics": mock_metrics}) as mock_lb:

            monitor = _make_monitor("sp500")
            result = monitor._load_backtest_metrics("mean_reversion")

        assert result == mock_metrics
        mock_lb.assert_called_once_with("mean_reversion", "sp500", regime_state=None)

    def test_regime_conditioned_read_when_regime_known(self, tmp_path):
        """When regime is known, regime-specific row takes priority over cross-regime."""
        regime_metrics = {"sharpe": 1.10, "total_trades": 150}
        cross_regime_metrics = {"sharpe": 0.85, "total_trades": 200}

        def mock_load_best(strategy, universe, regime_state=None):
            if regime_state == "bull_risk_on":
                return {"metrics": regime_metrics}
            return {"metrics": cross_regime_metrics}

        with patch("monitor.strategy_health._safe_current_regime",
                   return_value="bull_risk_on"), \
             patch("research.loop.load_best", side_effect=mock_load_best):

            monitor = _make_monitor("sp500")
            result = monitor._load_backtest_metrics("mean_reversion")

        assert result == regime_metrics
        assert result["sharpe"] == pytest.approx(1.10)

    def test_fallback_to_json_when_sqlite_empty(self, tmp_path):
        """Falls back to legacy JSON file when load_best returns None for both paths."""
        # Write a fallback JSON file at the expected path within tmp_path
        best_dir = tmp_path / "research" / "best"
        best_dir.mkdir(parents=True, exist_ok=True)
        json_metrics = {"sharpe": 0.75, "win_rate_pct": 60.0, "total_trades": 100}
        (best_dir / "test_strategy.json").write_text(json.dumps({
            "strategy": "test_strategy",
            "metrics": json_metrics,
        }))

        with patch("monitor.strategy_health._safe_current_regime", return_value=None), \
             patch("research.loop.load_best", return_value=None), \
             patch("monitor.strategy_health.PROJECT", tmp_path):

            monitor = _make_monitor("sp500")
            result = monitor._load_backtest_metrics("test_strategy")

        assert result is not None
        assert result["sharpe"] == pytest.approx(0.75)
        assert result["win_rate_pct"] == pytest.approx(60.0)

    def test_regime_engine_errors_gracefully(self, tmp_path):
        """When get_current_regime_state raises, _load_backtest_metrics does not crash.

        _safe_current_regime has its own try/except wrapper, so it returns None
        and load_best is still called with regime_state=None.
        """
        mock_metrics = {"sharpe": 0.90, "total_trades": 120}

        # Patch the underlying DB function to raise; _safe_current_regime catches it
        with patch("db.atlas_db.get_current_regime_state",
                   side_effect=RuntimeError("DB error")), \
             patch("research.loop.load_best",
                   return_value={"metrics": mock_metrics}) as mock_lb:

            monitor = _make_monitor("sp500")
            # Should NOT raise — graceful degradation
            result = monitor._load_backtest_metrics("mean_reversion")

        assert result == mock_metrics
        # load_best was called with regime_state=None (because error caused fallback)
        mock_lb.assert_called_once_with("mean_reversion", "sp500", regime_state=None)

    def test_full_consumer_wiring_uses_regime_sharpe(self, tmp_path):
        """compare_to_backtest uses regime-conditioned sharpe from research_best.

        Inserts a research_best row with sharpe=1.20 for bull_risk_on regime.
        Mocks current regime as bull_risk_on.
        Verifies compare_to_backtest picks up sharpe=1.20 as backtest_sharpe.
        """
        # Insert a research_best row directly into the temp DB
        with _adb.get_db() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO research_best
                    (strategy, universe, regime_state, params, sharpe, trades,
                     max_dd_pct, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "test_strategy", "sp500", "bull_risk_on",
                    "{}", 1.20, 50, 5.0, "2026-05-06T00:00:00",
                ),
            )

        # Mock current regime as bull_risk_on
        with patch("monitor.strategy_health._safe_current_regime",
                   return_value="bull_risk_on"):
            monitor = _make_monitor("sp500")
            assessment = monitor.compare_to_backtest("test_strategy")

        # With no live trades, status = INSUFFICIENT_DATA
        # but backtest_sharpe must be sourced from research_best (regime row)
        assert assessment.backtest_sharpe == pytest.approx(1.20), (
            f"Expected backtest_sharpe=1.20 from research_best, got {assessment.backtest_sharpe}"
        )
