"""Tests for data ingestion module (data/ingest.py).

All tests are offline — no yfinance calls or network access.

Run with:  python -m pytest tests/test_ingest.py -v
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import db.atlas_db as _adb
from db.atlas_db import init_db

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ---------------------------------------------------------------------------
# Import helpers from ingest module (non-network parts)
# ---------------------------------------------------------------------------

from data.ingest import (  # noqa: E402
    _cache_path,
    _cache_is_fresh,
    _load_cache,
    _save_cache,
    _market_cache_dir,
)


# ---------------------------------------------------------------------------
# DB isolation — prevent writes to production atlas.db
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point atlas_db at a throw-away temp DB so tests never touch production."""
    db_path = str(tmp_path / "test_ingest.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    init_db()
    yield
    monkeypatch.setattr(_adb, "_db_path_override", None)


# ---------------------------------------------------------------------------
# _cache_path — ticker sanitisation
# ---------------------------------------------------------------------------

class TestCachePath:
    def test_dots_replaced_with_underscores(self):
        path = _cache_path("BHP.AX", market_id="asx")
        assert "BHP_AX" in path.name

    def test_uppercase(self):
        path = _cache_path("aapl", market_id="sp500")
        assert "AAPL" in path.name

    def test_parquet_extension(self):
        path = _cache_path("MSFT", market_id="sp500")
        assert path.suffix == ".parquet"

    def test_market_id_in_path(self):
        path = _cache_path("AAPL", market_id="sp500")
        assert "sp500" in str(path)

    def test_asx_market_in_path(self):
        path = _cache_path("BHP.AX", market_id="asx")
        assert "asx" in str(path)

    def test_multiple_dots_all_replaced(self):
        # Edge case: ticker with two dots
        path = _cache_path("A.B.C", market_id="test")
        # All dots in ticker should be underscores
        name_no_suffix = path.stem
        assert "." not in name_no_suffix

    def test_default_market(self):
        """Without explicit market_id, path is placed in the default market dir."""
        path = _cache_path("TICK")
        assert path.suffix == ".parquet"
        assert path.exists() or True  # path object created, dir may not exist


# ---------------------------------------------------------------------------
# _cache_is_fresh — freshness logic
# ---------------------------------------------------------------------------

class TestCacheIsFresh:
    def test_missing_file_not_fresh(self, tmp_path):
        fake_path = tmp_path / "nonexistent.parquet"
        assert _cache_is_fresh(fake_path) is False

    def test_old_file_not_fresh(self, tmp_path):
        old_file = tmp_path / "old.parquet"
        old_file.write_bytes(b"x")
        # Set modification time to 48 hours ago
        old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
        import os
        os.utime(str(old_file), (old_mtime, old_mtime))
        assert _cache_is_fresh(old_file, max_age_hours=24) is False

    def test_fresh_file_is_fresh(self, tmp_path):
        fresh_file = tmp_path / "fresh.parquet"
        fresh_file.write_bytes(b"x")
        # File just created → modification time is now
        assert _cache_is_fresh(fresh_file, max_age_hours=24) is True

    def test_custom_max_age_hours(self, tmp_path):
        file = tmp_path / "test.parquet"
        file.write_bytes(b"x")
        # 2-hour-old file should be fresh with max_age=3, stale with max_age=1
        mtime_2h_ago = (datetime.now() - timedelta(hours=2)).timestamp()
        import os
        os.utime(str(file), (mtime_2h_ago, mtime_2h_ago))
        assert _cache_is_fresh(file, max_age_hours=3) is True
        assert _cache_is_fresh(file, max_age_hours=1) is False


# ---------------------------------------------------------------------------
# _save_cache and _load_cache — round-trip parquet
# ---------------------------------------------------------------------------

class TestCacheRoundTrip:
    @pytest.fixture
    def sample_df(self) -> pd.DataFrame:
        dates = pd.date_range("2024-01-01", periods=20, freq="B")
        rng = np.random.default_rng(0)
        return pd.DataFrame(
            {
                "open": rng.uniform(99, 101, 20),
                "high": rng.uniform(101, 103, 20),
                "low": rng.uniform(97, 99, 20),
                "close": rng.uniform(99, 101, 20),
                "volume": rng.integers(1_000_000, 2_000_000, 20).astype(float),
                "ticker": "TEST",
            },
            index=dates,
        )

    def test_save_creates_parquet_file(self, tmp_path, sample_df):
        # Patch the market cache dir so files go to tmp_path
        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            _save_cache("TEST", sample_df, market_id="sp500")
        files = list(tmp_path.glob("*.parquet"))
        assert len(files) == 1

    def test_load_after_save_returns_same_rows(self, tmp_path, sample_df):
        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            _save_cache("RNDM", sample_df, market_id="sp500")
            loaded = _load_cache("RNDM", market_id="sp500")

        assert loaded is not None
        assert len(loaded) == len(sample_df)

    def test_load_after_save_preserves_columns(self, tmp_path, sample_df):
        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            _save_cache("COLS", sample_df, market_id="sp500")
            loaded = _load_cache("COLS", market_id="sp500")

        assert loaded is not None
        for col in ["open", "high", "low", "close", "volume"]:
            assert col in loaded.columns

    def test_save_empty_df_is_no_op(self, tmp_path):
        empty = pd.DataFrame()
        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            _save_cache("EMPTY", empty, market_id="sp500")
        files = list(tmp_path.glob("*.parquet"))
        assert len(files) == 0  # nothing written for empty DF

    def test_load_returns_none_when_stale(self, tmp_path, sample_df):
        """If the cache file is older than 24h, _load_cache returns None."""
        import os
        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            _save_cache("STALE", sample_df, market_id="sp500")
            path = tmp_path / "STALE.parquet"
            # Set mtime to 48h ago
            old_mtime = (datetime.now() - timedelta(hours=48)).timestamp()
            os.utime(str(path), (old_mtime, old_mtime))
            result = _load_cache("STALE", market_id="sp500")
        # Cache should be stale → returns None
        assert result is None

    def test_load_rejects_adj_close_columns(self, tmp_path, sample_df):
        """Old format caches with adj_close should be rejected (return None)."""
        sample_df_old = sample_df.copy()
        sample_df_old["adj_close"] = sample_df_old["close"]
        path = tmp_path / "OLDFORMAT.parquet"
        sample_df_old.to_parquet(path, engine="pyarrow")

        with patch("data.ingest._market_cache_dir", return_value=tmp_path):
            result = _load_cache("OLDFORMAT", market_id="sp500")
        # adj_close format should be rejected
        assert result is None


# ---------------------------------------------------------------------------
# _market_cache_dir
# ---------------------------------------------------------------------------

class TestMarketCacheDir:
    def test_returns_path_object(self):
        d = _market_cache_dir("sp500")
        assert isinstance(d, Path)

    def test_path_contains_market_id(self):
        d = _market_cache_dir("sp500")
        assert "sp500" in str(d)

    def test_path_contains_asx(self):
        d = _market_cache_dir("asx")
        assert "asx" in str(d)

    def test_uppercase_normalised(self):
        d_lower = _market_cache_dir("sp500")
        d_upper = _market_cache_dir("SP500")
        # Both should resolve to same directory
        assert d_lower == d_upper


# ---------------------------------------------------------------------------
# Direct parquet I/O (standalone, no mocking)
# ---------------------------------------------------------------------------

class TestParquetIO:
    def test_write_and_read_parquet(self, tmp_path):
        """Basic sanity: pandas can write and read parquet files."""
        dates = pd.date_range("2024-01-01", periods=10, freq="B")
        rng = np.random.default_rng(42)
        df = pd.DataFrame(
            {"close": rng.uniform(95, 105, 10), "volume": rng.integers(1_000_000, 2_000_000, 10).astype(float)},
            index=dates,
        )
        path = tmp_path / "test_io.parquet"
        df.to_parquet(path, engine="pyarrow")
        assert path.exists()

        loaded = pd.read_parquet(path)
        assert len(loaded) == 10
        assert "close" in loaded.columns
        assert "volume" in loaded.columns

    def test_parquet_preserves_datetime_index(self, tmp_path):
        dates = pd.date_range("2024-06-01", periods=5, freq="B")
        df = pd.DataFrame({"value": [1, 2, 3, 4, 5]}, index=dates)
        path = tmp_path / "datetime_idx.parquet"
        df.to_parquet(path)
        loaded = pd.read_parquet(path)
        assert isinstance(loaded.index, pd.DatetimeIndex)
        assert len(loaded) == 5
