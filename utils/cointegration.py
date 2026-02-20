"""
Atlas-ASX Cointegration Analysis Module (Phase 9)
===================================================
Pair discovery, cointegration testing, and caching for
the pairs-trading signal filter.

Functions:
    - calculate_half_life: AR(1) half-life of mean reversion
    - calculate_hurst_exponent: R/S analysis for mean-reversion detection
    - get_pair_zscore: Rolling z-score of the pair spread
    - find_cointegrated_pairs: Full pair discovery pipeline
    - load_or_build_pair_universe: Cached pair universe management

Usage:
    from utils.cointegration import load_or_build_pair_universe, get_pair_zscore
"""

import json
import logging
import time
from datetime import datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats as scipy_stats
from statsmodels.tsa.stattools import coint
from statsmodels.regression.linear_model import OLS
from statsmodels.tools import add_constant

logger = logging.getLogger(__name__)


def calculate_half_life(spread_series: pd.Series) -> float:
    """Calculate the half-life of mean reversion using AR(1) regression.

    Regresses spread[t] on spread[t-1]. The half-life is:
        half_life = -log(2) / log(coefficient)

    Args:
        spread_series: Series of spread values.

    Returns:
        Half-life in days (float). Returns np.inf if non-mean-reverting.
    """
    spread = spread_series.dropna()
    if len(spread) < 20:
        return np.inf

    lag = spread.shift(1)
    delta = spread - lag

    # Drop NaN from shift
    lag = lag.iloc[1:]
    delta = delta.iloc[1:]

    try:
        lag_const = add_constant(lag)
        model = OLS(delta, lag_const).fit()
        beta = model.params.iloc[1] if hasattr(model.params, 'iloc') else model.params[1]

        if beta >= 0:
            return np.inf  # Not mean-reverting

        half_life = -np.log(2) / np.log(1 + beta)
        return max(half_life, 0.1)  # Floor at 0.1 days
    except Exception as e:
        logger.debug(f"Half-life calculation failed: {e}")
        return np.inf


