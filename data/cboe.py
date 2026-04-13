"""
CBOE Put/Call Ratio Data
========================
Fetches daily equity put/call ratio from CBOE market statistics.
The FRED API does not carry this data, so we scrape directly from CBOE.

Sources tried in order:
    1. Fresh cache (< 12 hours old)
    2. SPY options chain (yfinance) — volume-weighted P/C from nearest expirations
    3. https://cdn.cboe.com/resources/options/totalpc.csv (and other CBOE URLs)
    4. VIX / VIX3M term-structure proxy (yfinance) — sentiment approximation
    5. Stale cache (if all live sources fail)

Usage::

    from data.cboe import fetch_put_call_ratio

    s = fetch_put_call_ratio(start_date="2020-01-01")
    print(s.tail())
"""

import logging
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

# Optional scraper library for bypassing CDN protection.
# Imported at module level so tests can patch data.cboe.cloudscraper.
try:
    import cloudscraper
except ImportError:
    cloudscraper = None  # type: ignore[assignment]

# yfinance is used by _compute_pc_from_spy_options and _compute_vix_term_proxy.
# Imported at module level so tests can mock data.cboe.yf.download.
try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "cboe"
CACHE_FILE = CACHE_DIR / "totalpc.parquet"

# TTL matches the FRED client default
_CACHE_TTL_HOURS = 12

# Request timeout (seconds)
_REQUEST_TIMEOUT = 20

# Known CBOE CSV endpoints — tried in order
CBOE_CSV_URLS = [
    "https://cdn.cboe.com/resources/options/totalpc.csv",
    "https://cdn.cboe.com/data/us/options/market_statistics/daily/totalpc.csv",
    "https://cdn.cboe.com/api/global/us_options/market_statistics/daily/equity_put_call_ratio.csv",
    "https://cdn.cboe.com/resources/options/equitypc.csv",
]

# Possible column names for the date field (case-insensitive after strip)
_DATE_ALIASES = {"date", "trade date", "as of date"}

# Possible column names for the equity put/call ratio
_PC_RATIO_ALIASES = {
    "equity p/c ratio",
    "p/c ratio",
    "put/call ratio",
    "total p/c ratio",
    "pc ratio",
    "ratio",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Atlas/1.0)",
    "Accept": "text/csv,text/plain,*/*",
    "Referer": "https://www.cboe.com/",
}


# ── Private helpers ─────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    """Return True if the local cache file exists and is within TTL."""
    if not CACHE_FILE.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(CACHE_FILE.stat().st_mtime)
    return age < timedelta(hours=_CACHE_TTL_HOURS)


def _read_cache() -> pd.Series:
    """Read the cached parquet file. Returns empty Series on failure."""
    try:
        df = pd.read_parquet(CACHE_FILE)
        return df.iloc[:, 0]
    except Exception as exc:
        logger.debug("CBOE cache read failed: %s", exc)
        return pd.Series(dtype=float)


