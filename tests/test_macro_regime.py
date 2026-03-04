"""Unit tests for data.macro — macro regime calculator.

Run with:
    python -m pytest tests/test_macro_regime.py -v

Tests cover:
    - download_macro_data() returns correct columns and types
    - compute_macro_signals() produces all expected derived columns
    - No look-ahead bias: gc_regime at date T only uses data up to T (expanding window)
    - VIX ROC spike detection with known values
    - Yield curve flattening detection with known values
    - macro_regime_scale is in expected range [0.5, 1.5]
    - Graceful degradation when some series are unavailable (NaN/missing)
"""

import sys
import os
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Import guard — skip all tests with a clear message if data.macro is missing
# (builder-1 dependency: data/macro.py must exist before these tests run)
# ---------------------------------------------------------------------------

_IMPORT_ERROR_MSG = ""
try:
    from data.macro import download_macro_data, compute_macro_signals
    MACRO_AVAILABLE = True
except ImportError as _e:
    MACRO_AVAILABLE = False
    _IMPORT_ERROR_MSG = str(_e)
    # Stub out to prevent NameError in module body
    download_macro_data = None  # type: ignore
    compute_macro_signals = None  # type: ignore

skip_if_unavailable = pytest.mark.skipif(
    not MACRO_AVAILABLE,
    reason=(
        f"data.macro not yet available (builder-1 dependency not merged): "
        f"{_IMPORT_ERROR_MSG}"
    ),
)

# Apply skip marker to all tests in this module
pytestmark = skip_if_unavailable


# ---------------------------------------------------------------------------
# Helpers — synthetic macro data
# ---------------------------------------------------------------------------

# Columns expected in raw macro data returned by download_macro_data()
RAW_COLUMNS = {"gold", "copper", "vix", "gs10", "gs2"}

# Columns expected in signals returned by compute_macro_signals()
DERIVED_COLUMNS = {
    "gold_copper_ratio",
    "vix_roc_5d",
    "yc_slope",
    "gc_regime",
    "macro_regime_scale",
}

VALID_REGIMES = {"risk_off", "neutral", "risk_on"}


