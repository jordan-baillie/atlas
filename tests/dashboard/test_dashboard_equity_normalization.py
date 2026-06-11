"""Tests for equity-curve normalization in _get_portfolio_history (Fix 1 v2).

Verifies that cash-flow events (deposits/withdrawals) are removed from the
visible chart using Alpaca's account activities API.

Normalization formula:
    normalized_equity[i] = raw_equity[i] - cum_deposits_at_date[i] + total_deposits_ever

All tests use synthetic data and a MagicMock broker — no live Alpaca calls.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_broker(history_response, activities_response):
    """Build a mock broker that returns history_response from
    get_portfolio_history and activities_response from get_account_activities.

    Distinguishes calls by the REQUEST class name:
      - 'GetPortfolioHistoryRequest' → history_response
      - 'GetAccountActivitiesRequest' → activities_response
    """
    broker = MagicMock()

    def fake_call(fn, req):
        cls = type(req).__name__
        if "Portfolio" in cls:
            return history_response
        if "Activit" in cls:
            # fn is the _do_fetch_activities closure; it receives the req
            # object but we intercept here to return the mock list directly.
            return activities_response
        raise RuntimeError(f"Unexpected request class: {cls}")

    broker._broker_call = fake_call
    broker._trade_client = MagicMock()
    return broker


def _ph(rows):
    """rows = [(unix_ts, equity, profit_loss), ...]"""
    return SimpleNamespace(
        timestamp=[r[0] for r in rows],
        equity=[r[1] for r in rows],
        profit_loss=[r[2] for r in rows],
    )


def _act(events):
    """events = [(date_str, net_amount), ...] — synthetic activities.

    Returns SimpleNamespace objects so they exercise the non-dict path
    (getattr) in the activity loop.
    """
    return [
        SimpleNamespace(date=d, net_amount=str(amt), activity_type="CSD")
        for d, amt in events
    ]


def _call_fn(broker) -> list:
    """Import and call _get_portfolio_history directly."""
    from atlas.dashboard.api.dashboard import _get_portfolio_history
    return _get_portfolio_history(broker)


# Base timestamp: 2026-01-01 00:00 UTC → sequential daily increments
_BASE_TS = 1767225600  # 2026-01-01 00:00:00 UTC (verified)
_DAY_SEC = 86400


def _ts(day_offset: int) -> int:
    return _BASE_TS + day_offset * _DAY_SEC


def _date(day_offset: int) -> str:
    """Derive date string from _BASE_TS — guaranteed consistent with _ts()."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(_BASE_TS + day_offset * _DAY_SEC, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Test 1: normalization removes funding spike
# ---------------------------------------------------------------------------

class TestNormalizationRemovesFundingSpike:
    """Core correctness: deposit days produce zero normalized jump."""

    def test_normalization_removes_funding_spike(self) -> None:
        """
        History: day0=$1000, day1=$1010, day2=$6010 (deposit!), day3=$6015
        Activities: [(day0, +1000), (day2, +5000)]
        Expected normalized: [6000, 6010, 6010, 6015] — day2 has NO spike vs day1
        """
        d0, d2 = _date(0), _date(2)
        history = _ph([
            (_ts(0), 1000.0, 1000.0),   # day0: deposit+open
            (_ts(1), 1010.0,   10.0),   # day1: +$10 PnL
            (_ts(2), 6010.0, 5000.0),   # day2: $5000 deposit (pl includes deposit)
            (_ts(3), 6015.0,    5.0),   # day3: +$5 PnL
        ])
        activities = _act([(d0, 1000.0), (d2, 5000.0)])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 4

        # total_deposits_ever=6000
        # day0: 1000 - 1000 + 6000 = 6000
        # day1: 1010 - 1000 + 6000 = 6010
        # day2: 6010 - 6000 + 6000 = 6010  ← no spike!
        # day3: 6015 - 6000 + 6000 = 6015
        assert result[0]["equity"] == 6000.0
        assert result[1]["equity"] == 6010.0
        assert result[2]["equity"] == 6010.0  # deposit day is flat, no spike
        assert result[3]["equity"] == 6015.0

        # No day-over-day jump > $50 (the $5000 deposit is invisible in normalized)
        for i in range(1, len(result)):
            delta = abs(result[i]["equity"] - result[i - 1]["equity"])
            assert delta <= 50, (
                f"Funding spike not removed on day {i}: "
                f"delta=${delta:.2f} (prev={result[i-1]['equity']}, curr={result[i]['equity']})"
            )


