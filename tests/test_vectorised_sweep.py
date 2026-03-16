"""Tests for research.vectorised_sweep — vectorised mean-reversion sweep engine.

Coverage:
  - Performance: 10 tickers × 500 days, 240 combos in < 2 seconds
  - Correctness: known oversold RSI signals detected
  - Precision: single-combo signal count matches manual expectation
  - Edge cases: empty param grid, missing keys, no-signal combos
  - Output contract: sorted by score, correct columns, one row per combo
  - Unit tests: _vectorised_rsi and _vectorised_zscore against pandas reference
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from research.vectorised_sweep import (
    _vectorised_rsi,
    _vectorised_zscore,
    sweep_mean_reversion,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_random_data(
    n_tickers: int,
    n_days: int,
    seed: int = 42,
) -> dict[str, pd.DataFrame]:
    """Synthetic OHLCV dataset for performance tests."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B")
    data: dict[str, pd.DataFrame] = {}
    for i in range(n_tickers):
        log_ret = rng.normal(0.0, 0.015, size=n_days)
        prices = 100.0 * np.exp(np.cumsum(log_ret))
        data[f"T{i:03d}"] = pd.DataFrame({"close": prices}, index=dates)
    return data


def _make_declining_ticker(
    n_days: int,
    flat_prefix: int,
    decline_pct_per_day: float,
    decline_days: int,
) -> pd.DataFrame:
    """Price series: flat for *flat_prefix* days, then declining, then flat."""
    prices: list[float] = []
    p = 100.0
    for _ in range(flat_prefix):
        prices.append(p)
    for _ in range(decline_days):
        p *= 1.0 - decline_pct_per_day
        prices.append(p)
    remainder = n_days - flat_prefix - decline_days
    for _ in range(remainder):
        prices.append(p)
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B")
    return pd.DataFrame({"close": prices}, index=dates)


# ---------------------------------------------------------------------------
# Guard: empty / incomplete param grid → empty DataFrame
# ---------------------------------------------------------------------------


class TestEmptyParamGrid:
    """Empty or malformed param_grid should return an empty DataFrame."""

    _FULL_GRID = {
        "rsi_period": [14],
        "rsi_threshold": [30],
        "zscore_lookback": [20],
        "zscore_threshold": [-2.0],
    }
    _DATA = {"A": pd.DataFrame({"close": [100.0, 101.0, 102.0]},
                               index=pd.date_range("2020-01-01", periods=3, freq="B"))}
    _EXPECTED_COLS = [
        "rsi_period", "rsi_threshold", "zscore_lookback",
        "zscore_threshold", "signal_count", "mean_return", "win_rate", "score",
    ]

    def _assert_empty(self, result: pd.DataFrame) -> None:
        assert isinstance(result, pd.DataFrame)
        assert result.empty
        assert list(result.columns) == self._EXPECTED_COLS

    def test_empty_dict(self):
        self._assert_empty(sweep_mean_reversion(self._DATA, {}))

    def test_missing_required_key(self):
        grid = dict(self._FULL_GRID)
        del grid["zscore_threshold"]
        self._assert_empty(sweep_mean_reversion(self._DATA, grid))

    def test_empty_list_for_one_key(self):
        grid = dict(self._FULL_GRID)
        grid["rsi_period"] = []
        self._assert_empty(sweep_mean_reversion(self._DATA, grid))

    def test_empty_data_dict(self):
        self._assert_empty(sweep_mean_reversion({}, self._FULL_GRID))

    def test_missing_close_column(self):
        data = {"A": pd.DataFrame({"open": [100.0, 101.0]},
                                  index=pd.date_range("2020-01-01", periods=2, freq="B"))}
        self._assert_empty(sweep_mean_reversion(data, self._FULL_GRID))


# ---------------------------------------------------------------------------
# Performance: 10 tickers × 500 days × 240 combos in < 2 seconds
# ---------------------------------------------------------------------------


