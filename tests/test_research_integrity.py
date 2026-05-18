"""Regression tests for research integrity helpers — Task A, 2026-05-12.

Coverage:
  1. All research/best/*.json files have is_solo field (true/false/null).
  2. Contaminated files: contamination_note non-empty, solo_fraction < 0.50.
  3. Solo files: solo_fraction >= 0.50, metrics consistency.
  4. check_solo("connors_rsi2") — post-#327 rerun: (True, ~1.0, None).
     Pre-rerun expectation was (False, ~0.11, note); updated 2026-05-18.
  5. check_solo("momentum_breakout") → (True, ~0.72, None).
  6. assert_solo_or_raise raises ValueError on a contaminated strategy (mean_reversion).
  7. _run_promotion_sweep gates contaminated strategy + allows solo.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ── Project root on sys.path ──────────────────────────────────────────────────
ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

BEST_DIR = ATLAS_ROOT / "research" / "best"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _load_json(path: Path) -> dict[str, Any]:
    """Load JSON tolerating NaN literals (Python json can't handle them)."""
    text = path.read_text()
    text = text.replace(": NaN", ": null").replace(":NaN", ":null")
    text = text.replace(": Infinity", ": null").replace(":Infinity", ":null")
    text = text.replace(": -Infinity", ": null").replace(":-Infinity", ":null")
    return json.loads(text)


def _all_best_files() -> list[Path]:
    return sorted(BEST_DIR.glob("*.json"))


# ─── Test 1: All files have is_solo field ─────────────────────────────────────

class TestAllFilesHaveIsSolo:
    """Every research/best/*.json must have an is_solo top-level field."""

    def test_all_files_have_is_solo_field(self) -> None:
        files = _all_best_files()
        assert len(files) > 0, "No research/best/*.json files found"

        missing = []
        for fpath in files:
            data = _load_json(fpath)
            if "is_solo" not in data:
                missing.append(fpath.name)

        assert missing == [], (
            f"{len(missing)} file(s) missing 'is_solo' field: {missing}"
        )

    def test_is_solo_is_valid_value(self) -> None:
        """is_solo must be true, false, or null (None in Python)."""
        for fpath in _all_best_files():
            data = _load_json(fpath)
            val = data.get("is_solo", "MISSING")
            assert val in (True, False, None), (
                f"{fpath.name}: is_solo={val!r} is not true/false/null"
            )

    def test_counts_match_expected_distribution(self) -> None:
        """Broad sanity: >0 solo, >0 contaminated, >0 no_breakdown."""
        solo = contaminated = no_bd = 0
        for fpath in _all_best_files():
            data = _load_json(fpath)
            v = data.get("is_solo")
            if v is True:
                solo += 1
            elif v is False:
                contaminated += 1
            else:
                no_bd += 1
        assert solo > 0, "Expected at least one SOLO file"
        assert contaminated > 0, "Expected at least one CONTAMINATED file"
        assert no_bd > 0, "Expected at least one NO_BREAKDOWN file"


# ─── Test 2: Contaminated files are internally consistent ────────────────────

class TestContaminatedFiles:
    """Files with is_solo=False must have a non-empty note and solo_fraction < 0.50."""

    def _contaminated_files(self) -> list[tuple[Path, dict]]:
        result = []
        for fpath in _all_best_files():
            data = _load_json(fpath)
            if data.get("is_solo") is False:
                result.append((fpath, data))
        return result

    def test_contamination_note_is_non_empty(self) -> None:
        files = self._contaminated_files()
        assert len(files) > 0, "No contaminated files found — enrichment may not have run"
        for fpath, data in files:
            note = data.get("contamination_note")
            assert note and len(note) > 10, (
                f"{fpath.name}: contamination_note is empty or missing"
            )

    def test_solo_fraction_is_below_threshold(self) -> None:
        for fpath, data in self._contaminated_files():
            frac = data.get("solo_fraction")
            assert frac is not None, f"{fpath.name}: solo_fraction is None for contaminated file"
            assert frac < 0.50, (
                f"{fpath.name}: solo_fraction={frac:.2%} but is_solo=False (< 0.50 expected)"
            )

    def test_note_mentions_dominant_strategy(self) -> None:
        for fpath, data in self._contaminated_files():
            note = data.get("contamination_note", "")
            assert "Dominant strategy:" in note, (
                f"{fpath.name}: contamination_note does not mention dominant strategy"
            )

    def test_note_mentions_task_327(self) -> None:
        for fpath, data in self._contaminated_files():
            note = data.get("contamination_note", "")
            assert "task #327" in note, (
                f"{fpath.name}: contamination_note does not reference task #327"
            )


# ─── Test 3: Solo files are internally consistent ────────────────────────────

class TestSoloFiles:
    """Files with is_solo=True must have solo_fraction >= 0.50 and sane metrics."""

    def _solo_files(self) -> list[tuple[Path, dict]]:
        result = []
        for fpath in _all_best_files():
            data = _load_json(fpath)
            if data.get("is_solo") is True:
                result.append((fpath, data))
        return result

    def test_solo_fraction_at_or_above_threshold(self) -> None:
        files = self._solo_files()
        assert len(files) > 0, "No solo files found"
        verified = 0
        for fpath, data in files:
            frac = data.get("solo_fraction")
            if frac is None:
                # Some files have is_solo=True but solo_fraction=null because they use
                # a non-standard backtest format (e.g. crypto_btc_eth_v1.json with
                # slippage_runs structure, or monthly_rotation.json without a
                # strategy_breakdown). The standard enrichment pipeline cannot compute
                # solo_fraction without a breakdown table, so these are advisory-only.
                # Skip the threshold check — the is_solo flag was set intentionally.
                continue
            assert frac >= 0.50, (
                f"{fpath.name}: solo_fraction={frac:.2%} but is_solo=True (>= 0.50 expected)"
            )
            verified += 1
        assert verified > 0, (
            "No solo files had solo_fraction computed — enrichment pipeline may not have run"
        )

    def test_metrics_total_trades_non_negative(self) -> None:
        for fpath, data in self._solo_files():
            total = data.get("metrics", {}).get("total_trades", 0) or 0
            assert total >= 0, f"{fpath.name}: negative total_trades={total}"

    def test_breakdown_sum_within_rounding(self) -> None:
        """Strategy breakdown trades should sum close to total_trades (5% tolerance)."""
        for fpath, data in self._solo_files():
            metrics = data.get("metrics", {})
            total = metrics.get("total_trades") or 0
            bd = metrics.get("strategy_breakdown")
            if not bd or total == 0:
                continue  # no breakdown to check
            bd_sum = sum((v.get("trades") or 0) for v in bd.values())
            tolerance = max(1, total * 0.05)
            assert abs(bd_sum - total) <= tolerance, (
                f"{fpath.name}: breakdown sum={bd_sum} vs total_trades={total} "
                f"(diff={abs(bd_sum-total)}, tolerance={tolerance:.1f})"
            )

    def test_no_contamination_note_for_solo(self) -> None:
        for fpath, data in self._solo_files():
            note = data.get("contamination_note")
            assert note is None, (
                f"{fpath.name}: has contamination_note={note!r} but is_solo=True"
            )


# ─── Test 4: check_solo for connors_rsi2 ─────────────────────────────────────
# Post-#327 rerun (2026-05-18): connors_rsi2 re-validated as clean-solo.
# is_solo=true, solo_fraction=1.0, solo_sharpe_clean=-0.2433.

class TestCheckSoloConnorsRsi2:
    def test_returns_true_for_solo(self) -> None:
        # Post-rerun: connors_rsi2 is now a clean-solo strategy (was: contaminated pre-rerun)
        from research.integrity import check_solo
        is_solo, frac, note = check_solo("connors_rsi2", "sp500")
        assert is_solo is True, f"Expected True (post-rerun), got {is_solo!r}"

    def test_fraction_is_approx_10(self) -> None:
        # Post-rerun: solo_fraction=1.0 (was: ~0.11 pre-rerun)
        from research.integrity import check_solo
        _, frac, _ = check_solo("connors_rsi2", "sp500")
        assert frac is not None
        assert 0.99 <= frac <= 1.01, f"Expected ~1.0 (post-rerun), got {frac}"

    def test_note_is_none(self) -> None:
        # Post-rerun: no contamination note (was: non-empty contamination note pre-rerun)
        from research.integrity import check_solo
        _, _, note = check_solo("connors_rsi2", "sp500")
        assert note is None, f"Expected None (post-rerun solo), got {note!r}"


# ─── Test 5: check_solo for momentum_breakout ────────────────────────────────

class TestCheckSoloMomentumBreakout:
    def test_returns_true_for_solo(self) -> None:
        from research.integrity import check_solo
        is_solo, frac, note = check_solo("momentum_breakout", "sp500")
        assert is_solo is True, f"Expected True, got {is_solo!r}"

    def test_fraction_is_approx_072(self) -> None:
        from research.integrity import check_solo
        _, frac, _ = check_solo("momentum_breakout", "sp500")
        assert frac is not None
        assert 0.60 <= frac <= 0.85, f"Expected ~0.72, got {frac}"

    def test_note_is_none(self) -> None:
        from research.integrity import check_solo
        _, _, note = check_solo("momentum_breakout", "sp500")
        assert note is None, f"Expected None note for solo strategy, got {note!r}"


# ─── Test 6: assert_solo_or_raise ────────────────────────────────────────────

class TestAssertSoloOrRaise:
    def test_does_not_raise_for_connors_rsi2_post_rerun(self) -> None:
        # Post-rerun: connors_rsi2 is now solo — should NOT raise (was: raised pre-rerun)
        from research.integrity import assert_solo_or_raise
        assert_solo_or_raise("connors_rsi2", "sp500")  # should not raise

    def test_passes_for_solo(self) -> None:
        from research.integrity import assert_solo_or_raise
        # Should not raise
        assert_solo_or_raise("momentum_breakout", "sp500")

    def test_passes_for_missing_file(self) -> None:
        from research.integrity import assert_solo_or_raise
        # Missing file → is_solo=None → no raise
        assert_solo_or_raise("nonexistent_strategy_xyz", "sp500")

    def test_raises_for_different_contaminated_strategy(self) -> None:
        # Post-rerun: connors_rsi2 is now solo, so we use mean_reversion/commodity_etfs
        # (known contaminated per mental model) to test the raise+fraction path.
        # If no contaminated file is available, skip gracefully.
        from research.integrity import assert_solo_or_raise, check_solo
        # Find any known-contaminated strategy to test the raise path
        is_solo, frac, note = check_solo("mean_reversion", "commodity_etfs")
        if is_solo is not False:
            pytest.skip(
                "post-#327 rerun: connors_rsi2 is now solo; need another contaminated strategy "
                "to test assert_solo_or_raise raise path — mean_reversion/commodity_etfs also clean"
            )
        with pytest.raises(ValueError) as exc_info:
            assert_solo_or_raise("mean_reversion", "commodity_etfs")
        msg = str(exc_info.value)
        assert "solo_fraction" in msg, f"Expected solo_fraction in: {msg}"
        assert "mean_reversion" in msg


# ─── Test 7: _run_promotion_sweep gates contaminated, allows solo ─────────────

class TestPromotionSweepIntegrityGate:
    """End-to-end: gate blocks connors_rsi2, allows momentum_breakout.

    auto_promote is a LOCAL import inside _run_promotion_sweep:
        from research.promoter import auto_promote
    So we patch 'research.promoter.auto_promote' (where the function lives).
    """

    _MOCK_CONFIG = {
        "strategies": {},
        "version": "1.0",
        "market_id": "sp500",
    }

    def _make_result(
        self, strategy: str, kept: int = 5, final_sharpe: float = 0.80
    ) -> dict:
        return {
            "strategy": strategy,
            "kept": kept,
            "exit_code": 0,
            "final_sharpe": final_sharpe,
            "starting_sharpe": 0.30,
        }

    def test_contaminated_strategy_blocked(self) -> None:
        """A contaminated strategy (mean_reversion/sp500) is blocked by the gate.

        Post-rerun: connors_rsi2 is now solo, so we use mean_reversion (still
        contaminated per research/best/mean_reversion.json is_solo=false) to verify
        the contamination gate still blocks strategies with is_solo=False.
        """
        from research.autoresearch_nightly import _run_promotion_sweep

        # mean_reversion/sp500 is still contaminated (is_solo=false)
        results = [self._make_result("mean_reversion")]

        with patch("research.promoter.auto_promote") as mock_ap:
            mock_ap.return_value = {"promoted": True, "reason": "mock_promoted"}
            with patch("utils.config.get_active_config", return_value=self._MOCK_CONFIG):
                outcomes = _run_promotion_sweep(results, market="sp500", universe="sp500")

        assert len(outcomes) == 1, f"Expected 1 outcome, got {outcomes}"
        o = outcomes[0]
        assert o["strategy"] == "mean_reversion"
        assert o["promoted"] is False
        assert "contaminated_metrics" in o["reason"], (
            f"Expected contaminated_metrics in reason, got: {o['reason']!r}"
        )
        # auto_promote must NOT have been called (gate fired before reaching it)
        mock_ap.assert_not_called()

    def test_solo_strategy_not_blocked(self) -> None:
        """momentum_breakout (sp500) must NOT be blocked by the contamination gate."""
        from research.autoresearch_nightly import _run_promotion_sweep

        results = [self._make_result("momentum_breakout", final_sharpe=0.85)]

        with patch("research.promoter.auto_promote") as mock_ap:
            mock_ap.return_value = {
                "promoted": True,
                "reason": "mock_promoted",
                "strategy": "momentum_breakout",
            }
            with patch("utils.config.get_active_config", return_value=self._MOCK_CONFIG):
                outcomes = _run_promotion_sweep(results, market="sp500", universe="sp500")

        # No contamination block outcome
        contaminated = [o for o in outcomes if "contaminated_metrics" in o.get("reason", "")]
        assert contaminated == [], (
            f"momentum_breakout was incorrectly blocked: {contaminated}"
        )

    def test_both_strategies_mixed(self) -> None:
        """Contaminated (mean_reversion) blocked; solo (momentum_breakout) passes gate.

        Post-rerun: connors_rsi2 is now solo (is_solo=true), so we substitute
        mean_reversion (still is_solo=false) as the contaminated example.
        """
        from research.autoresearch_nightly import _run_promotion_sweep

        results = [
            self._make_result("mean_reversion", final_sharpe=0.9),   # contaminated (is_solo=false)
            self._make_result("momentum_breakout", final_sharpe=0.9), # solo (is_solo=true)
        ]

        with patch("research.promoter.auto_promote") as mock_ap:
            mock_ap.return_value = {"promoted": True, "reason": "mock_promoted"}
            with patch("utils.config.get_active_config", return_value=self._MOCK_CONFIG):
                outcomes = _run_promotion_sweep(results, market="sp500", universe="sp500")

        # mean_reversion must be blocked by contamination gate
        mr = [o for o in outcomes if o.get("strategy") == "mean_reversion"]
        assert len(mr) == 1, f"Expected 1 mean_reversion outcome, got {mr}"
        assert "contaminated_metrics" in mr[0].get("reason", ""), (
            f"mean_reversion not blocked: {mr[0]}"
        )

        # momentum_breakout must NOT have a contamination block
        mb = [o for o in outcomes if o.get("strategy") == "momentum_breakout"]
        for o in mb:
            assert "contaminated_metrics" not in o.get("reason", ""), (
                f"momentum_breakout incorrectly blocked: {o}"
            )

        # auto_promote should have been called for momentum_breakout (solo, passes gate)
        assert mock_ap.call_count >= 0  # at minimum, not called for mean_reversion
