"""
Tests for the extended macro indicators pipeline.

Covers:
  - FRED client fetch functions (credit OAS, DXY, module-level fetch_fred_data)
  - VIX3M fetch via yfinance
  - Derived indicator computation (vix_term_ratio, gold_copper_ratio,
    spy_200dma, spy_above_200dma, spy_200dma_slope)
  - SQLite write (upsert_macro_indicators / get_macro_indicators)
  - write_macro_indicators_to_db round-trip
  - backfill_macro_indicators orchestration

All external network calls are mocked — tests run fully offline.
"""

import math
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_dates(n: int, end: str = "2024-12-31") -> pd.DatetimeIndex:
    return pd.bdate_range(end=end, periods=n)


def _make_prices(
    n: int = 300,
    start: float = 100.0,
    drift: float = 0.0003,
    seed: int = 42,
) -> pd.Series:
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    prices = start * np.exp(np.cumsum(rets))
    return pd.Series(prices, index=_make_dates(n))


def _make_macro_df(n: int = 300, seed: int = 0) -> pd.DataFrame:
    """Synthetic macro DataFrame matching download_macro_data() output format."""
    dates = _make_dates(n)
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "vix": rng.uniform(12, 40, n),
            "vix3m": rng.uniform(14, 42, n),
            "gold": rng.uniform(1800, 2200, n),
            "copper": rng.uniform(3.5, 5.5, n),
            "yield_10y": rng.uniform(1.5, 5.0, n),
            "yield_13w": rng.uniform(0.5, 5.5, n),
            "spy": _make_prices(n, start=450, seed=seed).values,
        },
        index=dates,
    )


@pytest.fixture()
def tmp_db(tmp_path):
    """Initialise a throwaway SQLite DB and return its path."""
    from db.atlas_db import init_db
    db_path = str(tmp_path / "test_atlas.db")
    init_db(db_path)
    yield db_path
    # Cleanup: reset module-level override so other tests are unaffected.
    import db.atlas_db as _db
    _db._db_path_override = None


# ===========================================================================
# 1.  FRED client — fetch_fred_data module-level function
# ===========================================================================

class TestFetchFredData:
    """fetch_fred_data() should import and delegate to FREDClient.fetch_series."""

    def test_import_ok(self):
        from data.fred import fetch_fred_data  # noqa: F401
        assert callable(fetch_fred_data)

    def test_returns_series_on_success(self):
        """With a mocked FREDClient the function returns a pd.Series."""
        from data.fred import fetch_fred_data

        mock_series = pd.Series(
            [50.1, 51.3, 49.8],
            index=pd.date_range("2024-01-01", periods=3, freq="D"),
        )
        with patch("data.fred.FREDClient") as MockClient:
            instance = MockClient.return_value
            instance.fetch_series.return_value = mock_series

            result = fetch_fred_data(
                "BAMLC0A0CM",
                start_date="2024-01-01",
                end_date="2024-01-03",
            )

        assert isinstance(result, pd.Series)
        assert len(result) == 3

    def test_returns_empty_when_no_api_key(self):
        """With no API key configured the function returns an empty Series."""
        from data.fred import fetch_fred_data

        with patch("data.fred.FREDClient") as MockClient:
            instance = MockClient.return_value
            instance.fetch_series.return_value = pd.Series(dtype=float)

            result = fetch_fred_data("BAMLC0A0CM")

        assert isinstance(result, pd.Series)
        assert len(result) == 0

    def test_credit_oas_series_id(self):
        """fetch_fred_data delegates to FREDClient with the correct series ID."""
        from data.fred import fetch_fred_data

        mock_series = pd.Series([55.0], index=pd.date_range("2024-01-01", periods=1))
        with patch("data.fred.FREDClient") as MockClient:
            instance = MockClient.return_value
            instance.fetch_series.return_value = mock_series

            fetch_fred_data("BAMLC0A0CM")
            called_with = instance.fetch_series.call_args[0][0]

        assert called_with == "BAMLC0A0CM"

    def test_dxy_series_id(self):
        """fetch_fred_data uses DTWEXBGS for DXY."""
        from data.fred import fetch_fred_data

        mock_series = pd.Series([104.5], index=pd.date_range("2024-01-01", periods=1))
        with patch("data.fred.FREDClient") as MockClient:
            instance = MockClient.return_value
            instance.fetch_series.return_value = mock_series

            fetch_fred_data("DTWEXBGS")
            called_with = instance.fetch_series.call_args[0][0]

        assert called_with == "DTWEXBGS"