def _write_cache(series: pd.Series) -> None:
    """Persist a Series to the parquet cache (non-fatal on failure)."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df = series.rename("put_call_ratio").to_frame()
        df.to_parquet(CACHE_FILE)
        logger.debug("CBOE put/call ratio cached: %d rows -> %s", len(series), CACHE_FILE)
    except Exception as exc:
        logger.warning("CBOE cache write failed: %s", exc)


def _parse_cboe_csv(text: str) -> pd.Series:
    """Parse CBOE CSV text into a pd.Series indexed by DatetimeIndex.

    The CBOE file has a header row and columns like:
        Date,Calls,Puts,Total,P/C Ratio
    or:
        "Date","EQUITY P/C RATIO","INDEX P/C RATIO","TOTAL P/C RATIO"

    Returns an empty Series if parsing fails.
    """
    try:
        df = pd.read_csv(StringIO(text), skipinitialspace=True)
    except Exception as exc:
        logger.warning("CBOE CSV parse error: %s", exc)
        return pd.Series(dtype=float)

    if df.empty:
        logger.warning("CBOE CSV parsed to empty DataFrame")
        return pd.Series(dtype=float)

    # Normalise column names for matching
    col_map = {col: col.strip().lower() for col in df.columns}
    df = df.rename(columns=col_map)

    # Find date column
    date_col = next((c for c in df.columns if c in _DATE_ALIASES), None)
    if date_col is None:
        # Fall back: use first column if it smells like dates
        first_col = df.columns[0]
        if df[first_col].dtype == object:
            date_col = first_col
        else:
            logger.warning("CBOE CSV: cannot identify date column. Columns: %s", list(df.columns))
            return pd.Series(dtype=float)

    # Find put/call ratio column — prefer "equity p/c ratio" if present
    pc_col = None
    for alias in ("equity p/c ratio", "p/c ratio", "total p/c ratio", "pc ratio", "ratio"):
        if alias in df.columns:
            pc_col = alias
            break
    if pc_col is None:
        # Last resort: any column containing "p/c" or "ratio"
        for col in df.columns:
            if "p/c" in col or ("ratio" in col and "date" not in col):
                pc_col = col
                break
    if pc_col is None:
        logger.warning(
            "CBOE CSV: cannot identify put/call ratio column. Columns: %s", list(df.columns)
        )
        return pd.Series(dtype=float)

    logger.debug("CBOE CSV: using date_col=%r, pc_col=%r", date_col, pc_col)

    # Parse dates and values
    try:
        dates = pd.to_datetime(df[date_col], errors="coerce")
        values = pd.to_numeric(df[pc_col], errors="coerce")
    except Exception as exc:
        logger.warning("CBOE CSV value parsing failed: %s", exc)
        return pd.Series(dtype=float)

    series = (
        pd.Series(values.values, index=dates, name="put_call_ratio")
        .dropna(how="any")
        .sort_index()
    )

    if series.empty:
        logger.warning("CBOE CSV: no valid rows after parsing date+ratio columns")
    else:
        logger.info(
            "CBOE put/call ratio: %d rows (%s -> %s)",
            len(series),
            series.index.min().date(),
            series.index.max().date(),
        )

    return series


def _fetch_from_url(url: str) -> Optional[pd.Series]:
    """Attempt to download and parse the CBOE CSV from *url*.

    Uses cloudscraper (if available) to bypass CDN protection (Cloudflare/
    Akamai) that causes 403 errors with plain requests.  Falls back to
    requests.Session on ImportError.

    Returns a non-empty Series on success, or None on any failure
    (HTTP error, parse error, empty result).
    """
    try:
        if cloudscraper is not None:
            scraper = cloudscraper.create_scraper()
            resp = scraper.get(url, timeout=_REQUEST_TIMEOUT)
        else:
            logger.debug("cloudscraper not available — falling back to requests.Session")
            session = requests.Session()
            session.headers.update(_HEADERS)
            resp = session.get(url, timeout=_REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        logger.warning("CBOE request timed out: %s", url)
        return None
    except requests.exceptions.ConnectionError as exc:
        logger.warning("CBOE connection error for %s: %s", url, exc)
        return None
    except Exception as exc:
        logger.warning("CBOE unexpected request error for %s: %s", url, exc)
        return None

    if resp.status_code == 403:
        logger.warning("CBOE 403 Access Denied: %s (CDN protection active)", url)
        return None
    if resp.status_code == 404:
        logger.warning("CBOE 404 Not Found: %s", url)
        return None
    if not resp.ok:
        logger.warning("CBOE HTTP %d for %s", resp.status_code, url)
        return None

    series = _parse_cboe_csv(resp.text)
    if series.empty:
        return None
    return series


def _compute_pc_from_spy_options() -> Optional[pd.Series]:
    """Compute put/call ratio from SPY options chain as CBOE fallback.

    Uses the first 4 expirations to get a representative ratio.
    Only provides TODAY's ratio — that's fine, it accumulates over daily runs.

    Returns a single-element Series with today's date, or None on any failure.
    """
    try:
        import yfinance as yf
        spy = yf.Ticker("SPY")
        exps = spy.options
        if not exps:
            logger.warning("SPY options chain: no expirations available")
            return None

        total_calls = 0
        total_puts = 0
        # Use first 4 expirations for a representative ratio
        for exp in exps[:4]:
            try:
                chain = spy.option_chain(exp)
                total_calls += chain.calls["volume"].sum()
                total_puts += chain.puts["volume"].sum()
            except Exception:
                continue

        if total_calls <= 0:
            logger.warning("SPY options chain: zero call volume — cannot compute P/C ratio")
            return None

        ratio = total_puts / total_calls
        today = pd.Timestamp.now().normalize()
        series = pd.Series([ratio], index=pd.DatetimeIndex([today]), name="put_call_ratio")
        logger.info("Put/call ratio computed from SPY options chain: %.3f", ratio)
        return series
    except Exception as exc:
        logger.warning("SPY options chain P/C computation failed: %s", exc)
        return None


def _compute_vix_term_proxy() -> Optional[pd.Series]:
    """Approximate put/call sentiment from VIX term structure.

    VIX / VIX3M > 1.0 = inverted term structure (short-term fear, analogous
    to high put/call ratio).  The ratio naturally lives in the 0.8-1.2 range,
    which coincidentally overlaps with typical equity P/C ratio values.

    This is a PROXY, not a true put/call ratio.  Used only when both CBOE
    scraping and SPY options chain are unavailable.

    Returns a single-element Series with today's date, or None on failure.
    """
    try:
        import yfinance as yf
        vix_data = yf.download("^VIX ^VIX3M", period="5d", progress=False, auto_adjust=True)
        if vix_data.empty:
            logger.warning("VIX term proxy: download returned empty")
            return None

        # Extract latest close for both
        if isinstance(vix_data.columns, pd.MultiIndex):
            vix_close = vix_data[("Close", "^VIX")].dropna()
            vix3m_close = vix_data[("Close", "^VIX3M")].dropna()
        else:
            logger.warning("VIX term proxy: unexpected column structure")
            return None

        if vix_close.empty or vix3m_close.empty:
            logger.warning("VIX term proxy: missing VIX or VIX3M data")
            return None

        latest_vix = vix_close.iloc[-1]
        latest_vix3m = vix3m_close.iloc[-1]

        if latest_vix3m <= 0:
            logger.warning("VIX term proxy: VIX3M is zero/negative")
            return None

        # VIX/VIX3M ratio as P/C sentiment proxy
        ratio = float(latest_vix / latest_vix3m)
        today = pd.Timestamp.now().normalize()
        series = pd.Series([ratio], index=pd.DatetimeIndex([today]), name="put_call_ratio")
        logger.info(
            "Put/call ratio approximated from VIX term structure: %.3f "
            "(VIX=%.1f, VIX3M=%.1f) [PROXY — not actual P/C ratio]",
            ratio, latest_vix, latest_vix3m,
        )
        return series
    except ImportError:
        logger.warning("VIX term proxy: yfinance not available")
        return None
    except Exception as exc:
        logger.warning("VIX term proxy computation failed: %s", exc)
        return None


def fetch_spy_put_call_ratio() -> Optional[float]:
    """Compute today's put/call ratio from SPY options chain via yfinance.

    Uses nearest 4 expirations for a representative volume-weighted ratio.

    Returns:
        Float P/C ratio (typically 0.5-1.5), or None if market is closed
        or data is unavailable.
    """
    result = _compute_pc_from_spy_options()
    if result is None or result.empty:
        return None
    return round(float(result.iloc[0]), 4)


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_put_call_ratio(
    start_date: Optional[str] = None,
    max_age_hours: int = _CACHE_TTL_HOURS,
) -> pd.Series:
    """Fetch CBOE equity put/call ratio.

    Tries SPY options chain first (CBOE CSV scraping blocked by 403 as of
    2026-04).  Falls back to CBOE CSV URLs, then VIX/VIX3M term-structure
    proxy, and finally stale cached data.

    Args:
        start_date:    Earliest date to include (``'YYYY-MM-DD'``).
                       ``None`` returns the full available history.
        max_age_hours: Cache TTL in hours (default 12).

    Returns:
        :class:`pandas.Series` indexed by :class:`pandas.DatetimeIndex`
        with float values (put/call ratio).

        * Values > 1.0 = more puts than calls (bearish sentiment).
        * Extreme values > 1.2 are often contrarian bullish signals.
        * Returns an **empty Series** if all sources fail.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. Fresh cache ──────────────────────────────────────────────────────
    if _cache_is_fresh():
        series = _read_cache()
        if not series.empty:
            logger.debug("CBOE put/call ratio: serving from fresh cache")
            return _apply_start_date(series, start_date)

    # ── 2. SPY options chain — primary live source ──────────────────────────
    # CBOE blocks direct CSV scraping (403) as of 2026-04.  Compute P/C
    # ratio from SPY's options chain instead (volume-weighted).
    spy_series = _compute_pc_from_spy_options()
    if spy_series is not None:
        # Merge with stale cache to preserve history across daily runs
        live_series: Optional[pd.Series] = None
        if CACHE_FILE.exists():
            stale = _read_cache()
            if not stale.empty:
                live_series = pd.concat(
                    [stale[~stale.index.isin(spy_series.index)], spy_series]
                ).sort_index()
            else:
                live_series = spy_series
        else:
            live_series = spy_series

        if live_series is not None:
            _write_cache(live_series)
            return _apply_start_date(live_series, start_date)

    # ── 3. CBOE CSV URLs — try each (often blocked by CDN/403) ──────────────
    live_series = None
    for url in CBOE_CSV_URLS:
        result = _fetch_from_url(url)
        if result is not None:
            live_series = result
            logger.info("CBOE put/call ratio fetched from %s", url)
            break

    if live_series is not None:
        _write_cache(live_series)
        return _apply_start_date(live_series, start_date)

    # ── 4. VIX term-structure proxy ─────────────────────────────────────────
    vix_series = _compute_vix_term_proxy()
    if vix_series is not None:
        if CACHE_FILE.exists():
            stale = _read_cache()
            if not stale.empty:
                live_series = pd.concat(
                    [stale[~stale.index.isin(vix_series.index)], vix_series]
                ).sort_index()
            else:
                live_series = vix_series
        else:
            live_series = vix_series

        if live_series is not None:
            _write_cache(live_series)
            return _apply_start_date(live_series, start_date)

    # ── 5. Stale cache fallback ─────────────────────────────────────────────
    if CACHE_FILE.exists():
        stale = _read_cache()
        if not stale.empty:
            logger.warning(
                "CBOE: all live sources failed — using stale cache (%s)",
                CACHE_FILE,
            )
            return _apply_start_date(stale, start_date)

    # ── 6. Nothing available ─────────────────────────────────────────────────
    logger.warning(
        "Put/call ratio unavailable: SPY options chain failed, all %d CBOE URL(s) "
        "returned 403/error, VIX proxy failed, and no cache exists.",
        len(CBOE_CSV_URLS),
    )
    return pd.Series(dtype=float)


def _apply_start_date(series: pd.Series, start_date: Optional[str]) -> pd.Series:
    """Filter series to rows on or after start_date (if provided)."""
    if start_date and not series.empty:
        cutoff = pd.Timestamp(start_date)
        series = series.loc[series.index >= cutoff]
    return series
