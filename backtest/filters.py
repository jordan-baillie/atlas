"""Backtest entry gate filters — extracted from engine._simulate_day.

Each filter returns (blocked: bool, reason: str, metadata: dict).
All filters are pure functions — no side effects, no class state mutation.
"""
import logging
import pandas as pd
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def check_vix_gate(
    vix_series: Optional[pd.Series],
    yesterday: pd.Timestamp,
    vix_max_entry: float,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Check if VIX exceeds entry threshold.

    Replicates the VIX regime filter block in engine._simulate_day.

    Returns:
        (blocked, reason, {"current_vix": float})
        blocked=True when VIX data is present and VIX > vix_max_entry.
        When data is absent, returns blocked=False (fail-open).
    """
    if vix_series is None or yesterday not in vix_series.index:
        return False, "", {}
    current_vix = float(vix_series.loc[yesterday])
    if current_vix > vix_max_entry:
        return (
            True,
            f"VIX {current_vix:.1f} > {vix_max_entry}",
            {"current_vix": current_vix},
        )
    return False, "", {"current_vix": current_vix}


def check_fred_macro(
    fred_yield_curve: Optional[pd.Series],
    fred_claims: Optional[pd.Series],
    yesterday: pd.Timestamp,
    fred_cfg: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, Any]]:
    """Check FRED macro indicators (yield curve inversion, claims spike).

    Replicates the FRED macro regime filter block in engine._simulate_day.
    Uses ``fred_cfg`` keys:
      - ``yield_curve_min``: float or None — block when T10Y2Y < this
      - ``claims_max``:      float or None — block when ICSA > this

    Returns:
        (blocked, reason, metadata)
        blocked=True when any adverse macro condition is detected.
        fail-open when data or config is absent.
    """
    fred_yield_curve_min = fred_cfg.get("yield_curve_min", None)
    fred_claims_max = fred_cfg.get("claims_max", None)

    fred_blocked = False
    if fred_yield_curve is not None and fred_yield_curve_min is not None:
        # Use latest available value on or before yesterday
        yc_mask = fred_yield_curve.index <= yesterday
        if yc_mask.any():
            yc_val = float(fred_yield_curve.loc[yc_mask].iloc[-1])
            if yc_val < fred_yield_curve_min:
                fred_blocked = True
    if fred_claims is not None and fred_claims_max is not None and not fred_blocked:
        cl_mask = fred_claims.index <= yesterday
        if cl_mask.any():
            cl_val = float(fred_claims.loc[cl_mask].iloc[-1])
            if cl_val > fred_claims_max:
                fred_blocked = True

    reason = "FRED macro conditions adverse" if fred_blocked else ""
    return fred_blocked, reason, {}


def check_turn_of_month(
    today: pd.Timestamp,
    trading_dates: pd.DatetimeIndex,
    tom_cfg: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, Any]]:
    """Check turn-of-month (TOM) calendar filter.

    Replicates the TOM filter block in engine._simulate_day, inlining the
    logic from engine._is_tom_window.  Uses ``tom_cfg`` keys:
      - ``mode``:                  False | True | "boost"
      - ``days_before_month_end``: int (default 5)
      - ``days_after_month_start``: int (default 3)

    Returns:
        (blocked, reason, {"tom_in_window": bool})
        blocked=True only when mode=True and today is outside the TOM window.
        "boost" mode never blocks — it only sets tom_in_window for callers.
    """
    tom_mode = tom_cfg.get("mode", False)
    tom_days_before_end = tom_cfg.get("days_before_month_end", 5)
    tom_days_after_start = tom_cfg.get("days_after_month_start", 3)

    # Replicate _is_tom_window logic
    tom_in_window = False
    if tom_mode:
        month = today.month
        year = today.year

        # Trading days in the same month as `today`
        same_month = trading_dates[
            (trading_dates.month == month) & (trading_dates.year == year)
        ]
        if len(same_month) > 0:
            # Check: is today within the last N trading days of its month?
            last_n = same_month[-tom_days_before_end:]
            if today in last_n:
                tom_in_window = True

            if not tom_in_window:
                # Check: is today within the first M trading days of its month?
                first_m = same_month[:tom_days_after_start]
                if today in first_m:
                    tom_in_window = True

    tom_blocked = tom_mode is True and not tom_in_window
    if tom_blocked:
        logger.debug(f"TOM BLOCKED {today.date()}: outside turn-of-month window")

    reason = "outside turn-of-month window" if tom_blocked else ""
    return tom_blocked, reason, {"tom_in_window": tom_in_window}


def check_macro_regime(
    macro_signals: Optional[pd.DataFrame],
    yesterday: pd.Timestamp,
    today: pd.Timestamp,
    macro_cfg: Dict[str, Any],
) -> Tuple[bool, str, Dict[str, float]]:
    """Check macro regime filter (gold/copper, VIX ROC, yield curve).

    Replicates the macro regime filter block in engine._simulate_day.
    Uses ``macro_cfg`` keys:
      - ``enabled``:  bool
      - ``mode``:     "sizing" | "gate" | "boost"

    Returns:
        (blocked, reason, {"macro_scale": float, "macro_boost": float})
        blocked=True only when mode="gate" and macro_regime_scale < 0.7.
        macro_boost is 0.05 when mode="boost" and scale > 1.2.
    """
    macro_regime_enabled = macro_cfg.get("enabled", False)
    macro_mode = macro_cfg.get("mode", "sizing")

    macro_blocked = False
    macro_scale_today = 1.0
    macro_boost_today = 0.0

    if macro_signals is not None and macro_regime_enabled:
        # Use latest available macro data on or before yesterday
        _macro_mask = macro_signals.index <= yesterday
        if _macro_mask.any():
            _macro_row = macro_signals.loc[_macro_mask].iloc[-1]
            macro_scale_today = float(_macro_row.get("macro_regime_scale", 1.0))
            _gc_regime = int(_macro_row.get("gc_regime", 2))
            _vix_roc = float(_macro_row.get("vix_roc_5d", 0.0))
            _vix_spike = bool(_macro_row.get("vix_spike", False))
            _yc_spread = float(_macro_row.get("yield_curve_10y_3m", 0.0))
            _yc_flatten = bool(_macro_row.get("yc_flattening", False))

            if macro_mode == "gate" and macro_scale_today < 0.7:
                macro_blocked = True
                logger.debug(
                    f"MACRO GATE BLOCKED {today.date()}: "
                    f"scale={macro_scale_today:.2f} < 0.7 "
                    f"(gc_regime={_gc_regime}, vix_roc={_vix_roc:.2%}, "
                    f"yc_spread={_yc_spread:.3f})"
                )
            elif macro_mode == "boost" and macro_scale_today > 1.2:
                macro_boost_today = 0.05
            logger.debug(
                f"MACRO {today.date()}: scale={macro_scale_today:.2f}, "
                f"gc_regime={_gc_regime}, vix_roc={_vix_roc:.2%}, "
                f"yc={_yc_spread:.3f}, mode={macro_mode}"
            )

    reason = (
        f"macro gate blocked (scale={macro_scale_today:.2f})" if macro_blocked else ""
    )
    return (
        macro_blocked,
        reason,
        {"macro_scale": macro_scale_today, "macro_boost": macro_boost_today},
    )