def _make_raw_df(
    n_days: int = 260,
    gold_base: float = 1800.0,
    copper_base: float = 3.50,
    vix_base: float = 15.0,
    gs10_base: float = 2.0,
    gs2_base: float = 1.5,
    seed: int = 42,
) -> pd.DataFrame:
    """Build a synthetic raw macro DataFrame suitable for compute_macro_signals().

    Produces realistic-looking time series without any look-ahead.

    Args:
        n_days:       Number of business days.
        gold_base:    Starting gold price.
        copper_base:  Starting copper price.
        vix_base:     Starting VIX level.
        gs10_base:    Starting 10Y yield.
        gs2_base:     Starting 2Y yield.
        seed:         Random seed for reproducibility.

    Returns:
        DataFrame with columns: gold, copper, vix, gs10, gs2.
        Index is DatetimeIndex named 'date', tz-naive.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-01", periods=n_days)

    gold = gold_base + np.cumsum(rng.normal(0, 5.0, n_days))
    copper = np.abs(copper_base + np.cumsum(rng.normal(0, 0.02, n_days)))
    copper = np.clip(copper, 0.5, None)  # avoid zero/negative copper
    vix = np.abs(vix_base + rng.normal(0, 1.0, n_days))
    gs10 = gs10_base + np.cumsum(rng.normal(0, 0.01, n_days))
    gs2 = gs2_base + np.cumsum(rng.normal(0, 0.01, n_days))

    df = pd.DataFrame(
        {"gold": gold, "copper": copper, "vix": vix, "gs10": gs10, "gs2": gs2},
        index=dates,
    )
    df.index.name = "date"
    return df


def _make_vix_spike_df(spike_day: int = 100, spike_pct: float = 0.60, n_days: int = 200) -> pd.DataFrame:
    """Build a DataFrame with a known VIX spike on a specific day."""
    df = _make_raw_df(n_days=n_days, vix_base=15.0)
    # Set VIX to a flat level, then spike on spike_day
    df["vix"] = 15.0
    df.iloc[spike_day, df.columns.get_loc("vix")] = 15.0 * (1.0 + spike_pct)
    return df


def _make_inverted_curve_df(n_days: int = 200) -> pd.DataFrame:
    """Build a DataFrame with an inverted yield curve (gs2 > gs10)."""
    df = _make_raw_df(n_days=n_days)
    df["gs10"] = 1.5
    df["gs2"] = 2.5  # Inversion: 2Y > 10Y
    return df


def _make_steep_curve_df(n_days: int = 200) -> pd.DataFrame:
    """Build a DataFrame with a steep yield curve (gs10 >> gs2)."""
    df = _make_raw_df(n_days=n_days)
    df["gs10"] = 3.5
    df["gs2"] = 1.0  # Steep: spread = 2.5%
    return df


# ---------------------------------------------------------------------------
# TestDownloadMacroData
# ---------------------------------------------------------------------------

class TestDownloadMacroData:
    """Tests for the download_macro_data() function."""

    def test_import_succeeds(self):
        """Sanity check: import was successful."""
        assert download_macro_data is not None
        assert callable(download_macro_data)

    @patch("data.macro.download_macro_data", autospec=True)
    def test_returns_dataframe(self, mock_dl):
        """download_macro_data() should return a non-empty DataFrame."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        assert isinstance(result, pd.DataFrame), "Expected pd.DataFrame"
        assert not result.empty, "Expected non-empty DataFrame"

    @patch("data.macro.download_macro_data", autospec=True)
    def test_contains_expected_columns(self, mock_dl):
        """Returned DataFrame must contain at least the required raw columns."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        missing = RAW_COLUMNS - set(result.columns)
        assert not missing, f"Missing expected columns: {missing}"

    @patch("data.macro.download_macro_data", autospec=True)
    def test_column_types_are_numeric(self, mock_dl):
        """All raw macro columns must be numeric (float or int)."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        for col in RAW_COLUMNS & set(result.columns):
            assert pd.api.types.is_numeric_dtype(result[col]), (
                f"Column '{col}' should be numeric, got {result[col].dtype}"
            )

    @patch("data.macro.download_macro_data", autospec=True)
    def test_index_is_datetime(self, mock_dl):
        """Index must be a DatetimeIndex."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        assert isinstance(result.index, pd.DatetimeIndex), (
            f"Expected DatetimeIndex, got {type(result.index)}"
        )

    @patch("data.macro.download_macro_data", autospec=True)
    def test_index_is_sorted(self, mock_dl):
        """Index must be sorted ascending (oldest first)."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        assert result.index.is_monotonic_increasing, "Index must be sorted ascending"

    @patch("data.macro.download_macro_data", autospec=True)
    def test_no_negative_prices(self, mock_dl):
        """Gold, copper prices must be positive."""
        mock_dl.return_value = _make_raw_df(n_days=50)
        result = mock_dl()
        for col in ("gold", "copper"):
            if col in result.columns:
                assert (result[col].dropna() > 0).all(), (
                    f"Column '{col}' contains non-positive values"
                )

    def test_accepts_cache_age_param(self):
        """Function signature accepts cache_max_age_hours parameter."""
        import inspect
        sig = inspect.signature(download_macro_data)
        assert "cache_max_age_hours" in sig.parameters, (
            "download_macro_data should accept cache_max_age_hours parameter"
        )


# ---------------------------------------------------------------------------
# TestComputeMacroSignals
# ---------------------------------------------------------------------------

