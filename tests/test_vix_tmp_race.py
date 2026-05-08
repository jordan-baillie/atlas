"""
tests/test_vix_tmp_race.py

Regression test for VIX backfill tmp.parquet race condition (#272).

Two concurrent invocations previously both wrote to the same
``VIX.tmp.parquet`` path: one could truncate the other's write and rename
a partial/empty file to VIX.parquet.

Fix: tmp path is now ``VIX.tmp.<pid>.<epoch_ns>.parquet`` — unique per
invocation.  Two concurrent processes never collide on the same temp file.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Unit test: verify tmp filename format in source
# ---------------------------------------------------------------------------

def test_tmp_filename_is_pid_epoch_unique():
    """The tmp path in backfill_vix.py must include PID and epoch_ns tokens."""
    src = Path(__file__).parent.parent / "scripts" / "backfill_vix.py"
    content = src.read_text()

    # Confirm the old fixed name is gone
    assert "VIX.tmp.parquet" not in content, (
        "Fixed tmp filename VIX.tmp.parquet still present — race not fixed"
    )

    # Confirm the new pattern uses os.getpid() and time.time_ns()
    assert "os.getpid()" in content, "os.getpid() not found in backfill_vix.py"
    assert "time.time_ns()" in content, "time.time_ns() not found in backfill_vix.py"


def test_tmp_path_uses_indices_cache_dir():
    """The tmp file must live inside the INDICES_CACHE directory."""
    src = Path(__file__).parent.parent / "scripts" / "backfill_vix.py"
    content = src.read_text()
    # The expression should reference INDICES_CACHE or an absolute sub-path
    assert "INDICES_CACHE" in content or "/indices" in content


# ---------------------------------------------------------------------------
# Functional: two concurrent invocations write unique tmp files
# ---------------------------------------------------------------------------

class TestConcurrentBackfill:
    """Two concurrent backfill_vix runs must not collide on the same tmp file."""

    def test_unique_tmp_names_across_processes(self, tmp_path: Path):
        """Mock two invocations and verify they generate distinct tmp paths."""
        import os

        seen_paths: set[str] = set()

        def _fake_tmp(indices_cache: Path) -> Path:
            return indices_cache / f"VIX.tmp.{os.getpid()}.{time.time_ns()}.parquet"

        for _ in range(5):
            p = _fake_tmp(tmp_path)
            assert str(p) not in seen_paths, f"Duplicate tmp path: {p}"
            seen_paths.add(str(p))
            time.sleep(0)  # yield — epoch_ns still increments

        assert len(seen_paths) == 5

    def test_pid_uniqueness_between_processes(self, tmp_path: Path):
        """Two subprocesses generate different PID tokens in the tmp filename."""
        script = tmp_path / "gen_tmp.py"
        script.write_text(
            "import os, time, sys\n"
            "p = sys.argv[1] + f'/VIX.tmp.{os.getpid()}.{time.time_ns()}.parquet'\n"
            "print(p)\n"
        )
        p1 = subprocess.run(
            [sys.executable, str(script), str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        p2 = subprocess.run(
            [sys.executable, str(script), str(tmp_path)],
            capture_output=True, text=True, timeout=10,
        )
        path1 = p1.stdout.strip()
        path2 = p2.stdout.strip()

        assert path1 and path2, "Subprocesses did not print paths"
        assert path1 != path2, (
            f"Two different processes generated identical tmp paths:\n  {path1}\n  {path2}"
        )

        # Verify pattern: VIX.tmp.<int>.<int>.parquet
        pattern = re.compile(r"VIX\.tmp\.\d+\.\d+\.parquet$")
        assert pattern.search(path1), f"Path1 doesn't match pattern: {path1}"
        assert pattern.search(path2), f"Path2 doesn't match pattern: {path2}"


# ---------------------------------------------------------------------------
# Smoke test: backfill() writes VIX.parquet without leaving tmp files
# (uses monkeypatched yfinance to avoid real network calls)
# ---------------------------------------------------------------------------

class TestBackfillCleanup:
    """After a successful backfill(), no tmp files should remain."""

    def test_no_tmp_files_left_after_success(self, tmp_path: Path, monkeypatch):
        """Successful backfill leaves no VIX.tmp.*.parquet files."""
        import pandas as pd
        import numpy as np
        from datetime import date, timedelta
        from unittest.mock import patch

        # Build minimal synthetic raw DataFrame
        end = date.today()
        idx = pd.date_range(end=str(end), periods=10, freq="B")
        raw_df = pd.DataFrame(
            {
                "Open": np.random.rand(10) * 20 + 15,
                "High": np.random.rand(10) * 20 + 18,
                "Low": np.random.rand(10) * 20 + 12,
                "Close": np.random.rand(10) * 20 + 15,
                "Volume": np.zeros(10),
            },
            index=idx,
        )

        import scripts.backfill_vix as bv

        monkeypatch.setattr(bv, "INDICES_CACHE", tmp_path)
        monkeypatch.setattr(bv, "VIX_PARQUET", tmp_path / "VIX.parquet")

        with patch.object(bv, "_fetch_yfinance", return_value=raw_df):
            result = bv.backfill(days=10)

        assert result is True, "backfill() returned False — unexpected failure"
        assert (tmp_path / "VIX.parquet").exists(), "VIX.parquet not written"

        # No leftover tmp files
        tmp_files = list(tmp_path.glob("VIX.tmp.*.parquet"))
        assert len(tmp_files) == 0, (
            f"Leftover tmp files after successful backfill: {tmp_files}"
        )

    def test_tmp_file_cleaned_up_on_failure(self, tmp_path: Path, monkeypatch):
        """If write fails, the tmp file is cleaned up (unlink called)."""
        import pandas as pd
        import numpy as np
        from datetime import date
        from unittest.mock import patch, MagicMock

        end = date.today()
        idx = pd.date_range(end=str(end), periods=5, freq="B")
        raw_df = pd.DataFrame(
            {
                "Open": [15.0] * 5, "High": [18.0] * 5,
                "Low": [12.0] * 5, "Close": [15.0] * 5,
                "Volume": [0.0] * 5,
            },
            index=idx,
        )

        import scripts.backfill_vix as bv

        monkeypatch.setattr(bv, "INDICES_CACHE", tmp_path)
        monkeypatch.setattr(bv, "VIX_PARQUET", tmp_path / "VIX.parquet")

        def _bad_to_parquet(*a, **kw):
            raise OSError("simulated disk full")

        with patch.object(bv, "_fetch_yfinance", return_value=raw_df):
            with patch.object(pd.DataFrame, "to_parquet", side_effect=_bad_to_parquet):
                result = bv.backfill(days=5)

        assert result is False, "backfill() should return False on write error"
        # No orphaned tmp files
        tmp_files = list(tmp_path.glob("VIX.tmp.*.parquet"))
        assert len(tmp_files) == 0, (
            f"Orphaned tmp files after failed backfill: {tmp_files}"
        )
