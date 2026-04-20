"""
Regression guard: ensures tests/ never write to production data/atlas.db.

Runs the two historically-leaking test modules in a subprocess and asserts
that atlas.db mtime + size are unchanged afterwards. Also verifies no
dummy-pattern rows (open=0, volume=1000) linger in the ohlcv table.
"""

import os
import sys
import sqlite3
import subprocess
import pytest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
PROD_DB = PROJECT / "data" / "atlas.db"

if not PROD_DB.exists():
    pytest.skip(
        f"Production DB not found at {PROD_DB} — skipping isolation guards",
        allow_module_level=True,
    )


def test_auto_exclusions_does_not_write_prod_db() -> None:
    """Running test_auto_exclusions.py must not alter atlas.db."""
    before = os.stat(PROD_DB)
    before_mtime = before.st_mtime_ns
    before_size = before.st_size

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_auto_exclusions.py",
            "--timeout=30",
            "-q",
            "--no-header",
        ],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"tests/test_auto_exclusions.py failed (returncode={result.returncode}).\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )

    after = os.stat(PROD_DB)
    assert after.st_mtime_ns == before_mtime and after.st_size == before_size, (
        f"tests/test_auto_exclusions.py wrote to {PROD_DB}!\n"
        f"  mtime_ns before={before_mtime}, after={after.st_mtime_ns}\n"
        f"  st_size  before={before_size}, after={after.st_size}\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )


def test_ingest_does_not_write_prod_db() -> None:
    """Running test_ingest.py must not alter atlas.db."""
    before = os.stat(PROD_DB)
    before_mtime = before.st_mtime_ns
    before_size = before.st_size

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_ingest.py",
            "--timeout=30",
            "-q",
            "--no-header",
        ],
        cwd=str(PROJECT),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        f"tests/test_ingest.py failed (returncode={result.returncode}).\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )

    after = os.stat(PROD_DB)
    assert after.st_mtime_ns == before_mtime and after.st_size == before_size, (
        f"tests/test_ingest.py wrote to {PROD_DB}!\n"
        f"  mtime_ns before={before_mtime}, after={after.st_mtime_ns}\n"
        f"  st_size  before={before_size}, after={after.st_size}\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )


def test_no_dummy_ohlcv_rows_in_prod() -> None:
    """Assert zero rows that match the dummy-data pattern used in leaking tests."""
    conn = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM ohlcv
            WHERE open = 0.0
              AND high = 0.0
              AND low  = 0.0
              AND volume = 1000
              AND source   = 'yfinance'
              AND universe = 'sp500'
              AND ticker IN ('AAPL', 'MSFT')
              AND date >= '2026-04-01'
            """
        )
        count = cur.fetchone()[0]
    finally:
        conn.close()

    assert count == 0, (
        f"Found {count} dummy-pattern ohlcv row(s) in production {PROD_DB}. "
        "The DB-isolation fixture in the leaking test is missing or broken."
    )
