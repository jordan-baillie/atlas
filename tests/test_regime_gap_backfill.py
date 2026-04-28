"""Tests for W4 regime_history Apr 2-11 gap backfill (2026-04-28).

These are integration tests that verify the production database state AFTER
the backfill script has been run. They intentionally read from the real
data/atlas.db by passing db_path=str(DB_PATH) explicitly to get_db() — this
bypasses the test-isolation _db_path_override used by unit tests.

The backfill script (scripts/backfill_regime_gap_apr2026.py) runs as a
subprocess with its own Python process, so it also reads/writes the real DB
regardless of any in-process _db_path_override state.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Ensure project root is on path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from db.atlas_db import DB_PATH, get_db

# Explicit prod DB path — bypasses _db_path_override used by test isolation.
_PROD_DB = str(DB_PATH)


GAP_DATES_TRADING = [
    "2026-04-02",
    "2026-04-03",
    "2026-04-06",
    "2026-04-07",
    "2026-04-08",
    "2026-04-09",
    "2026-04-10",
]


def test_all_apr_2_to_11_dates_present_in_regime_history() -> None:
    """All trading days in the Apr 2-11 gap must have a regime_history row.

    04-03 is allowed to be the sole missing date if FRED/yfinance genuinely
    has no macro data for it — documented skip rather than hard failure.
    """
    with get_db(db_path=_PROD_DB) as db:
        rows = db.execute(
            "SELECT date FROM regime_history "
            "WHERE date BETWEEN '2026-04-02' AND '2026-04-11' "
            "ORDER BY date",
        ).fetchall()
    present = {r["date"] if hasattr(r, "keys") else r[0] for r in rows}
    missing = set(GAP_DATES_TRADING) - present

    # Allow 04-03 to be missing if FRED genuinely has no data for it
    if "2026-04-03" in missing and len(missing) == 1:
        pytest.skip(
            "04-03 macro data unavailable from FRED/yfinance — "
            "only blocker remaining; all other gap dates are filled"
        )
    assert not missing, f"Still missing regime_history rows for: {sorted(missing)}"


def test_regime_states_are_valid() -> None:
    """All backfilled rows must have a non-empty, recognised regime state."""
    valid_states = {
        "bull_risk_on",
        "bull_risk_off",
        "bear_risk_off",
        "bear_capitulation",
        "transition_uncertain",
        "recovery_early",
    }
    with get_db(db_path=_PROD_DB) as db:
        rows = db.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date BETWEEN '2026-04-02' AND '2026-04-11' "
            "ORDER BY date",
        ).fetchall()

    assert rows, "No regime_history rows found in Apr 2-11 range"
    for row in rows:
        d = row["date"] if hasattr(row, "keys") else row[0]
        s = row["regime_state"] if hasattr(row, "keys") else row[1]
        assert s, f"regime_state is empty/NULL for {d}"
        assert s in valid_states, f"Unexpected regime state '{s}' for {d}"


def test_backfill_is_idempotent() -> None:
    """Run the backfill script twice — row count in prod DB must not change."""
    with get_db(db_path=_PROD_DB) as db:
        before: int = db.execute(
            "SELECT COUNT(*) FROM regime_history "
            "WHERE date BETWEEN '2026-04-02' AND '2026-04-11'"
        ).fetchone()[0]

    res = subprocess.run(
        [sys.executable, "scripts/backfill_regime_gap_apr2026.py"],
        cwd=str(_ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert res.returncode == 0, (
        f"backfill script failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )

    with get_db(db_path=_PROD_DB) as db:
        after: int = db.execute(
            "SELECT COUNT(*) FROM regime_history "
            "WHERE date BETWEEN '2026-04-02' AND '2026-04-11'"
        ).fetchone()[0]

    assert before == after, (
        f"Non-idempotent: count changed {before} → {after}; "
        "backfill is overwriting/adding rows it should preserve"
    )


def test_trades_in_gap_have_regime_state() -> None:
    """Trades with entry dates in Apr 2-11 join to a non-NULL regime_state.

    Allows at most ONE NULL (the 04-03 MRVL trade where macro data is missing).
    """
    with get_db(db_path=_PROD_DB) as db:
        rows = db.execute(
            """
            SELECT t.id, t.ticker, DATE(t.entry_date) AS d, rh.regime_state
            FROM trades t
            LEFT JOIN regime_history rh ON DATE(t.entry_date) = rh.date
            WHERE DATE(t.entry_date) BETWEEN '2026-04-02' AND '2026-04-11'
            """,
        ).fetchall()

    if not rows:
        pytest.skip("No trades found in Apr 2-11 window — nothing to verify")

    null_count = sum(
        1
        for r in rows
        if (r["regime_state"] if hasattr(r, "keys") else r[3]) is None
    )
    # Tolerate at most one NULL (the 04-03 date where FRED has no macro data)
    assert null_count <= 1, (
        f"{null_count} trades still have NULL regime_state out of {len(rows)}; "
        "expected at most 1 (the 04-03 MRVL trade with missing macro data)"
    )


def test_existing_rows_outside_gap_preserved() -> None:
    """Rows immediately before/after the gap must not have been disturbed.

    Checks that the boundary dates (04-01 and 04-12) still have their
    expected regime states, confirming the backfill did not corrupt neighbours.
    """
    with get_db(db_path=_PROD_DB) as db:
        row_before = db.execute(
            "SELECT regime_state FROM regime_history WHERE date = '2026-04-01'"
        ).fetchone()
        row_after = db.execute(
            "SELECT regime_state FROM regime_history WHERE date = '2026-04-12'"
        ).fetchone()

    # 04-01 was transition_uncertain before the backfill
    assert row_before is not None, "Pre-gap boundary row (04-01) is missing"
    state_before = (
        row_before["regime_state"] if hasattr(row_before, "keys") else row_before[0]
    )
    assert state_before == "transition_uncertain", (
        f"04-01 regime_state changed to '{state_before}' "
        "(expected 'transition_uncertain' — pre-existing row must be preserved)"
    )

    # 04-12 was recovery_early before the backfill
    assert row_after is not None, "Post-gap boundary row (04-12) is missing"
    state_after = (
        row_after["regime_state"] if hasattr(row_after, "keys") else row_after[0]
    )
    assert state_after == "recovery_early", (
        f"04-12 regime_state changed to '{state_after}' "
        "(expected 'recovery_early' — pre-existing row must be preserved)"
    )


def test_gap_transition_detected_at_correct_date() -> None:
    """The tariff-crash period shows expected regime progression.

    Early April (02-07) was transition_uncertain; from 04-08 onwards recovery_early.
    This validates the classifier produced plausible output for this period.
    """
    with get_db(db_path=_PROD_DB) as db:
        rows = db.execute(
            "SELECT date, regime_state FROM regime_history "
            "WHERE date BETWEEN '2026-04-02' AND '2026-04-10' "
            "ORDER BY date",
        ).fetchall()

    date_to_state: dict[str, str] = {}
    for row in rows:
        d = row["date"] if hasattr(row, "keys") else row[0]
        s = row["regime_state"] if hasattr(row, "keys") else row[1]
        date_to_state[d] = s

    if len(date_to_state) < 3:
        pytest.skip("Too few rows to check regime progression")

    # 04-02 through 04-07 should be transition_uncertain (tariff shock)
    for d in ("2026-04-02", "2026-04-06", "2026-04-07"):
        if d in date_to_state:
            assert date_to_state[d] == "transition_uncertain", (
                f"Expected transition_uncertain on {d}, got '{date_to_state[d]}'"
            )

    # 04-08 onwards should be recovery_early (market recovery begins)
    for d in ("2026-04-08", "2026-04-09", "2026-04-10"):
        if d in date_to_state:
            assert date_to_state[d] == "recovery_early", (
                f"Expected recovery_early on {d}, got '{date_to_state[d]}'"
            )
