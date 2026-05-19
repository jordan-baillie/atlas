"""Regression tests: mean_reversion falling-knife guard (Prereq 2, 2026-05-19).

Tests two mechanisms added by the guard comparison:

1. RS confidence modifier (roc_60 floor) — verifies the backtest enrichment
   pipeline correctly penalises signals with roc_60 < -0.20 when
   relative_strength.enabled=True.  This is the Option B / C mechanism and
   is tested here so future changes to enrichment.py regress visibly.

2. SMA-200 filter (Option A — CHOSEN) — verifies the config is correctly set
   and that MeanReversion.__init__ reads sma200_filter=True from config.

All tests use the autouse DB-isolation fixtures from conftest.py and operate
on in-memory / synthetic data only (no network calls, no Alpaca).
"""

import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------------------------------------------------------------------------
# Helper: minimal Signal constructor
# ---------------------------------------------------------------------------

def _make_signal(
    ticker: str = "ZTS",
    strategy: str = "mean_reversion",
    confidence: float = 0.85,
    features: dict | None = None,
) -> object:
    """Create a minimal Signal for enrichment testing."""
    from strategies.base import Signal

    sig = Signal(
        ticker=ticker,
        strategy=strategy,
        direction="long",
        entry_price=100.0,
        stop_price=95.0,
        take_profit=110.0,
        position_size=10,
        position_value=1000.0,
        risk_amount=50.0,
        confidence=confidence,
        rationale="test signal",
        features=features or {},
    )
    return sig


# ---------------------------------------------------------------------------
# Tests: RS confidence modifier (Option B mechanism)
# ---------------------------------------------------------------------------


class TestRocFloorConfidenceModifier:
    """Verify apply_rs_confidence() applies the roc_60 penalty correctly."""

    def _rs_cfg_enabled(self) -> dict:
        """Return a strategies dict with mean_reversion RS enabled at Option B params."""
        return {
            "mean_reversion": {
                "relative_strength": {
                    "enabled": True,
                    "metric": "roc_60",
                    "low_threshold": -0.20,
                    "high_threshold": 0.0,
                    "low_penalty": 0.30,
                    "high_boost": 0.0,
                }
            }
        }

    def test_falling_knife_roc60_below_floor_drops_confidence(self):
        """roc_60=-0.41 (below -0.20 threshold): penalty -0.30 applied.

        confidence 0.85 - 0.30 = 0.55.  Since sp500 min_confidence=0.65,
        this signal would be rejected as a falling-knife entry.
        """
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.85, features={"roc_60": -0.41})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        assert sig.confidence == pytest.approx(0.55, abs=1e-9), (
            f"Expected 0.55 after -0.30 penalty, got {sig.confidence}"
        )
        assert sig.features.get("rs_confidence_adj") == pytest.approx(-0.30, abs=1e-9)
        assert sig.features.get("rs_confidence_orig") == pytest.approx(0.85, abs=1e-9)

        # Key assertion: result is below min_confidence=0.65 → entry blocked
        MIN_CONFIDENCE = 0.65
        assert sig.confidence < MIN_CONFIDENCE, (
            f"Signal at {sig.confidence:.3f} should be below min_confidence={MIN_CONFIDENCE}"
        )

    def test_falling_knife_at_exact_threshold_no_penalty(self):
        """roc_60=-0.20 (exactly at threshold): boundary is exclusive (<), no penalty."""
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.85, features={"roc_60": -0.20})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        # roc_60 == low_threshold → the condition is val < thresh → False, no penalty
        assert sig.confidence == pytest.approx(0.85, abs=1e-9), (
            "At the exact threshold, no penalty should be applied"
        )

    def test_positive_roc60_no_penalty_no_boost(self):
        """roc_60=+0.05 (above high_threshold=0.0 but high_boost=0.0): no change.

        Confidence stays at 0.85 — well above min_confidence=0.65 → entry accepted.
        """
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.85, features={"roc_60": 0.05})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        assert sig.confidence == pytest.approx(0.85, abs=1e-9), (
            f"Expected 0.85 (no change), got {sig.confidence}"
        )
        # No annotation keys expected (adj only set when adj != 0)
        assert "rs_confidence_adj" not in sig.features

        # Key assertion: above min_confidence → entry accepted
        MIN_CONFIDENCE = 0.65
        assert sig.confidence >= MIN_CONFIDENCE

    def test_moderate_negative_roc60_no_penalty(self):
        """roc_60=-0.10 (negative but above -0.20 threshold): no penalty."""
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.85, features={"roc_60": -0.10})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        assert sig.confidence == pytest.approx(0.85, abs=1e-9)

    def test_rs_disabled_no_modification(self):
        """When relative_strength.enabled=False, no modifier is applied."""
        from backtest.enrichment import apply_rs_confidence

        disabled_cfg = {
            "mean_reversion": {
                "relative_strength": {
                    "enabled": False,
                    "metric": "roc_60",
                    "low_threshold": -0.20,
                    "high_threshold": 0.0,
                    "low_penalty": 0.30,
                    "high_boost": 0.0,
                }
            }
        }
        sig = _make_signal(confidence=0.85, features={"roc_60": -0.99})
        apply_rs_confidence([sig], disabled_cfg)

        assert sig.confidence == pytest.approx(0.85, abs=1e-9)

    def test_missing_roc60_feature_no_penalty(self):
        """When roc_60 is not in features, no penalty is applied (missing data = no filter)."""
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.85, features={})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        # rs_val is None -> no modification
        assert sig.confidence == pytest.approx(0.85, abs=1e-9)

    def test_confidence_floor_at_zero(self):
        """Confidence cannot go below 0.0 even with large penalty."""
        from backtest.enrichment import apply_rs_confidence

        sig = _make_signal(confidence=0.15, features={"roc_60": -0.99})
        apply_rs_confidence([sig], self._rs_cfg_enabled())

        # 0.15 - 0.30 = -0.15, clamped to 0.0
        assert sig.confidence == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Tests: SMA-200 filter (Option A — CHOSEN variant applied to config)
