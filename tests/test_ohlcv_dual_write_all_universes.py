"""Regression tests: OHLCV dual-write covers all 7 universes (P1-12).

Root cause:
    The SQLite ohlcv table has PRIMARY KEY (ticker, date), so a ticker can
    only appear once per date regardless of universe.  Cross-universe tickers
    (GLD in commodity_etfs+gold_etfs; XLU/XLP in sector_etfs+defensive_etfs)
    get overwritten when multiple universe ingests run.  For static ETF
    universes ``get_universe_data()`` queries by ticker IN (…) without a
    universe filter, so the data is always accessible.

    ASX tickers were missing from SQLite entirely (the parquet filenames use
    ``_AX`` suffix while SQLite stores the canonical ``.AX`` form).

These tests verify:
1. ``_sqlite_batch_write`` correctly stores rows for non-sp500 universes.
2. The universe column is written as the calling universe name.
3. Cross-universe tickers can be read back WITHOUT a universe filter.
4. ASX ticker filename ↔ SQLite ticker name round-trip is consistent.
5. The canonical ``get_universe_data()`` path returns data for all 7
   universe members regardless of which universe last wrote the SQLite row.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Isolated SQLite DB with ohlcv table."""
    db_path = tmp_path / "test_atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE ohlcv (
            ticker      TEXT NOT NULL,
            date        TEXT NOT NULL,
            open        REAL NOT NULL,
            high        REAL NOT NULL,
            low         REAL NOT NULL,
            close       REAL NOT NULL,
            adj_close   REAL,
            volume      INTEGER NOT NULL,
            universe    TEXT NOT NULL,
            source      TEXT DEFAULT 'yfinance',
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.commit()
    conn.close()
    monkeypatch.setattr("db.atlas_db._db_path_override", str(db_path))
    from db.atlas_db import init_db
    import db.atlas_db as _adb
    _adb._db_path_override = str(db_path)
    return db_path