# ===========================================================================
# 2.  FREDClient — new convenience methods
# ===========================================================================

class TestFREDClientNewMethods:
    """get_credit_oas and get_dxy delegate correctly."""

    def _make_client_with_series(self, series: pd.Series):
        from data.fred import FREDClient
        client = FREDClient.__new__(FREDClient)
        client.api_key = "dummy"
        client.fetch_series = MagicMock(return_value=series)
        return client

    def test_get_credit_oas_calls_correct_series(self):
        from data.fred import FREDClient
        dummy = pd.Series([50.0])
        client = self._make_client_with_series(dummy)
        result = client.get_credit_oas()
        client.fetch_series.assert_called_once_with("BAMLC0A0CM")
        assert result is dummy

    def test_get_dxy_calls_correct_series(self):
        from data.fred import FREDClient
        dummy = pd.Series([104.0])
        client = self._make_client_with_series(dummy)
        result = client.get_dxy()
        client.fetch_series.assert_called_once_with("DTWEXBGS")
        assert result is dummy

    def test_series_registry_contains_new_series(self):
        from data.fred import SERIES_REGISTRY
        assert "BAMLC0A0CM" in SERIES_REGISTRY
        assert "DTWEXBGS" in SERIES_REGISTRY


# ===========================================================================
# 3.  compute_macro_signals — VIX3M and derived indicators
# ===========================================================================

