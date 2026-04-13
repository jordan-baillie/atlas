"""
Atlas Treasury Yield Curve Data
================================
Fetches daily Treasury yield curve data from Treasury.gov (no API key required).

Source URL pattern:
    https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
    daily-treasury-rates.csv/all/{YYYYMM}?type=daily_treasury_yield_curve
    &field_tdr_date_value={YYYY}&page&_format=csv

Provides:
    - fetch_treasury_curve(start_date, end_date, use_cache=True)
      → DataFrame indexed by date with 11 yield columns
    - compute_curve_metrics(df)
      → Adds treasury_slope, treasury_curvature, treasury_level
    - get_treasury_data(start_date, end_date, use_cache=True)
      → Convenience wrapper: fetch + compute
    - backfill_treasury(start_date, end_date)
      → Fetch full history and write to treasury_curve DB table
    - write_treasury_to_db(df)
      → Persist a yield curve DataFrame to SQLite
"""

import io
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).resolve().parent / "cache" / "treasury"
CACHE_TTL_HOURS = 24

_BASE_URL = (
    "https://home.treasury.gov/resource-center/data-chart-center/"
    "interest-rates/daily-treasury-rates.csv/all/{yyyymm}"
    "?type=daily_treasury_yield_curve"
    "&field_tdr_date_value={yyyy}"
    "&page&_format=csv"
)

# Treasury CSV column → our standardized name.
# Extra columns ("1.5 Month", "2 Mo", "4 Mo") are silently ignored.
_COL_MAP = {
    "1 Mo":  "yield_1m",
    "3 Mo":  "yield_3m",
    "6 Mo":  "yield_6m",
    "1 Yr":  "yield_1y",
    "2 Yr":  "yield_2y",
    "3 Yr":  "yield_3y",
    "5 Yr":  "yield_5y",
    "7 Yr":  "yield_7y",
    "10 Yr": "yield_10y",
    "20 Yr": "yield_20y",
    "30 Yr": "yield_30y",
}

_YIELD_COLS = list(_COL_MAP.values())  # canonical order
_ALL_COLS = _YIELD_COLS + ["treasury_slope", "treasury_curvature", "treasury_level"]


# ── Cache helpers ────────────────────────────────────────────────────────────

def _cache_path(yyyymm: str) -> Path:
    return CACHE_DIR / f"treasury_{yyyymm}.csv"


def _cache_is_fresh(yyyymm: str) -> bool:
    """Return True if the monthly cache file exists and is < CACHE_TTL_HOURS old."""
    p = _cache_path(yyyymm)
    if not p.exists():
        return False
    age = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    return age < timedelta(hours=CACHE_TTL_HOURS)


# ── Month enumeration ────────────────────────────────────────────────────────

def _months_in_range(start_date: str, end_date: str) -> List[str]:
    """Return list of 'YYYYMM' strings covering [start_date, end_date]."""
    start = pd.Timestamp(start_date)
    end = pd.Timestamp(end_date)
    months = []
    cur = start.replace(day=1)
    while cur <= end:
        months.append(cur.strftime("%Y%m"))
        # Advance to next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1)
        else:
            cur = cur.replace(month=cur.month + 1)
    return months


# ── Single-month fetch ───────────────────────────────────────────────────────

def _fetch_month(yyyymm: str, use_cache: bool = True) -> pd.DataFrame:
    """Fetch one month of Treasury yield curve data.

    Returns a DataFrame indexed by date (Timestamp) with _YIELD_COLS columns,
    or an empty DataFrame on failure.
    """
    cache_file = _cache_path(yyyymm)

    # --- Cache check ---
    if use_cache and _cache_is_fresh(yyyymm):
        try:
            df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
            logger.debug("treasury: cache hit for %s (%d rows)", yyyymm, len(df))
            return df
        except Exception as exc:
            logger.debug("treasury: cache read failed for %s: %s", yyyymm, exc)

    # --- Fetch from Treasury.gov ---
    yyyy = yyyymm[:4]
    url = _BASE_URL.format(yyyymm=yyyymm, yyyy=yyyy)
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        raw_text = resp.text
    except requests.RequestException as exc:
        logger.warning("treasury: HTTP error fetching %s: %s", yyyymm, exc)
        # Fall back to stale cache if available
        if cache_file.exists():
            try:
                df = pd.read_csv(cache_file, index_col=0, parse_dates=True)
                logger.info("treasury: using stale cache for %s", yyyymm)
                return df
            except Exception:
                pass
        return pd.DataFrame()

    # --- Parse CSV ---
    try:
        df = _parse_treasury_csv(raw_text)
    except Exception as exc:
        logger.warning("treasury: parse error for %s: %s", yyyymm, exc)
        return pd.DataFrame()

    if df.empty:
        logger.debug("treasury: no data rows for %s", yyyymm)
        return df

    # --- Write cache ---
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_file)
        logger.debug("treasury: cached %s (%d rows)", yyyymm, len(df))
    except Exception as exc:
        logger.debug("treasury: cache write failed for %s: %s", yyyymm, exc)

    return df


