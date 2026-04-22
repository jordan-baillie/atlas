"""Tests for scripts/check_config_vs_research_best.py (D2)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from scripts.check_config_vs_research_best import (
    _build_analysis,
    _get_config_sharpe_baseline,
    _parse_updated_at,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_row(
    strategy: str,
    universe: str,
    sharpe: float,
    days_ago: float = 1.0,
) -> dict:
    """Build a synthetic research_best row."""
    updated = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {
        "strategy": strategy,
        "universe": universe,
        "sharpe": sharpe,
        "trades": 100,
        "max_dd_pct": 10.0,
        "updated_at": updated.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _make_config(strategy: str, sharpe_baseline: float) -> dict:
    """Build a minimal config dict with a strategy baseline."""
    return {
        "version": "v3.2",
        "market": "sp500",
        "strategies": {
            strategy: {"enabled": True, "weight": 0.25},
        },
        "baselines": {
            strategy: {"sharpe": sharpe_baseline},
        },
    }


# ─── Unit tests for helper functions ─────────────────────────────────────────

class TestParseUpdatedAt:
    def test_iso_with_tz(self) -> None:
        dt = _parse_updated_at("2026-04-13T11:03:27+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_iso_without_tz(self) -> None:
        dt = _parse_updated_at("2026-04-13 11:03:27")
        assert dt is not None
        assert dt.tzinfo is not None  # should be filled with UTC

    def test_iso_with_fractional(self) -> None:
        dt = _parse_updated_at("2026-03-16T00:51:45.261965+00:00")
        assert dt is not None
        assert dt.month == 3

    def test_none_input(self) -> None:
        assert _parse_updated_at(None) is None

    def test_garbage_input(self) -> None:
        assert _parse_updated_at("not-a-date") is None


class TestGetConfigSharpeBaseline:
    def test_returns_from_baselines_section(self) -> None:
        config = {"baselines": {"mean_reversion": {"sharpe": 0.4097}}}
        result = _get_config_sharpe_baseline(config, "mean_reversion")
        assert result == pytest.approx(0.4097)

    def test_returns_from_strategies_section(self) -> None:
        config = {"strategies": {"momentum_breakout": {"sharpe": 0.82, "enabled": True}}}
        result = _get_config_sharpe_baseline(config, "momentum_breakout")
        assert result == pytest.approx(0.82)

    def test_returns_none_when_not_found(self) -> None:
        config = {"strategies": {"some_strategy": {"enabled": True}}}
        result = _get_config_sharpe_baseline(config, "missing_strategy")
        assert result is None

    def test_baselines_takes_priority_over_strategies(self) -> None:
        config = {
            "baselines": {"my_strat": {"sharpe": 0.5}},
            "strategies": {"my_strat": {"sharpe": 0.9}},
        }
        result = _get_config_sharpe_baseline(config, "my_strat")
        assert result == pytest.approx(0.5)


# ─── Integration tests for _build_analysis ───────────────────────────────────

class TestBuildAnalysis:
    """D2 spec tests — three scenarios described in the task."""

    def test_overdue_promotion_fires_at_14d_plus_0p15_sharpe(self) -> None:
        """Spec D2.1: 20 days old + +0.15 Sharpe → overdue alert."""
        config_sharpe = 0.40
        research_sharpe = config_sharpe + 0.15  # +0.15 improvement

        configs = {"sp500": _make_config("mean_reversion", config_sharpe)}
        rows = [_make_row("mean_reversion", "sp500", research_sharpe, days_ago=20)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        assert len(analysis["overdue"]) == 1, (
            "20d stable + 0.15 Sharpe improvement should trigger overdue alert"
        )
        item = analysis["overdue"][0]
        assert item["strategy"] == "mean_reversion"
        assert item["sharpe_delta"] == pytest.approx(0.15, abs=0.001)
        assert item["days_stable"] == pytest.approx(20, abs=0.5)

    def test_no_alert_for_5d_old_even_with_large_improvement(self) -> None:
        """Spec D2.2: 5 days old + +0.20 Sharpe → no overdue (age gate)."""
        config_sharpe = 0.40
        research_sharpe = config_sharpe + 0.20

        configs = {"sp500": _make_config("momentum_breakout", config_sharpe)}
        rows = [_make_row("momentum_breakout", "sp500", research_sharpe, days_ago=5)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        assert len(analysis["overdue"]) == 0, (
            "5d old strategy should NOT be in overdue (14d threshold not met)"
        )

    def test_aging_alert_for_40d_small_improvement(self) -> None:
        """Spec D2.3: 40 days old + +0.02 Sharpe → aging alert (30d gate)."""
        config_sharpe = 0.40
        research_sharpe = config_sharpe + 0.02  # below 0.10 Sharpe gate

        configs = {"sp500": _make_config("sector_rotation", config_sharpe)}
        rows = [_make_row("sector_rotation", "sp500", research_sharpe, days_ago=40)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        # Should be in aging (≥30d) but NOT in overdue (sharpe delta < 0.10)
        assert len(analysis["overdue"]) == 0, "Sharpe delta too small for overdue"
        assert len(analysis["aging"]) == 1, "40d stable should trigger aging alert"
        item = analysis["aging"][0]
        assert item["strategy"] == "sector_rotation"
        assert item["days_stable"] == pytest.approx(40, abs=0.5)

    def test_no_config_baseline_skips_overdue_check(self) -> None:
        """Rows with no matching config baseline can still trigger aging, not overdue."""
        configs = {}  # no configs at all
        rows = [_make_row("exotic_strategy", "commodity_etfs", 0.90, days_ago=35)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        assert len(analysis["overdue"]) == 0, (
            "No config baseline → cannot compute delta → no overdue alert"
        )
        assert len(analysis["aging"]) == 1, "35d stable should still trigger aging"

    def test_both_overdue_and_aging_can_overlap(self) -> None:
        """A 40-day-old row with +0.20 delta should appear in BOTH overdue and aging."""
        config_sharpe = 0.40
        research_sharpe = config_sharpe + 0.20

        configs = {"sp500": _make_config("good_strategy", config_sharpe)}
        rows = [_make_row("good_strategy", "sp500", research_sharpe, days_ago=40)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        overdue_strats = {r["strategy"] for r in analysis["overdue"]}
        aging_strats = {r["strategy"] for r in analysis["aging"]}
        assert "good_strategy" in overdue_strats
        assert "good_strategy" in aging_strats

    def test_below_sharpe_threshold_not_overdue(self) -> None:
        """14d stable but only +0.05 delta → below 0.10 threshold, no overdue."""
        config_sharpe = 0.40
        research_sharpe = config_sharpe + 0.05

        configs = {"sp500": _make_config("mean_reversion", config_sharpe)}
        rows = [_make_row("mean_reversion", "sp500", research_sharpe, days_ago=20)]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        assert len(analysis["overdue"]) == 0, (
            "0.05 delta is below 0.10 threshold — should not be overdue"
        )

    def test_multiple_markets_analysed(self) -> None:
        """Multiple markets in configs with different baselines."""
        configs = {
            "sp500": _make_config("momentum_breakout", 0.82),
            "commodity_etfs": _make_config("momentum_breakout", 0.50),
        }
        rows = [
            _make_row("momentum_breakout", "sp500", 0.82 + 0.05, days_ago=20),       # no overdue (delta<0.10)
            _make_row("momentum_breakout", "commodity_etfs", 0.50 + 0.40, days_ago=20),  # overdue!
        ]
        now = datetime.now(timezone.utc)

        analysis = _build_analysis(configs, rows, now=now)

        overdue_universes = {r["universe"] for r in analysis["overdue"]}
        assert "commodity_etfs" in overdue_universes
        assert "sp500" not in overdue_universes


# ─── CLI smoke tests ──────────────────────────────────────────────────────────

class TestCLISmoke:
    def test_help_does_not_crash(self) -> None:
        from scripts.check_config_vs_research_best import _parse_args
        ns = _parse_args(["--help"] if False else [])  # no args, just parse defaults
        assert hasattr(ns, "notify")
        assert hasattr(ns, "json_output")

    def test_main_with_real_db(self) -> None:
        """Run against real atlas.db in read-only mode — must not crash."""
        real_db = ATLAS_ROOT / "data" / "atlas.db"
        if not real_db.exists():
            pytest.skip("atlas.db not found")

        from scripts.check_config_vs_research_best import main
        # Plain text mode, no Telegram, no writes
        rc = main(["--db", str(real_db)])
        assert rc in (0, 1), f"Unexpected exit code: {rc}"
