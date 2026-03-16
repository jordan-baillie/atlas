#!/usr/bin/env python3
"""Vectorised mean-reversion parameter sweep engine.

Replaces the serial backtest loop for mean-reversion signal research.
Precomputes RSI and z-score for **all tickers × dates × parameters**
simultaneously using NumPy vectorisation, then scores every parameter
combination in a single forward-return pass.

Typical speed-up vs. serial sweep: 20-50× for a 500-ticker universe with
240 parameter combinations (5 RSI periods × 4 RSI thresholds × 3 zscore
lookbacks × 4 zscore thresholds).

Usage example::

    from research.vectorised_sweep import sweep_mean_reversion

    param_grid = {
        "rsi_period":       [5, 7, 10, 14, 20],
        "rsi_threshold":    [25, 30, 35, 40],
        "zscore_lookback":  [15, 20, 30],
        "zscore_threshold": [-1.5, -2.0, -2.5],
    }
    results = sweep_mean_reversion(data, param_grid, hold_days=10)
    print(results.head(10))
"""

from __future__ import annotations

import itertools
import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = [
    "sweep_mean_reversion",
    "_vectorised_rsi",
    "_vectorised_zscore",
]

# Keys required in param_grid
_REQUIRED_KEYS: tuple[str, ...] = (
    "rsi_period",
    "rsi_threshold",
    "zscore_lookback",
    "zscore_threshold",
)

# Output columns in canonical order
_OUTPUT_COLS: List[str] = [
    "rsi_period",
    "rsi_threshold",
    "zscore_lookback",
    "zscore_threshold",
    "signal_count",
    "mean_return",
    "win_rate",
    "score",
]


# ---------------------------------------------------------------------------
# Core vectorised indicator functions
# ---------------------------------------------------------------------------


def _vectorised_rsi(close_matrix: np.ndarray, periods: List[int]) -> np.ndarray:
    """Compute RSI for all tickers × dates × periods simultaneously.

    Uses Wilder's exponential weighted mean (``adjust=False``) to match
    the reference implementation in ``utils.helpers.calc_rsi``.

    Specifically, for each period *p*:

    * ``alpha = 1 / p``
    * State initialised from the first price difference (``y[0] = x[0]``).
    * ``y[t] = (1 - alpha) * y[t-1] + alpha * x[t]``
    * Results are ``NaN`` for close indices ``0 .. period - 1`` (insufficient
      history to accumulate *p* observations).

    Parameters
    ----------
    close_matrix:
        Shape ``(T, D)`` — *T* tickers aligned to a common *D*-length date
        index.  Values must be positive close prices.
    periods:
        List of RSI look-back periods to compute.  Duplicate-free; order
        preserved in the output's third axis.

    Returns
    -------
    np.ndarray
        Shape ``(T, D, P)`` where ``P = len(periods)``.  Close-date columns
        before index ``period`` are ``NaN`` (insufficient history).
    """
    T, D = close_matrix.shape
    P = len(periods)
    result = np.full((T, D, P), np.nan, dtype=np.float64)

    if D < 2 or P == 0:
        return result

    # shape (T, D-1): diff[t, d] = close[t, d+1] − close[t, d]
    diff = np.diff(close_matrix.astype(np.float64), axis=1)
    gains = np.where(diff > 0.0, diff, 0.0)   # (T, D-1)
    losses = np.where(diff < 0.0, -diff, 0.0)  # (T, D-1)

    for pi, period in enumerate(periods):
        if period < 1 or period >= D:
            continue  # not enough data for this period

        alpha = 1.0 / period
        one_minus_alpha = 1.0 - alpha

        # Initialise EWM state from diff[0]  (adjust=False: y[0] = x[0])
        avg_gain = gains[:, 0].copy()   # shape (T,)
        avg_loss = losses[:, 0].copy()  # shape (T,)

        # Write initial-state RSI if period == 1 (n_obs=1 already satisfies min_periods)
        if period == 1:
            with np.errstate(invalid="ignore", divide="ignore"):
                rs = avg_gain / avg_loss   # 0/0→NaN, x/0→inf, 0/x→0
                result[:, 1, pi] = 100.0 - (100.0 / (1.0 + rs))

        # Iterate over diffs[1..D-2]; state carries forward
        for d in range(1, D - 1):
            avg_gain = one_minus_alpha * avg_gain + alpha * gains[:, d]
            avg_loss = one_minus_alpha * avg_loss + alpha * losses[:, d]

            # n_obs = d + 1  (diffs 0..d processed, including init from diff[0])
            # write only once we have ≥ period observations
            if d + 1 >= period:
                close_idx = d + 1  # diff[d] = close[d+1] − close[d]
                with np.errstate(invalid="ignore", divide="ignore"):
                    rs = avg_gain / avg_loss   # 0/0→NaN, x/0→inf, 0/x→0
                    result[:, close_idx, pi] = 100.0 - (100.0 / (1.0 + rs))

    return result