def test_sweep_completes_under_2_seconds():
    """Full 240-combo grid over 10 tickers × 500 days must finish in < 2s."""
    data = _make_random_data(n_tickers=10, n_days=500)
    param_grid = {
        "rsi_period": [5, 7, 10, 14, 20],
        "rsi_threshold": [25, 30, 35, 40],
        "zscore_lookback": [15, 20, 30],
        "zscore_threshold": [-1.5, -2.0, -2.5],
    }

    t0 = time.perf_counter()
    results = sweep_mean_reversion(data, param_grid, hold_days=10)
    elapsed = time.perf_counter() - t0

    assert elapsed < 2.0, f"Sweep took {elapsed:.3f}s (limit: 2.0s)"
    # 5 rsi_period × 4 rsi_threshold × 3 zscore_lookback × 3 zscore_threshold = 180
    expected_rows = 5 * 4 * 3 * 3
    assert len(results) == expected_rows, (
        f"Expected {expected_rows} rows, got {len(results)}"
    )


# ---------------------------------------------------------------------------
# Correctness: RSI oversold signals detected on known declining data
# ---------------------------------------------------------------------------


def test_rsi_oversold_signals_detected():
    """Sharply declining prices must produce RSI < 30 and negative z-score signals."""
    # Construct: flat 20 days → decline 3%/day for 25 days → flat 35 days
    ticker_df = _make_declining_ticker(
        n_days=80,
        flat_prefix=20,
        decline_pct_per_day=0.03,
        decline_days=25,
    )
    data = {"TICKER": ticker_df}

    param_grid = {
        "rsi_period": [14],
        "rsi_threshold": [30],     # RSI → 0 during pure decline
        "zscore_lookback": [20],
        "zscore_threshold": [-1.0],  # current price well below 20-day mean
    }
    results = sweep_mean_reversion(data, param_grid, hold_days=5)

    assert not results.empty, "Expected at least one result row"
    row = results.iloc[0]

    # Must detect signals during the decline phase
    assert row["signal_count"] > 0, (
        f"Expected signal_count > 0, got {row['signal_count']}"
    )
    assert 0.0 <= row["win_rate"] <= 1.0

    # Verify column completeness
    for col in ["rsi_period", "rsi_threshold", "zscore_lookback",
                "zscore_threshold", "signal_count", "mean_return", "win_rate", "score"]:
        assert col in results.columns


def test_rsi_oversold_detected_vectorised_rsi_directly():
    """_vectorised_rsi must return values near 0 for pure-decline prices."""
    n_days = 50
    # Prices decline 2% per day starting from 100
    prices = 100.0 * np.cumprod(np.concatenate([[1.0], np.full(n_days - 1, 0.98)]))
    close_matrix = prices.reshape(1, -1)  # 1 ticker × 50 days

    rsi_out = _vectorised_rsi(close_matrix, [14])  # (1, 50, 1)

    # First 14 values should be NaN (insufficient history)
    assert np.all(np.isnan(rsi_out[0, :14, 0])), "Expected NaN before period"

    # From index 14 onward: RSI should be extremely low (< 10) due to pure decline
    valid_rsi = rsi_out[0, 14:, 0]
    assert np.all(valid_rsi < 10.0), (
        f"Expected all RSI < 10 during pure decline, max={np.nanmax(valid_rsi):.2f}"
    )


# ---------------------------------------------------------------------------
# Precision: single-combo signal count matches manual calculation
# ---------------------------------------------------------------------------


