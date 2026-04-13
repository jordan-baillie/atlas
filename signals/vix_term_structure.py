"""VIX term structure signal generation.

VIX/VIX3M ratio classification:
  < 0.95: strong contango (risk-on)
  0.95-1.00: contango
  1.00-1.05: flat
  1.05-1.20: backwardation
  > 1.20: extreme backwardation (panic)

Persistent backwardation (3+ days) flags risk-off regime.
"""
from __future__ import annotations
import logging
import statistics
from datetime import date, timedelta
from typing import Optional

from db.atlas_db import get_db

logger = logging.getLogger(__name__)


def classify_term_structure(ratio: float) -> str:
    """Classify the term structure ratio into a regime label."""
    if ratio is None:
        return "unknown"
    if ratio < 0.95:
        return "strong_contango"
    elif ratio < 1.00:
        return "contango"
    elif ratio < 1.05:
        return "flat"
    elif ratio < 1.20:
        return "backwardation"
    else:
        return "extreme_backwardation"


def get_vix_term_structure(end_date: Optional[date] = None, lookback_days: int = 90) -> list:
    """Fetch VIX and VIX3M data and compute the ratio over a window."""
    end = end_date or date.today()
    start = end - timedelta(days=lookback_days * 2)  # Buffer for weekends/holidays

    with get_db() as db:
        rows = db.execute(
            "SELECT date, vix, vix3m FROM macro_indicators "
            "WHERE date BETWEEN ? AND ? AND vix IS NOT NULL AND vix3m IS NOT NULL "
            "ORDER BY date",
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    result = []
    for r in rows:
        vix = float(r["vix"])
        vix3m = float(r["vix3m"])
        if vix3m <= 0:
            continue
        ratio = vix / vix3m
        result.append({
            "date": r["date"],
            "vix": round(vix, 2),
            "vix3m": round(vix3m, 2),
            "ratio": round(ratio, 4),
            "regime": classify_term_structure(ratio),
        })

    return result[-lookback_days:] if len(result) > lookback_days else result


def compute_persistence(history: list) -> int:
    """Count consecutive days in the current regime (including today)."""
    if not history:
        return 0
    current_regime = history[-1]["regime"]
    persistence = 1
    for i in range(len(history) - 2, -1, -1):
        if history[i]["regime"] == current_regime:
            persistence += 1
        else:
            break
    return persistence


def compute_slope_roc(history: list, period: int = 5) -> Optional[float]:
    """Rate of change of the term structure ratio over `period` days.

    Positive = ratio increasing (moving toward backwardation)
    Negative = ratio decreasing (moving toward contango)

    Returns percentage change, or None if insufficient data.
    """
    if len(history) < period + 1:
        return None
    current_ratio = history[-1]["ratio"]
    prior_ratio = history[-(period + 1)]["ratio"]
    if prior_ratio == 0:
        return None
    return round((current_ratio - prior_ratio) / prior_ratio * 100, 4)


def get_current_signal() -> dict:
    """Get the current VIX term structure signal with persistence and action."""
    history = get_vix_term_structure(lookback_days=30)

    if not history:
        return {"error": "No VIX data available"}

    current = history[-1]
    persistence = compute_persistence(history)
    regime = current["regime"]

    # Compute slope ROC
    slope_roc = compute_slope_roc(history, period=5)
    slope_roc_10d = compute_slope_roc(history, period=10)

    # Severe stress: ratio > 1.05 means VIX is 5%+ above VIX3M
    severe_stress = current["ratio"] > 1.05

    # Rapidly deteriorating: slope ROC > 3% over 5 days
    rapidly_deteriorating = slope_roc is not None and slope_roc > 3.0

    # Determine signal action
    if regime == "extreme_backwardation" or (severe_stress and persistence >= 3):
        action = "REDUCE_GROSS"
        severity = "high"
    elif regime == "backwardation" and persistence >= 3:
        action = "REDUCE_GROSS"
        severity = "medium"
    elif severe_stress or regime == "extreme_backwardation":
        action = "WATCH"
        severity = "high"
    elif rapidly_deteriorating:
        action = "WATCH"
        severity = "medium"
    elif regime == "flat":
        action = "WATCH"
        severity = "low"
    else:
        action = "NORMAL"
        severity = "low"

    ratios = [h["ratio"] for h in history]
    mean_30d = statistics.mean(ratios)

    return {
        "as_of": current["date"],
        "vix": current["vix"],
        "vix3m": current["vix3m"],
        "ratio": current["ratio"],
        "regime": current["regime"],
        "persistence_days": persistence,
        "action": action,
        "severity": severity,
        "ratio_30d_mean": round(mean_30d, 4),
        "ratio_30d_max": round(max(ratios), 4),
        "ratio_30d_min": round(min(ratios), 4),
        "slope_roc_5d": slope_roc,
        "slope_roc_10d": slope_roc_10d,
        "severe_stress": severe_stress,
        "rapidly_deteriorating": rapidly_deteriorating,
        "history": history,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal = get_current_signal()
    if "error" in signal:
        print(signal["error"])
        raise SystemExit(1)

    print("\nVIX TERM STRUCTURE SIGNAL")
    print("=" * 60)
    print(f"As of:       {signal['as_of']}")
    print(f"VIX:         {signal['vix']:.2f}")
    print(f"VIX3M:       {signal['vix3m']:.2f}")
    print(f"Ratio:       {signal['ratio']:.4f}")
    print(f"Regime:      {signal['regime']}")
    print(f"Persistence: {signal['persistence_days']} days")
    print(f"Action:      {signal['action']}")
    print(f"Severity:    {signal['severity']}")
    print()
    print(f"30d Mean:    {signal['ratio_30d_mean']:.4f}")
    print(f"30d Max:     {signal['ratio_30d_max']:.4f}")
    print(f"30d Min:     {signal['ratio_30d_min']:.4f}")
    print(f"Slope ROC 5d: {signal.get('slope_roc_5d', 'N/A')}")
    print(f"Slope ROC 10d: {signal.get('slope_roc_10d', 'N/A')}")
    print(f"Severe Stress: {signal['severe_stress']}")
    print(f"Rapidly Deteriorating: {signal['rapidly_deteriorating']}")
    print(f"History:     {len(signal['history'])} days")
    print("=" * 60)