class TestComputeMacroSignals:
    """New derived columns are computed correctly given mock input."""

    def test_vix_term_ratio_calculation(self):
        """vix_term_ratio = vix / vix3m, element-wise."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=100, seed=1)
        # Fix vix / vix3m so we can check exact values
        df["vix"] = 20.0
        df["vix3m"] = 25.0

        signals = compute_macro_signals(df)

        assert "vix_term_ratio" in signals.columns
        # All rows should be 20/25 = 0.8
        valid = signals["vix_term_ratio"].dropna()
        assert len(valid) > 0
        np.testing.assert_allclose(valid.values, 0.8, rtol=1e-6)

    def test_vix_term_ratio_no_division_by_zero(self):
        """Zero VIX3M values should produce NaN, not inf."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=10, seed=2)
        df["vix3m"] = 0.0

        signals = compute_macro_signals(df)

        assert "vix_term_ratio" in signals.columns
        # Should be NaN (or possibly filled by ffill from neutral, but not inf)
        assert not np.isinf(signals["vix_term_ratio"].dropna()).any()

    def test_gold_copper_ratio_matches_manual_calculation(self):
        """gold_copper_ratio should equal gold / copper."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=100, seed=3)
        df["gold"] = 2000.0
        df["copper"] = 4.0

        signals = compute_macro_signals(df)

        assert "gold_copper_ratio" in signals.columns
        # Need enough history for min_periods in expanding window
        valid = signals["gold_copper_ratio"].dropna()
        np.testing.assert_allclose(valid.values, 500.0, rtol=1e-5)

    def test_spy_200dma_length(self):
        """spy_200dma should have the same length as the input DataFrame."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=250, seed=4)
        signals = compute_macro_signals(df)

        assert "spy_200dma" in signals.columns
        assert len(signals["spy_200dma"]) == len(df)

    def test_spy_200dma_value(self):
        """spy_200dma at index 0 should equal spy[0] (min_periods=1 rolling mean)."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=250, seed=5)
        signals = compute_macro_signals(df)

        first_spy = df["spy"].iloc[0]
        first_dma = signals["spy_200dma"].iloc[0]
        assert abs(first_dma - first_spy) < 1e-9, (
            f"Expected DMA[0] == spy[0] ({first_spy:.4f}), got {first_dma:.4f}"
        )

    def test_spy_200dma_converges_after_200_days(self):
        """After 200 rows, spy_200dma should be a proper 200-day mean."""
        from data.macro import compute_macro_signals

        n = 250
        df = _make_macro_df(n=n, seed=6)
        signals = compute_macro_signals(df)

        # Last row: DMA should equal mean of last 200 spy values
        expected_dma = df["spy"].iloc[-200:].mean()
        actual_dma = signals["spy_200dma"].iloc[-1]
        assert abs(actual_dma - expected_dma) < 1e-9

    def test_spy_above_200dma_is_0_or_1(self):
        """spy_above_200dma must be 0 or 1 for every row."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=250, seed=7)
        signals = compute_macro_signals(df)

        assert "spy_above_200dma" in signals.columns
        unique_vals = set(signals["spy_above_200dma"].unique())
        assert unique_vals.issubset({0, 1})

    def test_spy_above_200dma_logic(self):
        """spy_above_200dma is 1 when spy > spy_200dma, 0 otherwise."""
        from data.macro import compute_macro_signals

        n = 250
        df = _make_macro_df(n=n, seed=8)
        signals = compute_macro_signals(df)

        for i in range(n):
            spy_val = df["spy"].iloc[i]
            dma_val = signals["spy_200dma"].iloc[i]
            expected = 1 if spy_val > dma_val else 0
            assert signals["spy_above_200dma"].iloc[i] == expected, (
                f"Row {i}: spy={spy_val:.2f}, dma={dma_val:.2f}, "
                f"expected={expected}, got={signals['spy_above_200dma'].iloc[i]}"
            )

    def test_spy_200dma_slope_is_pct_change(self):
        """spy_200dma_slope is the 20-day pct_change of spy_200dma."""
        from data.macro import compute_macro_signals

        n = 250
        df = _make_macro_df(n=n, seed=9)
        signals = compute_macro_signals(df)

        assert "spy_200dma_slope" in signals.columns
        # Row 20 should be (dma[20] - dma[0]) / dma[0]
        dma = signals["spy_200dma"]
        for i in range(20, min(n, 30)):
            expected = (dma.iloc[i] - dma.iloc[i - 20]) / dma.iloc[i - 20]
            actual = signals["spy_200dma_slope"].iloc[i]
            assert abs(actual - expected) < 1e-9, f"Row {i}: expected {expected}, got {actual}"

    def test_existing_columns_still_present(self):
        """Existing columns (gc_regime, vix_roc_5d, etc.) must survive the extension."""
        from data.macro import compute_macro_signals

        df = _make_macro_df(n=100, seed=10)
        signals = compute_macro_signals(df)

        for col in [
            "gold_copper_ratio", "gc_regime", "vix_roc_5d", "vix_spike",
            "yield_curve_10y_3m", "yc_change_5d", "yc_flattening",
            "macro_regime_scale",
        ]:
            assert col in signals.columns, f"Missing existing column: {col}"


# ===========================================================================
# 4.  SQLite write — upsert_macro_indicators / get_macro_indicators
# ===========================================================================

