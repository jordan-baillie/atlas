"""
ETF Flow Proxy — Volume Z-Score Rotation Detection
=====================================================
Uses volume anomalies in sector ETFs to detect institutional rotation.
Computed entirely from existing OHLCV data (no new data sources).

A surge in cyclical ETF volume with declining defensive volume
signals risk-on rotation. The opposite signals risk-off.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# The 11 SPDR Select Sector ETFs
SECTOR_ETFS = {
    "XLB": "Materials",
    "XLC": "Communication Services",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLI": "Industrials",
    "XLK": "Technology",
    "XLP": "Consumer Staples",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}

# Classification
CYCLICAL_ETFS = {"XLK", "XLF", "XLI", "XLY"}
DEFENSIVE_ETFS = {"XLU", "XLP", "XLV"}

# Thresholds
SURGE_THRESHOLD = 2.0    # z-score > 2.0 = volume surge
DROUGHT_THRESHOLD = -1.5  # z-score < -1.5 = volume drought
LOOKBACK_DAYS = 20       # 20-day rolling average for normalization


def compute_volume_zscores(
    end_date: Optional[date] = None,
    lookback: int = LOOKBACK_DAYS,
) -> pd.DataFrame:
    """Compute volume z-scores for all sector ETFs.

    Uses Atlas DB OHLCV data (primary) or yfinance (fallback).
    Computes z-scores relative to 20-day rolling mean/std.

    Returns DataFrame with columns: ticker, name, volume, avg_volume_20d,
    volume_zscore, signal (surge/drought/normal)
    """
    if end_date is None:
        end_date = date.today()

    start = end_date - timedelta(days=60)  # ~40 trading days buffer
    tickers = list(SECTOR_ETFS.keys())

    # Try Atlas DB first
    vol_data = _load_volumes_from_db(tickers, start, end_date)

    # Fallback to yfinance if DB has insufficient data
    if vol_data is None or len(vol_data) < lookback + 1:
        logger.info("ETF flows: DB data insufficient, falling back to yfinance")
        vol_data = _load_volumes_from_yfinance(tickers, start, end_date)

    if vol_data is None or vol_data.empty:
        logger.warning("ETF volume: no data from any source")
        return pd.DataFrame()

    results = []
    for ticker in tickers:
        try:
            if ticker not in vol_data.columns:
                continue

            vol = vol_data[ticker].dropna()
            if len(vol) < lookback + 1:
                logger.warning(f"{ticker}: only {len(vol)} rows, need {lookback + 1}")
                continue

            rolling_mean = vol.rolling(lookback).mean()
            rolling_std = vol.rolling(lookback).std()

            latest_vol = vol.iloc[-1]
            mean_val = rolling_mean.iloc[-1]
            std_val = rolling_std.iloc[-1]

            if std_val > 0:
                zscore = float((latest_vol - mean_val) / std_val)
            else:
                zscore = 0.0

            signal = "normal"
            if zscore > SURGE_THRESHOLD:
                signal = "surge"
            elif zscore < DROUGHT_THRESHOLD:
                signal = "drought"

            results.append({
                "ticker": ticker,
                "name": SECTOR_ETFS[ticker],
                "volume": int(latest_vol),
                "avg_volume_20d": int(mean_val),
                "volume_zscore": round(zscore, 2),
                "signal": signal,
                "date": vol.index[-1].strftime("%Y-%m-%d") if hasattr(vol.index[-1], 'strftime') else str(vol.index[-1]),
            })
        except Exception as e:
            logger.warning(f"Failed to compute z-score for {ticker}: {e}")

    return pd.DataFrame(results)


def _load_volumes_from_db(tickers: list, start: date, end: date) -> Optional[pd.DataFrame]:
    """Load volume data from Atlas DB OHLCV table."""
    try:
        from db.atlas_db import get_db
        placeholders = ",".join("?" * len(tickers))
        with get_db() as db:
            rows = db.execute(
                f"SELECT date, ticker, volume FROM ohlcv "
                f"WHERE ticker IN ({placeholders}) "
                f"AND date >= ? AND date <= ? "
                f"ORDER BY date",
                tickers + [start.isoformat(), end.isoformat()],
            ).fetchall()

        if not rows:
            return None

        df = pd.DataFrame([dict(r) for r in rows])
        df["date"] = pd.to_datetime(df["date"])
        pivot = df.pivot_table(index="date", columns="ticker", values="volume", aggfunc="first")
        return pivot
    except Exception as e:
        logger.warning(f"ETF flows DB load failed: {e}")
        return None


def _load_volumes_from_yfinance(tickers: list, start: date, end: date) -> Optional[pd.DataFrame]:
    """Fallback: load volume data from yfinance."""
    try:
        import yfinance as yf
        raw = yf.download(
            tickers,
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        if raw.empty:
            return None

        # Extract volume columns
        if isinstance(raw.columns, pd.MultiIndex):
            vol_df = raw["Volume"] if "Volume" in raw.columns.get_level_values(0) else None
            if vol_df is None:
                return None
            return vol_df
        else:
            return raw[["Volume"]].rename(columns={"Volume": tickers[0]}) if len(tickers) == 1 else None
    except Exception as e:
        logger.warning(f"yfinance fallback failed: {e}")
        return None


def detect_rotation(zscores_df: pd.DataFrame) -> dict:
    """Detect risk-on/risk-off rotation pattern.

    Risk-on: cyclical ETFs surging + defensive ETFs draining
    Risk-off: defensive ETFs surging + cyclical ETFs draining

    Returns dict with rotation_signal, confidence, cyclical_avg_zscore,
    defensive_avg_zscore, details
    """
    if zscores_df.empty:
        return {"rotation_signal": "neutral", "confidence": 0.0, "details": "No data"}

    cyclical = zscores_df[zscores_df["ticker"].isin(CYCLICAL_ETFS)]
    defensive = zscores_df[zscores_df["ticker"].isin(DEFENSIVE_ETFS)]

    cyc_avg_z = float(cyclical["volume_zscore"].mean()) if not cyclical.empty else 0.0
    def_avg_z = float(defensive["volume_zscore"].mean()) if not defensive.empty else 0.0

    # Count surges and droughts
    cyc_surges = len(cyclical[cyclical["signal"] == "surge"])
    def_surges = len(defensive[defensive["signal"] == "surge"])
    cyc_droughts = len(cyclical[cyclical["signal"] == "drought"])
    def_droughts = len(defensive[defensive["signal"] == "drought"])

    # Determine rotation direction
    rotation = "neutral"
    confidence = 0.0

    # Risk-on: cyclicals elevated, defensives flat/negative
    if cyc_avg_z > 1.0 and def_avg_z < 0.5:
        rotation = "risk_on"
        confidence = min(1.0, (cyc_avg_z - def_avg_z) / 3.0)
    # Risk-off: defensives elevated, cyclicals flat/negative
    elif def_avg_z > 1.0 and cyc_avg_z < 0.5:
        rotation = "risk_off"
        confidence = min(1.0, (def_avg_z - cyc_avg_z) / 3.0)
    # Strong divergence signals
    elif cyc_surges >= 2 and def_droughts >= 1:
        rotation = "risk_on"
        confidence = 0.7
    elif def_surges >= 2 and cyc_droughts >= 1:
        rotation = "risk_off"
        confidence = 0.7

    details = (
        f"Cyclical avg z={cyc_avg_z:.2f} ({cyc_surges} surges, {cyc_droughts} droughts), "
        f"Defensive avg z={def_avg_z:.2f} ({def_surges} surges, {def_droughts} droughts)"
    )

    return {
        "rotation_signal": rotation,
        "confidence": round(confidence, 2),
        "cyclical_avg_zscore": round(cyc_avg_z, 2),
        "defensive_avg_zscore": round(def_avg_z, 2),
        "details": details,
    }


def get_etf_flow_signal(end_date: Optional[date] = None) -> dict:
    """Get the current ETF flow rotation signal.

    Returns dict with:
        - rotation_signal: "risk_on", "risk_off", or "neutral"
        - confidence: 0.0 to 1.0
        - cyclical_avg_zscore: average z-score of cyclical ETFs
        - defensive_avg_zscore: average z-score of defensive ETFs
        - zscores: list of per-ETF z-score dicts
        - details: human-readable summary
    """
    zscores_df = compute_volume_zscores(end_date=end_date)
    rotation = detect_rotation(zscores_df)

    return {
        "rotation_signal": rotation["rotation_signal"],
        "confidence": rotation["confidence"],
        "cyclical_avg_zscore": rotation.get("cyclical_avg_zscore", 0.0),
        "defensive_avg_zscore": rotation.get("defensive_avg_zscore", 0.0),
        "details": rotation["details"],
        "zscores": zscores_df.to_dict("records") if not zscores_df.empty else [],
    }


if __name__ == "__main__":
    signal = get_etf_flow_signal()
    print(f"Rotation: {signal['rotation_signal']} (confidence={signal['confidence']})")
    print(f"Details: {signal['details']}")
    if signal["zscores"]:
        print("\nPer-ETF Z-Scores:")
        for z in sorted(signal["zscores"], key=lambda x: x["volume_zscore"], reverse=True):
            print(f"  {z['ticker']:4s} ({z['name']:24s}): z={z['volume_zscore']:+.2f} [{z['signal']}]")