def _vectorised_zscore(close_matrix: np.ndarray, lookbacks: List[int]) -> np.ndarray:
    """Compute rolling z-score for all tickers × dates × lookbacks simultaneously.

    Uses prefix-sum vectorisation: computes ``cumsum`` and ``cumsum²`` once,
    then derives window mean and variance for every lookback in O(1) slices —
    no inner date loop.

    Z-score definition (matches ``utils.helpers.calc_zscore``)::

        z[t] = (close[t] - mean(close[t-lb+1 : t+1])) / std(close[t-lb+1 : t+1], ddof=1)

    Parameters
    ----------
    close_matrix:
        Shape ``(T, D)`` — *T* tickers, *D* trading dates.
    lookbacks:
        List of rolling window sizes.  Must be ≥ 2 (``ddof=1`` requires at
        least two points for a non-degenerate std).

    Returns
    -------
    np.ndarray
        Shape ``(T, D, L)`` where ``L = len(lookbacks)``.  Close-date columns
        before index ``lookback - 1`` are ``NaN``.  Dates with zero rolling
        standard deviation are also ``NaN``.
    """
    T, D = close_matrix.shape
    L = len(lookbacks)
    result = np.full((T, D, L), np.nan, dtype=np.float64)

    if D < 1 or L == 0:
        return result

    cm = close_matrix.astype(np.float64)

    # Padded prefix sums: padded[t, k] = sum(close[t, 0..k-1])
    # This lets us compute window sums as padded[d+1] - padded[d-lb+1].
    pad = np.zeros((T, 1), dtype=np.float64)
    padded_s1 = np.concatenate([pad, np.cumsum(cm, axis=1)], axis=1)     # (T, D+1)
    padded_s2 = np.concatenate([pad, np.cumsum(cm ** 2, axis=1)], axis=1)  # (T, D+1)

    for li, lb in enumerate(lookbacks):
        if lb < 2 or lb > D:
            # Need ≥ 2 points for ddof=1 std; skip if not enough data
            continue

        n_valid = D - lb + 1  # number of valid date positions

        # Window sum of close and close² for all valid dates at once
        # valid dates: d = lb-1, lb, ..., D-1
        # sum of window ending at d = padded[d+1] - padded[d-lb+1]
        #   => index slices: [lb : D+1] and [0 : D-lb+1]
        s1 = padded_s1[:, lb : D + 1] - padded_s1[:, 0 : D - lb + 1]   # (T, n_valid)
        s2 = padded_s2[:, lb : D + 1] - padded_s2[:, 0 : D - lb + 1]   # (T, n_valid)

        mean_w = s1 / lb  # (T, n_valid)

        # Variance with ddof=1:  var = (Σx² − n·mean²) / (n−1)
        var_w = (s2 - (s1 ** 2) / lb) / (lb - 1)
        # Guard against tiny floating-point negatives before sqrt
        std_w = np.sqrt(np.maximum(var_w, 0.0))

        # Close values at valid dates: close[:, lb-1 : D]
        close_valid = cm[:, lb - 1 : D]  # (T, n_valid)

        with np.errstate(invalid="ignore", divide="ignore"):
            z = np.where(std_w == 0.0, np.nan, (close_valid - mean_w) / std_w)

        result[:, lb - 1 : D, li] = z

    return result


# ---------------------------------------------------------------------------
# Public sweep entry point
# ---------------------------------------------------------------------------