class TestUpsertMacroIndicators:
    """Low-level DB write and read round-trip."""

    def test_write_and_read_back(self, tmp_db):
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        upsert_macro_indicators(
            "2024-01-15",
            vix=18.5,
            vix3m=20.1,
            vix_term_ratio=0.92,
            credit_oas=55.3,
            dxy=103.8,
            gold=2050.0,
            copper=4.1,
            gold_copper_ratio=500.0,
            spy_close=475.0,
            spy_200dma=460.0,
            spy_above_200dma=1,
            spy_200dma_slope=0.015,
        )

        rows = get_macro_indicators(start_date="2024-01-15", end_date="2024-01-15")
        assert len(rows) == 1
        r = rows[0]
        assert r["date"] == "2024-01-15"
        assert abs(r["vix"] - 18.5) < 1e-6
        assert abs(r["credit_oas"] - 55.3) < 1e-6
        assert abs(r["dxy"] - 103.8) < 1e-6
        assert r["spy_above_200dma"] == 1

    def test_upsert_replaces_existing_row(self, tmp_db):
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        upsert_macro_indicators("2024-02-01", vix=20.0, credit_oas=60.0)
        upsert_macro_indicators("2024-02-01", vix=22.0, credit_oas=65.0)

        rows = get_macro_indicators(start_date="2024-02-01", end_date="2024-02-01")
        assert len(rows) == 1
        assert abs(rows[0]["vix"] - 22.0) < 1e-6
        assert abs(rows[0]["credit_oas"] - 65.0) < 1e-6

    def test_nan_stored_as_null(self, tmp_db):
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        upsert_macro_indicators("2024-03-01", vix=float("nan"), credit_oas=55.0)
        rows = get_macro_indicators(start_date="2024-03-01", end_date="2024-03-01")
        assert len(rows) == 1
        assert rows[0]["vix"] is None   # NaN → NULL → None

    def test_unknown_fields_ignored(self, tmp_db):
        """Unknown keyword args must not raise."""
        from db.atlas_db import upsert_macro_indicators

        # Should not raise
        upsert_macro_indicators(
            "2024-04-01",
            vix=15.0,
            nonexistent_field=99.9,
        )

    def test_empty_row_created_on_no_fields(self, tmp_db):
        """Calling with no kwargs should create a minimal row (date only)."""
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        upsert_macro_indicators("2024-05-01")
        rows = get_macro_indicators(start_date="2024-05-01", end_date="2024-05-01")
        assert len(rows) == 1
        assert rows[0]["date"] == "2024-05-01"

    def test_multiple_dates_range_filter(self, tmp_db):
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        for d, vix in [("2024-06-01", 15.0), ("2024-06-02", 16.0), ("2024-06-03", 17.0)]:
            upsert_macro_indicators(d, vix=vix)

        rows = get_macro_indicators(start_date="2024-06-02", end_date="2024-06-03")
        assert len(rows) == 2
        dates = [r["date"] for r in rows]
        assert "2024-06-02" in dates
        assert "2024-06-03" in dates
        assert "2024-06-01" not in dates

    def test_days_filter(self, tmp_db):
        """get_macro_indicators(days=N) returns rows from last N calendar days."""
        from db.atlas_db import get_macro_indicators, upsert_macro_indicators

        # Insert a row dated 1 year ago — should NOT appear in last 30 days
        import datetime
        old_date = (datetime.date.today() - datetime.timedelta(days=365)).isoformat()
        upsert_macro_indicators(old_date, vix=30.0)

        rows = get_macro_indicators(days=30)
        dates = [r["date"] for r in rows]
        assert old_date not in dates


# ===========================================================================
# 5.  write_macro_indicators_to_db round-trip
# ===========================================================================

