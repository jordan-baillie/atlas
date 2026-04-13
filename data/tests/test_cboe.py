"""
Tests for data/cboe.py — CBOE Put/Call Ratio scraper.

All network calls are mocked.  Tests run fully offline.
"""

import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_CSV_STANDARD = """Date,Calls,Puts,Total,P/C Ratio
2024-01-02,1234567,987654,2222221,0.80
2024-01-03,1100000,900000,2000000,0.82
2024-01-04,1050000,1100000,2150000,1.05
"""

_SAMPLE_CSV_EQUITY = """DATE,CALLS,PUTS,TOTAL,EQUITY P/C RATIO,INDEX P/C RATIO,TOTAL P/C RATIO
2024-01-02,500000,400000,900000,0.79,1.10,0.88
2024-01-03,510000,420000,930000,0.83,1.15,0.91
"""

_SAMPLE_CSV_MINIMAL = """date,ratio
2024-03-01,0.75
2024-03-04,0.85
"""

_BAD_CSV = "this is not valid csv content\n###\n???"


def _make_response(status_code: int, text: str) -> MagicMock:
    r = MagicMock()
    r.status_code = status_code
    r.ok = (status_code == 200)
    r.text = text
    return r


# ---------------------------------------------------------------------------
# 1. _parse_cboe_csv
# ---------------------------------------------------------------------------

class TestParseCboeCsv:
    """Unit tests for the internal CSV parser."""

    def _parse(self, text: str) -> pd.Series:
        from data.cboe import _parse_cboe_csv
        return _parse_cboe_csv(text)

    def test_standard_format(self):
        s = self._parse(_SAMPLE_CSV_STANDARD)
        assert isinstance(s, pd.Series)
        assert len(s) == 3
        assert abs(s.iloc[0] - 0.80) < 1e-6

    def test_equity_format_prefers_equity_column(self):
        """When 'equity p/c ratio' column is present it should be chosen."""
        s = self._parse(_SAMPLE_CSV_EQUITY)
        assert len(s) == 2
        # First row equity ratio is 0.79
        assert abs(s.iloc[0] - 0.79) < 1e-6

    def test_minimal_ratio_column(self):
        s = self._parse(_SAMPLE_CSV_MINIMAL)
        assert len(s) == 2
        assert abs(s.iloc[0] - 0.75) < 1e-6

    def test_returns_empty_on_bad_csv(self):
        s = self._parse(_BAD_CSV)
        assert isinstance(s, pd.Series)
        # May be empty (no valid date/ratio columns) or have 0 numeric rows
        # after dropna — just must not raise.

    def test_returns_empty_on_empty_string(self):
        s = self._parse("")
        assert isinstance(s, pd.Series)

    def test_index_is_datetimeindex(self):
        s = self._parse(_SAMPLE_CSV_STANDARD)
        assert isinstance(s.index, pd.DatetimeIndex)

    def test_series_is_sorted(self):
        s = self._parse(_SAMPLE_CSV_STANDARD)
        assert s.index.is_monotonic_increasing

    def test_no_nan_values_in_result(self):
        s = self._parse(_SAMPLE_CSV_STANDARD)
        assert not s.isna().any()

    def test_mixed_whitespace_headers(self):
        """Headers with leading/trailing spaces should still match."""
        csv = "  Date , Calls , Puts , Total , P/C Ratio \n2024-06-01,100,200,300,0.95\n"
        s = self._parse(csv)
        assert len(s) == 1
        assert abs(s.iloc[0] - 0.95) < 1e-6


# ---------------------------------------------------------------------------
# 2. _fetch_from_url
# ---------------------------------------------------------------------------

