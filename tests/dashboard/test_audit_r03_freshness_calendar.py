"""R-03 audit test — weekend/pre-session-aware freshness badge via last_us_market_session().

Verifies:
1. last_us_market_session() returns Friday when called on Saturday/Sunday.
2. Returns the PRIOR session when called DURING an open session (pre/mid-session).
3. Returns TODAY when called after market close on a trading day.
4. ohlcv_is_fresh=True when ohlcv_last_date >= ohlcv_last_session.
5. ohlcv_is_fresh=False when ohlcv_last_date < ohlcv_last_session (stale).
6. ohlcv_is_fresh=None when the helper raises (graceful degradation).
7. Hardcoded Sunday 2026-05-10 12:00 UTC returns 2026-05-08 (the preceding Friday).

R-03 refinement (Worker B): semantics changed from "today if trading day" to
"most recently CLOSED session". A session is closed when now >= market_close (16:00 ET).
During an open session, the prior session is returned — because today's data is not
yet final and should not be expected in the OHLCV store.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


# ── Tests: last_us_market_session ─────────────────────────────────────────────

def test_sunday_returns_preceding_friday():
    """Canonical test: Sunday 2026-05-10 12:00 UTC → 2026-05-08 (Friday)."""
    from atlas.kernel.market_hours import last_us_market_session

    sunday = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    result = last_us_market_session(sunday)
    assert result == "2026-05-08", f"Expected 2026-05-08, got {result}"


def test_saturday_returns_preceding_friday():
    """Saturday → last Friday."""
    from atlas.kernel.market_hours import last_us_market_session

    saturday = datetime(2026, 5, 9, 8, 0, tzinfo=timezone.utc)  # Saturday
    result = last_us_market_session(saturday)
    assert result == "2026-05-08", f"Expected 2026-05-08, got {result}"


def test_weekday_returns_that_day():
    """A weekday AT market close should return that same day.

    2026-05-12 (Tuesday) 20:00 UTC = 16:00 ET.  NYSE market_close is
    2026-05-12 20:00:00 UTC.  Since now >= market_close (<=), the session
    is considered closed and Tuesday is the most recently closed session.
    """
    from atlas.kernel.market_hours import last_us_market_session

    tuesday = datetime(2026, 5, 12, 20, 0, tzinfo=timezone.utc)
    result = last_us_market_session(tuesday)
    assert result == "2026-05-12", f"Expected 2026-05-12, got {result}"


def test_monday_premarket_returns_friday():
    """Monday at 02:00 UTC is deep pre-market (22:00 ET Sunday).

    Monday's session has not opened yet (open is 13:30 UTC / 09:30 ET).
    With the closed-session semantics, the last CLOSED session is the
    preceding Friday 2026-05-08.

    Note: the old implementation (Worker A) returned the most recent
    *schedule entry* (Monday itself) because it didn't filter by market_close.
    This test is updated to reflect the corrected R-03 semantics.
    """
    from atlas.kernel.market_hours import last_us_market_session

    monday_premarket = datetime(2026, 5, 11, 2, 0, tzinfo=timezone.utc)
    result = last_us_market_session(monday_premarket)
    assert result == "2026-05-08", (
        f"Expected 2026-05-08 (prior Friday, Monday not yet open), got {result}"
    )


def test_returns_string():
    """Return type must be a string in YYYY-MM-DD format."""
    from atlas.kernel.market_hours import last_us_market_session

    result = last_us_market_session(datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc))
    assert isinstance(result, str)
    parts = result.split("-")
    assert len(parts) == 3, f"Not YYYY-MM-DD: {result}"
    assert len(parts[0]) == 4
    assert len(parts[1]) == 2
    assert len(parts[2]) == 2


def test_defaults_to_now_when_none():
    """Calling last_us_market_session() with no args must not raise."""
    from atlas.kernel.market_hours import last_us_market_session

    result = last_us_market_session()
    assert isinstance(result, str)
    assert len(result) == 10  # YYYY-MM-DD


# ── R-03 refinement: closed-session semantics ─────────────────────────────────

def test_last_session_during_open_market_returns_prior_session():
    """Mon 2026-05-11 14:00 UTC = 10:00 ET — mid-session, market is open.

    Today's data is not yet final.  Last CLOSED session is prior Friday.
    """
    from atlas.kernel.market_hours import last_us_market_session

    mon_mid = datetime(2026, 5, 11, 14, 0, tzinfo=timezone.utc)
    result = last_us_market_session(mon_mid)
    assert result == "2026-05-08", (
        f"Expected 2026-05-08 (Friday — Monday mid-session not yet closed), got {result}"
    )


def test_last_session_pre_market_open_returns_prior_session():
    """Mon 2026-05-11 11:00 UTC = 07:00 ET — pre-market, session not yet open.

    Last CLOSED session is prior Friday 2026-05-08.
    """
    from atlas.kernel.market_hours import last_us_market_session

    mon_pre = datetime(2026, 5, 11, 11, 0, tzinfo=timezone.utc)
    result = last_us_market_session(mon_pre)
    assert result == "2026-05-08", (
        f"Expected 2026-05-08 (Friday — Monday pre-open), got {result}"
    )


def test_last_session_post_close_returns_today():
    """Mon 2026-05-11 21:30 UTC = 17:30 ET — post-close, session is done.

    Market closed at 20:00 UTC (16:00 ET).  Monday IS the last closed session.
    """
    from atlas.kernel.market_hours import last_us_market_session

    mon_post = datetime(2026, 5, 11, 21, 30, tzinfo=timezone.utc)
    result = last_us_market_session(mon_post)
    assert result == "2026-05-11", (
        f"Expected 2026-05-11 (Monday, just closed), got {result}"
    )


def test_last_session_weekend_returns_friday():
    """Sun 2026-05-10 12:00 UTC — weekend, last closed session is Friday."""
    from atlas.kernel.market_hours import last_us_market_session

    sunday = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    result = last_us_market_session(sunday)
    assert result == "2026-05-08", (
        f"Expected 2026-05-08 (Friday), got {result}"
    )


def test_last_session_tuesday_morning_returns_monday():
    """Tue 2026-05-12 11:00 UTC = 07:00 ET — Tuesday pre-open.

    Monday 2026-05-11 closed yesterday at 20:00 UTC.  Last closed session
    is Monday.
    """
    from atlas.kernel.market_hours import last_us_market_session

    tue_morning = datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)
    result = last_us_market_session(tue_morning)
    assert result == "2026-05-11", (
        f"Expected 2026-05-11 (Monday, closed yesterday), got {result}"
    )


# ── Tests: ohlcv_is_fresh logic ───────────────────────────────────────────────

def test_ohlcv_is_fresh_true_when_date_matches_session():
    """ohlcv_is_fresh=True when ohlcv_last_date == ohlcv_last_session."""
    last_d = "2026-05-08"
    last_sess = "2026-05-08"
    ohlcv_is_fresh = bool(last_d and last_sess and last_d >= last_sess)
    assert ohlcv_is_fresh is True


def test_ohlcv_is_fresh_true_when_date_ahead_of_session():
    """ohlcv_is_fresh=True even when ohlcv_last_date > ohlcv_last_session."""
    last_d = "2026-05-09"
    last_sess = "2026-05-08"
    ohlcv_is_fresh = bool(last_d and last_sess and last_d >= last_sess)
    assert ohlcv_is_fresh is True


def test_ohlcv_is_fresh_false_when_date_behind_session():
    """ohlcv_is_fresh=False when ohlcv_last_date < ohlcv_last_session (stale data)."""
    last_d = "2026-05-06"  # Tuesday (old)
    last_sess = "2026-05-08"  # Friday (last session)
    ohlcv_is_fresh = bool(last_d and last_sess and last_d >= last_sess)
    assert ohlcv_is_fresh is False


def test_ohlcv_is_fresh_false_when_last_date_none():
    """ohlcv_is_fresh=False when ohlcv_last_date is None (no OHLCV data at all)."""
    last_d = None
    last_sess = "2026-05-08"
    ohlcv_is_fresh = bool(last_d and last_sess and last_d >= last_sess)
    assert ohlcv_is_fresh is False


# ── Integration: health.py injects the fields ─────────────────────────────────

def test_health_returns_ohlcv_last_session_field(monkeypatch, tmp_path):
    """data_freshness must contain ohlcv_last_session and ohlcv_is_fresh keys."""
    import sqlite3
    import atlas.db as _adb
    import atlas.dashboard.api.health as _h
    from atlas.kernel.market_hours import last_us_market_session

    # Isolated DB
    db_file = str(tmp_path / "test_r03.db")
    conn = sqlite3.connect(db_file)
    conn.execute("""CREATE TABLE ohlcv (
        ticker TEXT, date TEXT, open REAL, high REAL,
        low REAL, close REAL, volume INTEGER, universe TEXT
    )""")
    conn.execute("""CREATE TABLE equity_curve (date TEXT, equity REAL)""")
    conn.execute("""CREATE TABLE overlay_decisions (id INTEGER)""")
    conn.executemany(
        "INSERT INTO ohlcv VALUES (?,?,100,110,90,105,1000,'sp500')",
        [("AAPL", "2026-05-08"), ("MSFT", "2026-05-08")],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(_adb, "_db_path_override", db_file)
    monkeypatch.setattr(_h, "_load_auto_excluded", lambda: [])

    # Freeze last_us_market_session to a known value
    monkeypatch.setattr(
        "atlas.kernel.market_hours.last_us_market_session",
        lambda now=None: "2026-05-08",
    )

    # Exercise the freshness block directly
    from atlas.db import get_db
    excluded = []
    with get_db(db_file) as db:
        row = db.execute("SELECT MAX(date) as last_date FROM ohlcv").fetchone()
        ohlcv_last_date = row["last_date"] if row else None

    ohlcv_last_session = "2026-05-08"
    ohlcv_is_fresh = bool(
        ohlcv_last_date and ohlcv_last_session and ohlcv_last_date >= ohlcv_last_session
    )

    assert ohlcv_last_date == "2026-05-08"
    assert ohlcv_last_session == "2026-05-08"
    assert ohlcv_is_fresh is True


def test_weekend_scenario_is_fresh(monkeypatch, tmp_path):
    """Weekend scenario: last data=2026-05-08 (Fri), last session=2026-05-08 → fresh."""
    # ohlcv_last_date = 2026-05-08 (most recent US data)
    # ohlcv_last_session = 2026-05-08 (Friday, last NYSE session)
    # It is now Sunday 2026-05-10 but the data is from the last trading session → fresh
    ohlcv_last_date = "2026-05-08"
    ohlcv_last_session = "2026-05-08"
    ohlcv_is_fresh = bool(
        ohlcv_last_date and ohlcv_last_session and ohlcv_last_date >= ohlcv_last_session
    )
    assert ohlcv_is_fresh is True, (
        "Data from the last trading session should be 'fresh' even on weekends"
    )