# ---------------------------------------------------------------------------


class TestSma200FilterConfig:
    """Verify Option A (SMA-200 filter) is correctly reflected in:
    - The active sp500.json config
    - The MeanReversion strategy __init__
    """

    def test_config_sma200_filter_is_true(self):
        """sp500.json mean_reversion.sma200_filter must be True after Prereq 2."""
        import json

        cfg_path = PROJECT / "config" / "active" / "sp500.json"
        cfg = json.loads(cfg_path.read_text())
        mr_cfg = cfg.get("strategies", {}).get("mean_reversion", {})

        assert mr_cfg.get("sma200_filter") is True, (
            "mean_reversion.sma200_filter must be True (Option A applied by Prereq 2)"
        )

    def test_config_version_bumped(self):
        """Config version should be v3.2.4 after Prereq 2."""
        import json

        cfg_path = PROJECT / "config" / "active" / "sp500.json"
        cfg = json.loads(cfg_path.read_text())
        assert cfg["version"] == "v3.2.4", f"Expected v3.2.4, got {cfg['version']}"

    def test_config_mean_reversion_still_disabled(self):
        """mean_reversion must remain disabled in live config (lifecycle=PAPER)."""
        import json

        cfg_path = PROJECT / "config" / "active" / "sp500.json"
        cfg = json.loads(cfg_path.read_text())
        mr_cfg = cfg.get("strategies", {}).get("mean_reversion", {})

        assert mr_cfg.get("enabled") is False, (
            "mean_reversion.enabled must remain False (lifecycle=PAPER, not promoted)"
        )

    def test_mean_reversion_reads_sma200_from_config(self):
        """MeanReversion.__init__ must read sma200_filter=True from config."""
        from strategies.mean_reversion import MeanReversion

        config = {
            "strategies": {
                "mean_reversion": {
                    "enabled": True,
                    "sma200_filter": True,
                    "rsi_period": 14,
                    "rsi_oversold": 35,
                    "zscore_lookback": 20,
                    "zscore_entry": -2.0,
                    "atr_period": 14,
                    "atr_stop_mult": 1.5,
                    "profit_target_atr_mult": 2.0,
                    "max_hold_days": 20,
                }
            }
        }
        mr = MeanReversion(config)
        assert mr.sma200_filter is True, (
            f"MeanReversion.sma200_filter expected True, got {mr.sma200_filter}"
        )

    def test_mean_reversion_sma200_off_when_false(self):
        """MeanReversion.sma200_filter is False when config sets false (control)."""
        from strategies.mean_reversion import MeanReversion

        config = {
            "strategies": {
                "mean_reversion": {
                    "enabled": True,
                    "sma200_filter": False,
                    "rsi_period": 14,
                    "rsi_oversold": 35,
                    "zscore_lookback": 20,
                    "zscore_entry": -2.0,
                    "atr_period": 14,
                    "atr_stop_mult": 1.5,
                    "profit_target_atr_mult": 2.0,
                    "max_hold_days": 20,
                }
            }
        }
        mr = MeanReversion(config)
        assert mr.sma200_filter is False