# ---------------------------------------------------------------------------
# Test 2: last row continuity
# ---------------------------------------------------------------------------

class TestLastRowContinuity:
    """Last row's normalized equity must always equal last raw equity."""

    def test_normalization_last_row_continuity(self) -> None:
        """Any history + activities → last normalized equity equals last raw equity."""
        d0, d1 = _date(0), _date(1)
        history = _ph([
            (_ts(0), 1000.0, 0.0),
            (_ts(1), 2500.0, 0.0),  # deposit on day1
            (_ts(2), 2510.0, 10.0),
        ])
        activities = _act([(d0, 1000.0), (d1, 1500.0)])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 3
        last = result[-1]
        assert last["equity"] == last["raw_equity"], (
            f"Last row continuity broken: equity={last['equity']}, raw={last['raw_equity']}"
        )
        assert last["equity"] == 2510.0


# ---------------------------------------------------------------------------
# Test 3: no activities → identity transform
# ---------------------------------------------------------------------------

class TestNoActivitiesIsIdentity:
    """When no activities are returned, normalized == raw for every row."""

    def test_normalization_no_activities_is_identity(self) -> None:
        history = _ph([
            (_ts(0), 5000.0, 10.0),
            (_ts(1), 5010.0, 10.0),
            (_ts(2), 5005.0, -5.0),
        ])
        activities = _act([])  # empty list
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 3
        for i, r in enumerate(result):
            assert r["equity"] == r["raw_equity"], (
                f"Row {i}: identity broken — equity={r['equity']}, raw={r['raw_equity']}"
            )


# ---------------------------------------------------------------------------
# Test 4: withdrawal handling
# ---------------------------------------------------------------------------

class TestWithdrawalHandling:
    """Withdrawals (negative net_amount) don't create a cliff in normalized curve."""

    def test_normalization_handles_withdrawal(self) -> None:
        """
        History: day0=$5000 (deposit), day1=$4000 (withdrawal of $1000)
        Activities: [(day0, +5000), (day1, -1000)]
        total_deposits_ever = 5000 + (-1000) = 4000
        normalized: day0 = 5000 - 5000 + 4000 = 4000
                    day1 = 4000 - 4000 + 4000 = 4000  ← flat, no cliff
        """
        d0, d1 = _date(0), _date(1)
        history = _ph([
            (_ts(0), 5000.0, 0.0),   # initial deposit
            (_ts(1), 4000.0, 0.0),   # $1000 withdrawal, no PnL
        ])
        activities = _act([(d0, 5000.0), (d1, -1000.0)])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 2
        # total_deposits_ever = 4000
        assert result[0]["equity"] == 4000.0
        assert result[1]["equity"] == 4000.0
        # Last point continuity
        assert result[-1]["equity"] == result[-1]["raw_equity"]
        # No cliff
        delta = abs(result[1]["equity"] - result[0]["equity"])
        assert delta < 0.02, f"Unexpected cliff after withdrawal: delta=${delta:.2f}"


# ---------------------------------------------------------------------------
# Test 5: activities API failure falls back to identity
# ---------------------------------------------------------------------------

class TestActivitiesApiFailureFallback:
    """If activities fetch raises, function returns rows with normalized==raw."""

    def test_normalization_activities_api_failure_falls_back_to_identity(self) -> None:
        history = _ph([
            (_ts(0), 5000.0, 10.0),
            (_ts(1), 5010.0, 10.0),
        ])

        broker = MagicMock()

        def fail_on_activities(fn, req):
            cls = type(req).__name__
            if "Portfolio" in cls:
                return history
            if "Activit" in cls:
                raise RuntimeError("activities API is down")
            raise RuntimeError(f"Unexpected: {cls}")

        broker._broker_call = fail_on_activities
        broker._trade_client = MagicMock()

        result = _call_fn(broker)

        # Should still return rows (not empty)
        assert len(result) == 2
        # With no cash-flow data, normalized == raw (identity)
        for r in result:
            assert r["equity"] == r["raw_equity"], (
                f"Fallback identity broken: equity={r['equity']}, raw={r['raw_equity']}"
            )


