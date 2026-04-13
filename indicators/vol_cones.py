"""
Volatility Cones + Dynamic Stop Placement — Phase 3.

Yang-Zhang OHLC volatility estimator, rolling historical cones with
percentile bands, and volatility-regime-aware stop placement.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, date
from typing import Optional

import numpy as np
import pandas as pd

from db.atlas_db import get_db, get_ohlcv

logger = logging.getLogger(__name__)

TRADING_DAYS_PER_YEAR = 252
DEFAULT_HORIZONS = (5, 10, 20, 60, 120)
DEFAULT_PERCENTILES = (5, 25, 50, 75, 95)
DEFAULT_LOOKBACK_YEARS = 5
MIN_HISTORY_DAYS = 60
FALLBACK_STOP_PCT = 0.05

REGIME_MULTIPLIERS = {
    "low": 1.5,
    "normal": 2.0,
    "high": 2.5,
    "extreme": 3.0,
}


def yang_zhang_volatility(ohlc_df: pd.DataFrame, window: int = 20) -> float:
    """
    Yang-Zhang (2000) OHLC volatility estimator — annualized.

    Uses the last ``window+1`` rows of OHLC data. Returns the annualized
    volatility (i.e. multiplied by sqrt(252)).

    Formula:
        sigma^2_YZ = sigma^2_overnight + k * sigma^2_open_close + (1-k) * sigma^2_RS
        k = 0.34 / (1.34 + (n+1)/(n-1))   where n = window

    Components:
        overnight:    log(O_t / C_{t-1})    -- variance around its mean
        open_close:   log(C_t / O_t)        -- variance around its mean
        Rogers-Satchell (no mean subtraction):
            log(H_t/C_t)*log(H_t/O_t) + log(L_t/C_t)*log(L_t/O_t)

    Returns 0.0 if input has fewer than window+1 rows.
    """
    required = {"open", "high", "low", "close"}
    if not required.issubset(ohlc_df.columns):
        raise ValueError(f"ohlc_df missing columns; needs {required}")
    if len(ohlc_df) < window + 1:
        return 0.0

    df = ohlc_df.iloc[-(window + 1):].copy()
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)

    # Need C_{t-1}, so work over indices 1..n (length = window)
    log_ho = np.log(h[1:] / o[1:])
    log_lo = np.log(l[1:] / o[1:])
    log_co = np.log(c[1:] / o[1:])
    log_oc_prev = np.log(o[1:] / c[:-1])   # overnight return (O_t / C_{t-1})
    log_cc_oo = np.log(c[1:] / o[1:])       # open-to-close return

    n = len(log_oc_prev)  # == window
    if n < 2:
        return 0.0

    # Overnight variance -- sample variance of overnight log-returns
    overnight_var = np.var(log_oc_prev, ddof=1)
    # Open-to-close variance -- sample variance of intraday returns
    open_close_var = np.var(log_cc_oo, ddof=1)
    # Rogers-Satchell -- no mean subtraction; population-style average
    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)
    rs_var = float(np.mean(rs))

    k = 0.34 / (1.34 + (n + 1) / (n - 1))

    yz_var = overnight_var + k * open_close_var + (1.0 - k) * rs_var
    if yz_var <= 0 or not math.isfinite(yz_var):
        return 0.0

    yz_daily_vol = math.sqrt(yz_var)
    return yz_daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)


def compute_vol_cone(
    ticker: str,
    horizons: tuple = DEFAULT_HORIZONS,
    lookback_years: int = DEFAULT_LOOKBACK_YEARS,
    percentiles: tuple = DEFAULT_PERCENTILES,
) -> dict:
    """
    Compute the full volatility cone for ``ticker``.

    Returns a dict with per-horizon current vol + historical percentiles,
    plus a 'current_regime' classification driven by the 20-day horizon.
    Returns an empty-ish dict with 'error' key if ticker has no data.
    """
    end_dt = date.today()
    start_dt = end_dt.replace(year=end_dt.year - lookback_years)

    df = get_ohlcv(ticker, start_date=start_dt.isoformat(), end_date=end_dt.isoformat())
    if df is None or df.empty:
        return {"ticker": ticker, "error": "no_data", "cone": {}}

    df = df.sort_index()
    n_total = len(df)
    if n_total < MIN_HISTORY_DAYS:
        return {
            "ticker": ticker,
            "error": "insufficient_history",
            "n_obs": n_total,
            "cone": {},
        }

    # As-of date = last row
    as_of_ts = df.index[-1]
    as_of = as_of_ts.strftime("%Y-%m-%d") if hasattr(as_of_ts, "strftime") else str(as_of_ts)[:10]

    cone: dict = {}
    for h in horizons:
        if n_total < h + 1:
            # Not enough for even one window
            continue
        rolling_vols: list[float] = []
        # Walk the DataFrame computing a Yang-Zhang estimate for each trailing window
        for end_idx in range(h, n_total):
            window_df = df.iloc[end_idx - h : end_idx + 1]  # h+1 rows
            v = yang_zhang_volatility(window_df, window=h)
            if v > 0 and math.isfinite(v):
                rolling_vols.append(v)
        if len(rolling_vols) < 10:
            continue
        arr = np.asarray(rolling_vols, dtype=float)
        pcts = {f"p{p}": float(np.percentile(arr, p)) for p in percentiles}
        cone[h] = {
            "current": float(arr[-1]),
            **pcts,
            "n_obs": int(len(arr)),
        }

    # Regime classification from 20-day cone
    regime = "normal"
    if 20 in cone:
        c20 = cone[20]
        cur = c20["current"]
        if cur < c20["p25"]:
            regime = "low"
        elif cur > c20["p95"]:
            regime = "extreme"
        elif cur > c20["p75"]:
            regime = "high"
        else:
            regime = "normal"

    return {
        "ticker": ticker,
        "as_of": as_of,
        "lookback_years": lookback_years,
        "cone": cone,
        "current_regime": regime,
    }


def get_vol_regime_multiplier(ticker: str, window: int = 20) -> float:
    """Return the stop-distance multiplier k for the ticker's current vol regime."""
    cone_result = compute_vol_cone(ticker)
    if cone_result.get("error") or not cone_result.get("cone"):
        logger.warning("vol_regime_multiplier: falling back to 2.0 for %s (%s)",
                       ticker, cone_result.get("error", "unknown"))
        return REGIME_MULTIPLIERS["normal"]
    regime = cone_result.get("current_regime", "normal")
    return REGIME_MULTIPLIERS.get(regime, REGIME_MULTIPLIERS["normal"])


