"""
Tests for overlay/sources/chart_renders.py
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SPY_PARQUET = Path("/root/atlas/data/cache/sector_etfs/SPY.parquet")


def _spy_available() -> bool:
    return SPY_PARQUET.exists()


# ---------------------------------------------------------------------------
# Test 1: render_daily_1y produces a wide PNG
# ---------------------------------------------------------------------------


def test_render_daily_1y_produces_wide_png(tmp_path):
    """render_daily_1y(SPY) → PNG width ≥ 2400 px."""
    if not _spy_available():
        pytest.skip("SPY parquet not found — skipping render test")

    from overlay.sources.chart_renders import render_daily_1y

    out = tmp_path / "SPY_daily_1y.png"
    result = render_daily_1y("SPY", out)

    assert result.exists(), f"Output file not created: {result}"
    assert result.stat().st_size > 0, "Output file is empty"

    try:
        from PIL import Image
        width, height = Image.open(result).size
        assert width >= 2400, f"PNG width {width} < 2400 px"
    except ImportError:
        pytest.skip("Pillow not installed — skipping size check")


# ---------------------------------------------------------------------------
# Test 2: render_reference_set handles a missing parquet gracefully
# ---------------------------------------------------------------------------


def test_render_reference_set_handles_missing_parquet(tmp_path):
    """Bogus ticker must be omitted from result without raising an exception."""
    from overlay.sources.chart_renders import render_reference_set

    bogus = "XXXXXXXX_DOES_NOT_EXIST"
    # Pass max_images=0 for indices so we only test position-ticker handling;
    # but actually let's just use a small max_images and focus on bogus omission.
    result = render_reference_set(
        positions=[bogus],
        out_dir=tmp_path,
        max_images=20,
    )

    for key in result:
        assert bogus not in key, f"Bogus ticker appeared in result: {key}"


# ---------------------------------------------------------------------------
# Test 3: render_reference_set caches within 4 h (no re-render during market)
# ---------------------------------------------------------------------------


def test_render_reference_set_caches_within_4h(tmp_path):
    """
    Second call must return the same Path with unchanged mtime
    when the cache-valid check returns True.

    Strategy: monkeypatch _cache_valid to always return True after the first
    render, then check mtime is stable.
    """
    if not _spy_available():
        pytest.skip("SPY parquet not found — skipping cache test")

    import overlay.sources.chart_renders as cr

    # First call — actually render
    result1 = cr.render_reference_set(
        positions=[],
        out_dir=tmp_path,
        max_images=1,  # only SPY daily_1y
    )

    if not result1:
        pytest.skip("No images rendered (all tickers missing) — skipping")

    # Record mtime of all produced files
    mtimes_before = {k: v.stat().st_mtime for k, v in result1.items()}

    # Small sleep to make any re-write detectable
    time.sleep(0.05)

    # Second call — with _cache_valid monkeypatched to always True
    with patch.object(cr, "_cache_valid", return_value=True):
        result2 = cr.render_reference_set(
            positions=[],
            out_dir=tmp_path,
            max_images=1,
        )

    assert set(result1.keys()) == set(result2.keys()), "Key sets differ between calls"

    for key, path2 in result2.items():
        mtime_before = mtimes_before[key]
        mtime_after = path2.stat().st_mtime
        assert mtime_after == mtime_before, (
            f"{key}: file was re-written during cache hit "
            f"(mtime changed from {mtime_before} to {mtime_after})"
        )
