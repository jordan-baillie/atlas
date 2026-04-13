"""
Macro Surprise Index
====================
Tracks macro data surprises relative to recent trends.
Uses existing data from macro_indicators table — no new data sources.

Positive surprise composite → economic strength → overweight cyclicals
Negative surprise composite → economic weakness → defensive posture
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

import numpy as np
import pandas as pd

try:
    from db.atlas_db import get_db
except ModuleNotFoundError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from db.atlas_db import get_db

logger = logging.getLogger(__name__)

# Series to track for surprises (must exist in macro_indicators)
SURPRISE_SERIES = {
    "unemployment_claims": {
        "name": "Initial Claims",
        "invert": True,    # Lower claims = positive surprise
        "weight": 0.35,
    },
    "credit_oas": {
        "name": "Credit Spreads (OAS)",
        "invert": True,    # Lower spreads = positive surprise
        "weight": 0.25,
    },
    "fed_funds": {
        "name": "Fed Funds Rate",
        "invert": True,    # Lower rate = positive surprise (easing)
        "weight": 0.15,
    },
    "vix": {
        "name": "VIX",
        "invert": True,    # Lower VIX = positive surprise
        "weight": 0.15,
    },
    "yield_curve_10y2y": {
        "name": "Yield Curve (10y-2y)",
        "invert": False,   # Steeper curve = positive surprise
        "weight": 0.10,
    },
}

LOOKBACK_DAYS = 63  # ~3 months of trading days


def compute_macro_surprises(
    as_of_date: Optional[date] = None,
    lookback: int = LOOKBACK_DAYS,
) -> dict:
    """Compute macro surprise index from macro_indicators data.

    For each tracked series:
        surprise = (latest - trailing_mean) / trailing_std

    Composite = weighted sum of individual surprises (normalised by total weight).

    Returns dict with individual surprises + composite score.
    """
    if as_of_date is None:
        as_of_date = date.today()

    cols = ", ".join(["date"] + list(SURPRISE_SERIES.keys()))
    fetch_limit = lookback + 10  # extra buffer for gaps

    with get_db() as db:
        rows = db.execute(
            f"SELECT {cols} FROM macro_indicators "
            f"WHERE date <= ? ORDER BY date DESC LIMIT ?",
            (as_of_date.isoformat(), fetch_limit),
        ).fetchall()

    if len(rows) < 20:  # minimum data requirement
        return {
            "composite_surprise": 0.0,
            "signal": "neutral",
            "confidence": 0.0,
            "details": f"Insufficient data ({len(rows)} rows, need 20+)",
            "surprises": {},
        }

    # Convert to DataFrame (rows come back newest-first, so reverse)
    col_names = ["date"] + list(SURPRISE_SERIES.keys())
    df = pd.DataFrame([dict(r) for r in rows], columns=col_names)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")

    # Compute individual surprises
    surprises: dict = {}
    composite = 0.0
    total_weight = 0.0

    for series_name, config in SURPRISE_SERIES.items():
        col = df[series_name].dropna()
        if len(col) < 20:
            logger.debug(f"Skipping {series_name}: only {len(col)} non-null rows")
            continue

        # Trailing stats — exclude the latest observation
        trailing = col.iloc[:-1].tail(lookback)
        if len(trailing) < 10:
            continue

        mean = float(trailing.mean())
        std = float(trailing.std())
        latest = float(col.iloc[-1])

        z = (latest - mean) / std if std > 0 else 0.0

        # Invert if lower = positive surprise
        if config["invert"]:
            z = -z

        surprises[series_name] = {
            "name": config["name"],
            "latest": latest,
            "trailing_mean": round(mean, 4),
            "trailing_std": round(std, 4),
            "z_score": round(z, 2),
            "direction": "positive" if z > 0 else "negative",
        }

        composite += z * config["weight"]
        total_weight += config["weight"]

    # Normalise by total weight actually used
    if total_weight > 0:
        composite /= total_weight

    # Classify signal
    signal = "neutral"
    if composite > 0.5:
        signal = "positive"  # economic strength
    elif composite < -0.5:
        signal = "negative"  # economic weakness

    confidence = min(1.0, abs(composite) / 2.0)

    return {
        "composite_surprise": round(float(composite), 3),
        "signal": signal,
        "confidence": round(confidence, 2),
        "details": f"Composite surprise z={composite:.2f} from {len(surprises)} series",
        "surprises": surprises,
    }


def get_macro_surprise_signal(as_of_date: Optional[date] = None) -> dict:
    """Get the current macro surprise signal.

    Returns:
        dict with composite_surprise, signal, confidence, surprises,
        and regime_implication.
    """
    result = compute_macro_surprises(as_of_date=as_of_date)

    # Translate signal to regime posture
    if result["signal"] == "positive":
        result["regime_implication"] = "overweight_cyclicals"
    elif result["signal"] == "negative":
        result["regime_implication"] = "defensive_posture"
    else:
        result["regime_implication"] = "neutral"

    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    signal = get_macro_surprise_signal()
    print(f"Macro Surprise: {signal['signal']} (composite z={signal['composite_surprise']})")
    print(f"Confidence: {signal['confidence']}")
    print(f"Regime implication: {signal['regime_implication']}")
    print(f"\nIndividual surprises:")
    for name, s in signal.get("surprises", {}).items():
        print(
            f"  {s['name']:25s}: z={s['z_score']:+.2f} ({s['direction']}) "
            f"[latest={s['latest']:.4g}, mean={s['trailing_mean']:.4g}]"
        )