def compute_dynamic_stop(
    entry_price: float,
    ticker: str,
    direction: str = "long",
    k_override: Optional[float] = None,
) -> dict:
    """
    Compute a volatility-aware stop price using the Yang-Zhang 20-day estimate.

    For 'long':  stop = entry * (1 - k * vol_daily)
    For 'short': stop = entry * (1 + k * vol_daily)

    Falls back to a fixed 5% stop if the ticker has no data or insufficient history.
    """
    cone_result = compute_vol_cone(ticker)

    if cone_result.get("error") or 20 not in cone_result.get("cone", {}):
        logger.warning("dynamic_stop: fallback to fixed %.1f%% for %s (%s)",
                       FALLBACK_STOP_PCT * 100, ticker,
                       cone_result.get("error", "no_20d_cone"))
        stop_distance_pct = FALLBACK_STOP_PCT
        if direction == "long":
            stop_price = entry_price * (1 - stop_distance_pct)
        else:
            stop_price = entry_price * (1 + stop_distance_pct)
        return {
            "entry_price": float(entry_price),
            "ticker": ticker,
            "direction": direction,
            "vol_20d_annual": None,
            "vol_20d_daily": None,
            "vol_regime": "unknown",
            "k": None,
            "stop_distance_pct": round(stop_distance_pct, 6),
            "stop_price": round(stop_price, 4),
            "method": "fixed_fallback",
            "needs_review": False,
        }

    cone20 = cone_result["cone"][20]
    vol_annual = float(cone20["current"])
    vol_daily = vol_annual / math.sqrt(TRADING_DAYS_PER_YEAR)
    regime = cone_result["current_regime"]
    k = float(k_override) if k_override is not None else REGIME_MULTIPLIERS[regime]
    stop_distance_pct = k * vol_daily

    if direction == "long":
        stop_price = entry_price * (1 - stop_distance_pct)
    elif direction == "short":
        stop_price = entry_price * (1 + stop_distance_pct)
    else:
        raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")

    return {
        "entry_price": float(entry_price),
        "ticker": ticker,
        "direction": direction,
        "vol_20d_annual": round(vol_annual, 6),
        "vol_20d_daily": round(vol_daily, 6),
        "vol_regime": regime,
        "k": k,
        "stop_distance_pct": round(stop_distance_pct, 6),
        "stop_price": round(stop_price, 4),
        "method": "yang_zhang_dynamic",
        "needs_review": regime == "extreme",
    }