def test_single_combo_expected_signal_count():
    """Monotonically decreasing prices yield an exact, predictable signal count.

    Setup:
      - 1 ticker, 50 trading days, prices = [100, 99, 98, ..., 51] (−1/day)
      - rsi_period=5, rsi_threshold=30  → RSI≈0 < 30 for all valid RSI dates
      - zscore_lookback=5, zscore_threshold=0.0  → z < 0 always (current price
        is always the lowest point in the window for a monotonically declining
        series)
      - hold_days=5

    Expected valid region:
      - RSI valid from close_idx=5 (first n_obs=5 diffs processed)
      - z-score valid from date_idx=4 (lb−1 = 4)
      - Forward return valid for date_idx=0..44 (need idx+5 ≤ 49)
      - Both indicators & forward return valid: date_idx=5..44 → 40 signals
    """
    n_days = 50
    prices = [100.0 - float(i) for i in range(n_days)]  # 100, 99, ..., 51
    dates = pd.date_range("2020-01-01", periods=n_days, freq="B")
    data = {"X": pd.DataFrame({"close": prices}, index=dates)}

    param_grid = {
        "rsi_period": [5],
        "rsi_threshold": [30],   # RSI ≈ 0 throughout
        "zscore_lookback": [5],
        "zscore_threshold": [0.0],  # z always negative for declining prices
    }

    results = sweep_mean_reversion(data, param_grid, hold_days=5)

    assert len(results) == 1, f"Expected 1 row, got {len(results)}"
    row = results.iloc[0]

    # Exact count verification:
    # RSI(5) valid from idx=5; zscore(5) valid from idx=4; fwd valid to idx=44
    # Signal region: idx 5..44 → 40 dates
    assert row["signal_count"] == 40, (
        f"Expected signal_count=40, got {row['signal_count']}"
    )

    # All forward returns from a pure decline are negative (price keeps falling)
    assert row["mean_return"] < 0.0, "Expected negative mean return for declining prices"
    assert row["win_rate"] == 0.0, "Expected win_rate=0 for purely declining prices"

    # Score = mean_return × sqrt(signal_count) — should be negative
    expected_score = row["mean_return"] * np.sqrt(40)
    assert abs(row["score"] - expected_score) < 1e-10, (
        f"Score mismatch: {row['score']} vs {expected_score}"
    )


# ---------------------------------------------------------------------------
# Output contract: sorted by score, correct columns, all combos present
# ---------------------------------------------------------------------------


def test_results_sorted_descending_by_score():
    """Results must be sorted in descending order by 'score'."""
    data = _make_random_data(n_tickers=5, n_days=200, seed=7)
    param_grid = {
        "rsi_period": [5, 14],
        "rsi_threshold": [30, 40],
        "zscore_lookback": [10, 20],
        "zscore_threshold": [-1.5, -2.0],
    }
    results = sweep_mean_reversion(data, param_grid, hold_days=5)

    scores = results["score"].values
    # Replace -inf with a very small number so diff is still valid
    finite_scores = np.where(np.isfinite(scores), scores, -1e18)
    assert np.all(np.diff(finite_scores) <= 0), "Results not sorted descending by score"


def test_all_parameter_combinations_returned():
    """len(results) == product of all param list lengths."""
    data = _make_random_data(n_tickers=3, n_days=300, seed=99)
    param_grid = {
        "rsi_period": [5, 10, 14],
        "rsi_threshold": [25, 35],
        "zscore_lookback": [15, 25],
        "zscore_threshold": [-1.5, -2.0, -2.5],
    }
    results = sweep_mean_reversion(data, param_grid)
    expected = 3 * 2 * 2 * 3  # = 36
    assert len(results) == expected, f"Expected {expected} rows, got {len(results)}"


def test_output_columns_complete_and_ordered():
    """Output DataFrame must have exactly the required columns in order."""
    data = _make_random_data(n_tickers=2, n_days=100, seed=1)
    param_grid = {
        "rsi_period": [14],
        "rsi_threshold": [30],
        "zscore_lookback": [20],
        "zscore_threshold": [-2.0],
    }
    results = sweep_mean_reversion(data, param_grid)
    expected_cols = [
        "rsi_period", "rsi_threshold", "zscore_lookback",
        "zscore_threshold", "signal_count", "mean_return", "win_rate", "score",
    ]
    assert list(results.columns) == expected_cols


# ---------------------------------------------------------------------------
# Unit tests: _vectorised_rsi internals
# ---------------------------------------------------------------------------