class TestFetchFromUrl:
    """Tests for the URL-fetching helper."""

    def _patch_session(self, mock_resp):
        """Return context managers that mock both cloudscraper and requests.Session."""
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session.headers = {}
        return (
            patch("data.cboe.cloudscraper", side_effect=ImportError("not installed")),
            patch("data.cboe.requests.Session", return_value=mock_session),
        )

    def test_success_returns_series(self):
        from data.cboe import _fetch_from_url

        mock_resp = _make_response(200, _SAMPLE_CSV_STANDARD)
        p1, p2 = self._patch_session(mock_resp)
        with p1, p2:
            result = _fetch_from_url("https://example.com/fake.csv")

        assert result is not None
        assert isinstance(result, pd.Series)
        assert len(result) == 3

    def test_403_returns_none(self):
        from data.cboe import _fetch_from_url

        mock_resp = _make_response(403, "Access Denied")
        p1, p2 = self._patch_session(mock_resp)
        with p1, p2:
            result = _fetch_from_url("https://cdn.cboe.com/resources/options/totalpc.csv")

        assert result is None

    def test_404_returns_none(self):
        from data.cboe import _fetch_from_url

        mock_resp = _make_response(404, "Not Found")
        p1, p2 = self._patch_session(mock_resp)
        with p1, p2:
            result = _fetch_from_url("https://example.com/missing.csv")

        assert result is None

    def test_timeout_returns_none(self):
        import requests as req_lib
        from data.cboe import _fetch_from_url

        mock_session = MagicMock()
        mock_session.get.side_effect = req_lib.exceptions.Timeout
        mock_session.headers = {}
        with (
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
        ):
            result = _fetch_from_url("https://example.com/slow.csv")

        assert result is None

    def test_connection_error_returns_none(self):
        import requests as req_lib
        from data.cboe import _fetch_from_url

        mock_session = MagicMock()
        mock_session.get.side_effect = req_lib.exceptions.ConnectionError
        mock_session.headers = {}
        with (
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
        ):
            result = _fetch_from_url("https://unreachable.example.com/")

        assert result is None

    def test_empty_csv_returns_none(self):
        from data.cboe import _fetch_from_url

        mock_resp = _make_response(200, "Date,P/C Ratio\n")  # header only
        p1, p2 = self._patch_session(mock_resp)
        with p1, p2:
            result = _fetch_from_url("https://example.com/empty.csv")

        assert result is None


# ---------------------------------------------------------------------------
# 3. fetch_put_call_ratio — main public function
# ---------------------------------------------------------------------------

