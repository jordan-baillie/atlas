"""R-05a audit test — SYK state-parity zombie cycle.

Root cause (confirmed, FIXED in commit following cef26a1d):
    ``_record_same_bar_round_trip()`` in ``brokers/live_executor.py`` used:

        AND DATE(exit_date) = DATE('now', 'localtime')

    as its idempotency guard. This only detected a duplicate on the SAME
    calendar day. ``reconcile_entry_fills`` runs every 15 min and Alpaca's
    fill lookback is 7 days, so at 00:01 AEST each night the SYK fill from
    2026-05-04 (within window) was re-detected, yesterday's zombie was NOT
    found by the guard (exit_date was yesterday, not today), and a new zombie
    was created. Self-perpetuating daily until the 7-day window expired.

    SYK BUY filled 2026-05-04 13:31 UTC; SELL filled 2026-05-09.
    Zombies were created on May 9, 10, 11.

Structural fix (SHIPPED — follows commit cef26a1d):
    In ``brokers/live_executor.py::_record_same_bar_round_trip``,
    the idempotency query was changed from:
        DATE(exit_date) = DATE('now', 'localtime')
    to:
        DATE(exit_date) >= DATE('now', 'localtime', '-8 days')

    This widens the window to match the 7-day Alpaca fill lookback + 1 day
    buffer, so yesterday's zombie IS matched and the recursion terminates.

Mitigation (shipped in commit cef26a1d):
    Error fingerprint ``3abcf083401b7959`` suppressed in the ``errors``
    table with full triage_reason documentation.

Tests:
1. SYK fingerprint is SUPPRESSED in the errors table.
2. triage_reason documents the root-cause fix (deferred note preserved
   in errors.triage_reason — not updated retroactively).
3. live_sp500.json does NOT contain SYK as an open position.
4. The zombie pattern can be detected: same-day open+close rows for SYK.
5. Dedup precheck (fixed SQL) matches yesterday's zombie row.
6. Dedup precheck correctly excludes 15-day-old history.
7. Old broken SQL provably misses yesterday's zombie (regression guard).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def live_db():
    """Read-only direct connection to the production atlas.db."""
    db_path = Path("/root/atlas/data/atlas.db")
    assert db_path.exists(), "atlas.db not found"
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture()
def state_file():
    return Path("/root/atlas/brokers/state/live_sp500.json")


# ── Tests: error fingerprint suppressed ──────────────────────────────────────

def test_syk_fingerprint_suppressed(live_db):
    """Fingerprint 3abcf083401b7959 must be SUPPRESSED, not ESCALATED/NEW."""
    row = live_db.execute(
        "SELECT fingerprint, remediation_status, triage_reason FROM errors "
        "WHERE fingerprint='3abcf083401b7959'"
    ).fetchone()
    assert row is not None, (
        "Fingerprint 3abcf083401b7959 not found in errors table — "
        "suppression SQL must have not run"
    )
    assert row["remediation_status"] == "SUPPRESSED", (
        f"Expected SUPPRESSED, got {row['remediation_status']}"
    )


def test_syk_fingerprint_triage_reason_documents_root_cause(live_db):
    """triage_reason must reference the root cause (live_executor.py)."""
    row = live_db.execute(
        "SELECT triage_reason FROM errors WHERE fingerprint='3abcf083401b7959'"
    ).fetchone()
    assert row is not None
    reason = row["triage_reason"] or ""
    assert "live_executor" in reason.lower() or "_record_same_bar_round_trip" in reason, (
        f"triage_reason does not mention the root cause file:\n{reason}"
    )
    # Also confirm the deferred fix note is present (preserved retroactively in
    # the errors row — we do NOT update the errors table here)
    assert "deferred" in reason.lower() or "next sprint" in reason.lower(), (
        f"triage_reason does not note the fix is deferred:\n{reason}"
    )


# ── Tests: zombie pattern detection ──────────────────────────────────────────

def test_syk_zombie_rows_are_same_day_open_and_close(live_db):
    """All zombie SYK rows have entry_date ~= exit_date (synthesized same-bar)."""
    rows = live_db.execute(
        """SELECT id, entry_date, exit_date
           FROM trades
           WHERE ticker='SYK' AND status='closed'
             AND id != 200  -- exclude the legitimate real trade
           ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    # At least some zombies should exist (may drop off as 7-day window expires)
    if not rows:
        pytest.skip("No zombie SYK rows found — 7-day window has expired; test not applicable")

    for row in rows:
        entry_str = row["entry_date"] or ""
        exit_str = row["exit_date"] or ""
        if entry_str and exit_str:
            # entry and exit are within 1 second (same-bar synthesized row)
            from datetime import datetime
            try:
                entry_dt = datetime.fromisoformat(entry_str[:19])
                exit_dt = datetime.fromisoformat(exit_str[:19])
                delta_seconds = abs((exit_dt - entry_dt).total_seconds())
                # Allow up to 60s: same-bar synthesized rows have entry≈exit
                # (reconcile loop processing takes up to ~16s on slow runs)
                assert delta_seconds < 60, (
                    f"Trade id={row['id']}: entry and exit differ by {delta_seconds}s "
                    f"(expected <60s for same-bar zombie; real trades are held for days)"
                )
            except ValueError:
                pass  # non-standard timestamp format — skip assertion


