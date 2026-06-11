"""Tests for dashboard data correctness fixes.

Tests:
1. Bug 1: total_pnl uses aggregated starting_equity across all market configs.
2. Bug 2: equity curve uses Alpaca portfolio_history (no per-market discontinuity).
3. Cache: second call within TTL is served from cache (broker not re-called).
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

# ── Project root on path ──────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_dashboard_cache():
    """Reset the module-level 30-second cache before and after each test."""
    from atlas.dashboard.api import dashboard as dash_mod
    dash_mod._DASHBOARD_CACHE["data"] = None
    dash_mod._DASHBOARD_CACHE["ts"] = 0.0
    yield
    dash_mod._DASHBOARD_CACHE["data"] = None
    dash_mod._DASHBOARD_CACHE["ts"] = 0.0


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_minimal_db() -> sqlite3.Connection:
    """In-memory SQLite DB with the minimal schema _build_dashboard_data needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY,
            ticker TEXT, strategy TEXT, entry_date TEXT,
            stop_price REAL, entry_price REAL,
            exit_date TEXT, pnl REAL, pnl_pct REAL,
            status TEXT, superseded INTEGER, exit_reason TEXT,
            universe TEXT
        );
        CREATE TABLE IF NOT EXISTS equity_curve (
            id INTEGER PRIMARY KEY,
            market_id TEXT, date TEXT, equity REAL, day_pnl REAL
        );
        CREATE TABLE IF NOT EXISTS ohlcv (
            id INTEGER PRIMARY KEY,
            ticker TEXT, date TEXT, close REAL,
            open REAL, high REAL, low REAL, volume REAL, market TEXT
        );
    """)
    conn.commit()
    return conn


@contextmanager
def _patch_db(conn: sqlite3.Connection):
    """Patch atlas.db.get_db to yield the given in-memory connection."""
    from contextlib import contextmanager as _cm

    @_cm
    def _fake(*_a, **_kw):
        yield conn

    # get_db is imported lazily inside _build_dashboard_data; patch at source.
    with patch("atlas.db.get_db", _fake):
        yield


def _make_account_info(equity: float = 5266.0):
    """Build a real AccountInfo dataclass instance (supports dataclasses.asdict)."""
    from atlas.brokers.base import AccountInfo
    return AccountInfo(
        equity=equity,
        cash=500.0,
        market_value=equity - 500.0,
        buying_power=1000.0,
        total_pnl=equity - 971.0,  # broker's wrong single-market value
        total_pnl_pct=round((equity - 971.0) / 971.0 * 100, 2),
        num_positions=0,
    )


def _make_mock_clock():
    """Return a mock clock object."""
    clk = MagicMock()
    clk.is_open = False
    clk.next_open = "2026-05-02T09:30:00"
    clk.next_close = "2026-05-02T16:00:00"
    clk.timestamp = "2026-05-01T12:00:00"
    return clk


def _make_broker(equity: float = 5266.0, account_call_counter: list | None = None):
    """Build a mock broker that passes basic _build_dashboard_data flows."""
    broker = MagicMock()
    broker.connect.return_value = True
    broker.get_positions.return_value = []
    broker.get_history_orders.return_value = []
    broker.get_open_orders.return_value = []

    # Track get_account_info call count
    _counter = account_call_counter if account_call_counter is not None else [0]

    def _account_info():
        _counter[0] += 1
        return _make_account_info(equity)

    broker.get_account_info.side_effect = _account_info

    # _broker_call: dispatch based on the first positional argument (the fn)
    clock = _make_mock_clock()

    def _broker_call(fn, *args):
        """Simulate _broker_call by recognising common fn objects."""
        fn_name = getattr(fn, "__name__", str(fn))
        if "get_clock" in fn_name or "clock" in str(fn).lower():
            return clock
        if "get_account" in fn_name and "get_all" not in fn_name:
            m = MagicMock()
            m.initial_margin = 0.0
            m.equity = equity
            return m
        if "get_all_positions" in fn_name:
            return []
        if "get_portfolio_history" in fn_name:
            # Return a plausible history object
            import datetime as _datetime
            today = _datetime.date.today()
            timestamps = []
            equities = []
            profit_losses = []
            for i in range(5, 0, -1):
                d = today - _datetime.timedelta(days=i)
                import calendar
                timestamps.append(int(calendar.timegm(d.timetuple())))
                equities.append(5200.0 + i * 10)
                profit_losses.append(10.0)
            ph = MagicMock()
            ph.timestamp = timestamps
            ph.equity = equities
            ph.profit_loss = profit_losses
            return ph
        return MagicMock()

    broker._broker_call.side_effect = _broker_call
    broker._trade_client = MagicMock()
    broker._trade_client.get_account = MagicMock(__name__="get_account")
    broker._trade_client.get_all_positions = MagicMock(__name__="get_all_positions")
    broker._trade_client.get_clock = MagicMock(__name__="get_clock")
    broker._trade_client.get_portfolio_history = MagicMock(__name__="get_portfolio_history")

    return broker, _counter


# ══════════════════════════════════════════════════════════════════════════════
# Test 1 — total_pnl uses aggregated starting_equity
# ══════════════════════════════════════════════════════════════════════════════

def test_total_pnl_uses_aggregated_starting_equity(tmp_path, monkeypatch):
    """Bug 1: total_pnl must be equity - SUM(starting_equity) across all markets.

    Three config files: starting_equity 971 + 3216 + 1001 = 5188.
    Broker reports equity = 5266.
    Expected: total_pnl = 78.0, total_pnl_pct ≈ 1.5.
    """
    from atlas.dashboard.api import dashboard as dash_mod

    # ── Build fake config directory ───────────────────────────────────────────
    config_active = tmp_path / "config" / "active"
    config_active.mkdir(parents=True)

    sp500_cfg = {
        "market_id": "sp500",
        "risk": {"starting_equity": 971, "max_open_positions": 10},
    }
    sector_cfg = {"market_id": "sector_etfs", "risk": {"starting_equity": 3216}}
    commodity_cfg = {"market_id": "commodity_etfs", "risk": {"starting_equity": 1001}}
    zero_cfg = {"market_id": "asx", "risk": {"starting_equity": 0}}

    for name, cfg in [
        ("sp500.json", sp500_cfg),
        ("sector_etfs.json", sector_cfg),
        ("commodity_etfs.json", commodity_cfg),
        ("asx.json", zero_cfg),
    ]:
        (config_active / name).write_text(json.dumps(cfg))

    monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

    broker, _counter = _make_broker(equity=5266.0)

    conn = _make_minimal_db()

    with (
        patch("atlas.brokers.registry.get_live_broker", return_value=broker),
        _patch_db(conn),
        patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
    ):
        result = dash_mod._build_dashboard_data()

    account = result["account"]
    expected_total_starting = 971.0 + 3216.0 + 1001.0  # = 5188
    expected_pnl = round(5266.0 - expected_total_starting, 2)   # = 78.0
    expected_pct = round(expected_pnl / expected_total_starting * 100, 2)  # ≈ 1.50

    assert account["total_pnl"] == expected_pnl, (
        f"total_pnl={account['total_pnl']} expected {expected_pnl}"
    )
    assert account["total_pnl_pct"] == expected_pct, (
        f"total_pnl_pct={account['total_pnl_pct']} expected {expected_pct}"
    )
    assert account["starting_equity_total"] == expected_total_starting
    # Summary should mirror the corrected values
    assert result["summary"]["total_pnl"] == expected_pnl
    assert result["summary"]["total_pnl_pct"] == expected_pct


# ══════════════════════════════════════════════════════════════════════════════
# Test 2 — equity curve uses Alpaca portfolio_history
# ══════════════════════════════════════════════════════════════════════════════

def test_equity_curve_uses_alpaca_portfolio_history(tmp_path, monkeypatch):
    """Bug 2: portfolio_history must come from Alpaca (single-account truth).

    _get_portfolio_history returns 5 daily rows. Result must have ≥5 rows,
    all with positive equity, monotonically non-decreasing dates, no zeros.
    """
    import datetime

    from atlas.dashboard.api import dashboard as dash_mod

    # Config dir
    config_active = tmp_path / "config" / "active"
    config_active.mkdir(parents=True)
    sp500_cfg = {
        "market_id": "sp500",
        "risk": {"starting_equity": 5000, "max_open_positions": 10},
    }
    (config_active / "sp500.json").write_text(json.dumps(sp500_cfg))
    monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

    # Build 5-day history from _get_portfolio_history
    today = datetime.date.today()
    fake_history = [
        {
            "date": (today - datetime.timedelta(days=5 - i)).strftime("%Y-%m-%d"),
            "equity": 5100.0 + i * 20,
            "value": 5100.0 + i * 20,
            "day_pnl": 20.0,
        }
        for i in range(5)
    ]

    broker, _ = _make_broker(equity=5266.0)
    conn = _make_minimal_db()

    with (
        patch("atlas.brokers.registry.get_live_broker", return_value=broker),
        _patch_db(conn),
        patch.object(dash_mod, "_get_portfolio_history", return_value=fake_history),
    ):
        result = dash_mod._build_dashboard_data()

    ph = result["portfolio_history"]

    # Rows before PAPER_BOOK_INCEPTION are dropped by design (honest paper-book
    # baseline) — expectations are computed against the post-inception subset.
    from atlas.dashboard.api.dashboard_builder import PAPER_BOOK_INCEPTION
    expected = [h for h in fake_history if h["date"] >= PAPER_BOOK_INCEPTION]
    assert len(ph) >= len(expected), f"Expected ≥{len(expected)} rows, got {len(ph)}"

    # All equities must be positive
    for row in ph:
        assert (row.get("equity") or 0) > 0, f"Zero/negative equity in row: {row}"

    # Dates must be non-decreasing
    dates = [r["date"] for r in ph]
    assert dates == sorted(dates), f"Dates not sorted: {dates}"

    # First row must match the Alpaca data (not a stale per-market DB row)
    if expected:
        assert ph[0]["date"] == expected[0]["date"]
        assert ph[0]["equity"] == expected[0]["equity"]


# ══════════════════════════════════════════════════════════════════════════════
# Test 3 — cache hit within TTL skips broker
# ══════════════════════════════════════════════════════════════════════════════

def test_dashboard_cache_hits_within_ttl(tmp_path, monkeypatch):
    """Third call within TTL must NOT re-invoke broker.get_account_info.

    Call 1: cold cache → broker called.
    Call 2: within 30s TTL → served from cache, broker NOT called again.
    """
    from atlas.dashboard.api import dashboard as dash_mod

    config_active = tmp_path / "config" / "active"
    config_active.mkdir(parents=True)
    sp500_cfg = {
        "market_id": "sp500",
        "risk": {"starting_equity": 5000, "max_open_positions": 10},
    }
    (config_active / "sp500.json").write_text(json.dumps(sp500_cfg))
    monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

    call_counter: list = [0]
    broker, _ = _make_broker(equity=5266.0, account_call_counter=call_counter)
    conn = _make_minimal_db()

    with (
        patch("atlas.brokers.registry.get_live_broker", return_value=broker),
        _patch_db(conn),
        patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
    ):
        # First call — cold cache
        r1 = dash_mod._build_dashboard_data()
        calls_after_first = call_counter[0]

        # Second call — should be cache hit (well within 30-second TTL)
        r2 = dash_mod._build_dashboard_data()
        calls_after_second = call_counter[0]

    assert calls_after_first == 1, (
        f"Expected 1 broker call after cold start, got {calls_after_first}"
    )
    assert calls_after_second == 1, (
        f"Second call should hit cache (no extra broker calls), "
        f"but call_counter went from {calls_after_first} to {calls_after_second}"
    )
    # Both calls return the same object (cache identity)
    assert r1 is r2, "Cache should return the same dict object on hit"
