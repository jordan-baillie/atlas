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
    "BAMLC0A0CM": {
        "name": "IG Corporate Bond OAS",
        "frequency": "daily",
        "description": (
            "ICE BofA US Corporate Index Option-Adjusted Spread (basis points). "
            "Widening = credit stress = risk-off signal."
        ),
    },
    "DTWEXBGS": {
        "name": "Trade-Weighted US Dollar Index (Broad, Goods)",
        "frequency": "daily",
        "description": (
            "Broad trade-weighted US dollar index. Rising USD = global liquidity "
            "tightening = headwind for risk assets and EM."
        ),
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

    def get_credit_oas(self, **kwargs) -> pd.Series:
        """Get IG corporate bond OAS (BAMLC0A0CM, basis points).

        The ICE BofA US Corporate Index Option-Adjusted Spread measures the
        yield premium of investment-grade corporate bonds over Treasuries.
        Widening spreads signal increasing credit stress and are a risk-off
        indicator for equities.
        """
        return self.fetch_series("BAMLC0A0CM", **kwargs)

    def get_dxy(self, **kwargs) -> pd.Series:
        """Get the broad trade-weighted US dollar index (DTWEXBGS).

        A rising dollar signals tightening of global USD liquidity conditions,
        which tends to be a headwind for risk assets and emerging markets.
        """
        return self.fetch_series("DTWEXBGS", **kwargs)

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


# ───────────────────────────────────────────────────────────────────────────────
# Module-level convenience functions
# ───────────────────────────────────────────────────────────────────────────────


def fetch_fred_data(
    series_id: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    api_key: Optional[str] = None,
    max_age_hours: int = 12,
) -> pd.Series:
    """Fetch a single FRED series by ID.  Module-level convenience wrapper.

    Thin wrapper around :meth:`FREDClient.fetch_series` that constructs a
    client instance and returns a single :class:`pandas.Series`.

    Args:
        series_id:      FRED series identifier (e.g. ``'T10Y2Y'``,
                        ``'BAMLC0A0CM'``, ``'DTWEXBGS'``).
        start_date:     Observation start date ``'YYYY-MM-DD'`` (default: 5 yrs ago).
        end_date:       Observation end date ``'YYYY-MM-DD'`` (default: today).
        api_key:        Optional FRED API key override (reads from
                        ``~/.atlas-secrets.json`` if omitted).
        max_age_hours:  Cache TTL in hours.

    Returns:
        :class:`pandas.Series` indexed by :class:`pandas.Timestamp`, or an
        empty Series if the API key is missing or the request fails.

    Example::

        from data.fred import fetch_fred_data

        oas = fetch_fred_data("BAMLC0A0CM", start_date="2020-01-01")
        dxy = fetch_fred_data("DTWEXBGS", start_date="2020-01-01")
    """
    client = FREDClient(api_key=api_key)
    return client.fetch_series(
        series_id,
        observation_start=start_date,
        observation_end=end_date,
        max_age_hours=max_age_hours,
    )


def fetch_regime_macro_series(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, pd.Series]:
    """Fetch all FRED series needed for the regime model.

    Returns a dict mapping logical name → pd.Series for:
        - ``yield_2y``           (GS2)
        - ``credit_oas``         (BAMLC0A0CM)
        - ``dxy``                (DTWEXBGS)
        - ``fed_funds``          (FEDFUNDS)
        - ``unemployment_claims``(ICSA)
        - ``yield_curve_10y2y``  (T10Y2Y, pre-computed by FRED)

    Series are *not* aligned to each other here — the caller is
    responsible for reindexing and forward-filling.
    """
    client = FREDClient(api_key=api_key)
    kwargs: Dict[str, Any] = {}
    if start_date:
        kwargs["observation_start"] = start_date
    if end_date:
        kwargs["observation_end"] = end_date

    return {
        "yield_2y": client.fetch_series("GS2", **kwargs),
        "credit_oas": client.fetch_series("BAMLC0A0CM", **kwargs),
        "dxy": client.fetch_series("DTWEXBGS", **kwargs),
        "fed_funds": client.fetch_series("FEDFUNDS", **kwargs),
        "unemployment_claims": client.fetch_series("ICSA", **kwargs),
        "yield_curve_10y2y_fred": client.fetch_series("T10Y2Y", **kwargs),
    }