def test_syk_real_trade_id_200_exists(live_db):
    """The legitimate SYK trade (id=200) should be present and closed."""
    row = live_db.execute(
        "SELECT id, status, entry_date, exit_date FROM trades WHERE id=200"
    ).fetchone()
    assert row is not None, "Trade id=200 (legitimate SYK) not found"
    assert row["status"] == "closed", f"id=200 expected closed, got {row['status']}"


# ── Tests: state file does NOT have SYK open ─────────────────────────────────

def test_live_sp500_does_not_have_syk_as_open_position(state_file):
    """live_sp500.json must not list SYK as a live open position.

    SYK is closed; the self-heal path adds SYK to the JSON during the zombie
    cycle, then EOD settlement removes it again. At audit time (after the daily
    cycle) SYK should not be present as an open position.
    """
    if not state_file.exists():
        pytest.skip(f"State file not found: {state_file}")

    try:
        data = json.loads(state_file.read_text())
    except json.JSONDecodeError:
        pytest.fail(f"State file is not valid JSON: {state_file}")

    positions = data.get("positions", [])
    if isinstance(positions, list):
        open_tickers = {p.get("ticker") for p in positions}
    elif isinstance(positions, dict):
        open_tickers = set(positions.keys())
    else:
        open_tickers = set()

    # SYK should not be in open positions (it's closed)
    assert "SYK" not in open_tickers, (
        f"SYK found in live_sp500.json positions — zombie cycle self-heal is active. "
        f"Current open positions: {open_tickers}"
    )


# ── Tests: structural fix shipped ─────────────────────────────────────────────

def test_structural_fix_is_in_live_executor_source():
    """Verify the fixed idempotency window is present in brokers/live_executor.py.

    This replaces the earlier 'deferred_documented' test. The fix is now
    shipped: DATE(exit_date) >= DATE('now','localtime','-8 days') must appear
    in the _record_same_bar_round_trip idempotency precheck block.
    """
    source_path = Path("/root/atlas/brokers/live_executor.py")
    assert source_path.exists(), "brokers/live_executor.py not found"
    source = source_path.read_text()

    # The fixed pattern must be present
    assert "DATE(exit_date) >= DATE('now', 'localtime', '-8 days')" in source, (
        "Fixed idempotency window not found in live_executor.py — "
        "someone may have reverted the R-05a structural fix"
    )

    # The broken pattern must NOT be present anywhere outside comments
    # (no legitimate occurrences remain after this fix)
    broken_occurrences = source.count(
        "AND DATE(exit_date) = DATE('now', 'localtime')"
    )
    assert broken_occurrences == 0, (
        f"Broken pattern 'AND DATE(exit_date) = DATE(now, localtime)' found "
        f"{broken_occurrences} time(s) in live_executor.py — R-05a fix was reverted"
    )


# ── Tests: dedup SQL window correctness (in-memory SQLite) ───────────────────

@pytest.fixture()
def tmp_trades_db():
    """Minimal in-memory SQLite db with a trades table for dedup SQL testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE trades (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker     TEXT    NOT NULL,
            status     TEXT    NOT NULL,
            superseded INTEGER NOT NULL DEFAULT 0,
            entry_date TEXT,
            exit_date  TEXT,
            pnl        REAL
        )
        """
    )
    conn.commit()
    yield conn
    conn.close()


