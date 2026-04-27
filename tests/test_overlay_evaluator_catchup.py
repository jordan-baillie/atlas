"""tests/test_overlay_evaluator_catchup.py — Tests for the two-pass overlay evaluator.

Covers:
  - Test 1: all 5 unevaluated decisions (ages 1,5,10,30,100d) evaluated when SPY
            data is fully available (mocked _get_spy_returns_after).
  - Test 2: SPY only covers last 7 days → only the 1-day-old gets evaluated;
            older ones skip cleanly (still unevaluated).
  - Test 3: check_evaluator_backlog returns unhealthy when 5 rows >2d old;
            healthy when only 1 such row.
  - Test 4: get_overlay_decisions(unevaluated_only=True) returns rows of any age;
            get_overlay_decisions(days=7) preserves old behaviour (regression guard).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Helpers — seed overlay_decisions rows directly into the isolated DB
# ---------------------------------------------------------------------------

def _ts(days_ago: float) -> str:
    """Return an ISO timestamp *days_ago* calendar days in the past (UTC)."""
    dt = datetime.now(tz=timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _insert_decision(db_path: str, days_ago: float, action: str = "tighten") -> int:
    """Insert a bare unevaluated overlay_decisions row; return its id."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ts = _ts(days_ago)
    cur = conn.execute(
        """
        INSERT INTO overlay_decisions
            (timestamp, regime_state, action, outcome_evaluated)
        VALUES (?, ?, ?, 0)
        """,
        (ts, "test_regime", action),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _count_unevaluated_old(db_path: str) -> int:
    """Count unevaluated decisions older than 2 days."""
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        """
        SELECT COUNT(*) FROM overlay_decisions
        WHERE outcome_evaluated = 0
          AND timestamp < datetime('now', '-2 days')
        """
    ).fetchone()[0]
    conn.close()
    return n