def calculate_hurst_exponent(series: pd.Series, max_lag: int = 100) -> float:
    """Calculate the Hurst exponent using the variance ratio method.

    For a series with Hurst exponent H, the variance of k-period
    differences scales as k^(2H). We regress log(Var) on log(k)
    and extract H = slope / 2.

    H < 0.5: mean-reverting (good for pairs trading)
    H = 0.5: random walk
    H > 0.5: trending

    Args:
        series: Price or spread series.
        max_lag: Maximum lag for variance ratio calculation.

    Returns:
        Hurst exponent (float). Returns 0.5 on failure.
    """
    ts = series.dropna().values
    n = len(ts)
    if n < 40:
        return 0.5

    max_lag = min(max_lag, n // 4)
    lags = []
    variances = []

    for lag in range(2, max_lag + 1):
        diffs = ts[lag:] - ts[:-lag]
        if len(diffs) < 10:
            continue
        var = np.var(diffs, ddof=1)
        if var > 0:
            lags.append(lag)
            variances.append(var)

    if len(lags) < 5:
        return 0.5

    try:
        log_lags = np.log(lags)
        log_vars = np.log(variances)
        slope, _, _, _, _ = scipy_stats.linregress(log_lags, log_vars)
        hurst = slope / 2.0
        return float(np.clip(hurst, 0.0, 1.0))
    except Exception:
        return 0.5


def get_pair_zscore(
    prices_a: pd.Series,
    prices_b: pd.Series,
    hedge_ratio: float,
    lookback: int = 60,
) -> pd.Series:
    """Calculate the rolling z-score of the pair spread.

    Spread = prices_a - hedge_ratio * prices_b
    Z-score = (spread - rolling_mean) / rolling_std

    Args:
        prices_a: Close prices of ticker A.
        prices_b: Close prices of ticker B.
        hedge_ratio: OLS hedge ratio (beta).
        lookback: Rolling window for z-score calculation.

    Returns:
        pd.Series of z-score values.
    """
    combined = pd.concat([prices_a, prices_b], axis=1, join='inner')
    if len(combined) < lookback:
        return pd.Series(dtype=float)

    a = combined.iloc[:, 0]
    b = combined.iloc[:, 1]
    spread = a - hedge_ratio * b

    roll_mean = spread.rolling(window=lookback).mean()
    roll_std = spread.rolling(window=lookback).std(ddof=1)

    zscore = (spread - roll_mean) / roll_std.replace(0, np.nan)
    return zscore


def find_cointegrated_pairs(
    prices_dict: Dict[str, pd.Series],
    significance: float = 0.05,
    min_correlation: float = 0.65,
    min_half_life: float = 5,
    max_half_life: float = 60,
    max_hurst: float = 0.45,
) -> List[Dict[str, Any]]:
    """Discover cointegrated pairs from a universe of price series.

    Pipeline:
        1. Pre-filter by Spearman correlation >= min_correlation
        2. Run Engle-Granger cointegration test on surviving pairs
        3. Filter by p-value < significance
        4. Calculate half-life and filter by range
        5. Calculate Hurst exponent and filter by < max_hurst

    Args:
        prices_dict: Dict of {ticker: pd.Series of close prices}.
        significance: Maximum p-value for cointegration test.
        min_correlation: Minimum Spearman correlation for pre-filter.
        min_half_life: Minimum half-life in days.
        max_half_life: Maximum half-life in days.
        max_hurst: Maximum Hurst exponent (< 0.5 = mean-reverting).

    Returns:
        List of dicts sorted by p-value:
        [{ticker_a, ticker_b, pvalue, half_life, hurst, hedge_ratio, correlation}, ...]
    """
    tickers = list(prices_dict.keys())
    n_tickers = len(tickers)
    n_total_pairs = n_tickers * (n_tickers - 1) // 2
    logger.info(
        f"Cointegration scan: {n_tickers} tickers, {n_total_pairs} total pairs"
    )

    # Step 1: Pre-filter by Spearman correlation
    t0 = time.time()
    candidate_pairs = []

    for i, j in combinations(range(n_tickers), 2):
        ta, tb = tickers[i], tickers[j]
        sa, sb = prices_dict[ta], prices_dict[tb]

        combined = pd.concat([sa, sb], axis=1, join='inner').dropna()
        if len(combined) < 120:
            continue

        corr, _ = scipy_stats.spearmanr(combined.iloc[:, 0], combined.iloc[:, 1])
        if corr >= min_correlation:
            candidate_pairs.append((ta, tb, corr, combined))

    logger.info(
        f"Pre-filter: {len(candidate_pairs)}/{n_total_pairs} pairs pass "
        f"correlation >= {min_correlation} ({time.time() - t0:.1f}s)"
    )

    # Step 2-5: Cointegration test + filters
    t1 = time.time()
    results = []

    for idx, (ta, tb, corr, combined) in enumerate(candidate_pairs):
        try:
            a_vals = combined.iloc[:, 0].values
            b_vals = combined.iloc[:, 1].values

            # Engle-Granger cointegration test
            score, pvalue, _ = coint(a_vals, b_vals)

            if pvalue >= significance:
                continue

            # OLS hedge ratio: a = alpha + beta * b
            b_const = add_constant(b_vals)
            ols_model = OLS(a_vals, b_const).fit()
            hedge_ratio = float(ols_model.params[1])

            # Spread and half-life
            spread = pd.Series(
                a_vals - hedge_ratio * b_vals,
                index=combined.index
            )
            half_life = calculate_half_life(spread)

            if half_life < min_half_life or half_life > max_half_life:
                continue

            # Hurst exponent
            hurst = calculate_hurst_exponent(spread)

            if hurst > max_hurst:
                continue

            results.append({
                'ticker_a': ta,
                'ticker_b': tb,
                'pvalue': round(float(pvalue), 6),
                'half_life': round(float(half_life), 2),
                'hurst': round(float(hurst), 4),
                'hedge_ratio': round(float(hedge_ratio), 6),
                'correlation': round(float(corr), 4),
            })

        except Exception as e:
            logger.debug(f"Pair {ta}/{tb} failed: {e}")
            continue

        if (idx + 1) % 200 == 0:
            logger.debug(
                f"Cointegration progress: {idx + 1}/{len(candidate_pairs)} "
                f"pairs tested, {len(results)} found"
            )

    results.sort(key=lambda x: x['pvalue'])

    logger.info(
        f"Cointegration scan complete: {len(results)} pairs found "
        f"({time.time() - t1:.1f}s)"
    )

    return results


def load_or_build_pair_universe(
    data_dir: str,
    prices_dict: Dict[str, pd.Series],
    cache_file: str = 'data/cache/cointegrated_pairs.json',
    max_age_days: int = 7,
    **kwargs,
) -> List[Dict[str, Any]]:
    """Load cached cointegrated pairs or build fresh.

    Args:
        data_dir: Project root directory path.
        prices_dict: Dict of {ticker: pd.Series of close prices}.
        cache_file: Relative path for cache file.
        max_age_days: Maximum age of cache in days.
        **kwargs: Passed to find_cointegrated_pairs().

    Returns:
        List of cointegrated pair dicts.
    """
    cache_path = Path(data_dir) / cache_file
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    # Check cache freshness
    if cache_path.exists():
        age = datetime.now() - datetime.fromtimestamp(cache_path.stat().st_mtime)
        if age < timedelta(days=max_age_days):
            try:
                with open(cache_path, 'r') as f:
                    cached = json.load(f)
                logger.info(
                    f"Loaded {len(cached)} cointegrated pairs from cache "
                    f"(age: {age.days}d {age.seconds // 3600}h)"
                )
                return cached
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"Cache file corrupt, rebuilding: {e}")

    # Build fresh
    logger.info("Building cointegrated pairs universe (this may take a minute)...")
    pairs = find_cointegrated_pairs(prices_dict, **kwargs)

    # Save cache
    try:
        with open(cache_path, 'w') as f:
            json.dump(pairs, f, indent=2, default=str)
        logger.info(f"Cached {len(pairs)} cointegrated pairs to {cache_path}")
    except Exception as e:
        logger.warning(f"Failed to save pair cache: {e}")

    return pairs
