"""
Tests that _save_cache() creates the per-market subdirectory if it is missing.

Covers Fix 5: mkdir parent before cache .tmp write to handle new universe subdirs.
"""
import pandas as pd
import pytest


def test_save_cache_creates_missing_parent_dir(tmp_path, monkeypatch):
    """Cache write should create the per-market subdirectory if it doesn't exist."""
    from data import ingest

    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)

    # Minimal OHLCV DataFrame matching _save_cache expectations
    df = pd.DataFrame(
        {
            "open": [1.0],
            "high": [1.0],
            "low": [1.0],
            "close": [1.0],
            "volume": [100],
        },
        index=pd.DatetimeIndex(["2026-05-04"], name="date"),
    )

    # Pre-condition: the per-market subdir must NOT exist yet
    new_market_dir = tmp_path / "newuniverse"
    assert not new_market_dir.exists(), "subdir should be absent before _save_cache"

    # Should NOT raise even though the directory is missing
    ingest._save_cache("NEWTKR", df, market_id="newuniverse")

    # Post-condition: parquet file now exists inside the auto-created subdir
    assert new_market_dir.exists(), "subdir was not created by _save_cache"
    assert (new_market_dir / "NEWTKR.parquet").exists(), (
        "parquet file not written to new_market_dir / NEWTKR.parquet"
    )


def test_save_cache_creates_dir_for_commodity_etfs(tmp_path, monkeypatch):
    """Regression: exact market_id that triggered the original [Errno 2]."""
    from data import ingest

    monkeypatch.setattr(ingest, "CACHE_DIR", tmp_path)

    df = pd.DataFrame(
        {
            "open": [2.0],
            "high": [2.5],
            "low": [1.5],
            "close": [2.0],
            "volume": [200],
        },
        index=pd.DatetimeIndex(["2026-05-04"], name="date"),
    )

    market_dir = tmp_path / "commodity_etfs"
    assert not market_dir.exists()

    ingest._save_cache("DBC", df, market_id="commodity_etfs")

    assert (market_dir / "DBC.parquet").exists(), (
        "parquet file not created for commodity_etfs/DBC"
    )