class TestFetchPutCallRatio:
    """End-to-end tests for the public API."""

    @pytest.fixture(autouse=True)
    def _isolate_cache(self, tmp_path, monkeypatch):
        """Point CACHE_DIR and CACHE_FILE at a temporary directory per test."""
        import data.cboe as cboe_mod
        tmp_cache = tmp_path / "cboe"
        tmp_cache.mkdir()
        monkeypatch.setattr(cboe_mod, "CACHE_DIR", tmp_cache)
        monkeypatch.setattr(cboe_mod, "CACHE_FILE", tmp_cache / "totalpc.parquet")

    def _mock_all_live_fail(self):
        """Return patches that make all live sources fail."""
        mock_session = MagicMock()
        mock_session.get.return_value = _make_response(403, "Access Denied")
        mock_session.headers = {}
        return (
            patch("data.cboe._compute_pc_from_spy_options", return_value=None),
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
            patch("data.cboe._compute_vix_term_proxy", return_value=None),
        )

    def test_returns_series_on_spy_success(self):
        """SPY options chain is now the primary source."""
        from data.cboe import fetch_put_call_ratio

        spy_result = pd.Series(
            [0.85],
            index=pd.DatetimeIndex([pd.Timestamp.now().normalize()]),
            name="put_call_ratio",
        )
        with patch("data.cboe._compute_pc_from_spy_options", return_value=spy_result):
            s = fetch_put_call_ratio()

        assert isinstance(s, pd.Series)
        assert len(s) >= 1

    def test_returns_empty_on_all_fail(self):
        from data.cboe import fetch_put_call_ratio

        p1, p2, p3, p4 = self._mock_all_live_fail()
        with p1, p2, p3, p4:
            s = fetch_put_call_ratio()

        assert isinstance(s, pd.Series)
        assert s.empty

    def test_falls_through_to_cboe_when_spy_fails(self):
        """If SPY options fail, CBOE URLs should be tried next."""
        from data.cboe import fetch_put_call_ratio

        mock_session = MagicMock()
        mock_session.get.return_value = _make_response(200, _SAMPLE_CSV_STANDARD)
        mock_session.headers = {}
        with (
            patch("data.cboe._compute_pc_from_spy_options", return_value=None),
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
        ):
            s = fetch_put_call_ratio()

        assert isinstance(s, pd.Series)
        assert len(s) == 3

    def test_falls_through_to_vix_proxy(self):
        """If SPY and CBOE fail, VIX proxy should be tried."""
        from data.cboe import fetch_put_call_ratio

        vix_result = pd.Series(
            [0.92],
            index=pd.DatetimeIndex([pd.Timestamp.now().normalize()]),
            name="put_call_ratio",
        )
        mock_session = MagicMock()
        mock_session.get.return_value = _make_response(403, "Blocked")
        mock_session.headers = {}
        with (
            patch("data.cboe._compute_pc_from_spy_options", return_value=None),
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
            patch("data.cboe._compute_vix_term_proxy", return_value=vix_result),
        ):
            s = fetch_put_call_ratio()

        assert isinstance(s, pd.Series)
        assert len(s) >= 1
        assert abs(s.iloc[-1] - 0.92) < 1e-6

    def test_start_date_filter(self):
        from data.cboe import fetch_put_call_ratio

        mock_session = MagicMock()
        mock_session.get.return_value = _make_response(200, _SAMPLE_CSV_STANDARD)
        mock_session.headers = {}
        with (
            patch("data.cboe._compute_pc_from_spy_options", return_value=None),
            patch("data.cboe.cloudscraper", side_effect=ImportError),
            patch("data.cboe.requests.Session", return_value=mock_session),
        ):
            s = fetch_put_call_ratio(start_date="2024-01-04")

        # Only 2024-01-04 should remain (the 1.05 row)
        assert len(s) == 1
        assert abs(s.iloc[0] - 1.05) < 1e-6

    def test_caches_result(self, tmp_path, monkeypatch):
        """After a successful fetch, cache file should exist."""
        import data.cboe as cboe_mod
        tmp_cache = tmp_path / "cboe_cache_test"
        tmp_cache.mkdir()
        monkeypatch.setattr(cboe_mod, "CACHE_DIR", tmp_cache)
        cache_file = tmp_cache / "totalpc.parquet"
        monkeypatch.setattr(cboe_mod, "CACHE_FILE", cache_file)

        spy_result = pd.Series(
            [0.85],
            index=pd.DatetimeIndex([pd.Timestamp.now().normalize()]),
            name="put_call_ratio",
        )
        with patch("data.cboe._compute_pc_from_spy_options", return_value=spy_result):
            fetch_put_call_ratio()

        assert cache_file.exists(), "Cache file should be created after successful fetch"

    def test_uses_fresh_cache_without_network(self, tmp_path, monkeypatch):
        """A fresh cache hit should NOT make any network requests or compute."""
        import data.cboe as cboe_mod

        tmp_cache = tmp_path / "cboe_cache2"
        tmp_cache.mkdir()
        cache_file = tmp_cache / "totalpc.parquet"
        monkeypatch.setattr(cboe_mod, "CACHE_DIR", tmp_cache)
        monkeypatch.setattr(cboe_mod, "CACHE_FILE", cache_file)

        # Pre-populate cache
        s = pd.Series(
            [0.80, 0.82],
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
            name="put_call_ratio",
        )
        s.to_frame().to_parquet(cache_file)

        with (
            patch("data.cboe._compute_pc_from_spy_options") as mock_spy,
            patch("data.cboe.requests.Session") as mock_session,
        ):
            result = fetch_put_call_ratio()

        mock_spy.assert_not_called()
        mock_session.assert_not_called()
        assert len(result) == 2

    def test_stale_cache_used_when_all_fail(self, tmp_path, monkeypatch):
        """If all live sources fail but stale cache exists, return stale data."""
        import data.cboe as cboe_mod

        tmp_cache = tmp_path / "cboe_stale"
        tmp_cache.mkdir()
        cache_file = tmp_cache / "totalpc.parquet"
        monkeypatch.setattr(cboe_mod, "CACHE_DIR", tmp_cache)
        monkeypatch.setattr(cboe_mod, "CACHE_FILE", cache_file)

        # Write stale cache
        s = pd.Series(
            [0.88],
            index=pd.to_datetime(["2023-12-29"]),
            name="put_call_ratio",
        )
        s.to_frame().to_parquet(cache_file)

        # Force stale
        monkeypatch.setattr(cboe_mod, "_cache_is_fresh", lambda: False)

        p1, p2, p3, p4 = self._mock_all_live_fail()
        with p1, p2, p3, p4:
            result = fetch_put_call_ratio()

        assert len(result) == 1
        assert abs(result.iloc[0] - 0.88) < 1e-6

    def test_index_is_datetimeindex(self):
        from data.cboe import fetch_put_call_ratio

        spy_result = pd.Series(
            [0.85],
            index=pd.DatetimeIndex([pd.Timestamp.now().normalize()]),
            name="put_call_ratio",
        )
        with patch("data.cboe._compute_pc_from_spy_options", return_value=spy_result):
            s = fetch_put_call_ratio()

        assert isinstance(s.index, pd.DatetimeIndex)

    def test_values_are_float(self):
        from data.cboe import fetch_put_call_ratio

        spy_result = pd.Series(
            [0.85],
            index=pd.DatetimeIndex([pd.Timestamp.now().normalize()]),
            name="put_call_ratio",
        )
        with patch("data.cboe._compute_pc_from_spy_options", return_value=spy_result):
            s = fetch_put_call_ratio()

        assert s.dtype == float or str(s.dtype).startswith("float")