class TestVectorisedRSI:
    """Unit tests for _vectorised_rsi."""

    def test_nan_before_min_period(self):
        """Close indices 0..period-1 must be NaN."""
        prices = np.arange(1.0, 31.0).reshape(1, 30)  # 1 ticker, 30 days
        for period in [5, 10, 14]:
            out = _vectorised_rsi(prices, [period])
            assert np.all(np.isnan(out[0, :period, 0])), (
                f"Expected NaN for first {period} indices (period={period})"
            )
            assert np.all(~np.isnan(out[0, period:, 0])), (
                f"Expected non-NaN from index {period} onward (period={period})"
            )

    def test_output_shape(self):
        """Output shape must be (T, D, P)."""
        T, D = 3, 40
        prices = np.random.default_rng(0).uniform(50, 150, (T, D))
        periods = [5, 10, 14]
        out = _vectorised_rsi(prices, periods)
        assert out.shape == (T, D, len(periods))

    def test_rsi_range(self):
        """All non-NaN RSI values must be in [0, 100]."""
        rng = np.random.default_rng(42)
        prices = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, (5, 200)), axis=1))
        out = _vectorised_rsi(prices, [5, 14, 20])
        valid = out[~np.isnan(out)]
        assert np.all(valid >= 0.0), f"RSI below 0: min={valid.min()}"
        assert np.all(valid <= 100.0), f"RSI above 100: max={valid.max()}"

    def test_pure_up_days_rsi_near_100(self):
        """Monotonically increasing prices → avg_loss≈0 → RSI≈100."""
        prices = np.arange(1.0, 51.0).reshape(1, 50)
        out = _vectorised_rsi(prices, [5])
        valid = out[0, 5:, 0]
        assert np.all(valid > 90.0), f"Expected RSI near 100 for up prices, got min={valid.min()}"

    def test_pure_down_days_rsi_near_0(self):
        """Monotonically decreasing prices → avg_gain=0 → RSI=0."""
        prices = np.arange(50.0, 0.0, -1.0).reshape(1, 50)
        out = _vectorised_rsi(prices, [5])
        valid = out[0, 5:, 0]
        assert np.all(valid < 10.0), f"Expected RSI near 0 for down prices, got max={valid.max()}"

    def test_matches_pandas_reference(self):
        """Vectorised RSI must match utils.helpers.calc_rsi within tolerance."""
        from utils.helpers import calc_rsi  # noqa: PLC0415

        rng = np.random.default_rng(12345)
        prices = 100.0 + np.cumsum(rng.normal(0, 0.5, 200))
        close_matrix = prices.reshape(1, -1)

        for period in [5, 10, 14, 20]:
            out = _vectorised_rsi(close_matrix, [period])
            ref = calc_rsi(pd.Series(prices), period=period).to_numpy()

            # Align: both should be NaN before index `period`
            numpy_rsi = out[0, :, 0]

            # Find the common valid region (both non-NaN)
            valid = ~np.isnan(numpy_rsi) & ~np.isnan(ref)
            assert valid.sum() > 100, f"Too few valid points for period={period}"

            np.testing.assert_allclose(
                numpy_rsi[valid],
                ref[valid],
                rtol=1e-5,
                atol=1e-6,
                err_msg=f"RSI mismatch for period={period}",
            )

    def test_empty_periods_list(self):
        """Empty periods list returns all-NaN array of shape (T, D, 0)."""
        prices = np.ones((3, 20))
        out = _vectorised_rsi(prices, [])
        assert out.shape == (3, 20, 0)

    def test_period_exceeds_data_length(self):
        """Period ≥ D returns all-NaN slice."""
        prices = np.arange(1.0, 11.0).reshape(1, 10)
        out = _vectorised_rsi(prices, [10, 15])  # both >= D
        # period=10 >= D=10, period=15 >= D → both slices all NaN
        assert np.all(np.isnan(out))


# ---------------------------------------------------------------------------
# Unit tests: _vectorised_zscore internals
# ---------------------------------------------------------------------------