# ---------------------------------------------------------------------------
# Test 6: pre-funding zeros skipped
# ---------------------------------------------------------------------------

class TestPreFundingZerosSkipped:
    """Equity values <= 0 are filtered before normalization."""

    def test_normalization_pre_funding_zeros_skipped(self) -> None:
        d2 = _date(2)
        history = _ph([
            (_ts(0), 0.0, 0.0),      # pre-funding → skip
            (_ts(1), 0.0, 0.0),      # pre-funding → skip
            (_ts(2), 5000.0, 0.0),   # first funded day
            (_ts(3), 5010.0, 10.0),
        ])
        activities = _act([(d2, 5000.0)])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        # Only 2 rows (the two zero-equity rows are skipped)
        assert len(result) == 2
        assert result[0]["raw_equity"] == 5000.0
        assert result[1]["raw_equity"] == 5010.0


# ---------------------------------------------------------------------------
# Test 7: empty history returns []
# ---------------------------------------------------------------------------

class TestEmptyHistoryReturnsEmptyList:
    def test_empty_history_returns_empty_list(self) -> None:
        history = SimpleNamespace(timestamp=[], equity=[], profit_loss=[])
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert result == []


# ---------------------------------------------------------------------------
# Test 8: broker history failure returns []
# ---------------------------------------------------------------------------

class TestBrokerHistoryFailureReturnsEmptyList:
    def test_broker_history_failure_returns_empty_list(self) -> None:
        broker = MagicMock()
        broker._broker_call = MagicMock(side_effect=RuntimeError("Alpaca API down"))
        broker._trade_client = MagicMock()
        result = _call_fn(broker)
        assert result == []


# ---------------------------------------------------------------------------
# Test 9: Y-axis tight after fix (production-like scenario)
# ---------------------------------------------------------------------------

class TestYAxisTightAfterFix:
    """Synthetic curve mimicking production: two funding events dominate raw spread."""

    def test_normalization_y_axis_tight_after_fix(self) -> None:
        """
        30 days: $3500 initial deposit → small wiggle → $1500 more deposit → more wiggle.
        Activities: 2 deposits ($3500 + $1500 = $5000 total).
        Assert: spread of normalized equity < 30% of raw spread.
        """
        import random
        random.seed(42)

        rows_data = []
        equity = 0.0

        # Day 0: pre-funding (equity=0)
        rows_data.append((_ts(0), 0.0, 0.0))

        # Day 1: $3500 deposit arrives
        equity = 3500.0
        rows_data.append((_ts(1), equity, 0.0))

        # Days 2-15: small trading PnL only
        for day in range(2, 16):
            pnl = random.uniform(-20, 20)
            equity += pnl
            rows_data.append((_ts(day), round(equity, 2), round(pnl, 2)))

        # Day 16: $1500 deposit — big raw spike
        equity += 1500.0
        rows_data.append((_ts(16), round(equity, 2), 0.0))

        # Days 17-29: small trading PnL only
        for day in range(17, 30):
            pnl = random.uniform(-20, 20)
            equity += pnl
            rows_data.append((_ts(day), round(equity, 2), round(pnl, 2)))

        history = _ph(rows_data)

        # Activities: two deposits on day1 and day16
        activities = _act([(_date(1), 3500.0), (_date(16), 1500.0)])

        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        # Filter out the pre-funding zero row (day 0)
        assert len(result) == 29  # 30 rows minus the 1 pre-funding zero

        raw_eqs = [r["raw_equity"] for r in result]
        norm_eqs = [r["equity"] for r in result]

        raw_spread = max(raw_eqs) - min(raw_eqs)
        norm_spread = max(norm_eqs) - min(norm_eqs)

        assert raw_spread > 0, "Raw spread should be > 0"
        reduction_pct = 1.0 - (norm_spread / raw_spread)
        assert reduction_pct > 0.70, (
            f"Normalization should reduce spread by >70% in deposit-dominated curve; "
            f"raw_spread=${raw_spread:.2f}, norm_spread=${norm_spread:.2f}, "
            f"reduction={reduction_pct:.1%}"
        )

        # Last row continuity always holds
        assert result[-1]["equity"] == result[-1]["raw_equity"]


