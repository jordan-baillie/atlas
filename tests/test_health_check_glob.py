"""Tests for the rglob fix in scripts/health_check.py::load_data_recent().

Covers the 3 acceptance criteria from the C6 spec.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from scripts.health_check import load_data_recent


# ── Helper — write a minimal parquet with 90 days of fake OHLCV ─────────────

def _write_fake_parquet(path: Path, days: int = 90) -> None:
    """Write a parquet file with `days` rows of fake OHLCV data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    idx = pd.date_range(
        end=pd.Timestamp.now(),
        periods=days,
        freq="D",
    )
    df = pd.DataFrame(
        {
            "open": np.full(days, 100.0),
            "high": np.full(days, 101.0),
            "low": np.full(days, 99.0),
            "close": np.full(days, 100.5),
            "volume": np.full(days, 1_000_000),
        },
        index=idx,
    )
    df.index.name = "date"
    df.to_parquet(path)


# ── Test 1: finds files in subdirectories ────────────────────────────────────

def test_finds_files_in_subdirs(tmp_path):
    """rglob finds parquet files nested in universe subdirs."""
    cache = tmp_path / "cache"
    _write_fake_parquet(cache / "sp500" / "AAPL.parquet")
    _write_fake_parquet(cache / "asx" / "CBA_AX.parquet")

    with patch("scripts.health_check.DATA_DIR", cache):
        result = load_data_recent(months=18)

    assert "AAPL" in result, f"Expected AAPL in result; got {list(result.keys())}"
    assert "CBA.AX" in result, f"Expected CBA.AX in result; got {list(result.keys())}"


# ── Test 2: missing universe dir returns empty dict gracefully ───────────────

def test_handles_missing_universe_dir_gracefully(tmp_path):
    """load_data_recent(universe='xyz') on non-existent dir → empty dict, no exception."""
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)

    with patch("scripts.health_check.DATA_DIR", cache):
        result = load_data_recent(universe="xyz")

    assert result == {}, f"Expected empty dict, got {result!r}"


# ── Test 3: per-universe filtering ───────────────────────────────────────────

def test_per_universe_filtering(tmp_path):
    """load_data_recent(universe='sp500') returns only sp500 tickers."""
    cache = tmp_path / "cache"
    _write_fake_parquet(cache / "sp500" / "AAPL.parquet")
    _write_fake_parquet(cache / "asx" / "CBA_AX.parquet")

    with patch("scripts.health_check.DATA_DIR", cache):
        result = load_data_recent(universe="sp500")

    assert "AAPL" in result, f"Expected AAPL in result; got {list(result.keys())}"
    assert "CBA.AX" not in result, f"CBA.AX should NOT be in sp500 result"
