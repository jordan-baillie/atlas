"""
Atlas FRED Economic Data Integration
=========================================
Fetches macro regime indicators from the FRED API for use as
optional trading filters alongside VIX.

Supported series:
  - GS10, GS2  → yield curve slope (10Y-2Y spread)
  - ICSA       → initial unemployment claims
  - FEDFUNDS   → federal funds effective rate
  - T10Y2Y     → 10Y-2Y spread (pre-computed by FRED)
  - VIXCLS     → CBOE VIX (alternative to ^VIX from Yahoo)

Data is cached locally to avoid repeated API calls.

Usage:
    from data.fred import FREDClient

    fred = FREDClient()  # reads key from ~/.atlas-secrets.json
    yc = fred.get_yield_curve_slope()
    claims = fred.get_unemployment_claims()
    ffr = fred.get_fed_funds_rate()

    # All-in-one regime snapshot
    regime = fred.get_regime_snapshot()
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

SECRETS_PATH = Path.home() / ".atlas-secrets.json"
CACHE_DIR = Path(__file__).resolve().parent / "cache" / "fred"
FRED_BASE_URL = "https://api.stlouisfed.org/fred"

# Series definitions — easy to extend
SERIES_REGISTRY = {
    "T10Y2Y": {
        "name": "Yield Curve Slope (10Y-2Y)",
        "frequency": "daily",
        "description": "10-Year minus 2-Year Treasury spread. Negative = inverted = recession signal.",
    },
    "GS10": {
        "name": "10-Year Treasury Rate",
        "frequency": "daily",
        "description": "Constant maturity 10-year Treasury yield.",
    },
    "GS2": {
        "name": "2-Year Treasury Rate",
        "frequency": "daily",
        "description": "Constant maturity 2-year Treasury yield.",
    },
    "ICSA": {
        "name": "Initial Unemployment Claims",
        "frequency": "weekly",
        "description": "Initial claims for unemployment insurance. Spikes signal economic stress.",
    },
    "FEDFUNDS": {
        "name": "Federal Funds Rate",
        "frequency": "monthly",
        "description": "Effective federal funds rate. Rising = tightening = risk-off.",
    },
    "VIXCLS": {
        "name": "CBOE VIX",
        "frequency": "daily",
        "description": "CBOE Volatility Index (closing). Alternative to ^VIX from Yahoo.",
    },
}

# Default series for regime analysis
DEFAULT_REGIME_SERIES = ["T10Y2Y", "ICSA", "FEDFUNDS", "VIXCLS"]


class FREDClient:
    """Lightweight FRED API client with local caching."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or self._load_api_key()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_api_key() -> Optional[str]:
        """Load FRED API key from secrets file."""
        if not SECRETS_PATH.exists():
            return None
        try:
            with open(SECRETS_PATH) as f:
                secrets = json.load(f)
            return secrets.get("fred_api_key") or secrets.get("FRED_API_KEY")
        except Exception:
            return None

    @property
    def available(self) -> bool:
        """Check if FRED API key is configured."""
        return bool(self.api_key)

    def fetch_series(
        self,
        series_id: str,
        observation_start: Optional[str] = None,
        observation_end: Optional[str] = None,
        max_age_hours: int = 12,
    ) -> pd.Series:
        """Fetch a FRED series, using local cache if fresh enough.

        Args:
            series_id: FRED series ID (e.g., 'T10Y2Y').
            observation_start: Start date 'YYYY-MM-DD' (default: 5 years ago).
            observation_end: End date 'YYYY-MM-DD' (default: today).
            max_age_hours: Cache validity in hours.

        Returns:
            pd.Series indexed by date with float values.
            Empty Series if API key missing or request fails.
        """
        if not self.api_key:
            logger.warning("FRED API key not configured — skipping %s", series_id)
            return pd.Series(dtype=float)

        cache_path = CACHE_DIR / f"{series_id}.parquet"

        # Check cache freshness
        if cache_path.exists():
            age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
            if age < timedelta(hours=max_age_hours):
                try:
                    df = pd.read_parquet(cache_path)
                    return df.iloc[:, 0]
                except Exception as e:
                    logger.debug("Cache read failed for %s: %s", series_id, e)

        # Fetch from API
        if not observation_start:
            observation_start = (datetime.now() - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
        if not observation_end:
            observation_end = datetime.now().strftime("%Y-%m-%d")

        try:
            resp = requests.get(
                f"{FRED_BASE_URL}/series/observations",
                params={
                    "api_key": self.api_key,
                    "series_id": series_id,
                    "file_type": "json",
                    "observation_start": observation_start,
                    "observation_end": observation_end,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("FRED API request failed for %s: %s", series_id, e)
            # Fall back to stale cache if available
            if cache_path.exists():
                try:
                    df = pd.read_parquet(cache_path)
                    logger.info("Using stale cache for %s", series_id)
                    return df.iloc[:, 0]
                except Exception:
                    pass
            return pd.Series(dtype=float)

        # Parse observations
        observations = data.get("observations", [])
        records = []
        for obs in observations:
            val = obs.get("value", ".")
            if val == "." or val == "":
                continue
            try:
                records.append({"date": pd.Timestamp(obs["date"]), "value": float(val)})
            except (ValueError, KeyError):
                continue

        if not records:
            logger.warning("FRED %s returned 0 valid observations", series_id)
            return pd.Series(dtype=float)

        df = pd.DataFrame(records).set_index("date").sort_index()

        # Cache
        try:
            df.to_parquet(cache_path)
            logger.info("Cached FRED %s: %d observations", series_id, len(df))
        except Exception as e:
            logger.debug("Cache write failed for %s: %s", series_id, e)

        return df["value"]

    # ── Convenience accessors ──

    def get_yield_curve_slope(self, **kwargs) -> pd.Series:
        """Get the 10Y-2Y Treasury spread (yield curve slope).

        Negative values indicate an inverted yield curve — historically
        one of the strongest recession predictors.
        """
        return self.fetch_series("T10Y2Y", **kwargs)

    def get_unemployment_claims(self, **kwargs) -> pd.Series:
        """Get initial unemployment claims (weekly).

        Sharp increases signal economic distress. Values above 300K
        are historically associated with economic weakness.
        """
        return self.fetch_series("ICSA", **kwargs)

    def get_fed_funds_rate(self, **kwargs) -> pd.Series:
        """Get the effective federal funds rate (monthly).

        Rising rates = tightening monetary policy = headwind for equities.
        """
        return self.fetch_series("FEDFUNDS", **kwargs)

    def get_vix(self, **kwargs) -> pd.Series:
        """Get CBOE VIX from FRED (alternative to Yahoo ^VIX)."""
        return self.fetch_series("VIXCLS", **kwargs)

    def get_10y_yield(self, **kwargs) -> pd.Series:
        """Get the 10-Year Treasury constant maturity rate."""
        return self.fetch_series("GS10", **kwargs)

    def get_2y_yield(self, **kwargs) -> pd.Series:
        """Get the 2-Year Treasury constant maturity rate."""
        return self.fetch_series("GS2", **kwargs)

    def get_regime_snapshot(self) -> Dict[str, Any]:
        """Get a comprehensive macro regime snapshot.

        Returns latest values for all default regime series plus
        derived signals (yield curve inversion, claims trend, etc.).
        """
        snapshot = {"timestamp": datetime.now().isoformat(), "series": {}}

        for sid in DEFAULT_REGIME_SERIES:
            s = self.fetch_series(sid, max_age_hours=24)
            if len(s) == 0:
                snapshot["series"][sid] = {
                    "name": SERIES_REGISTRY.get(sid, {}).get("name", sid),
                    "latest": None,
                    "available": False,
                }
                continue

            latest = float(s.iloc[-1])
            latest_date = s.index[-1].strftime("%Y-%m-%d")

            entry = {
                "name": SERIES_REGISTRY.get(sid, {}).get("name", sid),
                "latest": latest,
                "latest_date": latest_date,
                "available": True,
            }

            # Derived signals
            if sid == "T10Y2Y":
                entry["inverted"] = latest < 0
                # Slope trend: 20-day SMA direction
                if len(s) >= 20:
                    entry["sma_20"] = round(float(s.iloc[-20:].mean()), 3)
                    entry["steepening"] = latest > entry["sma_20"]
            elif sid == "ICSA":
                # Claims trend: compare to 4-week average
                if len(s) >= 4:
                    entry["avg_4w"] = round(float(s.iloc[-4:].mean()), 0)
                    entry["rising"] = latest > entry["avg_4w"]
                    entry["elevated"] = latest > 300000
            elif sid == "FEDFUNDS":
                # Rate direction
                if len(s) >= 2:
                    entry["prev"] = float(s.iloc[-2])
                    entry["tightening"] = latest > entry["prev"]
            elif sid == "VIXCLS":
                entry["elevated"] = latest > 25
                entry["crisis"] = latest > 35

            snapshot["series"][sid] = entry

        # Overall regime assessment
        yc = snapshot["series"].get("T10Y2Y", {})
        claims = snapshot["series"].get("ICSA", {})
        vix = snapshot["series"].get("VIXCLS", {})
        ffr = snapshot["series"].get("FEDFUNDS", {})

        risk_signals = 0
        risk_details = []
        if yc.get("inverted"):
            risk_signals += 2
            risk_details.append("yield curve inverted")
        if claims.get("elevated"):
            risk_signals += 1
            risk_details.append("claims elevated")
        if claims.get("rising"):
            risk_signals += 1
            risk_details.append("claims rising")
        if vix.get("crisis"):
            risk_signals += 2
            risk_details.append("VIX crisis level")
        elif vix.get("elevated"):
            risk_signals += 1
            risk_details.append("VIX elevated")
        if ffr.get("tightening"):
            risk_signals += 1
            risk_details.append("rates tightening")

        if risk_signals >= 4:
            regime = "risk_off"
        elif risk_signals >= 2:
            regime = "cautious"
        else:
            regime = "risk_on"

        snapshot["regime"] = regime
        snapshot["risk_signals"] = risk_signals
        snapshot["risk_details"] = risk_details

        return snapshot

    @staticmethod
    def list_series() -> Dict[str, Dict[str, str]]:
        """List all registered FRED series with descriptions."""
        return SERIES_REGISTRY.copy()