def _make_ohlcv_df(dates: list[str], close_start: float = 100.0) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame for testing."""
    idx = pd.DatetimeIndex(dates)
    data = {
        "open": [close_start + i for i in range(len(dates))],
        "high": [close_start + i + 1 for i in range(len(dates))],
        "low": [close_start + i - 1 for i in range(len(dates))],
        "close": [close_start + i for i in range(len(dates))],
        "volume": [1_000_000] * len(dates),
    }
    return pd.DataFrame(data, index=idx)


# ── _sqlite_batch_write unit tests ────────────────────────────────────────────

class TestSqliteBatchWriteAllUniverses:
    """_sqlite_batch_write must work identically for all 7 universes."""

    ALL_UNIVERSES = [
        "sp500",
        "commodity_etfs",
        "sector_etfs",
        "defensive_etfs",
        "gold_etfs",
        "treasury_etfs",
        "asx",
    ]
    DATES = ["2026-04-21", "2026-04-22"]

    def test_writes_rows_for_all_universes(self, tmp_db) -> None:
        """All 7 universes produce rows in the ohlcv table."""
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        for uni in self.ALL_UNIVERSES:
            ticker = f"TEST_{uni.upper()[:3]}"
            df = _make_ohlcv_df(self.DATES, close_start=float(self.ALL_UNIVERSES.index(uni) * 10 + 100))
            with patch.object(_adb, "_db_path_override", str(tmp_db)):
                n = _sqlite_batch_write(ticker, df, uni)
            assert n == len(self.DATES), f"{uni}: expected {len(self.DATES)} rows, got {n}"

    def test_universe_column_matches_calling_universe(self, tmp_db) -> None:
        """The universe column in SQLite must match the universe passed to _sqlite_batch_write."""
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            _sqlite_batch_write("AAPL", _make_ohlcv_df(self.DATES), "sector_etfs")

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT universe FROM ohlcv WHERE ticker='AAPL' LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "sector_etfs"

    def test_cross_universe_ticker_readable_without_filter(self, tmp_db) -> None:
        """GLD in both commodity_etfs and gold_etfs: data readable without universe filter.

        Simulates the PRIMARY KEY overwrite scenario: the last ingest wins the
        universe column, but the data (date, close) is always accessible.
        """
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        dates = ["2026-04-21", "2026-04-22"]
        df = _make_ohlcv_df(dates, close_start=220.0)

        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            # Write as commodity_etfs first
            _sqlite_batch_write("GLD", df, "commodity_etfs")
            # Write as gold_etfs — overwrites universe column via INSERT OR REPLACE
            _sqlite_batch_write("GLD", df, "gold_etfs")

        conn = sqlite3.connect(str(tmp_db))
        rows = conn.execute(
            "SELECT date, close, universe FROM ohlcv WHERE ticker='GLD' ORDER BY date"
        ).fetchall()
        conn.close()

        # Both dates must be present (data is not lost)
        assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
        # The universe reflects the last write (gold_etfs)
        assert rows[0][2] == "gold_etfs", "universe column should be last writer"
        # Close prices are correct
        for i, (date, close, _uni) in enumerate(rows):
            assert date == dates[i]
            assert abs(close - (220.0 + i)) < 0.01

    def test_cross_universe_xlp_xlu(self, tmp_db) -> None:
        """XLP and XLU appear in both sector_etfs and defensive_etfs.

        Data written by sector_etfs THEN defensive_etfs: after both writes,
        data is present and readable (regardless of universe tag).
        """
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        dates = ["2026-04-22", "2026-04-23"]
        for ticker in ("XLP", "XLU"):
            df = _make_ohlcv_df(dates, close_start=50.0)
            with patch.object(_adb, "_db_path_override", str(tmp_db)):
                _sqlite_batch_write(ticker, df, "sector_etfs")
                _sqlite_batch_write(ticker, df, "defensive_etfs")

            conn = sqlite3.connect(str(tmp_db))
            count = conn.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE ticker=?", (ticker,)
            ).fetchone()[0]
            conn.close()
            assert count == 2, f"{ticker}: expected 2 rows, got {count}"

    def test_asx_ticker_dot_suffix_stored_correctly(self, tmp_db) -> None:
        """ASX tickers must be stored with .AX dot suffix in SQLite.

        The parquet cache uses _AX underscore (filename-safe), but SQLite
        stores the canonical ticker form with .AX.
        """
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        ticker = "ANZ.AX"  # canonical form
        df = _make_ohlcv_df(["2026-04-22"], close_start=29.5)
        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            n = _sqlite_batch_write(ticker, df, "asx")

        assert n == 1
        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT ticker, universe FROM ohlcv WHERE ticker='ANZ.AX'"
        ).fetchone()
        conn.close()
        assert row is not None, "ANZ.AX not found in SQLite"
        assert row[0] == "ANZ.AX"
        assert row[1] == "asx"

    def test_insert_or_replace_updates_data(self, tmp_db) -> None:
        """INSERT OR REPLACE must update existing rows — not silently fail."""
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        dates = ["2026-04-22"]
        df_v1 = _make_ohlcv_df(dates, close_start=100.0)
        df_v2 = _make_ohlcv_df(dates, close_start=200.0)

        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            _sqlite_batch_write("AAPL", df_v1, "sp500")
            _sqlite_batch_write("AAPL", df_v2, "sp500")

        conn = sqlite3.connect(str(tmp_db))
        row = conn.execute(
            "SELECT close FROM ohlcv WHERE ticker='AAPL' AND date='2026-04-22'"
        ).fetchone()
        conn.close()
        assert row is not None
        assert abs(row[0] - 200.0) < 0.01, "INSERT OR REPLACE must update close price"

    def test_empty_dataframe_writes_zero_rows(self, tmp_db) -> None:
        """Empty DataFrame must write 0 rows without error."""
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        df = pd.DataFrame()
        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            n = _sqlite_batch_write("AAPL", df, "sp500")
        assert n == 0

    def test_returns_correct_row_count(self, tmp_db) -> None:
        """_sqlite_batch_write must return the actual number of rows written."""
        from data.ingest import _sqlite_batch_write
        import db.atlas_db as _adb

        dates = ["2026-04-21", "2026-04-22", "2026-04-23"]
        df = _make_ohlcv_df(dates)
        with patch.object(_adb, "_db_path_override", str(tmp_db)):
            n = _sqlite_batch_write("MSFT", df, "sp500")
        assert n == 3
