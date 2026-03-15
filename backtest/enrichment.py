"""Signal enrichment — inject features and adjust confidence.

Extracted from engine._simulate_day Phase 7B/7C blocks and macro
confidence adjustment.  All functions mutate signals in-place and
return None — consistent with the original implementation.
"""
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


def apply_macro_confidence(
    signals: list,
    macro_signals: Optional[pd.DataFrame],
    yesterday: pd.Timestamp,
    today: pd.Timestamp,
    macro_cfg: Dict[str, Any],
    macro_boost_today: float = 0.0,
) -> None:
    """Inject macro regime features and apply confidence boost (in-place).

    Replicates the "Macro regime features: inject into signals" block inside
    the ``for strategy in strategies:`` loop in engine._simulate_day.

    Injects these keys into each signal's .features dict:
      macro_gc_regime, macro_gc_ratio, macro_vix_roc, macro_vix_spike,
      macro_yc_spread, macro_yc_flattening, macro_regime_scale.

    When mode="boost" and macro_boost_today > 0, also boosts confidence and
    records macro_confidence_boost / macro_confidence_orig.

    Args:
        signals:          List of Signal objects for one strategy batch.
        macro_signals:    DataFrame of macro regime signals (or None).
        yesterday:        The lookback date for macro data selection.
        today:            Current simulation date (for logging).
        macro_cfg:        Config sub-dict from config["macro_regime"].
        macro_boost_today: Pre-computed boost value from check_macro_regime().
    """
    macro_regime_enabled = macro_cfg.get("enabled", False)
    macro_mode = macro_cfg.get("mode", "sizing")

    if macro_signals is not None and macro_regime_enabled:
        _macro_mask = macro_signals.index <= yesterday
        if _macro_mask.any():
            _macro_row = macro_signals.loc[_macro_mask].iloc[-1]
            for _sig in signals:
                _sig.features["macro_gc_regime"] = int(_macro_row.get("gc_regime", 2))
                _sig.features["macro_gc_ratio"] = float(_macro_row.get("gold_copper_ratio", 0.0))
                _sig.features["macro_vix_roc"] = float(_macro_row.get("vix_roc_5d", 0.0))
                _sig.features["macro_vix_spike"] = bool(_macro_row.get("vix_spike", False))
                _sig.features["macro_yc_spread"] = float(_macro_row.get("yield_curve_10y_3m", 0.0))
                _sig.features["macro_yc_flattening"] = bool(_macro_row.get("yc_flattening", False))
                _sig.features["macro_regime_scale"] = float(_macro_row.get("macro_regime_scale", 1.0))
            # Boost mode: add confidence when macro is favorable
            if macro_mode == "boost" and macro_boost_today > 0:
                for _sig in signals:
                    _orig_conf = _sig.confidence
                    _sig.confidence = min(1.0, _sig.confidence + macro_boost_today)
                    _sig.features["macro_confidence_boost"] = round(macro_boost_today, 4)
                    _sig.features["macro_confidence_orig"] = round(_orig_conf, 4)
                    logger.debug(
                        f"MACRO BOOST {_sig.ticker}: "
                        f"conf {_orig_conf:.3f} -> {_sig.confidence:.3f}"
                    )


def apply_tom_confidence(
    signals: list,
    tom_cfg: Dict[str, Any],
    tom_in_window: bool,
) -> None:
    """Apply TOM confidence boost and tag all signals with TOM window info (in-place).

    Replicates the TOM boost/tagging block inside the ``for strategy in strategies:``
    loop in engine._simulate_day.

    When mode="boost" and tom_in_window=True, boosts confidence by
    ``confidence_boost`` and records tom_boost / tom_confidence_orig on each signal.
    Always tags signals with ``tom_in_window``.

    Args:
        signals:       List of Signal objects for one strategy batch.
        tom_cfg:       Dict with keys: mode, confidence_boost.
        tom_in_window: Whether today is inside the TOM window.
    """
    tom_mode = tom_cfg.get("mode", False)
    tom_confidence_boost = tom_cfg.get("confidence_boost", 0.05)

    # TOM boost mode: add confidence during TOM window
    if tom_mode == "boost" and tom_in_window:
        for _sig in signals:
            _orig = _sig.confidence
            _sig.confidence = min(1.0, _sig.confidence + tom_confidence_boost)
            _sig.features["tom_boost"] = round(tom_confidence_boost, 4)
            _sig.features["tom_confidence_orig"] = round(_orig, 4)
    # Tag all signals with TOM window info
    for _sig in signals:
        _sig.features["tom_in_window"] = tom_in_window