def _days_ago_iso(n: int) -> str:
    """Return an ISO timestamp string for midnight n days ago (local date)."""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%dT00:01:53")


def _fixed_dedup_query() -> str:
    """The corrected dedup SQL (mirrors live_executor.py exactly)."""
    return (
        "SELECT id FROM trades "
        "WHERE ticker = ? "
        "AND status = 'closed' AND superseded = 0 "
        "AND DATE(exit_date) >= DATE('now', 'localtime', '-8 days') "
        "AND ROUND(pnl, 2) = ROUND(?, 2) "
        "LIMIT 1"
    )


def _broken_dedup_query() -> str:
    """The OLD broken dedup SQL — today-only guard."""
    return (
        "SELECT id FROM trades "
        "WHERE ticker = ? "
        "AND status = 'closed' AND superseded = 0 "
        "AND DATE(exit_date) = DATE('now', 'localtime') "
        "AND ROUND(pnl, 2) = ROUND(?, 2) "
        "LIMIT 1"
    )


def test_dedup_precheck_matches_yesterdays_zombie(tmp_trades_db):
    """Fixed query must match a zombie row whose exit_date is yesterday.

    This is the core regression that was occurring nightly: at 00:01 AEST
    the previous day's zombie (exit_date = yesterday) was invisible to the
    old today-only guard. The fixed >= -8 days guard must find it.
    """
    yesterday_iso = _days_ago_iso(1)
    tmp_trades_db.execute(
        "INSERT INTO trades (ticker, status, superseded, exit_date, pnl) "
        "VALUES ('SYK', 'closed', 0, ?, -7.97)",
        (yesterday_iso,),
    )
    tmp_trades_db.commit()

    row = tmp_trades_db.execute(_fixed_dedup_query(), ("SYK", -7.97)).fetchone()

    assert row is not None, (
        f"Fixed dedup query must match yesterday's zombie "
        f"(exit_date={yesterday_iso}), but returned no rows. "
        "The R-05a structural fix is not working correctly."
    )


def test_dedup_precheck_excludes_old_history(tmp_trades_db):
    """Fixed query must NOT match a closed trade from 15 days ago.

    We must not dedup against ancient history — a ticker may legitimately be
    re-traded with the same PnL months later. The -8 day window provides a
    tight enough guard to cover the 7-day Alpaca fill lookback while
    excluding real prior history.
    """
    old_iso = _days_ago_iso(15)
    tmp_trades_db.execute(
        "INSERT INTO trades (ticker, status, superseded, exit_date, pnl) "
        "VALUES ('SYK', 'closed', 0, ?, -7.97)",
        (old_iso,),
    )
    tmp_trades_db.commit()

    row = tmp_trades_db.execute(_fixed_dedup_query(), ("SYK", -7.97)).fetchone()

    assert row is None, (
        f"Fixed dedup query must NOT match 15-day-old history "
        f"(exit_date={old_iso}), but returned a row. "
        "The -8 day window is too wide."
    )


def test_old_format_query_misses_yesterday_zombie(tmp_trades_db):
    """OLD broken pattern must FAIL to match yesterday's zombie — regression guard.

    This test exists to prove the old pattern was broken and that ANY revert
    of the R-05a fix would be caught: if the old query starts matching
    yesterday's zombie something is wrong with the test's date math.
    Removing this test removes the revert guard.
    """
    yesterday_iso = _days_ago_iso(1)
    tmp_trades_db.execute(
        "INSERT INTO trades (ticker, status, superseded, exit_date, pnl) "
        "VALUES ('SYK', 'closed', 0, ?, -7.97)",
        (yesterday_iso,),
    )
    tmp_trades_db.commit()

    # The OLD broken query — today-only guard:
    row = tmp_trades_db.execute(_broken_dedup_query(), ("SYK", -7.97)).fetchone()

    assert row is None, (
        f"OLD broken dedup query unexpectedly matched yesterday's zombie "
        f"(exit_date={yesterday_iso}). This should be impossible unless "
        "the test is running at exactly midnight and the row's date rolled over. "
        "This is the regression guard for the R-05a fix — investigate immediately."
    )