class TestVectorisedZScore:
    """Unit tests for _vectorised_zscore."""

    def test_nan_before_min_lookback(self):
        """Close indices 0..lookback-2 must be NaN."""
        prices = np.arange(1.0, 31.0).reshape(1, 30)
        for lb in [5, 10, 20]:
            out = _vectorised_zscore(prices, [lb])
            assert np.all(np.isnan(out[0, : lb - 1, 0])), (
                f"Expected NaN before index {lb - 1} (lookback={lb})"
            )
            assert np.all(~np.isnan(out[0, lb - 1 :, 0])), (
                f"Expected non-NaN from index {lb - 1} (lookback={lb})"
            )

    def test_output_shape(self):
        """Output shape must be (T, D, L)."""
        T, D = 4, 60
        prices = np.random.default_rng(0).uniform(50, 150, (T, D))
        lookbacks = [10, 20, 30]
        out = _vectorised_zscore(prices, lookbacks)
        assert out.shape == (T, D, len(lookbacks))

    def test_declining_prices_negative_zscore(self):
        """Monotonically decreasing prices → current price below rolling mean → z < 0."""
        prices = np.arange(100.0, 50.0, -1.0).reshape(1, 50)  # 100..51
        out = _vectorised_zscore(prices, [10])
        valid = out[0, 9:, 0]  # from lb-1=9 onward
        assert np.all(valid < 0.0), f"Expected z < 0 for declining prices, max={valid.max()}"

    def test_matches_pandas_reference(self):
        """Vectorised z-score must match utils.helpers.calc_zscore within tolerance."""
        from utils.helpers import calc_zscore  # noqa: PLC0415

        rng = np.random.default_rng(77)
        prices = 100.0 + np.cumsum(rng.normal(0, 0.5, 200))
        close_matrix = prices.reshape(1, -1)

        for lb in [10, 20, 30]:
            out = _vectorised_zscore(close_matrix, [lb])
            ref = calc_zscore(pd.Series(prices), lookback=lb).to_numpy()

            numpy_z = out[0, :, 0]
            valid = ~np.isnan(numpy_z) & ~np.isnan(ref)
            assert valid.sum() > 100, f"Too few valid points for lookback={lb}"

            np.testing.assert_allclose(
                numpy_z[valid],
                ref[valid],
                rtol=1e-6,
                atol=1e-6,
                err_msg=f"Z-score mismatch for lookback={lb}",
            )

    def test_empty_lookbacks_list(self):
        """Empty lookbacks list returns all-NaN array of shape (T, D, 0)."""
        prices = np.ones((3, 20))
        out = _vectorised_zscore(prices, [])
        assert out.shape == (3, 20, 0)

    def test_constant_prices_zscore_nan(self):
        """Constant prices have std=0 → z-score must be NaN (no division by zero error)."""
        prices = np.ones((2, 30)) * 50.0
        out = _vectorised_zscore(prices, [10])
        valid_region = out[:, 9:, 0]  # from lb-1 onward
        assert np.all(np.isnan(valid_region)), "Expected NaN z-score for constant prices"


# ---------------------------------------------------------------------------
# Integration: no-signal combos handled gracefully
# ---------------------------------------------------------------------------


def test_no_signal_combos_have_score_neg_inf():
    """Parameter combos with zero signals must have score = -inf."""
    data = _make_random_data(n_tickers=2, n_days=100, seed=5)
    param_grid = {
        "rsi_period": [14],
        "rsi_threshold": [0.0],  # RSI < 0 → impossible, zero signals
        "zscore_lookback": [20],
        "zscore_threshold": [-10.0],  # zscore < -10 → extremely rare
    }
    results = sweep_mean_reversion(data, param_grid)
    assert len(results) == 1
    assert results.iloc[0]["signal_count"] == 0
    assert results.iloc[0]["score"] == -np.inf
    assert np.isnan(results.iloc[0]["mean_return"])
    assert np.isnan(results.iloc[0]["win_rate"])


def test_multiple_tickers_date_intersection():
    """Different-length ticker histories are correctly intersected."""
    dates_full = pd.date_range("2020-01-01", periods=100, freq="B")
    dates_short = pd.date_range("2020-03-01", periods=60, freq="B")

    data = {
        "LONG": pd.DataFrame({"close": np.linspace(100, 150, 100)}, index=dates_full),
        "SHORT": pd.DataFrame({"close": np.linspace(80, 120, 60)}, index=dates_short),
    }
    param_grid = {
        "rsi_period": [14],
        "rsi_threshold": [60],   # generous threshold to get some signals
        "zscore_lookback": [10],
        "zscore_threshold": [2.0],  # z < 2 is usually always true
    }
    # Should not raise; date intersection is handled internally
    results = sweep_mean_reversion(data, param_grid)
    assert isinstance(results, pd.DataFrame)
    assert len(results) == 1