def inject_breadth_features(
    signals: list,
    breadth_series: Optional[pd.DataFrame],
    today: pd.Timestamp,
    regime: str = "neutral",
    regime_scale: float = 1.0,
) -> None:
    """Inject market breadth features into signal.features (in-place).

    Replicates Phase 7C breadth injection block in engine._simulate_day.
    Adds keys: breadth_pct_above_50ma, breadth_pct_above_200ma,
    breadth_ad_ratio, breadth_thrust, breadth_momentum,
    breadth_net_new_highs_pct, regime, regime_scale.

    When today has no breadth data, signals are left unchanged.

    Args:
        signals:       List of Signal objects for one strategy batch.
        breadth_series: Precomputed breadth DataFrame (or None).
        today:         Current simulation date.
        regime:        Regime string from _compute_regime ("bull"/"neutral"/"bear").
        regime_scale:  Regime scale factor from _compute_regime.
    """
    if breadth_series is not None and today in breadth_series.index:
        _brd = breadth_series.loc[today]
        for _sig in signals:
            _sig.features["breadth_pct_above_50ma"] = float(_brd.get("pct_above_50ma", 0))
            _sig.features["breadth_pct_above_200ma"] = float(_brd.get("pct_above_200ma", 0))
            _sig.features["breadth_ad_ratio"] = float(_brd.get("ad_ratio", 0))
            _sig.features["breadth_thrust"] = float(_brd.get("breadth_thrust", 0))
            _sig.features["breadth_momentum"] = (
                float(_brd.get("breadth_momentum", 0))
                if not pd.isna(_brd.get("breadth_momentum", 0))
                else 0.0
            )
            _sig.features["breadth_net_new_highs_pct"] = float(_brd.get("net_new_highs_pct", 0))
            _sig.features["regime"] = regime
            _sig.features["regime_scale"] = regime_scale


def apply_breadth_confidence(
    signals: list,
    strategies_cfg: Dict[str, Any],
) -> None:
    """Apply breadth-based confidence modifiers (in-place).

    Replicates Phase 7C breadth confidence modifier block in engine._simulate_day.
    For each signal, looks up the per-strategy breadth config under
    ``strategies_cfg[signal.strategy]["breadth"]``.

    Adjusts confidence up/down based on breadth metric vs thresholds and
    records breadth_confidence_adj / breadth_confidence_orig on the signal.

    Args:
        signals:       List of Signal objects for one strategy batch.
        strategies_cfg: Dict mapping strategy_name -> strategy config dict
                        (i.e. config["strategies"]).
    """
    for _sig in signals:
        _strat_key = _sig.strategy  # e.g. 'trend_following', 'mean_reversion'
        _breadth_cfg = strategies_cfg.get(_strat_key, {}).get("breadth", {})
        if _breadth_cfg.get("enabled", False):
            _metric = _breadth_cfg.get("metric", "pct_above_50ma")
            _breadth_val = _sig.features.get(f"breadth_{_metric}", None)
            if _breadth_val is not None:
                _low_thresh = _breadth_cfg.get("low_threshold", 0.48)
                _high_thresh = _breadth_cfg.get("high_threshold", 0.58)
                _low_boost = _breadth_cfg.get("low_boost", 0.0)
                _high_penalty = _breadth_cfg.get("high_penalty", 0.0)
                _orig_conf = _sig.confidence
                _breadth_adj = 0.0
                if _breadth_val < _low_thresh:
                    _breadth_adj = _low_boost
                elif _breadth_val > _high_thresh:
                    _breadth_adj = -_high_penalty
                if _breadth_adj != 0.0:
                    _sig.confidence = max(0.0, min(1.0, _sig.confidence + _breadth_adj))
                    _sig.features["breadth_confidence_adj"] = round(_breadth_adj, 4)
                    _sig.features["breadth_confidence_orig"] = round(_orig_conf, 4)
                    logger.debug(
                        f"BREADTH {_sig.ticker} ({_strat_key}): "
                        f"breadth={_breadth_val:.2f}, adj={_breadth_adj:+.3f}, "
                        f"conf {_orig_conf:.3f} -> {_sig.confidence:.3f}"
                    )