def _parse_treasury_csv(raw_text: str) -> pd.DataFrame:
    """Parse raw Treasury CSV text into a clean DataFrame.

    Returns DataFrame indexed by date (Timestamp), columns = _YIELD_COLS.
    Rows with all-NaN yields are dropped.
    """
    raw = pd.read_csv(io.StringIO(raw_text))

    if raw.empty or "Date" not in raw.columns:
        return pd.DataFrame()

    # Rename known columns; drop unknown ones
    rename_map = {k: v for k, v in _COL_MAP.items() if k in raw.columns}
    raw = raw.rename(columns=rename_map)

    # Keep only the date + our yield columns
    keep_cols = ["Date"] + [c for c in _YIELD_COLS if c in raw.columns]
    raw = raw[keep_cols].copy()

    # Parse dates — Treasury format is MM/DD/YYYY
    raw["Date"] = pd.to_datetime(raw["Date"], format="%m/%d/%Y", errors="coerce")
    raw = raw.dropna(subset=["Date"])
    raw = raw.set_index("Date").sort_index()
    raw.index.name = "date"

    # Ensure all yield columns present (fill missing maturities with NaN)
    for col in _YIELD_COLS:
        if col not in raw.columns:
            raw[col] = float("nan")

    # Convert to numeric (some cells may be blank strings)
    for col in _YIELD_COLS:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")

    # Drop rows where ALL yields are NaN
    raw = raw.dropna(how="all", subset=_YIELD_COLS)

    return raw[_YIELD_COLS]


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_treasury_curve(
    start_date: str,
    end_date: str,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch Treasury yield curve data for a date range.

    Determines which months are needed, fetches each month's CSV from
    Treasury.gov (with 24-hour per-month caching), and returns a unified
    DataFrame filtered to [start_date, end_date].

    Args:
        start_date: First date to include, ``'YYYY-MM-DD'``.
        end_date:   Last date to include, ``'YYYY-MM-DD'``.
        use_cache:  If True, serve from per-month cache files when fresh.

    Returns:
        :class:`pandas.DataFrame` indexed by date (Timestamp) with columns:
        yield_1m, yield_3m, yield_6m, yield_1y, yield_2y, yield_3y,
        yield_5y, yield_7y, yield_10y, yield_20y, yield_30y.
        Returns empty DataFrame on total failure (partial data is preserved).
    """
    months = _months_in_range(start_date, end_date)
    if not months:
        logger.warning("treasury: no months in range [%s, %s]", start_date, end_date)
        return pd.DataFrame(columns=_YIELD_COLS)

    frames = []
    for yyyymm in months:
        df_month = _fetch_month(yyyymm, use_cache=use_cache)
        if not df_month.empty:
            frames.append(df_month)

    if not frames:
        logger.warning(
            "treasury: all months returned empty for [%s, %s]", start_date, end_date
        )
        return pd.DataFrame(columns=_YIELD_COLS)

    combined = pd.concat(frames)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()

    # Filter to requested date range
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    combined = combined.loc[
        (combined.index >= start_ts) & (combined.index <= end_ts)
    ]

    logger.info(
        "treasury: fetched %d rows [%s, %s] across %d months",
        len(combined),
        combined.index.min().date() if len(combined) else "empty",
        combined.index.max().date() if len(combined) else "empty",
        len(months),
    )
    return combined


def compute_curve_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Compute derived yield curve metrics and add them to the DataFrame.

    Adds three columns:
    - ``treasury_slope``:     yield_10y - yield_2y  (classic 10y-2y spread)
    - ``treasury_curvature``: (yield_2y + yield_10y) / 2 - yield_5y  (butterfly)
    - ``treasury_level``:     mean of all 11 maturities

    Args:
        df: DataFrame with yield columns (as returned by fetch_treasury_curve).

    Returns:
        Same DataFrame with the 3 metric columns appended.
    """
    if df.empty:
        return df

    df = df.copy()

    # 10y-2y spread
    if "yield_10y" in df.columns and "yield_2y" in df.columns:
        df["treasury_slope"] = df["yield_10y"] - df["yield_2y"]
    else:
        df["treasury_slope"] = float("nan")

    # Butterfly spread: (2y + 10y)/2 - 5y
    if all(c in df.columns for c in ("yield_2y", "yield_5y", "yield_10y")):
        df["treasury_curvature"] = (df["yield_2y"] + df["yield_10y"]) / 2 - df["yield_5y"]
    else:
        df["treasury_curvature"] = float("nan")

    # Average level across all 11 maturities
    present_yield_cols = [c for c in _YIELD_COLS if c in df.columns]
    if present_yield_cols:
        df["treasury_level"] = df[present_yield_cols].mean(axis=1, skipna=True)
    else:
        df["treasury_level"] = float("nan")

    return df


def get_treasury_data(
    start_date: str,
    end_date: str,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch Treasury yield curve data and compute derived metrics.

    Convenience wrapper around :func:`fetch_treasury_curve` +
    :func:`compute_curve_metrics`.

    Returns a DataFrame indexed by date with 11 yield columns plus
    treasury_slope, treasury_curvature, and treasury_level.
    """
    df = fetch_treasury_curve(start_date, end_date, use_cache=use_cache)
    if df.empty:
        return df
    return compute_curve_metrics(df)


def write_treasury_to_db(df: pd.DataFrame) -> int:
    """Write a Treasury yield curve DataFrame to the treasury_curve table.

    Each row is upserted (INSERT OR REPLACE).  NaN values are stored as NULL.

    Args:
        df: DataFrame with date index and yield + metric columns
            (as returned by get_treasury_data).

    Returns:
        Number of rows written.
    """
    from db.atlas_db import batch_upsert_treasury_curve

    if df.empty:
        return 0

    batch = []
    for ts, row in df.iterrows():
        date_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
        record: dict = {"date": date_str}
        for col in _ALL_COLS:
            val = row.get(col)
            if val is None:
                record[col] = None
            elif hasattr(val, "item"):
                val = val.item()
                record[col] = None if (isinstance(val, float) and (math.isnan(val) or math.isinf(val))) else val
            elif isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
                record[col] = None
            else:
                record[col] = val
        batch.append(record)

    n = batch_upsert_treasury_curve(batch)
    logger.info("write_treasury_to_db: wrote %d rows to treasury_curve", n)
    return n


def backfill_treasury(
    start_date: str = "2015-01-01",
    end_date: Optional[str] = None,
) -> int:
    """Fetch full yield curve history and write to the treasury_curve DB table.

    Intended for one-time backfills.  Cache is bypassed so we always fetch
    fresh data from Treasury.gov.

    Args:
        start_date: First date to backfill (default: 2015-01-01).
        end_date:   Last date to backfill (default: today).

    Returns:
        Number of rows written to the DB.
    """
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    logger.info("backfill_treasury: fetching [%s, %s]", start_date, end_date)

    df = get_treasury_data(start_date, end_date, use_cache=False)
    if df.empty:
        logger.warning("backfill_treasury: no data returned for [%s, %s]", start_date, end_date)
        return 0

    n = write_treasury_to_db(df)
    logger.info("backfill_treasury: wrote %d rows for [%s, %s]", n, start_date, end_date)
    return n


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Default: last 30 days
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    if len(sys.argv) >= 3:
        start = sys.argv[1]
        end = sys.argv[2]
    elif len(sys.argv) == 2:
        start = sys.argv[1]

    print(f"\nFetching Treasury yield curve: [{start}, {end}]")
    df = get_treasury_data(start, end)

    if df.empty:
        print("No data returned.")
        sys.exit(1)

    print(f"\nRows: {len(df)}")
    print(f"Columns: {list(df.columns)}")
    print("\nLatest 5 rows:")
    print(df.tail(5).to_string())

    latest = df.iloc[-1]
    print(f"\n--- Latest ({df.index[-1].date()}) ---")
    print(f"  Slope     (10y-2y):  {latest.get('treasury_slope', float('nan')):.3f}%")
    print(f"  Curvature (butterfly): {latest.get('treasury_curvature', float('nan')):.4f}%")
    print(f"  Level     (avg):     {latest.get('treasury_level', float('nan')):.3f}%")
    print(f"  2y yield:  {latest.get('yield_2y', float('nan')):.3f}%")
    print(f"  10y yield: {latest.get('yield_10y', float('nan')):.3f}%")
    print(f"  30y yield: {latest.get('yield_30y', float('nan')):.3f}%")
