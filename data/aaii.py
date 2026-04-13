"""
AAII Sentiment Survey Data
==========================
Fetches weekly AAII bull/bear/neutral sentiment data.
Used as a contrarian signal: extreme bearishness is bullish,
extreme bullishness is a caution signal.

Source: American Association of Individual Investors (AAII)
Published weekly (Thursdays).

Fallback: CNN Fear & Greed Index when AAII URLs return 403.
"""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "aaii"
CACHE_FILE = CACHE_DIR / "aaii_sentiment.parquet"

# Known AAII sentiment data URLs
AAII_URLS = [
    "https://www.aaii.com/files/surveys/sentiment.xls",
    "https://www.aaii.com/files/surveys/sentiment.csv",
]

# Contrarian thresholds
EXTREME_BEARISH_THRESHOLD = 50.0   # >50% bears = contrarian bullish
EXTREME_BULLISH_THRESHOLD = 60.0   # >60% bulls = caution signal
ELEVATED_BEARISH = 40.0            # >40% bears = mildly bullish contrarian
ELEVATED_BULLISH = 50.0            # >50% bulls = mild caution


# ── CNN Fear & Greed fallback ─────────────────────────────────────────────────

def _fetch_cnn_fear_greed() -> pd.DataFrame:
    """Fetch CNN Fear & Greed Index as AAII fallback.

    The CNN F&G index (0-100) maps to sentiment:
      0-25: Extreme Fear  → treat as extreme bearish (contrarian bullish)
      25-45: Fear          → treat as elevated bearish
      45-55: Neutral
      55-75: Greed         → treat as elevated bullish
      75-100: Extreme Greed → treat as extreme bullish (contrarian caution)
    """
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Atlas/1.0)"}
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # The response has: {"fear_and_greed": {"score": 38, "rating": "Fear", ...}, ...}
    # Also has "fear_and_greed_historical": {"data": [{"x": timestamp_ms, "y": score, "rating": "..."}, ...]}

    rows = []

    # Current reading
    fg = data.get("fear_and_greed", {})
    if fg:
        score = fg.get("score", 50)
        # Map F&G to bull/bear percentages (inverse mapping)
        bear_pct = max(0, (50 - score) * 2)  # score=0 → 100% bear, score=50 → 0%
        bull_pct = max(0, (score - 50) * 2)   # score=50 → 0%, score=100 → 100%
        neutral_pct = 100 - bull_pct - bear_pct
        rows.append({
            "date": pd.Timestamp.now().normalize(),
            "bullish": round(bull_pct, 1),
            "bearish": round(bear_pct, 1),
            "neutral": round(neutral_pct, 1),
            "source": "cnn_fg",
            "raw_score": score,
        })

    # Historical (if available)
    hist = data.get("fear_and_greed_historical", {}).get("data", [])
    for pt in hist[-90:]:  # last ~90 data points
        ts = pt.get("x", 0)
        score = pt.get("y", 50)
        dt = pd.Timestamp(ts, unit="ms").normalize()
        bear_pct = max(0, (50 - score) * 2)
        bull_pct = max(0, (score - 50) * 2)
        neutral_pct = 100 - bull_pct - bear_pct
        rows.append({
            "date": dt,
            "bullish": round(bull_pct, 1),
            "bearish": round(bear_pct, 1),
            "neutral": round(neutral_pct, 1),
            "source": "cnn_fg",
            "raw_score": score,
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).drop_duplicates(subset=["date"]).set_index("date").sort_index()
    return df


# ── Main fetch ────────────────────────────────────────────────────────────────

def fetch_aaii_sentiment(
    max_age_hours: int = 168,  # 7 days (weekly data)
) -> pd.DataFrame:
    """Fetch AAII sentiment survey data.

    Returns DataFrame with columns:
        date, bullish, neutral, bearish (all as percentages 0-100)
    Indexed by date (weekly, typically Thursday).

    Fallback chain: AAII XLS → AAII CSV → CNN Fear & Greed → empty DataFrame.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Check cache
    if CACHE_FILE.exists():
        age = datetime.now() - datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
        if age < timedelta(hours=max_age_hours):
            try:
                df = pd.read_parquet(CACHE_FILE)
                logger.info(f"AAII cache hit: {len(df)} rows")
                return df
            except Exception as e:
                logger.warning(f"AAII cache read failed: {e}")

    # Try each AAII URL
    df = pd.DataFrame()
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Atlas/1.0)"}

    for url in AAII_URLS:
        try:
            logger.info(f"Fetching AAII sentiment from {url}")
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()

            if url.endswith(".xls"):
                import io
                df = pd.read_excel(io.BytesIO(resp.content))
            else:
                import io
                df = pd.read_csv(io.StringIO(resp.text))

            if not df.empty:
                logger.info(f"AAII: fetched {len(df)} rows from {url}")
                break
        except Exception as e:
            logger.warning(f"AAII fetch from {url} failed: {e}")
            continue

    # ── CNN Fear & Greed fallback (before giving up entirely) ──────────────
    if df.empty:
        logger.info("AAII: direct fetch failed, trying CNN Fear & Greed fallback")
        try:
            df = _fetch_cnn_fear_greed()
            if not df.empty:
                logger.info(f"CNN F&G fallback: got {len(df)} rows")
                # Already normalised — skip _normalize_columns
                try:
                    df.to_parquet(CACHE_FILE, engine="pyarrow")
                except Exception as e:
                    logger.warning(f"Cache write failed: {e}")
                return df
        except Exception as e:
            logger.warning(f"CNN Fear & Greed fallback failed: {e}")

    if df.empty:
        logger.warning(
            "AAII: all fetch attempts failed, generating synthetic data from known values"
        )
        df = _generate_fallback_data()
        if df.empty:
            return pd.DataFrame()

    # Normalize column names
    df = _normalize_columns(df)

    if df.empty:
        return pd.DataFrame()

    # Cache result
    try:
        df.to_parquet(CACHE_FILE, engine="pyarrow")
        logger.info(f"AAII sentiment cached: {len(df)} rows")
    except Exception as e:
        logger.warning(f"AAII cache write failed: {e}")

    return df


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize AAII data columns to standard format."""
    # Find date column
    date_col = None
    for c in df.columns:
        cl = str(c).lower()
        if "date" in cl or "reported" in cl:
            date_col = c
            break

    if date_col is None and len(df.columns) > 0:
        # First column might be date
        date_col = df.columns[0]

    if date_col is None:
        return pd.DataFrame()

    # Find sentiment columns
    bull_col = bear_col = neutral_col = None
    for c in df.columns:
        cl = str(c).lower()
        if "bull" in cl:
            bull_col = c
        elif "bear" in cl:
            bear_col = c
        elif "neutral" in cl:
            neutral_col = c

    if not all([bull_col, bear_col]):
        logger.warning(
            f"AAII: could not identify bull/bear columns in {list(df.columns)}"
        )
        return pd.DataFrame()

    result = pd.DataFrame()
    result["date"] = pd.to_datetime(df[date_col], errors="coerce")
    result["bullish"] = pd.to_numeric(df[bull_col], errors="coerce")
    result["bearish"] = pd.to_numeric(df[bear_col], errors="coerce")
    if neutral_col:
        result["neutral"] = pd.to_numeric(df[neutral_col], errors="coerce")
    else:
        result["neutral"] = 100.0 - result["bullish"] - result["bearish"]

    # Convert to percentage if values are in decimal form (0-1 range)
    for col in ["bullish", "bearish", "neutral"]:
        if col in result.columns and result[col].dropna().max() <= 1.0:
            result[col] = result[col] * 100.0

    result = result.dropna(subset=["date"]).set_index("date").sort_index()
    result = result.dropna(subset=["bullish", "bearish"])

    return result


def _generate_fallback_data() -> pd.DataFrame:
    """Generate minimal fallback data using known AAII historical averages.

    Only used when live fetch fails.  Returns an empty DataFrame — the signal
    function handles the no-data case gracefully.
    """
    return pd.DataFrame()


# ── DB persistence ────────────────────────────────────────────────────────────

def store_sentiment_to_db(signal: dict) -> None:
    """Store sentiment signal in news_intel for overlay context."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            db.execute(
                "INSERT OR REPLACE INTO news_intel (timestamp, source, headline, category, summary) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    datetime.now().isoformat(),
                    signal.get("source", "aaii"),
                    f"Sentiment: {signal['signal']} (bull={signal['bullish_pct']}%, bear={signal['bearish_pct']}%)",
                    "sentiment",
                    signal.get("details", ""),
                ),
            )
    except Exception as e:
        logger.warning(f"Failed to store sentiment to DB: {e}")


# ── Signal extraction ─────────────────────────────────────────────────────────

def get_sentiment_signal(as_of_date=None) -> dict:
    """Get the current AAII sentiment contrarian signal.

    Returns dict with:
        - signal: "bullish" (extreme bearish = contrarian buy),
                  "caution" (extreme bullish = contrarian sell),
                  "neutral"
        - bullish_pct, bearish_pct, neutral_pct
        - spread: bull - bear spread
        - details: human-readable explanation
        - source: "cnn_fear_greed" or "aaii"
        - fear_greed_score: raw CNN score if source is CNN, else None
    """
    df = fetch_aaii_sentiment()

    if df.empty:
        return {
            "signal": "neutral",
            "bullish_pct": None,
            "bearish_pct": None,
            "neutral_pct": None,
            "spread": None,
            "confidence": 0.0,
            "details": "No AAII data available",
            "source": "none",
            "fear_greed_score": None,
        }

    # Get latest reading (optionally filtered to as_of_date)
    if as_of_date:
        mask = df.index <= pd.Timestamp(as_of_date)
        latest = df.loc[mask].iloc[-1] if mask.any() else df.iloc[-1]
    else:
        latest = df.iloc[-1]

    bull = float(latest["bullish"])
    bear = float(latest["bearish"])
    neutral = float(latest.get("neutral", 100.0 - bull - bear))
    spread = bull - bear

    # Determine contrarian signal
    signal = "neutral"
    confidence = 0.0
    details = ""

    if bear > EXTREME_BEARISH_THRESHOLD:
        signal = "bullish"  # Extreme bearishness is contrarian bullish
        confidence = min(1.0, (bear - EXTREME_BEARISH_THRESHOLD) / 15.0 + 0.5)
        details = f"Extreme bearish ({bear:.1f}% bears) — contrarian bullish signal"
    elif bear > ELEVATED_BEARISH:
        signal = "mild_bullish"
        confidence = 0.3
        details = f"Elevated bearish ({bear:.1f}% bears) — mild contrarian bullish"
    elif bull > EXTREME_BULLISH_THRESHOLD:
        signal = "caution"  # Extreme bullishness is a warning
        confidence = min(1.0, (bull - EXTREME_BULLISH_THRESHOLD) / 15.0 + 0.5)
        details = f"Extreme bullish ({bull:.1f}% bulls) — contrarian caution signal"
    elif bull > ELEVATED_BULLISH:
        signal = "mild_caution"
        confidence = 0.3
        details = f"Elevated bullish ({bull:.1f}% bulls) — mild contrarian caution"
    else:
        signal = "neutral"
        confidence = 0.1
        details = f"Neutral sentiment (bull={bull:.1f}%, bear={bear:.1f}%)"

    survey_date = (
        latest.name.strftime("%Y-%m-%d")
        if hasattr(latest.name, "strftime")
        else str(latest.name)
    )

    result = {
        "signal": signal,
        "bullish_pct": round(bull, 1),
        "bearish_pct": round(bear, 1),
        "neutral_pct": round(neutral, 1),
        "spread": round(spread, 1),
        "confidence": round(confidence, 2),
        "survey_date": survey_date,
        "details": details,
    }

    # Include source info
    result["source"] = (
        "cnn_fear_greed"
        if "source" in df.columns and (df["source"] == "cnn_fg").any()
        else "aaii"
    )
    if "raw_score" in df.columns:
        result["fear_greed_score"] = (
            float(latest.get("raw_score", 0))
            if "raw_score" in latest.index
            else None
        )
    else:
        result["fear_greed_score"] = None

    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    signal = get_sentiment_signal()
    print(f"\nSentiment Signal: {signal['signal']} (confidence={signal['confidence']})")
    print(f"Source:   {signal['source']}")
    if signal.get("fear_greed_score") is not None:
        print(f"CNN F&G Score: {signal['fear_greed_score']:.1f}/100")
    print(
        f"Bullish: {signal['bullish_pct']}%  "
        f"Bearish: {signal['bearish_pct']}%  "
        f"Neutral: {signal['neutral_pct']}%"
    )
    print(f"Spread:   {signal['spread']}")
    print(f"Details:  {signal['details']}")
    print(f"Survey date: {signal.get('survey_date', 'n/a')}")

    # Persist to DB
    store_sentiment_to_db(signal)
    print("\nStored to news_intel DB.")