def inject_rs_features(
    signals: list,
    rs_data: Optional[Dict],
    yesterday: pd.Timestamp,
) -> None:
    """Inject relative strength features into signal.features (in-place).

    Replicates Phase 7B RS feature injection block in engine._simulate_day.
    Adds keys: rs_percentile, rs_score, rs_momentum, roc_20, roc_60, roc_120.

    Uses yesterday's date for RS lookup (signal generation date).

    Args:
        signals:   List of Signal objects for one strategy batch.
        rs_data:   Dict mapping ticker -> RS DataFrame (or None).
        yesterday: The lookback date for RS data selection.
    """
    if rs_data is not None:
        for _sig in signals:
            _ticker = _sig.ticker
            if _ticker in rs_data:
                _rs_df = rs_data[_ticker]
                # Use yesterday's date for RS lookup (signal generation date)
                _rs_dates = _rs_df.index[_rs_df.index <= yesterday]
                if len(_rs_dates) > 0:
                    _rs_date = _rs_dates[-1]
                    _rs_row = _rs_df.loc[_rs_date]
                    _sig.features["rs_percentile"] = (
                        float(_rs_row.get("rs_percentile", 50.0))
                        if not pd.isna(_rs_row.get("rs_percentile", 50.0))
                        else 50.0
                    )
                    _sig.features["rs_score"] = (
                        float(_rs_row.get("rs_score", 0.0))
                        if not pd.isna(_rs_row.get("rs_score", 0.0))
                        else 0.0
                    )
                    _sig.features["rs_momentum"] = (
                        float(_rs_row.get("rs_momentum", 0.0))
                        if not pd.isna(_rs_row.get("rs_momentum", 0.0))
                        else 0.0
                    )
                    _sig.features["roc_20"] = (
                        float(_rs_row.get("roc_20", 0.0))
                        if not pd.isna(_rs_row.get("roc_20", 0.0))
                        else 0.0
                    )
                    _sig.features["roc_60"] = (
                        float(_rs_row.get("roc_60", 0.0))
                        if not pd.isna(_rs_row.get("roc_60", 0.0))
                        else 0.0
                    )
                    _sig.features["roc_120"] = (
                        float(_rs_row.get("roc_120", 0.0))
                        if not pd.isna(_rs_row.get("roc_120", 0.0))
                        else 0.0
                    )


def apply_rs_confidence(
    signals: list,
    strategies_cfg: Dict[str, Any],
) -> None:
    """Apply RS-based confidence modifiers (in-place).

    Replicates Phase 7B RS confidence modifier block in engine._simulate_day.
    For each signal, looks up the per-strategy RS config under
    ``strategies_cfg[signal.strategy]["relative_strength"]``.

    Adjusts confidence up/down based on RS metric vs thresholds and
    records rs_confidence_adj / rs_confidence_orig on the signal.

    Args:
        signals:       List of Signal objects for one strategy batch.
        strategies_cfg: Dict mapping strategy_name -> strategy config dict
                        (i.e. config["strategies"]).
    """
    for _sig in signals:
        _strat_key = _sig.strategy
        _rs_cfg = strategies_cfg.get(_strat_key, {}).get("relative_strength", {})
        if _rs_cfg.get("enabled", False):
            _rs_metric = _rs_cfg.get("metric", "rs_percentile")
            _rs_val = _sig.features.get(_rs_metric, None)
            if _rs_val is not None:
                _rs_low_thresh = _rs_cfg.get("low_threshold", 40.0)
                _rs_high_thresh = _rs_cfg.get("high_threshold", 60.0)
                _rs_low_penalty = _rs_cfg.get("low_penalty", 0.0)
                _rs_high_boost = _rs_cfg.get("high_boost", 0.0)
                _rs_orig_conf = _sig.confidence
                _rs_adj = 0.0
                if _rs_val < _rs_low_thresh:
                    _rs_adj = -_rs_low_penalty
                elif _rs_val > _rs_high_thresh:
                    _rs_adj = _rs_high_boost
                if _rs_adj != 0.0:
                    _sig.confidence = max(0.0, min(1.0, _sig.confidence + _rs_adj))
                    _sig.features["rs_confidence_adj"] = round(_rs_adj, 4)
                    _sig.features["rs_confidence_orig"] = round(_rs_orig_conf, 4)
                    logger.debug(
                        f"RS {_sig.ticker} ({_strat_key}): "
                        f"rs={_rs_val:.1f}, adj={_rs_adj:+.3f}, "
                        f"conf {_rs_orig_conf:.3f} -> {_sig.confidence:.3f}"
                    )