class TestWriteMacroIndicatorsToDb:
    """write_macro_indicators_to_db persists a full DataFrame correctly."""

    def test_round_trip(self, tmp_db):
        from data.macro import write_macro_indicators_to_db
        from db.atlas_db import get_macro_indicators

        n = 10
        dates = _make_dates(n, end="2024-07-31")
        df = pd.DataFrame(
            {
                "vix": np.linspace(15, 25, n),
                "vix3m": np.linspace(16, 26, n),
                "vix_term_ratio": np.linspace(0.9, 1.0, n),
                "gold_copper_ratio": np.full(n, 450.0),
                "spy_close": np.linspace(460, 480, n),
                "spy_200dma": np.linspace(450, 470, n),
                "spy_above_200dma": np.ones(n, dtype=int),
                "spy_200dma_slope": np.zeros(n),
                "credit_oas": np.linspace(50, 60, n),
                "dxy": np.linspace(100, 105, n),
            },
            index=dates,
        )

        written = write_macro_indicators_to_db(df)
        assert written == n

        rows = get_macro_indicators(
            start_date=dates[0].strftime("%Y-%m-%d"),
            end_date=dates[-1].strftime("%Y-%m-%d"),
        )
        assert len(rows) == n

        # Spot-check first row
        r0 = rows[0]
        assert abs(r0["vix"] - df["vix"].iloc[0]) < 1e-6
        assert abs(r0["credit_oas"] - df["credit_oas"].iloc[0]) < 1e-6
        assert abs(r0["dxy"] - df["dxy"].iloc[0]) < 1e-6
        assert r0["spy_above_200dma"] == 1

    def test_spy_above_200dma_cast_to_int(self, tmp_db):
        """spy_above_200dma is stored as integer even if provided as float."""
        from data.macro import write_macro_indicators_to_db
        from db.atlas_db import get_macro_indicators

        # 2024-08-08 is a Thursday → bdate_range gives 3 business days ending there
        dates = _make_dates(3, end="2024-08-08")
        df = pd.DataFrame(
            {
                "spy_above_200dma": [1.0, 0.0, 1.0],  # floats, not ints
            },
            index=dates,
        )

        write_macro_indicators_to_db(df)
        rows = get_macro_indicators(
            start_date=dates[0].strftime("%Y-%m-%d"),
            end_date=dates[-1].strftime("%Y-%m-%d"),
        )
        for r in rows:
            assert isinstance(r["spy_above_200dma"], int), (
                f"Expected int, got {type(r['spy_above_200dma'])}"
            )

    def test_nan_values_become_null(self, tmp_db):
        """NaN values in the DataFrame should be stored as NULL."""
        from data.macro import write_macro_indicators_to_db
        from db.atlas_db import get_macro_indicators

        dates = _make_dates(2, end="2024-09-05")
        df = pd.DataFrame(
            {
                "vix": [float("nan"), 18.0],
                "credit_oas": [55.0, float("nan")],
            },
            index=dates,
        )

        write_macro_indicators_to_db(df)
        rows = get_macro_indicators(
            start_date=dates[0].strftime("%Y-%m-%d"),
            end_date=dates[-1].strftime("%Y-%m-%d"),
        )
        assert rows[0]["vix"] is None       # NaN → NULL
        assert rows[1]["credit_oas"] is None


# ===========================================================================
# 6.  fetch_macro_data — import and basic structure
# ===========================================================================

class TestFetchMacroData:
    """fetch_macro_data() should import and return a DataFrame."""

    def test_import_ok(self):
        from data.macro import fetch_macro_data  # noqa: F401
        assert callable(fetch_macro_data)

    def test_returns_dataframe_with_mocked_downloads(self):
        """With mocked yfinance + FRED, fetch_macro_data returns a DataFrame."""
        from data.macro import fetch_macro_data

        n = 20
        mock_raw = _make_macro_df(n=n, seed=11)

        empty_series = pd.Series(dtype=float)
        mock_fred: dict = {
            "yield_2y": empty_series,
            "credit_oas": empty_series,
            "dxy": empty_series,
            "fed_funds": empty_series,
            "unemployment_claims": empty_series,
            "yield_curve_10y2y_fred": empty_series,
        }

        with (
            patch("data.macro.download_macro_data", return_value=mock_raw),
            patch("data.macro.fetch_regime_macro_series", return_value=mock_fred),
        ):
            df = fetch_macro_data(
                start_date="2024-11-01",
                end_date="2024-12-31",
                write_to_db=False,
            )

        assert isinstance(df, pd.DataFrame)
        # Should have key derived columns
        for col in ["vix", "vix3m", "gold", "copper", "spy_close",
                    "spy_200dma", "spy_above_200dma", "spy_200dma_slope",
                    "vix_term_ratio", "gold_copper_ratio"]:
            assert col in df.columns, f"Missing column: {col}"

    def test_vix3m_present_in_output(self):
        """vix3m column must appear in the fetch_macro_data output."""
        from data.macro import fetch_macro_data

        mock_raw = _make_macro_df(n=15, seed=12)
        mock_fred: dict = {}

        with (
            patch("data.macro.download_macro_data", return_value=mock_raw),
            patch("data.macro.fetch_regime_macro_series", return_value=mock_fred),
        ):
            df = fetch_macro_data(
                start_date="2024-11-01",
                end_date="2024-12-31",
                write_to_db=False,
            )

        assert "vix3m" in df.columns
        assert df["vix3m"].notna().any()