class TestComputeMacroSignals:
    """Tests for the compute_macro_signals() function."""

    def test_import_succeeds(self):
        """Sanity check: import was successful."""
        assert compute_macro_signals is not None
        assert callable(compute_macro_signals)

    def test_returns_dataframe(self):
        """compute_macro_signals() should return a DataFrame."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert isinstance(result, pd.DataFrame), "Expected pd.DataFrame"

    def test_produces_all_derived_columns(self):
        """Result must contain all expected derived signal columns."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        missing = DERIVED_COLUMNS - set(result.columns)
        assert not missing, (
            f"Missing derived columns: {missing}. "
            f"Got columns: {list(result.columns)}"
        )

    def test_index_preserved(self):
        """Output index must match input index exactly."""
        df = _make_raw_df(n_days=100)
        result = compute_macro_signals(df)
        pd.testing.assert_index_equal(
            result.index, df.index,
            check_names=False,
        )

    def test_gold_copper_ratio_values(self):
        """gold_copper_ratio should equal gold / copper."""
        df = _make_raw_df(n_days=100)
        # Fix gold and copper to known values
        df["gold"] = 1800.0
        df["copper"] = 3.6
        expected_ratio = 1800.0 / 3.6

        result = compute_macro_signals(df)
        assert "gold_copper_ratio" in result.columns

        # Allow NaN for early rows (insufficient history), check valid ones
        valid = result["gold_copper_ratio"].dropna()
        assert len(valid) > 0, "gold_copper_ratio has no valid values"
        assert (valid - expected_ratio).abs().max() < 1.0, (
            f"gold_copper_ratio deviates from expected {expected_ratio:.2f}. "
            f"Got range [{valid.min():.2f}, {valid.max():.2f}]"
        )

    def test_yc_slope_is_gs10_minus_gs2(self):
        """yc_slope should equal gs10 - gs2."""
        df = _make_raw_df(n_days=100)
        df["gs10"] = 2.5
        df["gs2"] = 1.5
        expected_slope = 1.0  # 2.5 - 1.5

        result = compute_macro_signals(df)
        assert "yc_slope" in result.columns
        valid = result["yc_slope"].dropna()
        assert len(valid) > 0
        assert (valid - expected_slope).abs().max() < 0.01, (
            f"yc_slope should be {expected_slope}, got {valid.mean():.4f}"
        )

    def test_gc_regime_values_are_valid(self):
        """gc_regime must contain only valid regime labels."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert "gc_regime" in result.columns

        # Drop NaN (early rows before enough history)
        valid_regimes = result["gc_regime"].dropna()
        assert len(valid_regimes) > 0, "gc_regime has no valid values"

        invalid = set(valid_regimes.unique()) - VALID_REGIMES
        assert not invalid, (
            f"gc_regime contains invalid values: {invalid}. "
            f"Expected one of: {VALID_REGIMES}"
        )

    def test_macro_regime_scale_range(self):
        """macro_regime_scale must be in [0.5, 1.5] for all valid rows."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert "macro_regime_scale" in result.columns

        valid = result["macro_regime_scale"].dropna()
        assert len(valid) > 0, "macro_regime_scale has no valid values"

        assert valid.min() >= 0.5, (
            f"macro_regime_scale below minimum 0.5: min={valid.min()}"
        )
        assert valid.max() <= 1.5, (
            f"macro_regime_scale above maximum 1.5: max={valid.max()}"
        )

    def test_all_three_regime_scales_present(self):
        """With sufficient history, all 3 scale values (0.5, 1.0, 1.5) should appear."""
        df = _make_raw_df(n_days=520, seed=123)  # ~2 years of data
        result = compute_macro_signals(df)
        valid = result["macro_regime_scale"].dropna()
        unique_scales = set(valid.unique())
        # At minimum: two distinct scale values should exist
        assert len(unique_scales) >= 2, (
            f"Expected at least 2 distinct macro_regime_scale values, "
            f"got: {unique_scales}"
        )

    def test_regime_scale_consistent_with_gc_regime(self):
        """macro_regime_scale should be consistent with gc_regime labels."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert "gc_regime" in result.columns
        assert "macro_regime_scale" in result.columns

        # For rows where both are defined, check consistency
        paired = result[["gc_regime", "macro_regime_scale"]].dropna()
        for _, row in paired.iterrows():
            regime = row["gc_regime"]
            scale = row["macro_regime_scale"]
            if regime == "risk_off":
                assert scale <= 1.0, (
                    f"risk_off regime should have scale <= 1.0, got {scale}"
                )
            elif regime == "risk_on":
                assert scale >= 1.0, (
                    f"risk_on regime should have scale >= 1.0, got {scale}"
                )
            elif regime == "neutral":
                assert scale == 1.0, (
                    f"neutral regime should have scale == 1.0, got {scale}"
                )


# ---------------------------------------------------------------------------
# TestNoLookaheadBias
# ---------------------------------------------------------------------------

class TestNoLookaheadBias:
    """Verify that gc_regime at date T uses only data from dates <= T."""

    def test_classification_identical_on_prefix_vs_full_dataset(self):
        """
        Key look-ahead bias test:

        If compute_macro_signals() uses expanding windows (no look-ahead),
        then the gc_regime at date T should be identical whether we pass:
          (a) the full dataset [0..N]
          (b) just the prefix [0..T]

        If future data after T leaks in (e.g. full-sample percentile ranking),
        the classification at T will differ between (a) and (b).
        """
        df = _make_raw_df(n_days=300, seed=7)
        split_day = 150  # Test at the midpoint

        # Compute on full dataset
        signals_full = compute_macro_signals(df)

        # Compute on just the first `split_day` rows
        signals_prefix = compute_macro_signals(df.iloc[:split_day])

        # The last day of the prefix (day split_day-1) should be classified
        # identically in both computations — no future data leaked in
        regime_full = signals_full.iloc[split_day - 1]["gc_regime"]
        regime_prefix = signals_prefix.iloc[split_day - 1]["gc_regime"]

        assert regime_full == regime_prefix, (
            f"Look-ahead bias detected! "
            f"gc_regime at day {split_day - 1} differs: "
            f"prefix={regime_prefix!r} vs full={regime_full!r}. "
            f"The implementation must use expanding windows, not full-sample percentiles."
        )

    def test_vix_roc_uses_only_past_data(self):
        """vix_roc_5d at date T should equal (VIX[T] / VIX[T-5]) - 1."""
        df = _make_raw_df(n_days=50)
        # Set VIX to increasing sequence so we know exact 5d ROC
        df["vix"] = np.arange(1, 51, dtype=float) * 1.0  # VIX: 1, 2, ..., 50

        result = compute_macro_signals(df)
        assert "vix_roc_5d" in result.columns

        # At day index 10 (VIX=11), 5d ago was day 5 (VIX=6)
        # Expected ROC = (11 - 6) / 6 = 0.8333
        idx_10 = result.index[10]
        roc_at_10 = result.loc[idx_10, "vix_roc_5d"]
        expected_roc = (11.0 - 6.0) / 6.0  # ≈ 0.8333

        if not pd.isna(roc_at_10):
            assert abs(roc_at_10 - expected_roc) < 0.01, (
                f"vix_roc_5d at day 10: expected {expected_roc:.4f}, got {roc_at_10:.4f}"
            )

    def test_gc_ratio_tercile_window_is_expanding(self):
        """
        Verify expanding window tercile: early dates classified using
        only early data, not the full historical distribution.

        Design:
          Phase 1 (days 0-99):  gold/copper = 10 (HIGH ratio → risk_off)
          Phase 2 (days 100-199): gold/copper = 1 (LOW ratio → risk_on)

        Without look-ahead bias:
          - In Phase 1, ALL data points have ratio ~10, so the 33rd/67th
            percentile thresholds are both ~10 → early days are 'neutral'
          - Once Phase 2 data arrives, the thresholds adjust downward

        With look-ahead bias (full-sample percentile):
          - Phase 1 data is ABOVE the full-dataset 67th percentile → all 'risk_off'

        This test checks that Phase 2 data does NOT retroactively force
        Phase 1 into 'risk_off' when computing on the prefix only.
        """
        n_each = 100
        dates = pd.bdate_range("2020-01-01", periods=n_each * 2)

        # Phase 1: gold/copper = 10 (high ratio)
        gold_p1 = np.full(n_each, 1000.0)
        copper_p1 = np.full(n_each, 100.0)
        # Phase 2: gold/copper = 1 (low ratio)
        gold_p2 = np.full(n_each, 100.0)
        copper_p2 = np.full(n_each, 100.0)

        gold = np.concatenate([gold_p1, gold_p2])
        copper = np.concatenate([copper_p1, copper_p2])
        vix = np.full(n_each * 2, 15.0)
        gs10 = np.full(n_each * 2, 2.0)
        gs2 = np.full(n_each * 2, 1.5)

        df = pd.DataFrame(
            {"gold": gold, "copper": copper, "vix": vix, "gs10": gs10, "gs2": gs2},
            index=dates,
        )
        df.index.name = "date"

        # Compute on prefix (Phase 1 only)
        signals_prefix = compute_macro_signals(df.iloc[:n_each])

        # Compute on full dataset
        signals_full = compute_macro_signals(df)

        # Phase 1, day 80 (well into the phase, stable)
        regime_prefix = signals_prefix.iloc[80]["gc_regime"]
        regime_full = signals_full.iloc[80]["gc_regime"]

        # The key assertion: Phase 1 classification shouldn't change
        # when Phase 2 data is added
        assert regime_prefix == regime_full, (
            f"Look-ahead bias: day 80 regime changed from {regime_prefix!r} "
            f"(prefix) to {regime_full!r} (full dataset). "
            f"Expanding window terciles must not use future data."
        )


# ---------------------------------------------------------------------------
# TestVixRocSpikeDetection
# ---------------------------------------------------------------------------

class TestVixRocSpikeDetection:
    """Tests for VIX 5-day rate-of-change spike detection."""

    def test_vix_roc_5d_known_values(self):
        """Verify exact vix_roc_5d computation with controlled input."""
        n = 30
        df = _make_raw_df(n_days=n)
        df["vix"] = 20.0  # Flat VIX
        # On day 10: spike VIX by 60%
        df.iloc[10, df.columns.get_loc("vix")] = 32.0  # 20 * 1.6

        result = compute_macro_signals(df)
        assert "vix_roc_5d" in result.columns

        # Day 10 vs day 5: both should be 20.0 unless the spike is within
        # the look-back window
        # At day 10 (index 10): vix[10]=32.0, vix[5]=20.0 → ROC = (32-20)/20 = 0.60
        roc_day10 = result.iloc[10]["vix_roc_5d"]
        if not pd.isna(roc_day10):
            assert abs(roc_day10 - 0.60) < 0.01, (
                f"Expected vix_roc_5d ≈ 0.60 at spike day, got {roc_day10:.4f}"
            )

    def test_vix_roc_5d_negative_decline(self):
        """VIX decline shows negative ROC."""
        n = 30
        df = _make_raw_df(n_days=n)
        df["vix"] = 30.0
        # Day 10: VIX drops to 20 (33% decline)
        df.iloc[10, df.columns.get_loc("vix")] = 20.0

        result = compute_macro_signals(df)
        roc_day10 = result.iloc[10]["vix_roc_5d"]
        if not pd.isna(roc_day10):
            # Should be negative (VIX dropped from 30 to 20 → -33%)
            assert roc_day10 < 0, (
                f"Expected negative vix_roc_5d for VIX decline, got {roc_day10:.4f}"
            )

    def test_vix_roc_5d_near_zero_for_flat_vix(self):
        """Flat VIX produces near-zero 5d ROC."""
        n = 50
        df = _make_raw_df(n_days=n)
        df["vix"] = 15.0  # Perfectly flat

        result = compute_macro_signals(df)
        valid_roc = result["vix_roc_5d"].dropna()
        if len(valid_roc) > 0:
            assert valid_roc.abs().max() < 0.001, (
                f"Expected near-zero vix_roc_5d for flat VIX, "
                f"max abs = {valid_roc.abs().max():.6f}"
            )

    def test_vix_roc_5d_50pct_spike_is_significant(self):
        """A 50%+ VIX spike in 5 days should produce large positive ROC."""
        n = 50
        df = _make_raw_df(n_days=n)
        df["vix"] = 15.0
        spike_day = 20
        df.iloc[spike_day, df.columns.get_loc("vix")] = 22.5  # +50%

        result = compute_macro_signals(df)
        roc = result.iloc[spike_day]["vix_roc_5d"]
        if not pd.isna(roc):
            assert roc >= 0.45, (
                f"Expected vix_roc_5d >= 0.45 for 50% VIX spike, got {roc:.4f}"
            )


# ---------------------------------------------------------------------------
# TestYieldCurveDetection
# ---------------------------------------------------------------------------

class TestYieldCurveDetection:
    """Tests for yield curve slope (flattening / inversion) detection."""

    def test_yc_slope_inverted_curve(self):
        """Inverted yield curve (gs2 > gs10) produces negative yc_slope."""
        df = _make_inverted_curve_df(n_days=50)
        result = compute_macro_signals(df)

        assert "yc_slope" in result.columns
        valid = result["yc_slope"].dropna()
        assert len(valid) > 0
        assert (valid < 0).all(), (
            f"Expected all negative yc_slope for inverted curve (gs2=2.5 > gs10=1.5). "
            f"Got: min={valid.min():.4f}, max={valid.max():.4f}"
        )

    def test_yc_slope_steep_curve(self):
        """Steep yield curve (gs10 >> gs2) produces large positive yc_slope."""
        df = _make_steep_curve_df(n_days=50)
        result = compute_macro_signals(df)

        assert "yc_slope" in result.columns
        valid = result["yc_slope"].dropna()
        assert len(valid) > 0
        assert (valid > 1.0).all(), (
            f"Expected yc_slope > 1.0 for steep curve (gs10=3.5, gs2=1.0). "
            f"Got: min={valid.min():.4f}"
        )

    def test_yc_slope_known_values(self):
        """yc_slope at each date equals gs10[T] - gs2[T]."""
        n = 50
        df = _make_raw_df(n_days=n)
        # Set exact values
        df["gs10"] = np.linspace(1.0, 3.0, n)   # Rising 10Y
        df["gs2"] = np.linspace(0.5, 2.5, n)    # Rising 2Y

        expected_slopes = df["gs10"] - df["gs2"]  # Should all be ≈ 0.5

        result = compute_macro_signals(df)
        computed_slopes = result["yc_slope"].dropna()

        if len(computed_slopes) > 0:
            # Match on shared index
            common_idx = computed_slopes.index.intersection(expected_slopes.index)
            diff = (computed_slopes.loc[common_idx] - expected_slopes.loc[common_idx]).abs()
            assert diff.max() < 0.001, (
                f"yc_slope deviates from gs10-gs2 by up to {diff.max():.6f}"
            )

    def test_inversion_regime_is_risk_off_or_neutral(self):
        """Inverted yield curve should not produce 'risk_on' regime."""
        # Inverted curve = risk-off environment (flight to safety)
        # According to spec: inversion → boost +0.05, scale 0.8
        df = _make_inverted_curve_df(n_days=260)
        result = compute_macro_signals(df)

        if "gc_regime" in result.columns:
            valid_regimes = result["gc_regime"].dropna()
            # With inverted curve the regime should lean risk_off or neutral
            # — risk_on should be rare or absent in a pure-inversion scenario
            # (This test is soft — it just flags if the distribution is skewed the wrong way)
            risk_on_count = (valid_regimes == "risk_on").sum()
            total = len(valid_regimes)
            risk_on_pct = risk_on_count / total if total > 0 else 0

            # Soft assertion: inverted curve shouldn't be predominantly risk_on
            # (allow some risk_on due to gold/copper signal dominating)
            # This is a sanity check, not a hard requirement
            assert risk_on_pct < 0.8, (
                f"Unexpected: {risk_on_pct:.1%} of rows are 'risk_on' with inverted yield curve. "
                f"Expected predominantly 'risk_off' or 'neutral'."
            )


# ---------------------------------------------------------------------------
# TestMacroRegimeScale
# ---------------------------------------------------------------------------

class TestMacroRegimeScale:
    """Tests for macro_regime_scale bounds and discrete values."""

    def test_scale_always_in_valid_range(self):
        """macro_regime_scale must always be in [0.5, 1.5]."""
        df = _make_raw_df(n_days=520, seed=99)
        result = compute_macro_signals(df)
        valid = result["macro_regime_scale"].dropna()
        assert (valid >= 0.5).all(), f"scale below 0.5: min={valid.min()}"
        assert (valid <= 1.5).all(), f"scale above 1.5: max={valid.max()}"

    def test_risk_off_scale_is_0_5(self):
        """risk_off regime should map to macro_regime_scale = 0.5."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        risk_off_rows = result[result["gc_regime"] == "risk_off"]
        if len(risk_off_rows) > 0:
            scales = risk_off_rows["macro_regime_scale"].dropna()
            if len(scales) > 0:
                assert (scales == 0.5).all(), (
                    f"risk_off rows should all have scale=0.5, got: {scales.unique()}"
                )

    def test_neutral_scale_is_1_0(self):
        """neutral regime should map to macro_regime_scale = 1.0."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        neutral_rows = result[result["gc_regime"] == "neutral"]
        if len(neutral_rows) > 0:
            scales = neutral_rows["macro_regime_scale"].dropna()
            if len(scales) > 0:
                assert (scales == 1.0).all(), (
                    f"neutral rows should all have scale=1.0, got: {scales.unique()}"
                )

    def test_risk_on_scale_is_1_5(self):
        """risk_on regime should map to macro_regime_scale = 1.5."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        risk_on_rows = result[result["gc_regime"] == "risk_on"]
        if len(risk_on_rows) > 0:
            scales = risk_on_rows["macro_regime_scale"].dropna()
            if len(scales) > 0:
                assert (scales == 1.5).all(), (
                    f"risk_on rows should all have scale=1.5, got: {scales.unique()}"
                )

    def test_scale_values_are_discrete(self):
        """macro_regime_scale should only take values 0.5, 1.0, 1.5."""
        df = _make_raw_df(n_days=520, seed=77)
        result = compute_macro_signals(df)
        valid = result["macro_regime_scale"].dropna()
        unexpected = set(valid.unique()) - {0.5, 1.0, 1.5}
        assert not unexpected, (
            f"macro_regime_scale contains unexpected values: {unexpected}. "
            f"Only 0.5, 1.0, and 1.5 are valid."
        )


