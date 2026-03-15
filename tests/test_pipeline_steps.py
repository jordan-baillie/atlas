"""Tests for backtest.filters, backtest.enrichment, and backtest.pipeline.

All tests run without network access and complete in < 5 seconds.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Ensure project root on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.filters import (
    check_fred_macro,
    check_macro_regime,
    check_turn_of_month,
    check_vix_gate,
)
from backtest.enrichment import (
    apply_breadth_confidence,
    apply_macro_confidence,
    apply_rs_confidence,
    apply_tom_confidence,
    inject_breadth_features,
    inject_rs_features,
)
from backtest.pipeline import DayContext, _build_tom_cfg, enrich_signals, run_entry_gates
from strategies.base import Signal


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_signal(ticker="AAPL", strategy="momentum_breakout", confidence=0.75):
    """Construct a minimal valid Signal."""
    return Signal(
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
        features={},
    )


def _make_vix_series(dates, values):
    return pd.Series(values, index=pd.DatetimeIndex(dates))


def _make_trading_dates(start="2024-01-02", periods=60):
    return pd.bdate_range(start, periods=periods)


# ─────────────────────────────────────────────────────────────────────────────
# check_vix_gate
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckVixGate:
    def test_blocks_when_vix_above_threshold(self):
        yesterday = pd.Timestamp("2024-01-10")
        vix = _make_vix_series([yesterday], [35.0])
        blocked, reason, meta = check_vix_gate(vix, yesterday, vix_max_entry=30.0)
        assert blocked is True
        assert "35.0" in reason
        assert meta["current_vix"] == pytest.approx(35.0)

    def test_passes_when_vix_below_threshold(self):
        yesterday = pd.Timestamp("2024-01-10")
        vix = _make_vix_series([yesterday], [20.0])
        blocked, reason, meta = check_vix_gate(vix, yesterday, vix_max_entry=30.0)
        assert blocked is False
        assert reason == ""
        assert meta["current_vix"] == pytest.approx(20.0)

    def test_passes_when_vix_exactly_at_threshold(self):
        yesterday = pd.Timestamp("2024-01-10")
        vix = _make_vix_series([yesterday], [30.0])
        blocked, _, _ = check_vix_gate(vix, yesterday, vix_max_entry=30.0)
        assert blocked is False  # > not >=

    def test_passes_when_vix_series_is_none(self):
        yesterday = pd.Timestamp("2024-01-10")
        blocked, reason, meta = check_vix_gate(None, yesterday, 30.0)
        assert blocked is False
        assert meta == {}

    def test_passes_when_date_not_in_series(self):
        yesterday = pd.Timestamp("2024-01-10")
        vix = _make_vix_series(["2024-01-09"], [99.0])
        blocked, _, meta = check_vix_gate(vix, yesterday, 30.0)
        assert blocked is False
        assert meta == {}


# ─────────────────────────────────────────────────────────────────────────────
# check_fred_macro
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckFredMacro:
    def _yc(self, date, val):
        return pd.Series([val], index=pd.DatetimeIndex([date]))

    def _claims(self, date, val):
        return pd.Series([val], index=pd.DatetimeIndex([date]))

    def test_blocks_on_inverted_yield_curve(self):
        yesterday = pd.Timestamp("2024-01-10")
        yc = self._yc("2024-01-09", -0.6)
        blocked, _, _ = check_fred_macro(yc, None, yesterday, {"yield_curve_min": -0.5})
        assert blocked is True

    def test_passes_normal_yield_curve(self):
        yesterday = pd.Timestamp("2024-01-10")
        yc = self._yc("2024-01-09", 0.3)
        blocked, _, _ = check_fred_macro(yc, None, yesterday, {"yield_curve_min": -0.5})
        assert blocked is False

    def test_blocks_on_high_claims(self):
        yesterday = pd.Timestamp("2024-01-10")
        claims = self._claims("2024-01-09", 350_000)
        blocked, _, _ = check_fred_macro(None, claims, yesterday, {"claims_max": 300_000})
        assert blocked is True

    def test_passes_low_claims(self):
        yesterday = pd.Timestamp("2024-01-10")
        claims = self._claims("2024-01-09", 250_000)
        blocked, _, _ = check_fred_macro(None, claims, yesterday, {"claims_max": 300_000})
        assert blocked is False

    def test_passes_with_none_inputs(self):
        yesterday = pd.Timestamp("2024-01-10")
        blocked, _, _ = check_fred_macro(None, None, yesterday, {})
        assert blocked is False

    def test_yield_curve_blocks_overrides_claims(self):
        """Yield curve block should prevent claims check."""
        yesterday = pd.Timestamp("2024-01-10")
        yc = self._yc("2024-01-09", -0.9)
        claims = self._claims("2024-01-09", 250_000)  # claims OK
        blocked, _, _ = check_fred_macro(
            yc, claims, yesterday,
            {"yield_curve_min": -0.5, "claims_max": 300_000},
        )
        assert blocked is True


# ─────────────────────────────────────────────────────────────────────────────
# check_turn_of_month
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckTurnOfMonth:
    def _trading_dates(self):
        return pd.bdate_range("2024-01-02", "2024-03-29")

    def test_disabled_mode_never_blocks(self):
        dates = self._trading_dates()
        today = dates[0]
        blocked, _, meta = check_turn_of_month(today, dates, {"mode": False})
        assert blocked is False
        assert meta["tom_in_window"] is False

    def test_blocks_outside_window_when_mode_true(self):
        dates = self._trading_dates()
        # Pick a mid-month date: Jan 17 is a Wednesday in 2024, well away from ends
        today = pd.Timestamp("2024-01-17")
        cfg = {"mode": True, "days_before_month_end": 5, "days_after_month_start": 3}
        blocked, reason, meta = check_turn_of_month(today, dates, cfg)
        assert blocked is True
        assert "turn-of-month" in reason
        assert meta["tom_in_window"] is False

    def test_passes_at_month_end(self):
        dates = self._trading_dates()
        # Jan 31 2024 is the last trading day of Jan 2024
        today = pd.Timestamp("2024-01-31")
        cfg = {"mode": True, "days_before_month_end": 5, "days_after_month_start": 3}
        blocked, _, meta = check_turn_of_month(today, dates, cfg)
        assert blocked is False
        assert meta["tom_in_window"] is True

    def test_passes_at_month_start(self):
        dates = self._trading_dates()
        # Feb 1 2024 is the first trading day of February
        today = pd.Timestamp("2024-02-01")
        cfg = {"mode": True, "days_before_month_end": 5, "days_after_month_start": 3}
        blocked, _, meta = check_turn_of_month(today, dates, cfg)
        assert blocked is False
        assert meta["tom_in_window"] is True

    def test_boost_mode_never_blocks(self):
        dates = self._trading_dates()
        today = pd.Timestamp("2024-01-17")  # mid-month
        cfg = {"mode": "boost", "days_before_month_end": 5, "days_after_month_start": 3}
        blocked, _, _ = check_turn_of_month(today, dates, cfg)
        assert blocked is False  # boost mode doesn't block


# ─────────────────────────────────────────────────────────────────────────────
# check_macro_regime
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckMacroRegime:
    def _macro_df(self, date, scale, gc_regime=2, vix_roc=0.0, yc_spread=0.1):
        return pd.DataFrame(
            {
                "macro_regime_scale": [scale],
                "gc_regime": [gc_regime],
                "vix_roc_5d": [vix_roc],
                "vix_spike": [False],
                "yield_curve_10y_3m": [yc_spread],
                "yc_flattening": [False],
            },
            index=pd.DatetimeIndex([date]),
        )

    def test_passes_when_macro_signals_none(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        blocked, _, meta = check_macro_regime(
            None, yesterday, today, {"enabled": True, "mode": "gate"}
        )
        assert blocked is False
        assert meta["macro_scale"] == pytest.approx(1.0)

    def test_gate_mode_blocks_low_scale(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        macro_df = self._macro_df("2024-01-10", scale=0.5)
        blocked, reason, meta = check_macro_regime(
            macro_df, yesterday, today, {"enabled": True, "mode": "gate"}
        )
        assert blocked is True
        assert "0.50" in reason
        assert meta["macro_scale"] == pytest.approx(0.5)

    def test_gate_mode_passes_adequate_scale(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        macro_df = self._macro_df("2024-01-10", scale=0.8)
        blocked, _, _ = check_macro_regime(
            macro_df, yesterday, today, {"enabled": True, "mode": "gate"}
        )
        assert blocked is False

    def test_sizing_mode_never_blocks(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        macro_df = self._macro_df("2024-01-10", scale=0.3)  # would block in gate mode
        blocked, _, _ = check_macro_regime(
            macro_df, yesterday, today, {"enabled": True, "mode": "sizing"}
        )
        assert blocked is False

    def test_boost_mode_sets_boost_when_scale_high(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        macro_df = self._macro_df("2024-01-10", scale=1.5)
        blocked, _, meta = check_macro_regime(
            macro_df, yesterday, today, {"enabled": True, "mode": "boost"}
        )
        assert blocked is False
        assert meta["macro_boost"] == pytest.approx(0.05)

    def test_disabled_never_blocks(self):
        yesterday = pd.Timestamp("2024-01-10")
        today = pd.Timestamp("2024-01-11")
        macro_df = self._macro_df("2024-01-10", scale=0.1)
        blocked, _, meta = check_macro_regime(
            macro_df, yesterday, today, {"enabled": False, "mode": "gate"}
        )
        assert blocked is False
        assert meta["macro_scale"] == pytest.approx(1.0)  # default when disabled

    def test_uses_latest_available_before_yesterday(self):
        """Uses the latest row on or before yesterday (not strictly equal)."""
        yesterday = pd.Timestamp("2024-01-12")
        today = pd.Timestamp("2024-01-13")
        # Macro data is from two days ago (gap in data)
        macro_df = self._macro_df("2024-01-10", scale=0.5)
        blocked, _, meta = check_macro_regime(
            macro_df, yesterday, today, {"enabled": True, "mode": "gate"}
        )
        assert blocked is True
        assert meta["macro_scale"] == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# inject_breadth_features
# ─────────────────────────────────────────────────────────────────────────────

class TestInjectBreadthFeatures:
    def _breadth_df(self, date):
        return pd.DataFrame(
            {
                "pct_above_50ma": [0.60],
                "pct_above_200ma": [0.55],
                "ad_ratio": [1.2],
                "breadth_thrust": [0.8],
                "breadth_momentum": [0.05],
                "net_new_highs_pct": [0.03],
            },
            index=pd.DatetimeIndex([date]),
        )

    def test_injects_expected_keys(self):
        today = pd.Timestamp("2024-01-10")
        sig = _make_signal()
        inject_breadth_features([sig], self._breadth_df(today), today, regime="bull", regime_scale=1.0)
        assert "breadth_pct_above_50ma" in sig.features
        assert "breadth_pct_above_200ma" in sig.features
        assert "breadth_ad_ratio" in sig.features
        assert "breadth_thrust" in sig.features
        assert "breadth_momentum" in sig.features
        assert "breadth_net_new_highs_pct" in sig.features
        assert "regime" in sig.features
        assert "regime_scale" in sig.features

    def test_injects_correct_values(self):
        today = pd.Timestamp("2024-01-10")
        sig = _make_signal()
        inject_breadth_features([sig], self._breadth_df(today), today, regime="bull", regime_scale=1.2)
        assert sig.features["breadth_pct_above_50ma"] == pytest.approx(0.60)
        assert sig.features["regime"] == "bull"
        assert sig.features["regime_scale"] == pytest.approx(1.2)

    def test_noop_when_breadth_series_none(self):
        today = pd.Timestamp("2024-01-10")
        sig = _make_signal()
        inject_breadth_features([sig], None, today)
        assert "breadth_pct_above_50ma" not in sig.features

    def test_noop_when_date_not_in_series(self):
        today = pd.Timestamp("2024-01-10")
        other_date = pd.Timestamp("2024-01-09")
        sig = _make_signal()
        inject_breadth_features([sig], self._breadth_df(other_date), today)
        assert "breadth_pct_above_50ma" not in sig.features

    def test_handles_nan_breadth_momentum(self):
        today = pd.Timestamp("2024-01-10")
        df = pd.DataFrame(
            {"pct_above_50ma": [0.5], "pct_above_200ma": [0.5],
             "ad_ratio": [1.0], "breadth_thrust": [0.5],
             "breadth_momentum": [float("nan")], "net_new_highs_pct": [0.0]},
            index=pd.DatetimeIndex([today]),
        )
        sig = _make_signal()
        inject_breadth_features([sig], df, today)
        assert sig.features["breadth_momentum"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# apply_breadth_confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyBreadthConfidence:
    def test_boosts_confidence_in_low_breadth(self):
        sig = _make_signal(strategy="trend_following", confidence=0.70)
        sig.features["breadth_pct_above_50ma"] = 0.40  # < 0.48
        strategies_cfg = {
            "trend_following": {
                "breadth": {
                    "enabled": True,
                    "metric": "pct_above_50ma",
                    "low_threshold": 0.48,
                    "high_threshold": 0.58,
                    "low_boost": 0.05,
                    "high_penalty": 0.0,
                }
            }
        }
        apply_breadth_confidence([sig], strategies_cfg)
        assert sig.confidence == pytest.approx(0.75)
        assert sig.features["breadth_confidence_adj"] == pytest.approx(0.05)
        assert sig.features["breadth_confidence_orig"] == pytest.approx(0.70)

    def test_penalises_confidence_in_high_breadth(self):
        sig = _make_signal(strategy="trend_following", confidence=0.80)
        sig.features["breadth_pct_above_50ma"] = 0.70  # > 0.58
        strategies_cfg = {
            "trend_following": {
                "breadth": {
                    "enabled": True,
                    "metric": "pct_above_50ma",
                    "low_threshold": 0.48,
                    "high_threshold": 0.58,
                    "low_boost": 0.0,
                    "high_penalty": 0.10,
                }
            }
        }
        apply_breadth_confidence([sig], strategies_cfg)
        assert sig.confidence == pytest.approx(0.70)

    def test_noop_when_disabled(self):
        sig = _make_signal(strategy="trend_following", confidence=0.70)
        sig.features["breadth_pct_above_50ma"] = 0.40
        apply_breadth_confidence([sig], {"trend_following": {"breadth": {"enabled": False}}})
        assert sig.confidence == pytest.approx(0.70)

    def test_noop_when_no_strategy_config(self):
        sig = _make_signal(strategy="unknown_strategy", confidence=0.70)
        sig.features["breadth_pct_above_50ma"] = 0.40
        apply_breadth_confidence([sig], {})
        assert sig.confidence == pytest.approx(0.70)

    def test_clamps_confidence_to_1(self):
        sig = _make_signal(strategy="trend_following", confidence=0.98)
        sig.features["breadth_pct_above_50ma"] = 0.40
        strategies_cfg = {
            "trend_following": {
                "breadth": {
                    "enabled": True,
                    "metric": "pct_above_50ma",
                    "low_threshold": 0.48,
                    "high_threshold": 0.58,
                    "low_boost": 0.10,
                    "high_penalty": 0.0,
                }
            }
        }
        apply_breadth_confidence([sig], strategies_cfg)
        assert sig.confidence <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# inject_rs_features
# ─────────────────────────────────────────────────────────────────────────────

class TestInjectRsFeatures:
    def _rs_data(self, ticker, date):
        df = pd.DataFrame(
            {
                "rs_percentile": [75.0],
                "rs_score": [0.8],
                "rs_momentum": [0.05],
                "roc_20": [0.12],
                "roc_60": [0.25],
                "roc_120": [0.40],
            },
            index=pd.DatetimeIndex([date]),
        )
        return {ticker: df}

    def test_injects_expected_keys(self):
        yesterday = pd.Timestamp("2024-01-09")
        sig = _make_signal(ticker="AAPL")
        inject_rs_features([sig], self._rs_data("AAPL", yesterday), yesterday)
        assert sig.features["rs_percentile"] == pytest.approx(75.0)
        assert sig.features["rs_score"] == pytest.approx(0.8)
        assert sig.features["rs_momentum"] == pytest.approx(0.05)
        assert sig.features["roc_20"] == pytest.approx(0.12)
        assert sig.features["roc_60"] == pytest.approx(0.25)
        assert sig.features["roc_120"] == pytest.approx(0.40)

    def test_noop_when_rs_data_none(self):
        yesterday = pd.Timestamp("2024-01-09")
        sig = _make_signal()
        inject_rs_features([sig], None, yesterday)
        assert "rs_percentile" not in sig.features

    def test_noop_when_ticker_not_in_rs_data(self):
        yesterday = pd.Timestamp("2024-01-09")
        sig = _make_signal(ticker="AAPL")
        inject_rs_features([sig], self._rs_data("TSLA", yesterday), yesterday)
        assert "rs_percentile" not in sig.features

    def test_handles_nan_rs_percentile(self):
        yesterday = pd.Timestamp("2024-01-09")
        df = pd.DataFrame(
            {
                "rs_percentile": [float("nan")],
                "rs_score": [0.0],
                "rs_momentum": [0.0],
                "roc_20": [0.0],
                "roc_60": [0.0],
                "roc_120": [0.0],
            },
            index=pd.DatetimeIndex([yesterday]),
        )
        sig = _make_signal(ticker="AAPL")
        inject_rs_features([sig], {"AAPL": df}, yesterday)
        assert sig.features["rs_percentile"] == pytest.approx(50.0)  # fallback


# ─────────────────────────────────────────────────────────────────────────────
# apply_tom_confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyTomConfidence:
    def test_boosts_confidence_in_window_boost_mode(self):
        sig = _make_signal(confidence=0.70)
        cfg = {"mode": "boost", "confidence_boost": 0.05}
        apply_tom_confidence([sig], cfg, tom_in_window=True)
        assert sig.confidence == pytest.approx(0.75)
        assert sig.features["tom_in_window"] is True
        assert sig.features["tom_boost"] == pytest.approx(0.05)

    def test_no_boost_outside_window_boost_mode(self):
        sig = _make_signal(confidence=0.70)
        cfg = {"mode": "boost", "confidence_boost": 0.05}
        apply_tom_confidence([sig], cfg, tom_in_window=False)
        assert sig.confidence == pytest.approx(0.70)
        assert sig.features["tom_in_window"] is False

    def test_always_tags_tom_in_window(self):
        sig = _make_signal(confidence=0.70)
        cfg = {"mode": False, "confidence_boost": 0.05}
        apply_tom_confidence([sig], cfg, tom_in_window=False)
        assert "tom_in_window" in sig.features

    def test_clamps_confidence_at_1(self):
        sig = _make_signal(confidence=0.98)
        cfg = {"mode": "boost", "confidence_boost": 0.10}
        apply_tom_confidence([sig], cfg, tom_in_window=True)
        assert sig.confidence <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# apply_macro_confidence
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyMacroConfidence:
    def _macro_df(self, date, scale=1.0, gc_regime=2):
        return pd.DataFrame(
            {
                "macro_regime_scale": [scale],
                "gc_regime": [gc_regime],
                "gold_copper_ratio": [2.5],
                "vix_roc_5d": [0.0],
                "vix_spike": [False],
                "yield_curve_10y_3m": [0.1],
                "yc_flattening": [False],
            },
            index=pd.DatetimeIndex([date]),
        )

    def test_injects_macro_features(self):
        yesterday = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        macro_df = self._macro_df(yesterday, scale=1.0, gc_regime=1)
        sig = _make_signal()
        apply_macro_confidence(
            [sig], macro_df, yesterday, today,
            {"enabled": True, "mode": "sizing"}, macro_boost_today=0.0,
        )
        assert sig.features["macro_gc_regime"] == 1
        assert sig.features["macro_regime_scale"] == pytest.approx(1.0)

    def test_boost_mode_applies_confidence_when_boost_positive(self):
        yesterday = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        macro_df = self._macro_df(yesterday, scale=1.5)
        sig = _make_signal(confidence=0.70)
        apply_macro_confidence(
            [sig], macro_df, yesterday, today,
            {"enabled": True, "mode": "boost"}, macro_boost_today=0.05,
        )
        assert sig.confidence == pytest.approx(0.75)
        assert sig.features["macro_confidence_boost"] == pytest.approx(0.05)

    def test_noop_when_macro_signals_none(self):
        yesterday = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        sig = _make_signal(confidence=0.70)
        apply_macro_confidence(
            [sig], None, yesterday, today,
            {"enabled": True, "mode": "boost"}, macro_boost_today=0.05,
        )
        assert sig.confidence == pytest.approx(0.70)
        assert "macro_gc_regime" not in sig.features

    def test_noop_when_disabled(self):
        yesterday = pd.Timestamp("2024-01-09")
        today = pd.Timestamp("2024-01-10")
        macro_df = self._macro_df(yesterday, scale=1.5)
        sig = _make_signal(confidence=0.70)
        apply_macro_confidence(
            [sig], macro_df, yesterday, today,
            {"enabled": False, "mode": "boost"}, macro_boost_today=0.05,
        )
        assert sig.confidence == pytest.approx(0.70)


# ─────────────────────────────────────────────────────────────────────────────
# DayContext
# ─────────────────────────────────────────────────────────────────────────────

class TestDayContext:
    def _make_ctx(self):
        today = pd.Timestamp("2024-01-10")
        yesterday = pd.Timestamp("2024-01-09")
        return DayContext(
            today=today,
            yesterday=yesterday,
            day_idx=1,
            equity=10_000.0,
            open_positions=[],
            closed_trades=[],
            data={},
        )

    def test_default_gate_fields(self):
        ctx = self._make_ctx()
        assert ctx.vix_blocked is False
        assert ctx.fred_blocked is False
        assert ctx.tom_blocked is False
        assert ctx.macro_blocked is False
        assert ctx.macro_scale == pytest.approx(1.0)
        assert ctx.macro_boost == pytest.approx(0.0)
        assert ctx.current_vix == pytest.approx(0.0)
        assert ctx.tom_in_window is False

    def test_default_regime_fields(self):
        ctx = self._make_ctx()
        assert ctx.regime == "neutral"
        assert ctx.regime_scale == pytest.approx(1.0)

    def test_all_signals_starts_empty(self):
        ctx = self._make_ctx()
        assert ctx.all_signals == []

    def test_any_gate_blocked_property_true(self):
        ctx = self._make_ctx()
        ctx.vix_blocked = True
        assert ctx.any_gate_blocked is True

    def test_any_gate_blocked_property_false(self):
        ctx = self._make_ctx()
        assert ctx.any_gate_blocked is False

    def test_any_gate_blocked_fred(self):
        ctx = self._make_ctx()
        ctx.fred_blocked = True
        assert ctx.any_gate_blocked is True

    def test_any_gate_blocked_tom(self):
        ctx = self._make_ctx()
        ctx.tom_blocked = True
        assert ctx.any_gate_blocked is True

    def test_any_gate_blocked_macro(self):
        ctx = self._make_ctx()
        ctx.macro_blocked = True
        assert ctx.any_gate_blocked is True


# ─────────────────────────────────────────────────────────────────────────────
# Import smoke test
# ─────────────────────────────────────────────────────────────────────────────

class TestImports:
    def test_filter_imports(self):
        from backtest.filters import (  # noqa: F401
            check_fred_macro,
            check_macro_regime,
            check_turn_of_month,
            check_vix_gate,
        )

    def test_enrichment_imports(self):
        from backtest.enrichment import (  # noqa: F401
            apply_breadth_confidence,
            apply_macro_confidence,
            apply_rs_confidence,
            apply_tom_confidence,
            inject_breadth_features,
            inject_rs_features,
        )

    def test_pipeline_imports(self):
        from backtest.pipeline import DayContext, enrich_signals, run_entry_gates  # noqa: F401
