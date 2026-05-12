"""Tests for dynamic silent-failure threshold in autoresearch_nightly.

Covers _resolve_min_rows() — the function that computes thresholds based on the
number of enabled strategies in a universe's active config and the hand-calibrated
operator floors in MIN_ROWS_PER_UNIVERSE.

Semantics (max, not min — corrected 2026-05-12):
  threshold = max(operator_floor, enabled_strategies * MIN_ROWS_PER_STRATEGY)

Errors resolved by initial fix (2026-05-06): db.errors table ids 19, 20, 21, 27, 28, 29
  (all "Research sweep silent failure: universe=gold_etfs/commodity_etfs rows=0
   threshold=10/20" false-positive alerts).
Follow-up fix (2026-05-12, commit eb647724 follow-up): min() → max() so operator
  floors for large universes (sp500=50) are never silently weakened by the dynamic
  floor (2*3=6).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

# Ensure atlas root is on sys.path
ATLAS_ROOT = Path(__file__).resolve().parents[1]
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

import research.autoresearch_nightly as autoresearch_nightly
from research.autoresearch_nightly import (
    DEFAULT_MIN_ROWS,
    MIN_ROWS_PER_STRATEGY,
    MIN_ROWS_PER_UNIVERSE,
    _resolve_min_rows,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _write_config(tmp_path: Path, universe: str, strategies: dict) -> Path:
    """Write a minimal active-config JSON for *universe* under *tmp_path*."""
    cfg_dir = tmp_path / "config" / "active"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"{universe}.json"
    cfg_path.write_text(json.dumps({"strategies": strategies}))
    return cfg_path


# ─── Tests using PRODUCTION configs (assert live behaviour) ──────────────────


class TestResolveMinRowsLiveConfigs:
    """Tests 1-3: use the real config/active/*.json files to assert live behaviour."""

    def test_resolve_min_rows_gold_etfs_1_strategy_returns_3(self):
        """gold_etfs has 1 enabled strategy (connors_rsi2).

        Expected: max(operator_floor=3, dynamic=max(3, 1*3)=3) = 3
        operator_floor was lowered 10→3 (2026-05-12) to match typical 1-8 row output.
        """
        result = _resolve_min_rows("gold_etfs")
        assert result == 3, (
            f"gold_etfs with 1 enabled strategy should give threshold=3, got {result}"
        )

    def test_resolve_min_rows_commodity_etfs_3_strategies_returns_9(self):
        """commodity_etfs has 3 enabled strategies.

        Expected: max(operator_floor=5, dynamic=max(3, 3*3)=9) = 9
        Dynamic wins here (9 > 5). operator_floor was lowered 20→5 (2026-05-12)
        to match actual recent production output (6-30 rows).
        """
        result = _resolve_min_rows("commodity_etfs")
        assert result == 9, (
            f"commodity_etfs with 3 enabled strategies should give threshold=9, got {result}"
        )

    def test_resolve_min_rows_sp500_2_strategies_returns_50(self):
        """sp500 has 2 enabled strategies (momentum_breakout + connors_rsi2).

        Expected: max(operator_floor=50, dynamic=max(3, 2*3)=6) = 50
        Operator floor dominates — preserves alert sensitivity for a universe
        that typically produces 100-330 rows per sweep.
        """
        result = _resolve_min_rows("sp500")
        assert result == 50, (
            f"sp500 with 2 enabled strategies should give threshold=50 "
            f"(operator floor dominates), got {result}"
        )


# ─── Tests using tmp_path (isolated from production configs) ─────────────────


class TestResolveMinRowsIsolated:
    """Tests 4-7: monkeypatch ATLAS_ROOT to a tmp dir for full isolation."""

    def test_resolve_min_rows_unknown_universe_returns_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Universe not in MIN_ROWS_PER_UNIVERSE + no config file -> DEFAULT_MIN_ROWS."""
        monkeypatch.setattr(autoresearch_nightly, "ATLAS_ROOT", tmp_path)
        # No config file written for "phantom_universe"
        result = _resolve_min_rows("phantom_universe")
        assert result == DEFAULT_MIN_ROWS, (
            f"Unknown universe with no config should return DEFAULT_MIN_ROWS={DEFAULT_MIN_ROWS}, "
            f"got {result}"
        )

    def test_resolve_min_rows_zero_enabled_returns_floor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Universe with 0 enabled strategies -> static operator floor (not 0 or 3).

        We still want to alert if rows ARE somehow produced for a universe where
        no strategies are enabled -- so the operator floor is preserved.
        """
        monkeypatch.setattr(autoresearch_nightly, "ATLAS_ROOT", tmp_path)
        _write_config(
            tmp_path,
            "gold_etfs",
            {
                "connors_rsi2": {"enabled": False},
                "momentum_breakout": {"enabled": False},
            },
        )
        result = _resolve_min_rows("gold_etfs")
        expected_floor = MIN_ROWS_PER_UNIVERSE["gold_etfs"]
        assert result == expected_floor, (
            f"0 enabled strategies should fall back to static operator floor "
            f"({expected_floor}), got {result}"
        )

    def test_resolve_min_rows_corrupt_config_falls_back(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """Corrupt JSON in config file -> WARNING log + fallback to static floor."""
        monkeypatch.setattr(autoresearch_nightly, "ATLAS_ROOT", tmp_path)
        cfg_dir = tmp_path / "config" / "active"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "gold_etfs.json").write_text("THIS IS NOT VALID JSON {{{{")

        with caplog.at_level(logging.WARNING, logger="research.autoresearch_nightly"):
            result = _resolve_min_rows("gold_etfs")

        expected_floor = MIN_ROWS_PER_UNIVERSE["gold_etfs"]
        assert result == expected_floor, (
            f"Corrupt config should fall back to static operator floor ({expected_floor}), got {result}"
        )
        assert any(
            "_resolve_min_rows" in record.message and "falling back" in record.message
            for record in caplog.records
        ), f"Expected a WARNING with '_resolve_min_rows' and 'falling back', got: {caplog.records}"

    def test_resolve_min_rows_max_semantics_high_enabled_count(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Dynamic floor wins when enabled count is very high.

        Synthesize a universe with 100 enabled strategies and operator_floor=10.
        max(10, 100*3) = max(10, 300) = 300 — dynamic wins, proving neither
        floor can weaken the other.
        """
        monkeypatch.setattr(autoresearch_nightly, "ATLAS_ROOT", tmp_path)
        # Use treasury_etfs (operator_floor=10) but write 100 enabled strategies
        strategies = {f"strat_{i:03d}": {"enabled": True} for i in range(100)}
        _write_config(tmp_path, "treasury_etfs", strategies)

        result = _resolve_min_rows("treasury_etfs")
        expected = 100 * MIN_ROWS_PER_STRATEGY  # 300
        assert result == expected, (
            f"100 enabled strategies with operator_floor=10 should return "
            f"300 (dynamic wins), got {result}"
        )


# ─── Regression test: sp500 operator floor must not be weakened ──────────────


def test_sp500_operator_floor_not_weakened_by_dynamic():
    """Regression test: sp500's operator floor must not be silently weakened.

    Bug fixed 2026-05-12: min(50, 6) returned 6, masking 90% drops.
    Now max(50, 6) returns 50, preserving sp500 alert sensitivity.
    """
    assert MIN_ROWS_PER_UNIVERSE["sp500"] == 50
    assert _resolve_min_rows("sp500") == 50


# --- Module-level constant sanity --------------------------------------------


class TestConstants:
    """Verify the constants are correctly defined."""

    def test_min_rows_per_strategy_is_3(self):
        assert MIN_ROWS_PER_STRATEGY == 3

    def test_default_min_rows_is_10(self):
        assert DEFAULT_MIN_ROWS == 10

    def test_sp500_operator_floor_dominates_over_dynamic(self):
        """For sp500, operator_floor (50) > dynamic (2*3=6), so max returns 50.

        This is the core business invariant for well-calibrated wide universes:
        the operator floor must dominate the dynamic floor so that a drop from
        100+ rows to 30 still triggers an alert even with only 2 enabled strategies.
        """
        operator_floor = MIN_ROWS_PER_UNIVERSE["sp500"]  # 50
        dynamic = max(3, 2 * MIN_ROWS_PER_STRATEGY)      # max(3, 6) = 6
        assert max(operator_floor, dynamic) == operator_floor, (
            "sp500 operator floor (50) must dominate dynamic floor (6)"
        )
        assert max(operator_floor, dynamic) > dynamic, (
            "max() must return the operator floor, not the dynamic floor, for sp500"
        )
