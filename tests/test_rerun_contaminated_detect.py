"""Tests for detect_contaminated_pairs() multi-criteria detection (#350).

Verifies that the dynamic detection correctly identifies:
1. is_solo == false  (criterion 1)
2. is_solo == true AND solo_sharpe_clean missing  (criterion 2 — orphan)
3. Neither field present  (criterion 3 — legacy)

And that main() falls back to KNOWN_CONTAMINATED when detection yields empty.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_best(tmp: Path, name: str, **kwargs) -> Path:
    """Write a mock research/best JSON file."""
    d: dict = {"strategy": name, "market": "sp500"}
    d.update(kwargs)
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(d))
    return p


# ---------------------------------------------------------------------------
# Import target with BEST_DIR patched to avoid touching prod data
# ---------------------------------------------------------------------------

from scripts.rerun_contaminated_backtests import (
    KNOWN_CONTAMINATED,
    detect_contaminated_pairs,
)


# ---------------------------------------------------------------------------
# Tests for detect_contaminated_pairs()
# ---------------------------------------------------------------------------

class TestDetectCriterion1IsNotSolo:
    """Criterion 1: is_solo == false → contaminated."""

    def test_is_solo_false_detected(self, tmp_path: Path) -> None:
        _write_best(tmp_path, "not_solo_strat", is_solo=False)
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0][0] == "not_solo_strat"

    def test_is_solo_true_with_clean_sharpe_not_detected(self, tmp_path: Path) -> None:
        """Clean entry: is_solo=True AND solo_sharpe_clean present → NOT contaminated."""
        _write_best(tmp_path, "clean_strat", is_solo=True, solo_sharpe_clean=0.55)
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert pairs == []


class TestDetectCriterion2OrphanMissingCleanSharpe:
    """Criterion 2: is_solo == true but solo_sharpe_clean is absent/None → orphan."""

    def test_is_solo_true_no_solo_sharpe_clean_key_detected(self, tmp_path: Path) -> None:
        """Key missing entirely (not just None)."""
        _write_best(tmp_path, "orphan_strat", is_solo=True)
        # Note: solo_sharpe_clean key is intentionally NOT written
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0][0] == "orphan_strat"

    def test_is_solo_true_solo_sharpe_clean_none_detected(self, tmp_path: Path) -> None:
        """Key present but value is None."""
        _write_best(tmp_path, "orphan_none", is_solo=True, solo_sharpe_clean=None)
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0][0] == "orphan_none"


class TestDetectCriterion3LegacyNoFields:
    """Criterion 3: neither is_solo nor solo_sharpe_clean present → legacy."""

    def test_neither_field_detected(self, tmp_path: Path) -> None:
        _write_best(tmp_path, "legacy_strat")  # no is_solo, no solo_sharpe_clean
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0][0] == "legacy_strat"

    def test_only_sharpe_present_not_legacy(self, tmp_path: Path) -> None:
        """Has solo_sharpe_clean but no is_solo — not legacy (clean Sharpe exists)."""
        _write_best(tmp_path, "partial_strat", solo_sharpe_clean=0.4)
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        # solo_sharpe_clean present but is_solo absent — not caught by criteria 1 or 2;
        # criterion 3 requires BOTH fields absent.  Should NOT be detected.
        assert pairs == []


class TestThreeMockFilesExactlyTwoContaminated:
    """Canonical 3-file test: clean + orphan + not-solo → exactly 2 contaminated."""

    def test_exactly_two_from_three_mock_files(self, tmp_path: Path) -> None:
        _write_best(tmp_path, "clean_strat",   is_solo=True, solo_sharpe_clean=0.55)
        _write_best(tmp_path, "orphan_strat",  is_solo=True)             # no solo_sharpe_clean
        _write_best(tmp_path, "not_solo_strat", is_solo=False)

        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        names = {p[0] for p in pairs}
        assert len(pairs) == 2, f"Expected 2 contaminated, got {len(pairs)}: {names}"
        assert "orphan_strat"  in names
        assert "not_solo_strat" in names
        assert "clean_strat"   not in names


class TestEmptyDirReturnsEmptyList:
    """Negative test: empty dir → [] → triggers KNOWN_CONTAMINATED fallback in main()."""

    def test_empty_dir_yields_empty(self, tmp_path: Path) -> None:
        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert pairs == []

    def test_fallback_warning_logged_in_main_when_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When detect returns [], main() --dry-run logs the KNOWN_CONTAMINATED fallback warning.

        Uses --dry-run (not --detect-only) so execution reaches the work-building section
        where the fallback WARNING is emitted.
        """
        import scripts.rerun_contaminated_backtests as rct

        # Patch BEST_DIR so detect_contaminated_pairs sees an empty dir
        with patch.object(rct, "BEST_DIR", tmp_path):
            with caplog.at_level(logging.WARNING, logger="rerun_contaminated"):
                ret = rct.main(["--dry-run"])

        assert ret == 0
        # With empty dir, detection returns [] → fallback warning must be logged
        assert "falling back to KNOWN_CONTAMINATED" in caplog.text


class TestPositiveConcreteConnorsRsi2Detection:
    """Positive test: connors_rsi2/{commodity_etfs,gold_etfs} pre-rerun state.

    Before the #327 rerun, those files had is_solo=True but no solo_sharpe_clean.
    The new detection (criterion 2) would have caught them automatically.
    """

    def test_connors_rsi2_commodity_etfs_prerun_state_detected(
        self, tmp_path: Path
    ) -> None:
        """Pre-rerun state: is_solo=True, no solo_sharpe_clean → detected."""
        d = {
            "strategy": "connors_rsi2",
            "market": "commodity_etfs",
            "is_solo": True,
            "solo_fraction": 1.0,
            # solo_sharpe_clean intentionally absent (pre-rerun orphan)
            "contamination_note": "portfolio-contaminated; metrics from combined backtest",
        }
        (tmp_path / "connors_rsi2_commodity_etfs.json").write_text(json.dumps(d))

        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        s, m, _ = pairs[0]
        assert s == "connors_rsi2"
        assert m == "commodity_etfs"

    def test_connors_rsi2_gold_etfs_prerun_state_detected(
        self, tmp_path: Path
    ) -> None:
        """Pre-rerun state for gold_etfs variant."""
        d = {
            "strategy": "connors_rsi2",
            "market": "gold_etfs",
            "is_solo": True,
            # solo_sharpe_clean absent
        }
        (tmp_path / "connors_rsi2_gold_etfs.json").write_text(json.dumps(d))

        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert len(pairs) == 1
        assert pairs[0][0] == "connors_rsi2"
        assert pairs[0][1] == "gold_etfs"

    def test_connors_rsi2_post_rerun_not_detected(self, tmp_path: Path) -> None:
        """After rerun: is_solo=True AND solo_sharpe_clean populated → CLEAN."""
        d = {
            "strategy": "connors_rsi2",
            "market": "commodity_etfs",
            "is_solo": True,
            "solo_fraction": 1.0,
            "solo_sharpe_clean": 0.9862,   # populated by rerun
        }
        (tmp_path / "connors_rsi2_commodity_etfs.json").write_text(json.dumps(d))

        pairs = detect_contaminated_pairs(best_dir=tmp_path)
        assert pairs == []
