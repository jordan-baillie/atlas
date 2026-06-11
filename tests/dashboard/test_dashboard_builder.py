"""Tests for services/api/dashboard_builder.py focused builder functions.

Covers:
  1. fetch_broker_state       — parallel RPCs, optional-failure fallbacks
  2. build_account_section    — margin, num_positions, total_pnl, starting_equity
  3. build_positions_section  — trade metadata, intraday enrichment, stop override
  4. build_orders_section     — flatten + normalize
  5. build_equity_curve_section — raw passthrough, SQLite fallback, today append
  6. build_strategy_stats     — strategy_performance, allocation, SPY benchmark
  7. build_pnl_summary        — today_pnl, return_pct, position mutation
  8. _build_dashboard_data    — shape regression (top-level keys + sub-shapes)
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── Shared helpers ────────────────────────────────────────────────────────────

def _make_minimal_db() -> sqlite3.Connection:
    """In-memory SQLite DB with schema needed by dashboard builders."""
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

    with patch("atlas.db.get_db", _fake):
        yield


def _make_account_info(equity: float = 5000.0):
    from atlas.brokers.base import AccountInfo
    return AccountInfo(
        equity=equity,
        cash=500.0,
        market_value=equity - 500.0,
        buying_power=1000.0,
        total_pnl=0.0,
        total_pnl_pct=0.0,
        num_positions=0,
    )


def _make_mock_clock(is_open: bool = False):
    clk = MagicMock()
    clk.is_open = is_open
    clk.next_open = "2026-05-08T09:30:00"
    clk.next_close = "2026-05-08T16:00:00"
    clk.timestamp = "2026-05-07T12:00:00"
    return clk


# ══════════════════════════════════════════════════════════════════════════════
# 1. fetch_broker_state
# ══════════════════════════════════════════════════════════════════════════════

class TestFetchBrokerState:
    """fetch_broker_state runs 8 RPCs in parallel and returns named results."""

    def _make_broker(self, clock=None):
        broker = MagicMock()
        broker.get_account_info.return_value = _make_account_info()
        broker.get_positions.return_value = []
        broker.get_history_orders.return_value = []
        broker.get_open_orders.return_value = []

        _clock = clock or _make_mock_clock()

        def _broker_call(fn, *args):
            fn_name = getattr(fn, "__name__", str(fn))
            if "get_clock" in fn_name or "clock" in str(fn).lower():
                return _clock
            if "get_account" in fn_name and "get_all" not in fn_name:
                m = MagicMock()
                m.initial_margin = 0.0
                m.equity = 5000.0
                return m
            if "get_all_positions" in fn_name:
                return []
            if "get_portfolio_history" in fn_name:
                m = MagicMock()
                m.timestamp = []
                m.equity = []
                m.profit_loss = []
                return m
            return MagicMock()

        broker._broker_call.side_effect = _broker_call
        broker._trade_client = MagicMock()
        broker._trade_client.get_account = MagicMock(__name__="get_account")
        broker._trade_client.get_all_positions = MagicMock(__name__="get_all_positions")
        broker._trade_client.get_clock = MagicMock(__name__="get_clock")
        broker._trade_client.get_portfolio_history = MagicMock(__name__="get_portfolio_history")
        return broker

    def test_returns_all_required_keys(self):
        from atlas.dashboard.api.dashboard_builder import fetch_broker_state

        broker = self._make_broker()
        state = fetch_broker_state(broker, portfolio_history_fn=lambda _b: [])

        required = {
            "account_info", "positions_info", "orders_info",
            "raw_acct", "raw_positions", "open_orders",
            "clock", "portfolio_history_raw",
        }
        assert required.issubset(set(state.keys()))

    def test_optional_rpc_failure_returns_defaults(self):
        from atlas.dashboard.api.dashboard_builder import fetch_broker_state

        broker = self._make_broker()
        # Make optional RPCs fail
        call_count = [0]
        original_side = broker._broker_call.side_effect

        def _side(fn, *args):
            fn_name = getattr(fn, "__name__", str(fn))
            if "get_all_positions" in fn_name:
                raise RuntimeError("positions API down")
            if "get_clock" in fn_name or "clock" in str(fn).lower():
                raise RuntimeError("clock API down")
            return original_side(fn, *args)

        broker._broker_call.side_effect = _side

        state = fetch_broker_state(broker, portfolio_history_fn=lambda _b: [])

        assert state["raw_positions"] is None   # failed → None
        assert state["clock"] is None           # failed → None
        assert state["portfolio_history_raw"] == []

    def test_custom_portfolio_history_fn_is_called(self):
        from atlas.dashboard.api.dashboard_builder import fetch_broker_state

        sentinel = [{"date": "2026-01-01", "equity": 5000.0}]
        ph_fn = MagicMock(return_value=sentinel)

        broker = self._make_broker()
        state = fetch_broker_state(broker, portfolio_history_fn=ph_fn)

        ph_fn.assert_called_once_with(broker)
        assert state["portfolio_history_raw"] is sentinel

    def test_required_rpc_failure_propagates(self):
        from atlas.dashboard.api.dashboard_builder import fetch_broker_state

        broker = self._make_broker()
        broker.get_account_info.side_effect = RuntimeError("account unavailable")

        with pytest.raises(RuntimeError, match="account unavailable"):
            fetch_broker_state(broker, portfolio_history_fn=lambda _b: [])


# ══════════════════════════════════════════════════════════════════════════════
# 2. build_account_section
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildAccountSection:
    """build_account_section builds the account dict with all enriched fields."""

    def test_num_positions_from_positions_list(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_account_section

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "risk": {"starting_equity": 5000}
        }))

        account_info = _make_account_info(equity=5200.0)
        positions = [{"ticker": "AAPL"}, {"ticker": "MSFT"}, {"ticker": "GOOG"}]

        result = build_account_section(account_info, None, positions, config_dir)

        assert result["num_positions"] == 3

    def test_margin_usage_pct_calculated(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_account_section

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({"risk": {}}))

        raw_acct = SimpleNamespace(initial_margin=1000.0, equity=5000.0)
        result = build_account_section(
            _make_account_info(equity=5000.0), raw_acct, [], config_dir
        )

        assert result["margin_usage_pct"] == 20.0

    def test_margin_usage_zero_on_failure(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_account_section

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({"risk": {}}))

        # raw_acct=None → should default to 0
        result = build_account_section(_make_account_info(), None, [], config_dir)
        assert result["margin_usage_pct"] == 0

    def test_starting_equity_aggregated_across_markets(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_account_section

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({"risk": {"starting_equity": 971}}))
        (config_dir / "sector_etfs.json").write_text(json.dumps({"risk": {"starting_equity": 3216}}))
        (config_dir / "commodity_etfs.json").write_text(json.dumps({"risk": {"starting_equity": 1001}}))

        result = build_account_section(
            _make_account_info(equity=5266.0), None, [], config_dir
        )

        assert result["starting_equity_total"] == 5188.0
        assert result["total_pnl"] == round(5266.0 - 5188.0, 2)  # 78.0
        assert result["total_pnl_pct"] == round(78.0 / 5188.0 * 100, 2)

    def test_zero_starting_equity_skipped(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_account_section

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "asx.json").write_text(json.dumps({"risk": {"starting_equity": 0}}))

        result = build_account_section(_make_account_info(equity=1000.0), None, [], config_dir)

        # starting_equity_total = 0 → total_pnl is NOT overridden by starting-equity calc
        assert result["starting_equity_total"] == 0.0
        # equity from AccountInfo (1000.0) is unchanged — not recomputed vs starting_equity
        assert result["equity"] == 1000.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. build_positions_section
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPositionsSection:
    """build_positions_section enriches positions from three passes."""

    def _make_position_info(self, ticker: str, qty: float = 10.0):
        from atlas.brokers.base import PositionInfo
        return PositionInfo(
            ticker=ticker,
            shares=int(qty),
            market_value=qty * 100.0,
            entry_price=100.0,
            current_price=105.0,
            unrealized_pnl=50.0,
            unrealized_pnl_pct=5.0,
        )

    def test_pass1_trade_metadata_from_sqlite(self):
        from atlas.dashboard.api.dashboard_builder import build_positions_section

        conn = _make_minimal_db()
        conn.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, stop_price, exit_date)"
            " VALUES (?,?,?,?,NULL)",
            ("AAPL", "momentum_breakout", "2026-04-01", 190.0),
        )
        conn.commit()

        with _patch_db(conn):
            result = build_positions_section(
                [self._make_position_info("AAPL")], None, []
            )

        assert len(result) == 1
        p = result[0]
        assert p["strategy"] == "momentum_breakout"
        assert p["entry_date"] == "2026-04-01"
        assert p["stop_price"] == 190.0

    def test_pass1_prefers_open_over_closed_trade(self):
        from atlas.dashboard.api.dashboard_builder import build_positions_section

        conn = _make_minimal_db()
        # Closed trade first (ORDER BY is_closed, id DESC → open comes first)
        conn.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, exit_date, stop_price)"
            " VALUES (?,?,?,?,?)",
            ("MSFT", "closed_strategy", "2026-01-01", "2026-02-01", 300.0),
        )
        conn.execute(
            "INSERT INTO trades (ticker, strategy, entry_date, exit_date, stop_price)"
            " VALUES (?,?,?,NULL,?)",
            ("MSFT", "open_strategy", "2026-04-01", 350.0),
        )
        conn.commit()

        with _patch_db(conn):
            result = build_positions_section(
                [self._make_position_info("MSFT")], None, []
            )

        assert result[0]["strategy"] == "open_strategy"

    def test_pass2_alpaca_intraday_enrichment(self):
        from atlas.dashboard.api.dashboard_builder import build_positions_section

        raw_pos = SimpleNamespace(
            symbol="AAPL",
            unrealized_intraday_pl="12.50",
            unrealized_intraday_plpc="0.0250",
            lastday_price="195.00",
        )

        conn = _make_minimal_db()
        with _patch_db(conn):
            with patch("atlas.brokers.alpaca.mapper.to_alpaca", return_value="AAPL"):
                result = build_positions_section(
                    [self._make_position_info("AAPL")], [raw_pos], []
                )

        p = result[0]
        assert p["intraday_pnl"] == 12.5
        assert abs(p["intraday_pnl_pct"] - 2.50) < 0.01
        assert p["lastday_price"] == 195.0

    def test_pass3_stop_override_from_open_orders(self):
        from atlas.dashboard.api.dashboard_builder import build_positions_section

        open_order = SimpleNamespace(
            symbol="AAPL",
            side="sell",
            order_type="stop",
            stop_price="188.0",
        )

        def _asdict():
            return {
                "ticker": "AAPL",
                "side": "sell",
                "order_type": "stop",
                "stop_price": "188.0",
                "raw": {
                    "symbol": "AAPL",
                    "side": "sell",
                    "order_type": "stop",
                    "stop_price": "188.0",
                },
            }
        open_order.asdict = _asdict

        conn = _make_minimal_db()
        with _patch_db(conn):
            result = build_positions_section(
                [self._make_position_info("AAPL")], None, [open_order]
            )

        p = result[0]
        assert p["stop_price"] == 188.0
        assert p["stop_source"] == "broker"

    def test_positions_without_open_order_get_ledger_source(self):
        from atlas.dashboard.api.dashboard_builder import build_positions_section

        conn = _make_minimal_db()
        with _patch_db(conn):
            result = build_positions_section(
                [self._make_position_info("NVDA")], None, []
            )

        assert result[0].get("stop_source") == "ledger"


# ══════════════════════════════════════════════════════════════════════════════
# 4. build_orders_section
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildOrdersSection:
    """build_orders_section flattens order dataclasses into frontend-compatible dicts."""

    def _make_order(self, symbol: str, side: str = "buy"):
        from atlas.brokers.base import OrderResult, OrderStatus, OrderSide
        return OrderResult(
            success=True,
            order_id="ord-001",
            ticker=symbol,
            side=OrderSide.BUY,
            requested_qty=10,
            fill_price=0.0,
            status=OrderStatus.SUBMITTED,
            raw={
                "symbol": symbol,
                "order_type": "limit",
                "qty": "10",
                "submitted_at": "2026-05-01T09:30:00Z",
                "limit_price": "150.00",
                "stop_price": None,
                "trail_price": None,
                "side": side,
                "status": "new",
            },
        )

    def test_flattens_raw_fields(self):
        from atlas.dashboard.api.dashboard_builder import build_orders_section

        orders = build_orders_section([self._make_order("AAPL")])

        assert len(orders) == 1
        o = orders[0]
        assert o["symbol"] == "AAPL"
        assert o["type"] == "limit"
        assert o["side"] == "buy"
        assert o["submitted_at"] == "2026-05-01T09:30:00Z"
        assert o["limit_price"] == 150.0
        assert o["stop_price"] == 0.0

    def test_empty_orders_returns_empty_list(self):
        from atlas.dashboard.api.dashboard_builder import build_orders_section

        assert build_orders_section([]) == []


# ══════════════════════════════════════════════════════════════════════════════
# 5. build_equity_curve_section
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildEquityCurveSection:
    """build_equity_curve_section builds the portfolio_history list."""

    def test_raw_data_passthrough(self):
        from atlas.dashboard.api.dashboard_builder import build_equity_curve_section

        raw = [
            {"date": "2026-06-09", "equity": 5000.0, "value": 5000.0, "day_pnl": 10.0},
            {"date": "2026-06-10", "equity": 5010.0, "value": 5010.0, "day_pnl": 10.0},
        ]
        result = build_equity_curve_section(raw, 5020.0, {"is_open": True})

        # dates before PAPER_BOOK_INCEPTION (2026-06-09) are dropped by design
        assert result[0]["date"] == "2026-06-09"
        assert result[0]["equity"] == 5000.0

    def test_sqlite_fallback_when_raw_empty(self):
        from atlas.dashboard.api.dashboard_builder import build_equity_curve_section

        conn = _make_minimal_db()
        conn.execute(
            "INSERT INTO equity_curve (market_id, date, equity, day_pnl) VALUES (?,?,?,?)",
            ("sp500", "2026-06-09", 4900.0, 5.0),
        )
        conn.commit()

        with _patch_db(conn):
            result = build_equity_curve_section([], 4950.0, {"is_open": False})

        dates = [r["date"] for r in result]
        assert "2026-06-09" in dates

    def test_appends_today_equity_if_not_present(self):
        from datetime import datetime
        from atlas.dashboard.api.dashboard_builder import build_equity_curve_section

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = "2026-05-06"  # known past date

        raw = [{"date": yesterday, "equity": 5000.0, "value": 5000.0, "day_pnl": 0.0}]
        result = build_equity_curve_section(raw, 5050.0, {"is_open": True})

        dates = [r["date"] for r in result]
        assert today in dates
        today_row = next(r for r in result if r["date"] == today)
        assert today_row["equity"] == 5050.0

    def test_updates_existing_today_row(self):
        from datetime import datetime
        from atlas.dashboard.api.dashboard_builder import build_equity_curve_section

        today = datetime.now().strftime("%Y-%m-%d")
        raw = [{"date": today, "equity": 5000.0, "value": 5000.0, "day_pnl": 0.0}]

        result = build_equity_curve_section(raw, 5100.0, {"is_open": True})

        today_row = next(r for r in result if r["date"] == today)
        assert today_row["equity"] == 5100.0

    def test_empty_raw_and_no_db_returns_empty(self):
        from atlas.dashboard.api.dashboard_builder import build_equity_curve_section

        conn = _make_minimal_db()  # empty equity_curve table
        with _patch_db(conn):
            result = build_equity_curve_section([], 0.0, {"is_open": False})

        # live_equity=0 → no today append; DB empty → empty result
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# 6. build_strategy_stats
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildStrategyStats:
    """build_strategy_stats aggregates strategy performance and allocation."""

    def test_strategy_performance_from_closed_trades(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        conn = _make_minimal_db()
        conn.executemany(
            "INSERT INTO trades (ticker, strategy, pnl, exit_date) VALUES (?,?,?,?)",
            [
                ("AAPL", "momentum", 100.0, "2026-04-01"),
                ("MSFT", "momentum", -50.0, "2026-04-02"),
                ("GOOG", "mean_reversion", 80.0, "2026-04-03"),
            ],
        )
        conn.commit()

        with _patch_db(conn):
            result = build_strategy_stats([], [])

        perf = result["strategy_performance"]
        assert "by_strategy" in perf
        assert perf["by_strategy"]["momentum"]["trades"] == 2
        assert perf["by_strategy"]["mean_reversion"]["trades"] == 1

    def test_overall_metrics_computed(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        conn = _make_minimal_db()
        conn.executemany(
            "INSERT INTO trades (ticker, strategy, pnl, exit_date) VALUES (?,?,?,?)",
            [("T1", "s", 100.0, "2026-04-01"), ("T2", "s", -50.0, "2026-04-02")],
        )
        conn.commit()

        with _patch_db(conn):
            result = build_strategy_stats([], [])

        overall = result["strategy_performance"]["overall"]
        assert overall["trades"] == 2
        assert overall["win_rate"] == 0.5
        assert "profit_factor" in overall

    def test_strategy_allocation_from_positions(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        positions = [
            {"strategy": "momentum", "market_value": 2000.0},
            {"strategy": "momentum", "market_value": 1000.0},
            {"strategy": "mean_reversion", "market_value": 500.0},
        ]
        conn = _make_minimal_db()
        with _patch_db(conn):
            result = build_strategy_stats(positions, [])

        alloc = result["strategy_allocation"]
        alloc_by_name = {a["strategy"]: a for a in alloc}
        assert alloc_by_name["momentum"]["value"] == 3000.0
        assert alloc_by_name["mean_reversion"]["value"] == 500.0

    def test_spy_benchmark_built_from_ohlcv(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        conn = _make_minimal_db()
        # Insert SPY rows
        conn.executemany(
            "INSERT INTO ohlcv (ticker, date, close, open, high, low, volume) VALUES (?,?,?,?,?,?,?)",
            [
                ("SPY", "2026-04-01", 500.0, 495.0, 505.0, 490.0, 1000000),
                ("SPY", "2026-04-02", 510.0, 500.0, 515.0, 498.0, 1100000),
                ("SPY", "2026-04-03", 520.0, 510.0, 525.0, 508.0, 1200000),
            ],
        )
        conn.commit()

        portfolio_history = [
            {"date": "2026-04-01", "equity": 5000.0, "value": 5000.0, "day_pnl": 0.0},
            {"date": "2026-04-02", "equity": 5100.0, "value": 5100.0, "day_pnl": 100.0},
            {"date": "2026-04-03", "equity": 5200.0, "value": 5200.0, "day_pnl": 100.0},
        ]

        with _patch_db(conn):
            result = build_strategy_stats([], portfolio_history)

        assert "benchmark" in result
        bench = result["benchmark"]
        assert bench["ticker"] == "SPY"
        assert len(bench["curve"]) == 3
        # SPY scaled to portfolio start: 5000/500=10 → last point = 520*10=5200
        assert bench["curve"][-1]["equity"] == 5200.0

    def test_no_spy_data_no_benchmark(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        conn = _make_minimal_db()  # no SPY in ohlcv
        portfolio_history = [
            {"date": "2026-04-01", "equity": 5000.0, "value": 5000.0, "day_pnl": 0.0},
        ]
        with _patch_db(conn):
            result = build_strategy_stats([], portfolio_history)

        assert "benchmark" not in result

    def test_phantom_trades_excluded(self):
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats

        conn = _make_minimal_db()
        conn.executemany(
            "INSERT INTO trades (ticker, strategy, pnl, exit_date, exit_reason) VALUES (?,?,?,?,?)",
            [
                ("AAPL", "momentum", 100.0, "2026-04-01", None),
                ("MSFT", "momentum", 200.0, "2026-04-02", "reconcile_phantom"),
            ],
        )
        conn.commit()

        with _patch_db(conn):
            result = build_strategy_stats([], [])

        assert result["strategy_performance"]["by_strategy"]["momentum"]["trades"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 7. build_pnl_summary
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPnlSummary:
    """build_pnl_summary computes today_pnl and return_pct."""

    def test_uses_alpaca_intraday_when_available(self):
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary

        positions = [
            {"ticker": "AAPL", "intraday_pnl": 50.0, "intraday_pnl_pct": 1.0,
             "lastday_price": 195.0, "current_price": 197.0, "qty": 10},
        ]
        result = build_pnl_summary(positions, "sp500", [], {})
        assert result["today_pnl"] == 50.0

    def test_falls_back_to_tiingo_when_no_intraday(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary
        import pandas as pd

        # Create fake parquet cache
        cache_dir = tmp_path / "data" / "cache" / "sp500"
        cache_dir.mkdir(parents=True)
        df = pd.DataFrame({"close": [100.0, 102.0]})
        df.to_parquet(cache_dir / "AAPL.parquet")

        positions = [{"ticker": "AAPL", "qty": 10, "intraday_pnl": None}]

        with patch("atlas.dashboard.api.dashboard_builder._PROJECT_ROOT", tmp_path):
            result = build_pnl_summary(positions, "sp500", [], {})

        # 10 shares × ($102 - $100) = $20
        assert result["today_pnl"] == 20.0

    def test_mutates_positions_inplace_for_tiingo(self, tmp_path):
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary
        import pandas as pd

        cache_dir = tmp_path / "data" / "cache" / "sp500"
        cache_dir.mkdir(parents=True)
        df = pd.DataFrame({"close": [100.0, 104.0]})
        df.to_parquet(cache_dir / "GOOG.parquet")

        positions = [{"ticker": "GOOG", "qty": 5, "intraday_pnl": None}]

        with patch("atlas.dashboard.api.dashboard_builder._PROJECT_ROOT", tmp_path):
            build_pnl_summary(positions, "sp500", [], {})

        assert positions[0]["current_price_tiingo"] == 104.0

    def test_return_pct_computed_from_portfolio_history(self):
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary

        ph = [
            {"date": "2026-04-01", "equity": 5000.0, "day_pnl": 0.0},
            {"date": "2026-04-30", "equity": 5250.0, "day_pnl": 10.0},
        ]
        result = build_pnl_summary([], "sp500", ph, {})
        assert result["return_pct"] == 5.0  # (5250/5000 - 1) * 100

    def test_backfills_today_day_pnl_in_portfolio_history(self):
        from datetime import datetime
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary

        today = datetime.now().strftime("%Y-%m-%d")
        ph = [
            {"date": "2026-04-01", "equity": 5000.0, "day_pnl": 0.0},
            {"date": today, "equity": 5100.0, "day_pnl": 0.0},  # will be backfilled
        ]
        positions = [
            {"ticker": "AAPL", "intraday_pnl": 75.0, "intraday_pnl_pct": 1.5,
             "lastday_price": 195.0, "current_price": 197.0, "qty": 10},
        ]
        build_pnl_summary(positions, "sp500", ph, {})

        today_row = next(r for r in ph if r["date"] == today)
        assert today_row["day_pnl"] == 75.0

    def test_max_positions_from_config(self):
        from atlas.dashboard.api.dashboard_builder import build_pnl_summary

        config = {"risk": {"max_open_positions": 15}}
        result = build_pnl_summary([], "sp500", [], config)
        assert result["max_positions"] == 15


# ══════════════════════════════════════════════════════════════════════════════
# 8. _build_dashboard_data integration — shape regression
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildDashboardDataShape:
    """Verify the refactored orchestrator produces the same dict shape as before."""

    @pytest.fixture(autouse=True)
    def reset_cache(self):
        from atlas.dashboard.api import dashboard as dash_mod
        dash_mod._DASHBOARD_CACHE["data"] = None
        dash_mod._DASHBOARD_CACHE["ts"] = 0.0
        yield
        dash_mod._DASHBOARD_CACHE["data"] = None
        dash_mod._DASHBOARD_CACHE["ts"] = 0.0

    def _make_full_broker(self, tmp_path, equity=5266.0):
        broker = MagicMock()
        broker.connect.return_value = True
        broker.get_positions.return_value = []
        broker.get_history_orders.return_value = []
        broker.get_open_orders.return_value = []
        broker.get_account_info.return_value = _make_account_info(equity)

        clock = _make_mock_clock()

        def _broker_call(fn, *args):
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
            return MagicMock()

        broker._broker_call.side_effect = _broker_call
        broker._trade_client = MagicMock()
        broker._trade_client.get_account = MagicMock(__name__="get_account")
        broker._trade_client.get_all_positions = MagicMock(__name__="get_all_positions")
        broker._trade_client.get_clock = MagicMock(__name__="get_clock")
        broker._trade_client.get_portfolio_history = MagicMock(__name__="get_portfolio_history")
        return broker

    def test_top_level_keys_present(self, tmp_path, monkeypatch):
        from atlas.dashboard.api import dashboard as dash_mod

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "market_id": "sp500",
            "risk": {"starting_equity": 5000, "max_open_positions": 10},
        }))
        monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

        broker = self._make_full_broker(tmp_path)
        conn = _make_minimal_db()

        with (
            patch("atlas.brokers.registry.get_live_broker", return_value=broker),
            _patch_db(conn),
            patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
        ):
            result = dash_mod._build_dashboard_data()

        for key in ("account", "positions", "recent_orders", "summary",
                    "market_clock", "portfolio_history", "strategy_performance",
                    "strategy_allocation", "timestamp"):
            assert key in result, f"Missing top-level key: {key!r}"

    def test_account_shape(self, tmp_path, monkeypatch):
        from atlas.dashboard.api import dashboard as dash_mod

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "market_id": "sp500",
            "risk": {"starting_equity": 5000, "max_open_positions": 10},
        }))
        monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

        broker = self._make_full_broker(tmp_path)
        conn = _make_minimal_db()

        with (
            patch("atlas.brokers.registry.get_live_broker", return_value=broker),
            _patch_db(conn),
            patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
        ):
            result = dash_mod._build_dashboard_data()

        account = result["account"]
        for field in ("equity", "cash", "margin_usage_pct", "num_positions",
                      "starting_equity_total"):
            assert field in account, f"Missing account field: {field!r}"

    def test_summary_shape(self, tmp_path, monkeypatch):
        from atlas.dashboard.api import dashboard as dash_mod

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "market_id": "sp500",
            "risk": {"starting_equity": 5000, "max_open_positions": 8},
        }))
        monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

        broker = self._make_full_broker(tmp_path)
        conn = _make_minimal_db()

        with (
            patch("atlas.brokers.registry.get_live_broker", return_value=broker),
            _patch_db(conn),
            patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
        ):
            result = dash_mod._build_dashboard_data()

        summary = result["summary"]
        for field in ("equity", "total_pnl", "total_pnl_pct", "open_positions",
                      "today_pnl", "max_positions"):
            assert field in summary, f"Missing summary field: {field!r}"
        assert summary["max_positions"] == 8

    def test_cache_hit_skips_rebuild(self, tmp_path, monkeypatch):
        from atlas.dashboard.api import dashboard as dash_mod

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "market_id": "sp500",
            "risk": {"starting_equity": 5000, "max_open_positions": 10},
        }))
        monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

        broker = self._make_full_broker(tmp_path)
        conn = _make_minimal_db()

        call_count = [0]
        orig_side = broker.get_account_info.side_effect

        def _counting():
            call_count[0] += 1
            return _make_account_info()

        broker.get_account_info.side_effect = _counting

        with (
            patch("atlas.brokers.registry.get_live_broker", return_value=broker),
            _patch_db(conn),
            patch.object(dash_mod, "_get_portfolio_history", return_value=[]),
        ):
            r1 = dash_mod._build_dashboard_data()
            r2 = dash_mod._build_dashboard_data()

        assert call_count[0] == 1, "Second call should hit cache, not re-invoke broker"
        assert r1 is r2

    def test_broker_failure_returns_graceful_result(self, tmp_path, monkeypatch):
        from atlas.dashboard.api import dashboard as dash_mod

        config_dir = tmp_path / "config" / "active"
        config_dir.mkdir(parents=True)
        (config_dir / "sp500.json").write_text(json.dumps({
            "market_id": "sp500",
            "risk": {"starting_equity": 5000, "max_open_positions": 10},
        }))
        monkeypatch.setattr(dash_mod, "_PROJECT_ROOT", tmp_path)

        conn = _make_minimal_db()

        with (
            patch("atlas.brokers.registry.get_live_broker", side_effect=RuntimeError("no broker")),
            _patch_db(conn),
        ):
            result = dash_mod._build_dashboard_data()

        # Should degrade gracefully, not raise
        assert result["account"] == {}
        assert result["positions"] == []
        assert result["recent_orders"] == []
        assert "timestamp" in result
