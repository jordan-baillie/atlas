"""
tests/test_audit_f11_ohlcv_freshness.py
========================================
Regression test for audit finding F-11: stale OHLCV tickers.

Verifies that zero non-excluded tickers have OHLCV data older than 7 days.

Auto-exclusions are stored in config/auto_excluded_tickers.json (managed by
data/auto_exclusions.py).  Tickers in that file are intentionally excluded from
daily refresh (passive universe, config exclusions, or refresh failure) and are
not counted as stale for the purposes of this audit check.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "atlas.db"
AUTO_EXCLUSION_FILE = PROJECT_ROOT / "config" / "auto_excluded_tickers.json"


def _auto_excluded() -> set[str]:
    """Load the set of auto-excluded tickers from config/auto_excluded_tickers.json.

    Also supports the legacy data/auto_exclusions.csv and data/auto_exclusions.json
    paths (spec compatibility).
    """
    # Primary store (managed by data/auto_exclusions.py)
    if AUTO_EXCLUSION_FILE.exists():
        data = json.loads(AUTO_EXCLUSION_FILE.read_text())
        return {t.upper() for t in data.get("excluded", {}).keys()}

    # Fallback: data/auto_exclusions.json (flat list or dict)
    legacy_json = PROJECT_ROOT / "data" / "auto_exclusions.json"
    if legacy_json.exists():
        data = json.loads(legacy_json.read_text())
        if isinstance(data, list):
            return {e.get("ticker", "").upper() for e in data if e.get("ticker")}
        return {k.upper() for k in data.keys()} if isinstance(data, dict) else set()

    # Fallback: data/auto_exclusions.csv
    import csv
    legacy_csv = PROJECT_ROOT / "data" / "auto_exclusions.csv"
    if legacy_csv.exists():
        with open(legacy_csv) as f:
            return {row["ticker"].upper() for row in csv.DictReader(f) if "ticker" in row}

    return set()


def test_no_stale_non_excluded_tickers() -> None:
    """F-11: zero non-excluded tickers stale >7 days.

    Tickers in auto_exclusions are acceptable misses (passive universe, config
    exclusions, or confirmed refresh failures).
    """
    excluded = _auto_excluded()
    assert DB_PATH.exists(), f"Database not found at {DB_PATH}"

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    rows = conn.execute(
        """
        SELECT ticker, MAX(date) AS last_date
        FROM ohlcv
        GROUP BY ticker
        HAVING julianday('now') - julianday(MAX(date)) > 7
        """
    ).fetchall()
    conn.close()

    stale_not_excluded = [t for (t, _d) in rows if t.upper() not in excluded]

    assert not stale_not_excluded, (
        f"Stale tickers not in auto_exclusions ({len(stale_not_excluded)}): "
        f"{stale_not_excluded}"
    )


def test_auto_exclusions_file_exists() -> None:
    """config/auto_excluded_tickers.json must exist and be valid JSON."""
    assert AUTO_EXCLUSION_FILE.exists(), (
        f"Auto-exclusion file missing: {AUTO_EXCLUSION_FILE}"
    )
    data = json.loads(AUTO_EXCLUSION_FILE.read_text())
    assert "excluded" in data, "auto_excluded_tickers.json missing 'excluded' key"
    assert isinstance(data["excluded"], dict), "'excluded' must be a dict"


def test_known_passive_tickers_in_exclusions() -> None:
    """F-11 fixture: the 7 ASX passive tickers must be in auto_exclusions."""
    excluded = _auto_excluded()
    known_passive = {
        "ADH.AX", "AGL.AX", "ANZ.AX", "ORA.AX", "PDN.AX", "REH.AX", "IOZ.AX"
    }
    missing = known_passive - excluded
    assert not missing, (
        f"Known ASX passive tickers not in auto_exclusions: {missing}"
    )


def test_mmc_in_exclusions() -> None:
    """F-11 fixture: MMC must be excluded (in sp500 config exclusions list)."""
    excluded = _auto_excluded()
    assert "MMC" in excluded, "MMC should be in auto_exclusions (excluded from sp500 config)"


def test_refreshed_tickers_not_stale() -> None:
    """F-11 fixture: IWM, QQQ, CIBR must be fresh (<= 7 days) after refresh."""
    assert DB_PATH.exists(), f"Database not found at {DB_PATH}"

    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """
        SELECT ticker, MAX(date) AS last_date,
               ROUND(julianday('now') - julianday(MAX(date)), 1) AS days_stale
        FROM ohlcv
        WHERE ticker IN ('IWM', 'QQQ', 'CIBR')
        GROUP BY ticker
        """
    ).fetchall()
    conn.close()

    result = {r[0]: (r[1], r[2]) for r in rows}
    stale = {t: d for t, (ld, d) in result.items() if d > 7}

    assert not stale, (
        f"Expected IWM/QQQ/CIBR to be refreshed (<= 7 days stale). "
        f"Still stale: {stale}"
    )