def sweep_mean_reversion(
    data: Dict[str, pd.DataFrame],
    param_grid: Dict[str, List],
    hold_days: int = 10,
) -> pd.DataFrame:
    """Grid-search mean-reversion entry parameters over a ticker universe.

    Precomputes all RSI and z-score matrices once, then scores every
    parameter combination using vectorised NumPy array operations.

    Entry signal: ``RSI(rsi_period) < rsi_threshold  AND
                   zscore(zscore_lookback) < zscore_threshold``

    Score metric: ``mean_return × sqrt(signal_count)`` — balances per-trade
    quality with statistical sample size.  Parameter combos with zero signals
    receive ``score = −∞``.

    Parameters
    ----------
    data:
        Dict mapping ticker symbol → OHLCV ``DataFrame``.  Each DataFrame
        **must** have a ``"close"`` column (case-sensitive).  DataFrames with
        different date ranges are automatically intersected.
    param_grid:
        Dict with four required keys, each mapping to a non-empty list:

        * ``"rsi_period"``       — RSI look-back periods to test.
        * ``"rsi_threshold"``    — RSI oversold thresholds (signal when
          ``RSI < threshold``).
        * ``"zscore_lookback"``  — Z-score rolling windows.
        * ``"zscore_threshold"`` — Z-score entry thresholds (signal when
          ``z-score < threshold``).
    hold_days:
        Number of trading days to hold after entry.  Used to compute forward
        returns ``(close[t+hold] − close[t]) / close[t]``.

    Returns
    -------
    pd.DataFrame
        Columns: ``rsi_period``, ``rsi_threshold``, ``zscore_lookback``,
        ``zscore_threshold``, ``signal_count``, ``mean_return``, ``win_rate``,
        ``score``.  Sorted **descending** by ``score``.  One row per
        parameter combination.  Returns an empty DataFrame (same columns) if
        the param grid is empty, missing keys, or no usable data is found.
    """
    _empty = pd.DataFrame(columns=_OUTPUT_COLS)

    # ── guard: empty / invalid param grid ────────────────────────────────
    if not param_grid or any(not param_grid.get(k) for k in _REQUIRED_KEYS):
        return _empty

    if not data:
        return _empty

    # ── build close matrix ────────────────────────────────────────────────
    # Sort tickers for deterministic ordering
    tickers = sorted(data.keys())

    # Intersect date indices across all tickers that have a 'close' column
    valid_tickers: List[str] = []
    common_idx: Optional[pd.Index] = None

    for tk in tickers:
        df = data[tk]
        if "close" not in df.columns:
            logger.warning("Ticker %s missing 'close' column — skipping", tk)
            continue
        valid_tickers.append(tk)
        idx = df.index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)

    if common_idx is None or len(common_idx) == 0:
        logger.warning("No common dates found across tickers")
        return _empty

    common_idx = common_idx.sort_values()
    D = len(common_idx)
    T = len(valid_tickers)

    if T == 0 or D < 2:
        return _empty

    close_matrix = np.empty((T, D), dtype=np.float64)
    for i, tk in enumerate(valid_tickers):
        close_matrix[i] = data[tk].loc[common_idx, "close"].to_numpy(dtype=np.float64)

    # ── precompute indicator matrices ─────────────────────────────────────
    rsi_periods: List[int] = list(param_grid["rsi_period"])
    zsc_lookbacks: List[int] = list(param_grid["zscore_lookback"])

    logger.debug(
        "Computing RSI×%d and ZScore×%d for %d tickers × %d dates",
        len(rsi_periods),
        len(zsc_lookbacks),
        T,
        D,
    )

    rsi_matrix = _vectorised_rsi(close_matrix, rsi_periods)    # (T, D, P)
    zsc_matrix = _vectorised_zscore(close_matrix, zsc_lookbacks)  # (T, D, L)

    # ── forward returns ───────────────────────────────────────────────────
    # fwd_ret[t, d] = (close[t, d+hold] − close[t, d]) / close[t, d]
    # Valid only for d in [0, D − hold_days − 1]; remainder stays NaN.
    fwd_ret = np.full((T, D), np.nan, dtype=np.float64)
    if hold_days > 0 and hold_days < D:
        with np.errstate(invalid="ignore", divide="ignore"):
            denom = close_matrix[:, : D - hold_days]
            # Guard against zero close prices
            fwd_ret[:, : D - hold_days] = np.where(
                denom == 0.0,
                np.nan,
                (close_matrix[:, hold_days:] - denom) / denom,
            )

    # ── grid search ───────────────────────────────────────────────────────
    results = []

    for rsi_p, rsi_th, zsc_lb, zsc_th in itertools.product(
        param_grid["rsi_period"],
        param_grid["rsi_threshold"],
        param_grid["zscore_lookback"],
        param_grid["zscore_threshold"],
    ):
        pi = rsi_periods.index(rsi_p)
        li = zsc_lookbacks.index(zsc_lb)

        # Boolean signal mask (T, D) — entry condition
        rsi_sig = rsi_matrix[:, :, pi] < rsi_th   # (T, D) bool
        zsc_sig = zsc_matrix[:, :, li] < zsc_th   # (T, D) bool

        # Intersect with valid forward returns (NaN → no usable trade)
        signal_mask: np.ndarray = rsi_sig & zsc_sig & ~np.isnan(fwd_ret)

        signal_count = int(signal_mask.sum())

        if signal_count == 0:
            results.append(
                {
                    "rsi_period": rsi_p,
                    "rsi_threshold": rsi_th,
                    "zscore_lookback": zsc_lb,
                    "zscore_threshold": zsc_th,
                    "signal_count": 0,
                    "mean_return": np.nan,
                    "win_rate": np.nan,
                    "score": -np.inf,
                }
            )
            continue

        rets = fwd_ret[signal_mask]
        mean_ret = float(np.mean(rets))
        win_rate = float(np.mean(rets > 0.0))
        score = mean_ret * float(np.sqrt(signal_count))

        results.append(
            {
                "rsi_period": rsi_p,
                "rsi_threshold": rsi_th,
                "zscore_lookback": zsc_lb,
                "zscore_threshold": zsc_th,
                "signal_count": signal_count,
                "mean_return": mean_ret,
                "win_rate": win_rate,
                "score": score,
            }
        )

    if not results:
        return _empty

    df = pd.DataFrame(results, columns=_OUTPUT_COLS)
    df = df.sort_values("score", ascending=False).reset_index(drop=True)
    return df