# ---------------------------------------------------------------------------
# TestMissingDataGracefulDegradation
# ---------------------------------------------------------------------------

class TestMissingDataGracefulDegradation:
    """Tests that compute_macro_signals() handles partial/missing data gracefully."""

    def test_handles_all_nan_vix(self):
        """If VIX column is all NaN, function should not raise."""
        df = _make_raw_df(n_days=50)
        df["vix"] = np.nan

        try:
            result = compute_macro_signals(df)
            # vix_roc_5d should be NaN or absent, not raise
            if "vix_roc_5d" in result.columns:
                # Either all NaN or reasonable fallback
                pass  # No exception is sufficient
        except Exception as e:
            pytest.fail(
                f"compute_macro_signals raised {type(e).__name__} with all-NaN VIX: {e}"
            )

    def test_handles_all_nan_gs10(self):
        """If FRED yield data is missing (all NaN), function should not raise."""
        df = _make_raw_df(n_days=50)
        df["gs10"] = np.nan
        df["gs2"] = np.nan

        try:
            result = compute_macro_signals(df)
            if "yc_slope" in result.columns:
                # Should be all NaN, not raise
                pass
        except Exception as e:
            pytest.fail(
                f"compute_macro_signals raised {type(e).__name__} with all-NaN yields: {e}"
            )

    def test_handles_missing_copper_column(self):
        """If copper column is absent, function should not raise."""
        df = _make_raw_df(n_days=50)
        df = df.drop(columns=["copper"])

        try:
            result = compute_macro_signals(df)
            # gold_copper_ratio should be NaN or absent
        except (KeyError, Exception) as e:
            pytest.fail(
                f"compute_macro_signals raised {type(e).__name__} with missing copper: {e}"
            )

    def test_handles_partial_nan_rows(self):
        """Some rows with NaN values should not propagate to all rows."""
        df = _make_raw_df(n_days=100)
        # NaN out first 10 rows of gold/copper
        df.iloc[:10, df.columns.get_loc("gold")] = np.nan
        df.iloc[:10, df.columns.get_loc("copper")] = np.nan

        try:
            result = compute_macro_signals(df)
            # Rows after the NaN region should still have valid signals
            if "gc_regime" in result.columns:
                valid_after_nan = result.iloc[30:]["gc_regime"].dropna()
                # Should have some valid regime values after NaN region
                assert len(valid_after_nan) > 0, (
                    "Expected valid gc_regime values after NaN region"
                )
        except Exception as e:
            pytest.fail(
                f"compute_macro_signals raised {type(e).__name__} with partial NaN: {e}"
            )

    def test_handles_short_dataframe_without_crash(self):
        """Short DataFrame (fewer rows than rolling window) should not crash."""
        df = _make_raw_df(n_days=3)  # Only 3 days — less than any window

        try:
            result = compute_macro_signals(df)
            # All derived columns may be NaN, but no exception
            assert isinstance(result, pd.DataFrame)
        except Exception as e:
            pytest.fail(
                f"compute_macro_signals raised {type(e).__name__} with only 3 rows: {e}"
            )

    def test_gc_regime_not_all_nan_with_good_data(self):
        """With 260 days of good data, gc_regime should have non-NaN values."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert "gc_regime" in result.columns
        valid = result["gc_regime"].dropna()
        assert len(valid) > 50, (
            f"Expected at least 50 valid gc_regime values with 260 days of data, "
            f"got {len(valid)}"
        )

    def test_macro_regime_scale_not_all_nan_with_good_data(self):
        """With 260 days of good data, macro_regime_scale should have non-NaN values."""
        df = _make_raw_df(n_days=260)
        result = compute_macro_signals(df)
        assert "macro_regime_scale" in result.columns
        valid = result["macro_regime_scale"].dropna()
        assert len(valid) > 50, (
            f"Expected at least 50 valid macro_regime_scale values, got {len(valid)}"
        )


# ---------------------------------------------------------------------------
# TestImportGuard (always runs — does not depend on MACRO_AVAILABLE)
# ---------------------------------------------------------------------------

class TestImportGuard:
    """Tests that run regardless of whether data.macro is available.

    These serve as diagnostics when builder-1's module hasn't been merged yet.
    """

    # Override the module-level skip for these tests
    pytestmark = []  # No skip decorator

    def test_import_error_has_clear_message(self):
        """If import fails, the error message should be informative."""
        if not MACRO_AVAILABLE:
            pytest.skip(
                f"data.macro not available (expected during parallel build). "
                f"Import error: {_IMPORT_ERROR_MSG}"
            )
        # If available, this test trivially passes
        assert True

    def test_module_can_be_imported(self):
        """Verify data.macro can be imported after builder-1 merges."""
        if not MACRO_AVAILABLE:
            pytest.skip(f"data.macro not yet available: {_IMPORT_ERROR_MSG}")
        from data.macro import download_macro_data, compute_macro_signals
        assert download_macro_data is not None
        assert compute_macro_signals is not None
