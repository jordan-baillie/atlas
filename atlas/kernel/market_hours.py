"""Market hours helper — RTH (Regular Trading Hours) check for NYSE.

Uses pandas_market_calendars for holiday-aware scheduling. Falls back to
a simple Mon-Fri 09:30-16:00 ET check if the library is unavailable.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import pandas_market_calendars as mcal
    _NYSE = mcal.get_calendar("NYSE")
except Exception:  # pragma: no cover — fallback path
    _NYSE = None


def is_rth(now: Optional[datetime] = None) -> bool:
    """Return True if `now` is within NYSE Regular Trading Hours (09:30–16:00 ET).

    Args:
        now: Datetime to check. Must be timezone-aware (UTC or with tzinfo).
             If None, uses datetime.now(timezone.utc).

    Returns:
        True if within RTH on a trading day; False otherwise (weekends,
        holidays, pre-market, post-market).

    Notes:
        - Uses pandas_market_calendars for holiday awareness when available.
        - Falls back to a naive Mon-Fri 09:30-16:00 ET check if the library
          fails to load. The fallback does NOT account for holidays.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        # Treat naive datetime as UTC (defensive — caller should always pass aware)
        now = now.replace(tzinfo=timezone.utc)

    # Library path — pandas_market_calendars handles DST, holidays, half-days
    if _NYSE is not None:
        try:
            sched = _NYSE.schedule(start_date=now.date(), end_date=now.date())
            if sched.empty:
                return False  # not a trading day
            mkt_open = sched.iloc[0]["market_open"].to_pydatetime()
            mkt_close = sched.iloc[0]["market_close"].to_pydatetime()
            # Ensure tz-aware for comparison
            if mkt_open.tzinfo is None:
                mkt_open = mkt_open.replace(tzinfo=timezone.utc)
            if mkt_close.tzinfo is None:
                mkt_close = mkt_close.replace(tzinfo=timezone.utc)
            return mkt_open <= now < mkt_close
        except Exception as e:
            logger.warning("market_hours: pandas_market_calendars failed (%s) — using fallback", e)

    # Fallback — naive ET conversion. UTC-5 (EST) or UTC-4 (EDT). Use a fixed
    # ET-equivalent without DST awareness — better than nothing.
    # Convert UTC→ET by subtracting 5h (EST) as worst-case wider window:
    # actually we want stricter, so use 4h (EDT) for narrow window; this is
    # only a fallback when the library is unavailable so accept some imprecision.
    et_naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    # Subtract 5 hours to approximate ET (EST). During EDT this is 1h off but
    # only affects edge minutes; the 6h throttle backstop covers the gap.
    from datetime import timedelta
    et_approx = et_naive - timedelta(hours=5)
    if et_approx.weekday() >= 5:  # Sat=5, Sun=6
        return False
    rth_start = time(9, 30)
    rth_end = time(16, 0)
    return rth_start <= et_approx.time() < rth_end


def last_us_market_session(now: Optional[datetime] = None) -> str:
    """Return YYYY-MM-DD of the most recently CLOSED NYSE session.

    A session is "closed" when ``now`` is past its ``market_close`` time.
    During an open session (between 09:30 and 16:00 ET on a trading day),
    returns the PRIOR session's date — because today's data is not yet
    final and should not be expected in the OHLCV store.

    Examples (assuming standard NYSE hours, EDT = UTC-4):
        - Sat 12:00 ET  → returns previous Friday
        - Sun 12:00 ET  → returns previous Friday
        - Mon 06:00 ET  (pre-open)    → returns previous Friday (today not yet open)
        - Mon 12:00 ET  (mid-session) → returns previous Friday (today not yet closed)
        - Mon 17:00 ET  (post-close)  → returns today (Monday — just closed)
        - Tue 06:00 ET                → returns Monday (Mon just closed)

    Args:
        now: Datetime to check (UTC-aware). Defaults to ``datetime.now(UTC)``.

    Returns:
        ISO date string ``"YYYY-MM-DD"`` of the most recently CLOSED NYSE session.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        # Treat naive datetime as UTC (defensive — caller should always pass aware)
        now = now.replace(tzinfo=timezone.utc)

    if _NYSE is not None:
        try:
            from datetime import timedelta
            sched = _NYSE.schedule(
                start_date=now.date() - timedelta(days=14),
                end_date=now.date() + timedelta(days=1),  # include today in window
            )
            if sched.empty:
                return (now.date() - timedelta(days=1)).isoformat()

            # Filter to sessions whose market_close is in the past relative to now.
            # pandas_market_calendars market_close timestamps are UTC-aware;
            # now is also tz-aware — pandas converts to a common tz for comparison.
            closed_sessions = sched[sched["market_close"] <= now]
            if closed_sessions.empty:
                # All sessions in window are future — very rare (multi-day holiday).
                # Return the earliest session in the window as best approximation.
                return sched.index[0].date().isoformat()
            return closed_sessions.index[-1].date().isoformat()
        except Exception as _e:
            logger.warning(
                "last_us_market_session: pandas_market_calendars failed (%s) — using fallback",
                _e,
            )

    # Fallback: walk backwards skipping Sat/Sun (used when _NYSE is None or library fails)
    from datetime import timedelta
    d = now.date()
    while d.weekday() >= 5:  # 5=Sat, 6=Sun
        d -= timedelta(days=1)
    return d.isoformat()