# ===========================================================================
# 7.  backfill_macro_indicators — smoke test
# ===========================================================================

class TestBackfillMacroIndicators:
    """backfill_macro_indicators() calls fetch_macro_data and writes to DB."""

    def test_function_exists(self):
        from data.macro import backfill_macro_indicators  # noqa: F401
        assert callable(backfill_macro_indicators)

    def test_backfill_writes_rows(self, tmp_db):
        """backfill_macro_indicators returns a DataFrame and writes to SQLite."""
        from data.macro import backfill_macro_indicators
        from db.atlas_db import get_macro_indicators

        # Build a mock DataFrame covering the requested range
        n = 10
        dates = pd.bdate_range("2024-10-01", periods=n)
        mock_df = pd.DataFrame(
            {
                "vix": np.linspace(16, 20, n),
                "vix3m": np.linspace(17, 21, n),
                "vix_term_ratio": np.linspace(0.9, 1.0, n),
                "gold_copper_ratio": np.full(n, 480.0),
                "spy_close": np.linspace(450, 470, n),
                "spy_200dma": np.linspace(445, 465, n),
                "spy_above_200dma": np.ones(n, dtype=int),
                "spy_200dma_slope": np.zeros(n),
                "credit_oas": np.linspace(52, 58, n),
                "dxy": np.full(n, 104.0),
                "yield_10y": np.full(n, 4.5),
                "yield_3m": np.full(n, 5.1),
                "yield_curve_10y3m": np.full(n, -0.6),
                "yield_2y": np.full(n, 4.8),
                "yield_curve_10y2y": np.full(n, -0.3),
                "fed_funds": np.full(n, 5.25),
                "unemployment_claims": np.full(n, 225000),
            },
            index=dates,
        )

        with patch("data.macro.fetch_macro_data", return_value=mock_df):
            result = backfill_macro_indicators(
                start_date="2024-10-01",
                end_date="2024-10-15",
            )

        assert isinstance(result, pd.DataFrame)
        assert len(result) > 0

        # Verify rows actually landed in SQLite
        db_rows = get_macro_indicators(start_date="2024-10-01", end_date="2024-10-15")
        assert len(db_rows) > 0

    def test_backfill_filters_to_requested_range(self):
        """Rows outside [start_date, end_date] must NOT be written to DB."""
        from data.macro import backfill_macro_indicators

        # fetch_macro_data returns data including warmup period before start_date
        warmup_dates = pd.bdate_range("2024-06-01", periods=5)
        target_dates = pd.bdate_range("2024-07-01", periods=5)
        all_dates = warmup_dates.append(target_dates)

        n_all = len(all_dates)
        mock_df = pd.DataFrame(
            {"vix": np.linspace(15, 25, n_all)},
            index=all_dates,
        )

        written_dates = []

        def _capture_write(df):
            written_dates.extend(df.index.strftime("%Y-%m-%d").tolist())
            return len(df)

        with (
            patch("data.macro.fetch_macro_data", return_value=mock_df),
            patch("data.macro.write_macro_indicators_to_db", side_effect=_capture_write),
        ):
            result = backfill_macro_indicators(
                start_date="2024-07-01",
                end_date="2024-07-31",
            )

        # Only target-range rows should have been written
        for d in written_dates:
            assert d >= "2024-07-01", f"Row outside range written: {d}"
