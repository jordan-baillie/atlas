"""
tests/test_backfill_intraday_5min.py
=====================================
Unit tests for scripts/backfill_intraday_5min.py.

Tests:
  (a) Idempotency skip: already-done months are skipped without API calls
  (b) Dry-run: prints expected calls without making any network requests
  (c) Successful download: writes expected schema to parquet

Run:
    cd /root/atlas && python3 -m pytest tests/test_backfill_intraday_5min.py -v --timeout=30
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.backfill_intraday_5min import (
    EXPECTED_COLUMNS,
    backfill_ticker,
    fetch_5min_bars,
    is_month_done,
    iter_monthly_windows,
    load_checkpoint,
    mark_month_done,
    merge_bars,
    parquet_path,
    read_existing,
    save_checkpoint,
    write_parquet,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
    """Override CACHE_DIR and CHECKPOINT_FILE to use a temporary directory."""
    import scripts.backfill_intraday_5min as mod

    monkeypatch.setattr(mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(mod, "CHECKPOINT_FILE", tmp_path / "_checkpoint.json")
    return tmp_path


@pytest.fixture
def sample_bars_response():
    """Minimal Tiingo IEX response for 3 bars."""
    return [
        {
            "date": "2026-05-12T13:30:00.000Z",
            "open": 736.87,
            "high": 737.18,
            "low": 736.165,
            "close": 736.335,
            "volume": 17133.0,
        },
        {
            "date": "2026-05-12T13:35:00.000Z",
            "open": 736.355,
            "high": 736.365,
            "low": 735.53,
            "close": 736.09,
            "volume": 16933.0,
        },
        {
            "date": "2026-05-12T13:40:00.000Z",
            "open": 736.09,
            "high": 736.78,
            "low": 735.795,
            "close": 736.71,
            "volume": 15749.0,
        },
    ]


@pytest.fixture
def sample_df(sample_bars_response):
    """Build expected DataFrame from sample bars."""
    rows = []
    for item in sample_bars_response:
        rows.append({
            "timestamp": pd.Timestamp(item["date"], tz="UTC"),
            "open":   float(item["open"]),
            "high":   float(item["high"]),
            "low":    float(item["low"]),
            "close":  float(item["close"]),
            "volume": int(item["volume"]),
        })
    df = pd.DataFrame(rows).set_index("timestamp")
    df.index.name = "timestamp"
    return df.sort_index()


# ─────────────────────────────────────────────────────────────────────────────
# Test (a): Idempotency skip
# ─────────────────────────────────────────────────────────────────────────────

class TestIdempotency:
    """Verify that already-done months are skipped without API calls."""

    def test_skip_done_month_no_api_call(self, tmp_cache, monkeypatch):
        """backfill_ticker skips a month marked 'done' in checkpoint, no HTTP call made."""
        import scripts.backfill_intraday_5min as mod

        # Mark May-2026 as done in checkpoint
        checkpoint = {"SPY": {"2026-05": "done"}}

        # Patch fetch_5min_bars to fail if called
        fetch_called = []

        def mock_fetch(*args, **kwargs):
            fetch_called.append(args)
            raise AssertionError("fetch_5min_bars should NOT be called for done months")

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)
        # Disable sleep so tests run fast
        monkeypatch.setattr(mod, "INTER_CALL_DELAY", 0)

        session = MagicMock()
        rows = backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
            dry_run=False,
            force_refresh=False,
        )

        # No rows fetched, no HTTP call made
        assert rows == 0, f"Expected 0 rows for already-done month, got {rows}"
        assert len(fetch_called) == 0, "fetch_5min_bars was called despite month being done"

    def test_force_refresh_ignores_done_checkpoint(self, tmp_cache, monkeypatch, sample_df):
        """force_refresh=True re-fetches even if checkpoint marks month done."""
        import scripts.backfill_intraday_5min as mod

        checkpoint = {"SPY": {"2026-05": "done"}}

        fetch_called = []

        def mock_fetch(ticker, start, end, token, session):
            fetch_called.append((ticker, start, end))
            return sample_df

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)
        monkeypatch.setattr(mod, "INTER_CALL_DELAY", 0)

        session = MagicMock()
        rows = backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
            dry_run=False,
            force_refresh=True,
        )

        # Should have called fetch and written rows
        assert len(fetch_called) == 1, "Expected 1 fetch call with force_refresh=True"
        assert rows == len(sample_df), f"Expected {len(sample_df)} rows, got {rows}"

    def test_is_month_done_returns_false_when_missing(self):
        """is_month_done returns False for ticker/month not in checkpoint."""
        checkpoint = {}
        assert is_month_done(checkpoint, "AAPL", "2024-01") is False

    def test_is_month_done_returns_true_when_present(self):
        """is_month_done returns True for a completed month."""
        checkpoint = {"AAPL": {"2024-01": "done"}}
        assert is_month_done(checkpoint, "AAPL", "2024-01") is True

    def test_is_month_done_force_refresh_always_false(self):
        """is_month_done returns False when force_refresh=True regardless of checkpoint."""
        checkpoint = {"AAPL": {"2024-01": "done"}}
        assert is_month_done(checkpoint, "AAPL", "2024-01", force_refresh=True) is False


# ─────────────────────────────────────────────────────────────────────────────
# Test (b): Dry-run
# ─────────────────────────────────────────────────────────────────────────────

class TestDryRun:
    """Verify dry-run prints expected calls without making HTTP requests."""

    def test_dry_run_prints_calls_no_network(self, tmp_cache, monkeypatch, capsys):
        """dry_run=True prints GET URLs but makes no HTTP requests and writes nothing."""
        import scripts.backfill_intraday_5min as mod

        fetch_called = []

        def mock_fetch(*args, **kwargs):
            fetch_called.append(args)
            raise AssertionError("fetch_5min_bars must NOT be called in dry-run mode")

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)

        checkpoint = {}
        session = MagicMock()

        rows = backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
            dry_run=True,
            force_refresh=False,
        )

        captured = capsys.readouterr()

        # No rows fetched
        assert rows == 0, "dry_run should return 0 rows"
        # No network calls
        assert len(fetch_called) == 0, "No fetch calls in dry-run mode"
        # Printed expected URL fragment
        assert "[DRY-RUN]" in captured.out, "Expected DRY-RUN marker in stdout"
        assert "SPY" in captured.out, "Expected ticker in dry-run output"
        assert "2026-05-12" in captured.out, "Expected start date in dry-run output"
        assert "resampleFreq=5min" in captured.out, "Expected resampleFreq in dry-run output"

    def test_dry_run_no_parquet_written(self, tmp_cache, monkeypatch):
        """dry_run=True leaves no parquet file on disk."""
        import scripts.backfill_intraday_5min as mod

        monkeypatch.setattr(mod, "fetch_5min_bars", lambda *a, **k: pd.DataFrame())

        checkpoint = {}
        session = MagicMock()
        backfill_ticker(
            ticker="AAPL",
            start_date="2026-05-01",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
            dry_run=True,
        )

        path = tmp_cache / "AAPL.parquet"
        assert not path.exists(), "No parquet file should exist after dry-run"

    def test_dry_run_via_cli(self, tmp_cache, monkeypatch, capsys):
        """CLI --dry-run prints correct output without executing any fetches."""
        import scripts.backfill_intraday_5min as mod

        monkeypatch.setattr(mod, "CACHE_DIR", tmp_cache)
        monkeypatch.setattr(mod, "CHECKPOINT_FILE", tmp_cache / "_checkpoint.json")

        # Patch load_tiingo_token so no secrets file needed
        monkeypatch.setattr(mod, "load_tiingo_token", lambda: "fake-token")

        # Ensure no real HTTP calls
        def mock_fetch(*a, **k):
            raise AssertionError("Should not fetch in dry-run")

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)

        rc = mod.main([
            "--ticker", "SPY",
            "--start", "2026-05-01",
            "--end", "2026-05-16",
            "--dry-run",
        ])

        captured = capsys.readouterr()
        assert rc == 0, f"Expected exit 0, got {rc}"
        assert "[DRY-RUN]" in captured.out
        assert "SPY" in captured.out


# ─────────────────────────────────────────────────────────────────────────────
# Test (c): Successful download writes expected schema
# ─────────────────────────────────────────────────────────────────────────────

class TestSuccessfulDownload:
    """Verify successful download writes correct schema to parquet."""

    def test_download_writes_expected_schema(
        self, tmp_cache, monkeypatch, sample_bars_response, sample_df
    ):
        """A successful fetch writes a parquet with correct columns and UTC DatetimeIndex."""
        import scripts.backfill_intraday_5min as mod

        def mock_fetch(ticker, start, end, token, session):
            return sample_df

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)
        monkeypatch.setattr(mod, "INTER_CALL_DELAY", 0)

        checkpoint = {}
        session = MagicMock()

        rows = backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
        )

        # Row count correct
        assert rows == len(sample_df), f"Expected {len(sample_df)} rows, got {rows}"

        # Parquet file written
        path = tmp_cache / "SPY.parquet"
        assert path.exists(), "SPY.parquet not found after successful download"

        # Schema validation
        df = pd.read_parquet(path)
        assert df.index.name == "timestamp", "Index must be named 'timestamp'"
        assert "UTC" in str(df.index.dtype), (
            f"Index must be UTC-aware datetime, got {df.index.dtype}"
        )
        for col in EXPECTED_COLUMNS:
            assert col in df.columns, f"Expected column '{col}' not found in parquet"

        # Data integrity
        assert len(df) == len(sample_df), "Row count mismatch"
        assert df.index.is_monotonic_increasing, "Index must be sorted ascending"
        assert (df["close"] > 0).all(), "All close prices must be positive"

    def test_download_merges_with_existing(self, tmp_cache, monkeypatch, sample_df):
        """Subsequent downloads are merged (not overwritten) into existing parquet."""
        import scripts.backfill_intraday_5min as mod

        # Pre-write existing data for April 2026
        existing_rows = pd.DataFrame([{
            "timestamp": pd.Timestamp("2026-04-01 13:30:00", tz="UTC"),
            "open": 700.0, "high": 701.0, "low": 699.0, "close": 700.5, "volume": 10000,
        }]).set_index("timestamp")
        write_parquet("SPY", existing_rows)

        # Fetch returns May 2026 data
        def mock_fetch(ticker, start, end, token, session):
            return sample_df  # 3 bars in May 2026

        monkeypatch.setattr(mod, "fetch_5min_bars", mock_fetch)
        monkeypatch.setattr(mod, "INTER_CALL_DELAY", 0)

        checkpoint = {}
        session = MagicMock()
        backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
        )

        # Both months should be present
        path = tmp_cache / "SPY.parquet"
        df = pd.read_parquet(path)
        assert len(df) == 1 + len(sample_df), (
            f"Expected {1 + len(sample_df)} total rows after merge, got {len(df)}"
        )
        # Should be sorted
        assert df.index.is_monotonic_increasing

    def test_checkpoint_marked_done_after_successful_fetch(
        self, tmp_cache, monkeypatch, sample_df
    ):
        """Checkpoint is written with 'done' for the month after successful fetch."""
        import scripts.backfill_intraday_5min as mod

        monkeypatch.setattr(mod, "fetch_5min_bars", lambda *a, **k: sample_df)
        monkeypatch.setattr(mod, "INTER_CALL_DELAY", 0)

        checkpoint = {}
        session = MagicMock()
        backfill_ticker(
            ticker="SPY",
            start_date="2026-05-12",
            end_date="2026-05-16",
            token="fake-token",
            session=session,
            checkpoint=checkpoint,
        )

        assert checkpoint.get("SPY", {}).get("2026-05") == "done", (
            "Checkpoint must be marked 'done' after successful fetch"
        )
        # Also persisted to disk
        ck_file = tmp_cache / "_checkpoint.json"
        assert ck_file.exists(), "Checkpoint file must be written to disk"
        ck_data = json.loads(ck_file.read_text())
        assert ck_data.get("SPY", {}).get("2026-05") == "done"


# ─────────────────────────────────────────────────────────────────────────────
# Test (d): Helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpers:
    """Unit tests for utility functions."""

    def test_iter_monthly_windows_single_month(self):
        """Single month produces one window."""
        from datetime import datetime
        windows = iter_monthly_windows(
            datetime(2026, 5, 12), datetime(2026, 5, 16)
        )
        assert len(windows) == 1
        key, start, end = windows[0]
        assert key == "2026-05"
        assert start == "2026-05-12"
        assert end == "2026-05-16"

    def test_iter_monthly_windows_spans_multiple_months(self):
        """Multi-month range produces one window per month."""
        from datetime import datetime
        windows = iter_monthly_windows(
            datetime(2026, 3, 15), datetime(2026, 5, 10)
        )
        assert len(windows) == 3
        keys = [w[0] for w in windows]
        assert keys == ["2026-03", "2026-04", "2026-05"]

    def test_iter_monthly_windows_spans_year_boundary(self):
        """Windows crossing Dec→Jan generate correct year-boundary entries."""
        from datetime import datetime
        windows = iter_monthly_windows(
            datetime(2025, 12, 1), datetime(2026, 1, 31)
        )
        assert len(windows) == 2
        assert windows[0][0] == "2025-12"
        assert windows[1][0] == "2026-01"

    def test_merge_bars_deduplication(self):
        """Duplicate timestamps are resolved by keeping the latest (last) value."""
        ts = pd.Timestamp("2026-05-12 13:30:00", tz="UTC")
        df_old = pd.DataFrame([{"timestamp": ts, "open": 700.0, "high": 701.0,
                                 "low": 699.0, "close": 700.5, "volume": 1000}]
                               ).set_index("timestamp")
        df_new = pd.DataFrame([{"timestamp": ts, "open": 736.87, "high": 737.18,
                                 "low": 736.165, "close": 736.335, "volume": 17133}]
                               ).set_index("timestamp")
        merged = merge_bars(df_old, df_new)
        assert len(merged) == 1, "Duplicate timestamp must be deduplicated"
        assert merged.iloc[0]["close"] == 736.335, "Last (newer) value must be kept"

    def test_fetch_5min_bars_empty_response(self):
        """Empty Tiingo response returns empty DataFrame."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        df = fetch_5min_bars("SPY", "2026-05-12", "2026-05-12", "token", mock_session)
        assert df.empty

    def test_fetch_5min_bars_404_returns_empty(self):
        """404 response returns empty DataFrame without raising."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        df = fetch_5min_bars("DELISTED", "2026-05-12", "2026-05-12", "token", mock_session)
        assert df.empty