# ---------------------------------------------------------------------------
# Additional preserved tests (edge cases + preservation properties)
# ---------------------------------------------------------------------------

class TestPreFundingNoneEquitySkipped:
    def test_pre_funding_none_equity_skipped(self) -> None:
        history = SimpleNamespace(
            timestamp=[_ts(0), _ts(1)],
            equity=[None, 5100.0],
            profit_loss=[0.0, 5.0],
        )
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert len(result) == 1
        assert result[0]["raw_equity"] == 5100.0


class TestDayPnlPreservation:
    def test_normalized_day_pnl_preserved(self) -> None:
        history = _ph([
            (_ts(0), 5000.0,  12.34),
            (_ts(1), 5010.0, -56.78),
            (_ts(2), 5000.0,   0.0),
            (_ts(3), 5005.0,   5.0),
        ])
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        expected_pnl = [12.34, -56.78, 0.0, 5.0]
        for i, exp in enumerate(expected_pnl):
            assert result[i]["day_pnl"] == round(exp, 2), (
                f"day_pnl mismatch row {i}: expected {exp}, got {result[i]['day_pnl']}"
            )

    def test_raw_equity_preserved_in_output(self) -> None:
        history = _ph([
            (_ts(0), 5000.0, 10.0),
            (_ts(1), 5010.0, 10.0),
        ])
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert result[0]["raw_equity"] == 5000.0
        assert result[1]["raw_equity"] == 5010.0


class TestMissingProfitLoss:
    def test_missing_profit_loss_defaults_to_zero(self) -> None:
        history = SimpleNamespace(
            timestamp=[_ts(0), _ts(1), _ts(2)],
            equity=[5000.0, 5010.0, 5005.0],
            profit_loss=[10.0],  # only 1 entry for 3 rows
        )
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert result[0]["day_pnl"] == 10.0
        assert result[1]["day_pnl"] == 0.0
        assert result[2]["day_pnl"] == 0.0


class TestSingleRow:
    def test_single_row_equity_equals_raw(self) -> None:
        history = _ph([(_ts(0), 5000.0, 25.0)])
        activities = _act([(_date(0), 5000.0)])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert len(result) == 1
        assert result[0]["equity"] == result[0]["raw_equity"]


class TestAllZeroEquityReturnsEmpty:
    def test_all_zero_equity_returns_empty_list(self) -> None:
        history = _ph([(_ts(i), 0.0, 0.0) for i in range(5)])
        activities = _act([])
        broker = _make_broker(history, activities)
        result = _call_fn(broker)
        assert result == []


class TestMultipleDepositsOnSameDay:
    """Two activity records on the same date should be summed."""

    def test_same_day_activities_summed(self) -> None:
        d0 = _date(0)
        history = _ph([
            (_ts(0), 3000.0, 0.0),   # two deposits land same day
            (_ts(1), 3005.0, 5.0),
        ])
        # Two separate activity records on same day (e.g., split transfer)
        activities = _act([(d0, 1000.0), (d0, 2000.0)])  # total=3000 on d0
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 2
        # total_deposits_ever=3000; day0: cum=3000; norm=3000-3000+3000=3000
        assert result[0]["equity"] == 3000.0
        # day1: cum=3000; norm=3005-3000+3000=3005
        assert result[1]["equity"] == 3005.0
        assert result[-1]["equity"] == result[-1]["raw_equity"]


class TestDateStringActivity:
    """Activity .date as a string (not datetime.date) is handled."""

    def test_activity_date_as_string(self) -> None:
        d0 = _date(0)
        history = _ph([
            (_ts(0), 1000.0, 0.0),
            (_ts(1), 1010.0, 10.0),
        ])
        # net_amount as a string like Alpaca often returns
        activities = [
            SimpleNamespace(date=d0, net_amount="1000.00", activity_type="CSD"),
        ]
        broker = _make_broker(history, activities)
        result = _call_fn(broker)

        assert len(result) == 2
        # total_deposits_ever=1000; day0: norm=1000-1000+1000=1000; day1: 1010-1000+1000=1010
        assert result[0]["equity"] == 1000.0
        assert result[1]["equity"] == 1010.0
        assert result[-1]["equity"] == result[-1]["raw_equity"]