def _count_evaluated(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(
        "SELECT COUNT(*) FROM overlay_decisions WHERE outcome_evaluated = 1"
    ).fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Test 1 — all 5 evaluated when SPY data fully available
# ---------------------------------------------------------------------------

def test_all_evaluated_when_spy_available(tmp_path, monkeypatch):
    """All 5 unevaluated decisions (ages 1,5,10,30,100 days) are evaluated
    when _get_spy_returns_after is mocked to always return a valid return value.
    """
    import db.atlas_db as _adb

    db_path = str(tmp_path / "test_eval.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    _adb.init_db()

    ages = [1, 5, 10, 30, 100]
    for age in ages:
        _insert_decision(db_path, age, action="tighten")

    # Mock SPY data available for every timestamp
    import overlay.evaluator as ev
    monkeypatch.setattr(
        ev, "_get_spy_returns_after", lambda ts, lookahead=3: -0.025
    )

    stats = ev.evaluate_overlay_decisions(days=7)

    assert stats["newly_evaluated"] == 5, (
        f"Expected 5 newly evaluated, got {stats['newly_evaluated']}"
    )
    assert stats["skipped_count"] == 0
    assert _count_evaluated(db_path) == 5
    assert _count_unevaluated_old(db_path) == 0


# ---------------------------------------------------------------------------
# Test 2 — partial SPY coverage: only recent decision evaluated
# ---------------------------------------------------------------------------

def test_partial_spy_coverage_skips_old_decisions(tmp_path, monkeypatch):
    """SPY data only available for decisions within the last 7 days.
    Only the 1-day-old decision gets evaluated; the older ones are skipped
    (still unevaluated, NOT marked with a wrong outcome).
    """
    import db.atlas_db as _adb

    db_path = str(tmp_path / "test_partial.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    _adb.init_db()

    ages = [1, 5, 10, 30, 100]
    for age in ages:
        _insert_decision(db_path, age, action="no_change")

    # SPY only available for decisions made within the last 3 calendar days.
    # This means only the 1-day-old decision gets SPY data;
    # the 5, 10, 30, 100-day-old decisions still lack lookahead.
    def _spy_mock(ts: str, lookahead: int = 3) -> Optional[float]:
        try:
            dt = datetime.fromisoformat(ts)
            # Use timezone-aware comparison
            now_utc = datetime.now(tz=timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if now_utc - dt <= timedelta(days=3):
                return 0.005  # small positive return
            return None  # old decisions have no data yet
        except Exception:
            return None

    import overlay.evaluator as ev
    monkeypatch.setattr(ev, "_get_spy_returns_after", _spy_mock)

    stats = ev.evaluate_overlay_decisions(days=7)

    # Only the 1-day-old decision is within 7 days AND has spy data
    assert stats["newly_evaluated"] == 1, (
        f"Expected 1 newly evaluated, got {stats['newly_evaluated']}"
    )
    # The 4 older decisions were skipped (insufficient lookahead)
    assert stats["skipped_count"] == 4

    # Verify old decisions are still unevaluated (not corrupt)
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT outcome_evaluated FROM overlay_decisions ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()
    evaluated_flags = [r[0] for r in rows]
    # oldest 4 (ages 5,10,30,100 in ascending order) should still be 0
    # newest (age 1) should be 1
    assert evaluated_flags[-1] == 1, "Most recent decision should be evaluated"
    assert all(f == 0 for f in evaluated_flags[:-1]), (
        f"All older decisions should remain unevaluated, got {evaluated_flags[:-1]}"
    )


# ---------------------------------------------------------------------------
# Test 3 — check_evaluator_backlog threshold behaviour
# ---------------------------------------------------------------------------

def test_check_evaluator_backlog_unhealthy_and_healthy(tmp_path, monkeypatch):
    """check_evaluator_backlog(threshold=2) is unhealthy when 5 old rows
    exist; healthy when only 1 old row exists.
    """
    import db.atlas_db as _adb
    from overlay.evaluator import check_evaluator_backlog

    db_path = str(tmp_path / "test_backlog.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    _adb.init_db()

    # Insert 5 decisions older than 2 days
    for age in [3, 5, 8, 15, 30]:
        _insert_decision(db_path, age)

    is_healthy, backlog_count, oldest_age = check_evaluator_backlog(threshold=2)
    assert not is_healthy, "Should be unhealthy with 5 stale rows and threshold=2"
    assert backlog_count == 5
    assert oldest_age >= 29.0, f"Oldest should be ~30d, got {oldest_age}"

    # Now evaluate 4 of them (simulate them being processed)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE overlay_decisions SET outcome_evaluated = 1
        WHERE id IN (
            SELECT id FROM overlay_decisions
            WHERE outcome_evaluated = 0
            ORDER BY timestamp ASC
            LIMIT 4
        )
        """
    )
    conn.commit()
    conn.close()

    is_healthy2, backlog_count2, _ = check_evaluator_backlog(threshold=2)
    assert is_healthy2, "Should be healthy with 1 stale row and threshold=2"
    assert backlog_count2 == 1


# ---------------------------------------------------------------------------
# Test 4 — get_overlay_decisions regression guard
# ---------------------------------------------------------------------------

def test_get_overlay_decisions_unevaluated_only_ignores_days_filter(tmp_path, monkeypatch):
    """get_overlay_decisions(unevaluated_only=True) returns rows from any age;
    get_overlay_decisions(days=7) only returns rows from the last 7 days.
    """
    import db.atlas_db as _adb
    from db.atlas_db import get_overlay_decisions

    db_path = str(tmp_path / "test_query.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    _adb.init_db()

    # Insert rows at various ages: 1, 5, 30 days old
    _insert_decision(db_path, 1,  action="tighten")   # within 7d
    _insert_decision(db_path, 5,  action="no_change")  # within 7d
    _insert_decision(db_path, 30, action="tighten")    # outside 7d

    # unevaluated_only=True should return all 3 regardless of age
    all_uneval = get_overlay_decisions(unevaluated_only=True)
    assert len(all_uneval) == 3, (
        f"unevaluated_only=True should return 3 rows, got {len(all_uneval)}"
    )
    assert all(r["outcome_evaluated"] == 0 for r in all_uneval)

    # days=7 should only return the 2 recent rows (age 1d and 5d)
    recent = get_overlay_decisions(days=7)
    assert len(recent) == 2, (
        f"days=7 should return 2 rows, got {len(recent)}"
    )

    # days=None (default) should return all 3
    all_rows = get_overlay_decisions()
    assert len(all_rows) == 3, (
        f"No filter should return 3 rows, got {len(all_rows)}"
    )


def test_get_overlay_decisions_evaluated_rows_excluded_from_unevaluated_only(
    tmp_path, monkeypatch
):
    """Evaluated rows are not returned when unevaluated_only=True."""
    import db.atlas_db as _adb
    from db.atlas_db import get_overlay_decisions

    db_path = str(tmp_path / "test_excl.db")
    monkeypatch.setattr(_adb, "_db_path_override", db_path)
    _adb.init_db()

    rid = _insert_decision(db_path, 10, action="tighten")
    # Mark it evaluated
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE overlay_decisions SET outcome_evaluated=1 WHERE id=?", (rid,)
    )
    conn.commit()
    conn.close()

    # Insert one unevaluated row
    _insert_decision(db_path, 20, action="no_change")

    uneval = get_overlay_decisions(unevaluated_only=True)
    assert len(uneval) == 1
    assert uneval[0]["outcome_evaluated"] == 0