def _ensure_vol_tables() -> None:
    """Create vol_cones and vol_regimes tables if missing."""
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS vol_cones (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            as_of TEXT NOT NULL,
            horizon INTEGER NOT NULL,
            current_vol REAL,
            p5 REAL,
            p25 REAL,
            p50 REAL,
            p75 REAL,
            p95 REAL,
            n_obs INTEGER,
            lookback_years INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, as_of, horizon)
        );
        CREATE INDEX IF NOT EXISTS idx_vol_cones_ticker_asof ON vol_cones(ticker, as_of);

        CREATE TABLE IF NOT EXISTS vol_regimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            as_of TEXT NOT NULL,
            regime TEXT NOT NULL,
            multiplier REAL,
            vol_20d REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(ticker, as_of)
        );
        CREATE INDEX IF NOT EXISTS idx_vol_regimes_ticker_asof ON vol_regimes(ticker, as_of);
        """)


def persist_vol_cone(cone_result: dict) -> None:
    """Persist a compute_vol_cone() result to vol_cones + vol_regimes."""
    if cone_result.get("error") or not cone_result.get("cone"):
        return
    ticker = cone_result["ticker"]
    as_of = cone_result["as_of"]
    lookback_years = cone_result.get("lookback_years", DEFAULT_LOOKBACK_YEARS)
    _ensure_vol_tables()
    with get_db() as db:
        for horizon, stats in cone_result["cone"].items():
            db.execute(
                """
                INSERT INTO vol_cones
                    (ticker, as_of, horizon, current_vol, p5, p25, p50, p75, p95, n_obs, lookback_years)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker, as_of, horizon) DO UPDATE SET
                    current_vol=excluded.current_vol,
                    p5=excluded.p5, p25=excluded.p25, p50=excluded.p50,
                    p75=excluded.p75, p95=excluded.p95,
                    n_obs=excluded.n_obs, lookback_years=excluded.lookback_years
                """,
                (ticker, as_of, int(horizon),
                 stats["current"], stats["p5"], stats["p25"], stats["p50"],
                 stats["p75"], stats["p95"], stats["n_obs"], lookback_years),
            )
        regime = cone_result.get("current_regime", "normal")
        multiplier = REGIME_MULTIPLIERS.get(regime, 2.0)
        vol_20d = cone_result["cone"].get(20, {}).get("current")
        db.execute(
            """
            INSERT INTO vol_regimes (ticker, as_of, regime, multiplier, vol_20d)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker, as_of) DO UPDATE SET
                regime=excluded.regime,
                multiplier=excluded.multiplier,
                vol_20d=excluded.vol_20d
            """,
            (ticker, as_of, regime, multiplier, vol_20d),
        )


def _percentile_position(current: float, stats: dict) -> int:
    """Roughly place ``current`` in the cone's percentile distribution."""
    pts = [(5, stats["p5"]), (25, stats["p25"]), (50, stats["p50"]),
           (75, stats["p75"]), (95, stats["p95"])]
    if current <= pts[0][1]:
        return 5
    if current >= pts[-1][1]:
        return 95
    for (p1, v1), (p2, v2) in zip(pts[:-1], pts[1:]):
        if v1 <= current <= v2:
            if v2 == v1:
                return p2
            frac = (current - v1) / (v2 - v1)
            return int(round(p1 + frac * (p2 - p1)))
    return 50


def _cli_main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    _ensure_vol_tables()
    with get_db() as db:
        rows = db.execute(
            "SELECT DISTINCT ticker FROM trades WHERE exit_date IS NULL ORDER BY ticker"
        ).fetchall()
    tickers = [r["ticker"] for r in rows]
    if not tickers:
        print("No open positions found.")
        return 0

    today = date.today().isoformat()
    print(f"VOLATILITY CONES -- {today}")
    print("=" * 70)
    print(f"{'Ticker':<9}{'20d Vol':<10}{'Position':<11}{'Regime':<10}{'k':<5}{'Stop Dist':<12}")
    print("-" * 70)

    for ticker in tickers:
        cone_result = compute_vol_cone(ticker)
        if cone_result.get("error"):
            print(f"{ticker:<9}  ERROR: {cone_result['error']}")
            continue
        persist_vol_cone(cone_result)
        c20 = cone_result["cone"].get(20)
        if c20 is None:
            print(f"{ticker:<9}  no 20d cone")
            continue
        pct_pos = _percentile_position(c20["current"], c20)
        regime = cone_result["current_regime"]
        k = REGIME_MULTIPLIERS[regime]
        vol_daily = c20["current"] / math.sqrt(TRADING_DAYS_PER_YEAR)
        stop_dist_pct = k * vol_daily
        print(f"{ticker:<9}{c20['current']*100:>6.1f}%   "
              f"P{pct_pos:<9}{regime:<10}{k:<5.1f}{stop_dist_pct*100:>6.2f}%")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli_main())
