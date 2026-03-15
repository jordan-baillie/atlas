"""Event Calendar — macro event schedule for trading awareness.

Tracks FOMC meetings, CPI releases, NFP reports, options expiration (OPEX),
and index rebalancing dates. Used for info-only annotations on trade plans.

Supported event types:
    FOMC  — Federal Reserve rate decision meetings (loaded from JSON)
    CPI   — Consumer Price Index release (loaded from JSON)
    NFP   — Non-Farm Payrolls (computed: first Friday of each month)
    OPEX  — Options expiration (computed: third Friday of each month)
    REBAL — S&P 500 quarterly rebalance (computed: third Friday of Mar/Jun/Sep/Dec)

Usage:
    from data.events import EventCalendar

    ec = EventCalendar()
    events = ec.get_events_near("2026-03-18", window_days=5)
    proximity = ec.get_event_proximity(date.today())
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEDULE_DIR = Path(__file__).parent / "event_schedules"

# Year range for programmatically computed events
_COMPUTED_YEAR_START = 2020
_COMPUTED_YEAR_END = 2026


class EventType:
    """Macro event type constants."""
    FOMC = "FOMC"    # Federal Reserve meetings (rate decisions)
    CPI = "CPI"      # Consumer Price Index release
    NFP = "NFP"      # Non-Farm Payrolls (first Friday of month)
    OPEX = "OPEX"    # Options expiration (third Friday of month)
    REBAL = "REBAL"  # S&P 500 quarterly rebalance (third Friday of Mar/Jun/Sep/Dec)


@dataclass
class MarketEvent:
    """A single macro market event."""
    event_type: str
    date: date
    description: str
    impact: str = "high"  # "high" | "medium" | "low"

    def __repr__(self) -> str:
        return f"MarketEvent({self.event_type} {self.date} impact={self.impact})"


class EventCalendar:
    """Calendar of macro market events.

    Loads FOMC and CPI from JSON schedule files.
    Computes NFP, OPEX, and REBAL dates programmatically.

    All event lookups are O(n) but the total event count is small
    (~200 events for 2020-2026), so performance is not a concern.
    """

    def __init__(self) -> None:
        self._events: List[MarketEvent] = []
        self._load_all()

    # ------------------------------------------------------------------
    # Internal loading helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load all events from JSON files and computed sources."""
        # JSON-backed schedules
        self._events.extend(self._load_schedule("fomc.json"))
        self._events.extend(self._load_schedule("cpi.json"))

        # Programmatically computed schedules
        self._events.extend(
            self._compute_nfp_dates(_COMPUTED_YEAR_START, _COMPUTED_YEAR_END)
        )
        self._events.extend(
            self._compute_opex_dates(_COMPUTED_YEAR_START, _COMPUTED_YEAR_END)
        )
        self._events.extend(
            self._compute_rebal_dates(_COMPUTED_YEAR_START, _COMPUTED_YEAR_END)
        )

        # Sort by date for consistent ordering
        self._events.sort(key=lambda e: e.date)
        logger.debug("EventCalendar loaded %d events (%d FOMC, %d CPI, %d NFP, %d OPEX, %d REBAL)",
                     len(self._events),
                     sum(1 for e in self._events if e.event_type == EventType.FOMC),
                     sum(1 for e in self._events if e.event_type == EventType.CPI),
                     sum(1 for e in self._events if e.event_type == EventType.NFP),
                     sum(1 for e in self._events if e.event_type == EventType.OPEX),
                     sum(1 for e in self._events if e.event_type == EventType.REBAL))

    def _load_schedule(self, filename: str) -> List[MarketEvent]:
        """Load a JSON schedule file and return a list of MarketEvent objects.

        JSON format expected:
        {
            "event_type": "FOMC",
            "impact": "high",
            "dates": [
                {"date": "2024-01-31", "description": "FOMC Meeting"},
                ...
            ]
        }
        """
        path = SCHEDULE_DIR / filename
        if not path.exists():
            logger.warning("Event schedule file not found: %s", path)
            return []

        try:
            with open(path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load event schedule %s: %s", filename, exc)
            return []

        event_type = data.get("event_type", "UNKNOWN")
        impact = data.get("impact", "high")
        events: List[MarketEvent] = []

        for entry in data.get("dates", []):
            raw_date = entry.get("date", "")
            description = entry.get("description", event_type)
            entry_impact = entry.get("impact", impact)  # per-entry override allowed
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                events.append(MarketEvent(
                    event_type=event_type,
                    date=parsed_date,
                    description=description,
                    impact=entry_impact,
                ))
            except ValueError:
                logger.warning("Invalid date '%s' in %s — skipping", raw_date, filename)
                continue

        logger.debug("Loaded %d %s events from %s", len(events), event_type, filename)
        return events

    # ------------------------------------------------------------------
    # Programmatic date computation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _nth_friday(year: int, month: int, n: int) -> date:
        """Return the nth Friday of a given year/month (n=1 for first, n=3 for third).

        If n exceeds the number of Fridays in the month, returns the last one.
        """
        # Find first day of month
        first = date(year, month, 1)
        # Weekday: 0=Monday, 4=Friday
        days_until_friday = (4 - first.weekday()) % 7
        first_friday = first + timedelta(days=days_until_friday)
        # nth Friday = first + (n-1) * 7 days
        target = first_friday + timedelta(weeks=n - 1)
        # Guard: ensure still in same month
        if target.month != month:
            target -= timedelta(weeks=1)
        return target

    def _compute_nfp_dates(self, year_start: int, year_end: int) -> List[MarketEvent]:
        """Compute Non-Farm Payroll dates (first Friday of each month).

        NFP is released on the first Friday of every month by the BLS.
        Covers year_start through year_end (inclusive).
        """
        events: List[MarketEvent] = []
        for year in range(year_start, year_end + 1):
            for month in range(1, 13):
                nfp_date = self._nth_friday(year, month, 1)
                events.append(MarketEvent(
                    event_type=EventType.NFP,
                    date=nfp_date,
                    description="Non-Farm Payrolls Report",
                    impact="high",
                ))
        logger.debug("Computed %d NFP dates (%d–%d)", len(events), year_start, year_end)
        return events

    def _compute_opex_dates(self, year_start: int, year_end: int) -> List[MarketEvent]:
        """Compute options expiration dates (third Friday of each month).

        Monthly OPEX falls on the third Friday of each month.
        Covers year_start through year_end (inclusive).
        """
        events: List[MarketEvent] = []
        for year in range(year_start, year_end + 1):
            for month in range(1, 13):
                opex_date = self._nth_friday(year, month, 3)
                events.append(MarketEvent(
                    event_type=EventType.OPEX,
                    date=opex_date,
                    description="Options Expiration (OPEX)",
                    impact="medium",
                ))
        logger.debug("Computed %d OPEX dates (%d–%d)", len(events), year_start, year_end)
        return events

    def _compute_rebal_dates(self, year_start: int, year_end: int) -> List[MarketEvent]:
        """Compute S&P 500 quarterly rebalancing dates (third Friday of Mar/Jun/Sep/Dec).

        The major quarterly rebalance (also Triple/Quad Witching) falls on the
        third Friday of March, June, September, and December.
        Covers year_start through year_end (inclusive).
        """
        rebal_months = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}
        events: List[MarketEvent] = []
        for year in range(year_start, year_end + 1):
            for month, quarter in rebal_months.items():
                rebal_date = self._nth_friday(year, month, 3)
                events.append(MarketEvent(
                    event_type=EventType.REBAL,
                    date=rebal_date,
                    description=f"S&P 500 {quarter} Quarterly Rebalance",
                    impact="medium",
                ))
        logger.debug("Computed %d REBAL dates (%d–%d)", len(events), year_start, year_end)
        return events

    # ------------------------------------------------------------------
    # Public query interface
    # ------------------------------------------------------------------

    def get_events_on(self, dt: date) -> List[MarketEvent]:
        """Return all events occurring on an exact date.

        Args:
            dt: The date to look up.

        Returns:
            List of MarketEvent objects occurring on dt (may be empty).
        """
        return [e for e in self._events if e.date == dt]

    def get_events_near(
        self,
        dt_str,
        window_days: int = 5,
    ) -> List[MarketEvent]:
        """Return events within ±window_days of a given date.

        Args:
            dt_str:      Date to centre the window on. Accepts "YYYY-MM-DD" string
                         or a date / datetime object.
            window_days: Half-width of the lookup window in calendar days.
                         Default 5 means ±5 days (11-day window total).

        Returns:
            List of MarketEvent objects sorted by date, within the window.
        """
        # Normalise input to a date object
        if isinstance(dt_str, str):
            try:
                centre = datetime.strptime(dt_str, "%Y-%m-%d").date()
            except ValueError as exc:
                logger.error("get_events_near: invalid date string '%s': %s", dt_str, exc)
                return []
        elif isinstance(dt_str, datetime):
            centre = dt_str.date()
        elif isinstance(dt_str, date):
            centre = dt_str
        else:
            logger.error("get_events_near: unsupported type %s", type(dt_str))
            return []

        lo = centre - timedelta(days=window_days)
        hi = centre + timedelta(days=window_days)
        return [e for e in self._events if lo <= e.date <= hi]

    def get_event_proximity(self, dt) -> Dict[str, int]:
        """Return days-to-next occurrence for each major event type.

        Searches forward from dt for the next FOMC, CPI, NFP, OPEX, and REBAL
        events.

        Args:
            dt: Reference date (typically today or the trade date).
                Accepts datetime.date, datetime.datetime, or pandas.Timestamp.

        Returns:
            Dictionary with keys:
                "days_to_fomc"  — calendar days until next FOMC after dt
                "days_to_cpi"   — calendar days until next CPI after dt
                "days_to_nfp"   — calendar days until next NFP after dt
                "is_opex_week"  — 1 if there is an OPEX event within 0-6 days, else 0
                "days_to_opex"  — calendar days until next OPEX after dt
                "days_to_rebal" — calendar days until next quarterly REBAL after dt
            Values of -1 indicate no future event found in the loaded schedule.
        """
        # Normalise to datetime.date for consistent comparison with event dates
        if hasattr(dt, "date") and callable(dt.date):
            dt = dt.date()  # handles pd.Timestamp, datetime.datetime
        result: Dict[str, int] = {
            "days_to_fomc": -1,
            "days_to_cpi": -1,
            "days_to_nfp": -1,
            "is_opex_week": 0,
            "days_to_opex": -1,
            "days_to_rebal": -1,
        }

        type_to_key = {
            EventType.FOMC: "days_to_fomc",
            EventType.CPI: "days_to_cpi",
            EventType.NFP: "days_to_nfp",
        }

        for event in self._events:
            if event.date < dt:
                continue

            # Days until this event (0 = same day)
            delta = (event.date - dt).days

            if event.event_type in type_to_key:
                key = type_to_key[event.event_type]
                if result[key] == -1:
                    # First (nearest) occurrence wins
                    result[key] = delta

            elif event.event_type == EventType.OPEX:
                if result["days_to_opex"] == -1:
                    result["days_to_opex"] = delta
                if delta <= 6 and result["is_opex_week"] == 0:
                    result["is_opex_week"] = 1

            elif event.event_type == EventType.REBAL:
                if result["days_to_rebal"] == -1:
                    result["days_to_rebal"] = delta

        return result

    # ------------------------------------------------------------------
    # Convenience / introspection
    # ------------------------------------------------------------------

    def all_events(self) -> List[MarketEvent]:
        """Return all loaded events sorted by date."""
        return list(self._events)

    def events_by_type(self, event_type: str) -> List[MarketEvent]:
        """Return all events of a specific type."""
        return [e for e in self._events if e.event_type == event_type]
