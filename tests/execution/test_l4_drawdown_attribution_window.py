"""tests/test_l4_drawdown_attribution_window.py — Task #314

Verifies that check_l4_drawdown() floors its lookback window at
ATTRIBUTION_CUTOVER_DATE ("2026-04-29"), preventing pre-refactor
global-broker equity rows from producing phantom drawdowns.

Background:
    Prior to 2026-04-29, equity_history rows for market_id='sp500' held the
    GLOBAL broker equity (~$5,300). After the per-market attribution refactor
    (cutover) they hold per-market sp500 equity (~$1,300).  Including both
    in a max() gives a phantom peak of $5,429 against a real current of
    $1,360, producing a 74% drawdown that falsely fires L4. (#314)

Tests:
    1. test_l4_skips_pre_cutover_global_equity     — pre-cutover row excluded
    2. test_l4_fires_on_real_post_cutover_drawdown — real >5% DD fires L4
    3. test_l4_no_block_when_no_post_cutover_rows  — fail-open on empty window
    4. test_l4_cutover_date_is_module_constant     — source-inspection guard
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import atlas.execution.kill_switch as ks


# ---------------------------------------------------------------------------
# Helper: create a minimal equity_history DB
# ---------------------------------------------------------------------------

def _make_eq_db(tmp_path: Path, rows: list) -> str:
    """Return path to a SQLite DB with equity_history table populated from rows.

    rows: list of (market_id, date, equity, pnl) tuples.
    """
    db_path = tmp_path / "eq_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """CREATE TABLE equity_history (
               market_id  TEXT NOT NULL,
               date       TEXT NOT NULL,
               equity     REAL NOT NULL,
               pnl        REAL,
               PRIMARY KEY (market_id, date)
           )"""
    )
    if rows:
        conn.executemany(
            "INSERT INTO equity_history (market_id, date, equity, pnl) VALUES (?,?,?,?)",
            rows,
        )
    conn.commit()
    conn.close()
    return str(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestL4AttributionWindow:
    """Task #314 — L4 lookback floor at ATTRIBUTION_CUTOVER_DATE."""

    def test_l4_skips_pre_cutover_global_equity(self, tmp_path: Path) -> None:
        """Test 1: pre-cutover $5429 row must NOT become the peak.

        Without the fix: peak=$5429, latest=$1360 => DD=74.94% => false L4 fire.
        With the fix: rows before 2026-04-29 are excluded, so only the
        post-cutover rows contribute.  Peak=$1360 (the only max), latest=$1360
        => DD=0% => None.

        Row set mirrors the actual production data described in the bug report.
        """
        rows = [
            ("sp500", "2026-04-15", 5429.0, None),   # pre-cutover global equity
            ("sp500", "2026-04-29", 1223.77, None),  # first post-cutover row (cutover day)
            ("sp500", "2026-05-08", 1360.74, None),  # recent post-cutover row
        ]
        db = _make_eq_db(tmp_path, rows)
        result = ks.check_l4_drawdown(db_path=db, window_days=30)
        assert result is None, (
            f"Expected None (no false L4 fire) but got: {result}. "
            "The pre-cutover $5429 global-equity row must be excluded by "
            f"ATTRIBUTION_CUTOVER_DATE={ks.ATTRIBUTION_CUTOVER_DATE!r}."
        )

    def test_l4_fires_on_real_post_cutover_drawdown(self, tmp_path: Path) -> None:
        """Test 2: a genuine >5% drawdown within the post-cutover window fires L4.

        Equity path: $1500 => $1490 => $1300.
        Peak=$1500, latest=$1300 => drawdown=(1500-1300)/1500*100=13.33% > 5%.
        All rows are post-cutover so none are suppressed.

        Dates are computed relative to today: the original hardcoded May-2026
        dates aged out of the 30-day lookback window and the test went stale.
        """
        from datetime import datetime, timedelta, timezone
        _d = lambda days_ago: (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        rows = [
            ("sp500", _d(9), 1500.0, None),
            ("sp500", _d(8), 1490.0, None),
            ("sp500", _d(1), 1300.0, None),
        ]
        db = _make_eq_db(tmp_path, rows)
        result = ks.check_l4_drawdown(db_path=db, window_days=30)
        assert result is not None, (
            "Expected L4 BlockReason for 13.3% drawdown but got None. "
            "Real post-cutover drawdowns must still trigger L4."
        )
        assert result.layer == "L4"
        assert result.detail["peak_equity"] == pytest.approx(1500.0)
        assert result.detail["latest_equity"] == pytest.approx(1300.0)
        assert result.detail["drawdown_pct"] == pytest.approx(13.333, abs=0.01)

    def test_l4_no_block_when_no_post_cutover_rows(self, tmp_path: Path) -> None:
        """Test 3: only pre-cutover rows => empty window after filter => fail-open (None).

        If the DB only contains rows before 2026-04-29, the cutover floor
        eliminates all of them.  The function must return None gracefully
        (fail-open) rather than crash or fire a false positive.
        """
        rows = [
            ("sp500", "2026-04-15", 5429.0, None),
            ("sp500", "2026-04-20", 5380.0, None),
            ("sp500", "2026-04-24", 5350.0, None),
        ]
        db = _make_eq_db(tmp_path, rows)
        result = ks.check_l4_drawdown(db_path=db, window_days=30)
        assert result is None, (
            f"Expected None (fail-open, no post-cutover rows) but got: {result}."
        )

    def test_l4_cutover_date_is_module_constant(self) -> None:
        """Test 4: source-inspection guard — constant must stay "2026-04-29".

        This prevents an accidental change that would re-introduce the phantom
        drawdown bug or suppress future real drawdowns.
        """
        assert ks.ATTRIBUTION_CUTOVER_DATE == "2026-04-29", (
            f"ATTRIBUTION_CUTOVER_DATE changed to {ks.ATTRIBUTION_CUTOVER_DATE!r}. "
            "This constant is load-bearing for L4 correctness (#314). "
            "Update this test AND the docstring if you intentionally change it."
        )