# ---------------------------------------------------------------------------
# 4. VIX term-structure proxy
# ---------------------------------------------------------------------------

class TestVixTermProxy:
    """Tests for the VIX/VIX3M term-structure proxy fallback."""

    def test_returns_series_on_success(self):
        from data.cboe import _compute_vix_term_proxy

        # Create mock yfinance download result with MultiIndex columns
        dates = pd.to_datetime(["2026-04-11", "2026-04-12"])
        arrays = [["Close", "Close"], ["^VIX", "^VIX3M"]]
        columns = pd.MultiIndex.from_arrays(arrays)
        mock_data = pd.DataFrame(
            [[18.5, 20.0], [19.0, 21.0]],
            index=dates,
            columns=columns,
        )
        with patch("data.cboe.yf.download", return_value=mock_data):
            result = _compute_vix_term_proxy()

        assert result is not None
        assert len(result) == 1
        expected = 19.0 / 21.0  # latest VIX / VIX3M
        assert abs(result.iloc[0] - expected) < 1e-6

    def test_returns_none_on_empty_download(self):
        from data.cboe import _compute_vix_term_proxy

        with patch("data.cboe.yf.download", return_value=pd.DataFrame()):
            result = _compute_vix_term_proxy()
        assert result is None

    def test_returns_none_on_exception(self):
        from data.cboe import _compute_vix_term_proxy

        with patch("data.cboe.yf.download", side_effect=RuntimeError("network error")):
            result = _compute_vix_term_proxy()
        assert result is None


# ---------------------------------------------------------------------------
# 5. fred.py integration — _fetch_cboe_put_call_ratio helper
# ---------------------------------------------------------------------------

class TestFredCboeIntegration:
    """Tests for the helper wired into fetch_regime_macro_series."""

    def test_helper_exists(self):
        from data.fred import _fetch_cboe_put_call_ratio
        assert callable(_fetch_cboe_put_call_ratio)

    def test_helper_returns_series_on_success(self):
        from data.fred import _fetch_cboe_put_call_ratio

        mock_series = pd.Series(
            [0.78, 0.82],
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )
        with patch("data.cboe.fetch_put_call_ratio", return_value=mock_series):
            result = _fetch_cboe_put_call_ratio(start_date="2024-01-01")

        assert isinstance(result, pd.Series)
        assert len(result) == 2

    def test_helper_applies_end_date(self):
        from data.fred import _fetch_cboe_put_call_ratio

        mock_series = pd.Series(
            [0.78, 0.82, 0.90],
            index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
        )
        with patch("data.cboe.fetch_put_call_ratio", return_value=mock_series):
            result = _fetch_cboe_put_call_ratio(end_date="2024-01-03")

        assert len(result) == 2  # 2024-01-04 trimmed by end_date

    def test_helper_returns_empty_on_exception(self):
        from data.fred import _fetch_cboe_put_call_ratio

        with patch("data.cboe.fetch_put_call_ratio", side_effect=RuntimeError("network down")):
            result = _fetch_cboe_put_call_ratio()

        assert isinstance(result, pd.Series)
        assert result.empty

    def test_fetch_regime_macro_series_includes_put_call_ratio(self):
        """fetch_regime_macro_series must include 'put_call_ratio' key."""
        from data.fred import fetch_regime_macro_series

        empty = pd.Series(dtype=float)
        mock_result = pd.Series(
            [0.80, 0.82],
            index=pd.to_datetime(["2024-01-02", "2024-01-03"]),
        )

        with (
            patch("data.fred.FREDClient") as MockClient,
            patch("data.fred._fetch_cboe_put_call_ratio", return_value=mock_result),
        ):
            inst = MockClient.return_value
            inst.fetch_series.return_value = empty
            result = fetch_regime_macro_series(start_date="2024-01-01")

        assert "put_call_ratio" in result, "put_call_ratio key must be in regime series dict"
        assert isinstance(result["put_call_ratio"], pd.Series)
        assert len(result["put_call_ratio"]) == 2

    def test_fetch_regime_macro_series_put_call_empty_on_cboe_failure(self):
        """put_call_ratio should be empty (not raise) when CBOE is down."""
        from data.fred import fetch_regime_macro_series

        empty = pd.Series(dtype=float)

        with (
            patch("data.fred.FREDClient") as MockClient,
            patch("data.fred._fetch_cboe_put_call_ratio", return_value=empty),
        ):
            inst = MockClient.return_value
            inst.fetch_series.return_value = empty
            result = fetch_regime_macro_series()

        assert "put_call_ratio" in result
        assert result["put_call_ratio"].empty

    def test_get_put_call_ratio_method_uses_cboe(self):
        """FREDClient.get_put_call_ratio() should delegate to data.cboe."""
        from data.fred import FREDClient

        mock_series = pd.Series(
            [0.75],
            index=pd.to_datetime(["2024-04-01"]),
        )
        with patch("data.cboe.fetch_put_call_ratio", return_value=mock_series):
            client = FREDClient.__new__(FREDClient)
            client.api_key = "dummy"
            result = client.get_put_call_ratio()

        assert isinstance(result, pd.Series)
        assert len(result) == 1

    def test_get_put_call_ratio_returns_empty_on_import_error(self):
        """get_put_call_ratio must return empty Series if data.cboe is missing."""
        from data.fred import FREDClient

        client = FREDClient.__new__(FREDClient)
        client.api_key = "dummy"

        with patch.dict("sys.modules", {"data.cboe": None}):
            result = client.get_put_call_ratio()

        assert isinstance(result, pd.Series)
        assert result.empty


# ---------------------------------------------------------------------------
# 6. Module constants / structure
# ---------------------------------------------------------------------------

class TestModuleStructure:
    def test_cboe_csv_urls_list_not_empty(self):
        from data.cboe import CBOE_CSV_URLS
        assert isinstance(CBOE_CSV_URLS, list)
        assert len(CBOE_CSV_URLS) >= 2

    def test_cache_dir_under_data_cache(self):
        from data.cboe import CACHE_DIR
        assert "cache" in str(CACHE_DIR)
        assert "cboe" in str(CACHE_DIR)

    def test_fetch_put_call_ratio_importable(self):
        from data.cboe import fetch_put_call_ratio  # noqa: F401
        assert callable(fetch_put_call_ratio)

    def test_cboe_urls_are_strings(self):
        from data.cboe import CBOE_CSV_URLS
        for url in CBOE_CSV_URLS:
            assert isinstance(url, str)
            assert url.startswith("https://")
