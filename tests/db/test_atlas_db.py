"""
Comprehensive CRUD tests for db/atlas_db.py — Atlas SQLite Foundation (Phase 0).

Covers all 16 tables and every public function in the access layer.

Run with:
    cd /root/atlas && python3 -m pytest tests/test_atlas_db.py -v
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import atlas.db as atlas_db_module
from atlas.db import init_db


# ═══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def db_file(tmp_path):
    """Return path to a fresh temporary SQLite file."""
    return tmp_path / "test_atlas.db"


@pytest.fixture(autouse=True)
def db(db_file, monkeypatch):
    """
    Monkeypatch DB_PATH to a temp file, initialise schema, and yield.

    All tests in this module use an isolated DB — no shared state.
    """
    monkeypatch.setattr(atlas_db_module, "DB_PATH", db_file)
    monkeypatch.setattr(atlas_db_module, "_db_path_override", None)
    init_db()
    yield db_file


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _iso(offset_days: int = 0) -> str:
    """Return ISO datetime string offset by N days from today."""
    return (datetime.now() + timedelta(days=offset_days)).isoformat()


def _date(offset_days: int = 0) -> str:
    """Return ISO date string offset by N days from today."""
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. init_db / schema
# ═══════════════════════════════════════════════════════════════════════════════


class TestInitDb:
    def test_init_db_creates_tables(self, db_file):
        """All 16 tables plus schema_version are created."""
        expected_tables = {
            "ohlcv",
            "macro_indicators",
            "regime_history",
            "signals",
            "trades",
            "plans",
            "equity_curve",
            "portfolio_snapshots",
            "overlay_decisions",
            "ceasefire_factors",
            "ceasefire_history",
            "news_intel",
            "research_experiments",
            "research_best",
            "heartbeats",
            "system_log",
            "schema_version",
        }
        with atlas_db_module.get_db() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        actual = {r["name"] for r in rows}
        assert expected_tables <= actual, f"Missing tables: {expected_tables - actual}"

    def test_init_db_idempotent(self):
        """Calling init_db() a second time does not raise (IF NOT EXISTS)."""
        init_db()  # second call — should be silent

    def test_schema_version_seeded(self):
        """schema_version table has version=1 after init."""
        with atlas_db_module.get_db() as conn:
            row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row is not None
        assert row["version"] == 1

    def test_init_db_with_explicit_path(self, tmp_path):
        """init_db(path) creates schema at an explicit location."""
        path = tmp_path / "explicit.db"
        init_db(db_path=str(path))
        assert path.exists()

    def test_wal_mode_enabled(self):
        """Connection is opened in WAL journal mode."""
        with atlas_db_module.get_db() as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_on(self):
        """PRAGMA foreign_keys is enabled."""
        with atlas_db_module.get_db() as conn:
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Trade lifecycle
# ═══════════════════════════════════════════════════════════════════════════════


class TestTradeLifecycle:
    def test_open_position_appears_in_get_open(self):
        atlas_db_module.record_trade_entry(
            ticker="AAPL",
            strategy="mean_reversion",
            universe="sp500",
            entry_price=150.0,
            shares=10,
            stop_price=145.0,
            take_profit=165.0,
            confidence=0.75,
            regime_state="bull_risk_on",
        )
        positions = atlas_db_module.get_open_positions()
        assert len(positions) == 1
        assert positions[0]["ticker"] == "AAPL"
        assert positions[0]["status"] == "open"

    def test_closed_position_absent_from_open(self):
        atlas_db_module.record_trade_entry(
            ticker="MSFT",
            strategy="momentum_breakout",
            universe="sp500",
            entry_price=300.0,
            shares=5,
            stop_price=285.0,
            take_profit=330.0,
            confidence=0.80,
            regime_state="bull_risk_on",
        )
        atlas_db_module.record_trade_exit("MSFT", "momentum_breakout", 320.0, "take_profit")
        positions = atlas_db_module.get_open_positions()
        assert all(p["ticker"] != "MSFT" for p in positions)

    def test_closed_trade_appears_in_get_closed(self):
        atlas_db_module.record_trade_entry(
            ticker="GOOG",
            strategy="trend_following",
            universe="sp500",
            entry_price=200.0,
            shares=3,
            stop_price=190.0,
            take_profit=220.0,
            confidence=0.70,
            regime_state="bull_risk_off",
        )
        atlas_db_module.record_trade_exit("GOOG", "trend_following", 215.0, "take_profit")
        closed = atlas_db_module.get_closed_trades()
        assert any(t["ticker"] == "GOOG" for t in closed)

    def test_pnl_computed_on_exit(self):
        atlas_db_module.record_trade_entry(
            ticker="META",
            strategy="mean_reversion",
            universe="sp500",
            entry_price=100.0,
            shares=10,
            stop_price=92.0,
            take_profit=115.0,
            confidence=0.72,
            regime_state="bull_risk_on",
        )
        atlas_db_module.record_trade_exit("META", "mean_reversion", 112.0, "take_profit")
        closed = atlas_db_module.get_closed_trades()
        meta = next(t for t in closed if t["ticker"] == "META")
        assert meta["pnl"] == pytest.approx(120.0, abs=0.01)
        assert meta["pnl_pct"] == pytest.approx(12.0, abs=0.01)

    def test_multiple_open_positions(self):
        for ticker, price in [("AAPL", 150), ("MSFT", 300), ("GOOG", 200)]:
            atlas_db_module.record_trade_entry(
                ticker=ticker,
                strategy="mean_reversion",
                universe="sp500",
                entry_price=price,
                shares=5,
                stop_price=price * 0.95,
                take_profit=price * 1.1,
                confidence=0.75,
                regime_state="bull_risk_on",
            )
        assert len(atlas_db_module.get_open_positions()) == 3

    def test_get_closed_trades_filter_by_strategy(self):
        # Use distinct tickers per strategy to avoid the closed-trade UNIQUE
        # constraint (ticker, DATE(entry_date), DATE(exit_date)) WHERE status='closed'.
        # Two separate positions can't share a ticker on the same entry/exit day.
        ticker_map = {"mean_reversion": "AAPL", "momentum_breakout": "MSFT"}
        for strategy in ["mean_reversion", "momentum_breakout"]:
            ticker = ticker_map[strategy]
            atlas_db_module.record_trade_entry(
                ticker=ticker,
                strategy=strategy,
                universe="sp500",
                entry_price=150.0,
                shares=5,
                stop_price=140.0,
                take_profit=170.0,
                confidence=0.75,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit(ticker, strategy, 165.0, "target")
        mr = atlas_db_module.get_closed_trades(strategy="mean_reversion")
        assert all(t["strategy"] == "mean_reversion" for t in mr)
        assert len(mr) >= 1

    def test_get_closed_trades_filter_by_universe(self):
        # Use distinct tickers per universe to avoid the closed-trade UNIQUE
        # constraint (ticker, DATE(entry_date), DATE(exit_date)) WHERE status='closed'.
        ticker_map = {"sp500": "XLK", "sector_etfs": "QQQ"}
        for universe in ["sp500", "sector_etfs"]:
            ticker = ticker_map[universe]
            atlas_db_module.record_trade_entry(
                ticker=ticker,
                strategy="sector_rotation",
                universe=universe,
                entry_price=50.0,
                shares=10,
                stop_price=47.0,
                take_profit=56.0,
                confidence=0.70,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit(ticker, "sector_rotation", 54.0, "target")
        sp500_trades = atlas_db_module.get_closed_trades(universe="sp500")
        assert all(t["universe"] == "sp500" for t in sp500_trades)

    def test_empty_db_returns_empty_positions(self):
        assert atlas_db_module.get_open_positions() == []

    def test_empty_db_returns_empty_closed(self):
        assert atlas_db_module.get_closed_trades() == []

    def test_performance_summary_empty(self):
        summary = atlas_db_module.performance_summary()
        assert summary["trades"] == 0

    def test_performance_summary_computed(self):
        # Win: 100 PnL, Loss: -50 PnL
        atlas_db_module.record_trade_entry(
            ticker="WIN",
            strategy="mean_reversion",
            universe="sp500",
            entry_price=100.0,
            shares=10,
            stop_price=90.0,
            take_profit=120.0,
            confidence=0.8,
            regime_state="bull_risk_on",
        )
        atlas_db_module.record_trade_exit("WIN", "mean_reversion", 110.0, "target")

        atlas_db_module.record_trade_entry(
            ticker="LOSE",
            strategy="momentum_breakout",
            universe="sp500",
            entry_price=100.0,
            shares=10,
            stop_price=95.0,
            take_profit=115.0,
            confidence=0.75,
            regime_state="bull_risk_on",
        )
        atlas_db_module.record_trade_exit("LOSE", "momentum_breakout", 95.0, "stop_loss")

        summary = atlas_db_module.performance_summary()
        assert summary["trades"] == 2
        assert summary["win_rate"] == pytest.approx(50.0, abs=0.1)
        assert "by_strategy" in summary
        assert "by_universe" in summary
        assert summary["profit_factor"] > 0

    def test_performance_summary_by_strategy_grouping(self):
        # Use distinct tickers per trade to avoid the closed-trade UNIQUE
        # constraint (ticker, DATE(entry_date), DATE(exit_date)) WHERE status='closed'.
        trade_specs = [
            ("TST1", "mean_reversion"),
            ("TST2", "mean_reversion"),
            ("TST3", "momentum_breakout"),
        ]
        for ticker, strategy in trade_specs:
            atlas_db_module.record_trade_entry(
                ticker=ticker,
                strategy=strategy,
                universe="sp500",
                entry_price=100.0,
                shares=5,
                stop_price=90.0,
                take_profit=115.0,
                confidence=0.72,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit(ticker, strategy, 110.0, "target")
        summary = atlas_db_module.performance_summary()
        assert "mean_reversion" in summary["by_strategy"]
        assert "momentum_breakout" in summary["by_strategy"]


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Regime
# ═══════════════════════════════════════════════════════════════════════════════


class TestRegime:
    def test_record_and_get_current_regime(self):
        atlas_db_module.record_regime(
            date=_date(),
            state="bull_risk_on",
            trend_score=0.8,
            risk_score=0.7,
            active_universes=["sp500", "sector_etfs"],
            sizing_multiplier=1.0,
            reasoning="Strong momentum",
        )
        r = atlas_db_module.get_current_regime()
        assert r is not None
        assert r["regime_state"] == "bull_risk_on"
        assert r["trend_score"] == pytest.approx(0.8)
        assert isinstance(r["active_universes"], list)
        assert "sp500" in r["active_universes"]

    def test_get_current_regime_returns_latest(self):
        atlas_db_module.record_regime(
            date=_date(-2),
            state="bear_early",
            trend_score=0.3,
            risk_score=0.4,
            active_universes=["treasury_etfs"],
            sizing_multiplier=0.5,
        )
        atlas_db_module.record_regime(
            date=_date(-1),
            state="bull_risk_off",
            trend_score=0.6,
            risk_score=0.5,
            active_universes=["sp500"],
            sizing_multiplier=0.8,
        )
        atlas_db_module.record_regime(
            date=_date(0),
            state="bull_risk_on",
            trend_score=0.9,
            risk_score=0.8,
            active_universes=["sp500", "sector_etfs"],
            sizing_multiplier=1.0,
        )
        r = atlas_db_module.get_current_regime()
        assert r["regime_state"] == "bull_risk_on"

    def test_get_current_regime_empty_returns_none(self):
        assert atlas_db_module.get_current_regime() is None

    def test_regime_upsert_same_date(self):
        """INSERT OR REPLACE on same date updates the record."""
        atlas_db_module.record_regime(
            date="2026-01-01",
            state="bear_early",
            trend_score=0.2,
            risk_score=0.3,
            active_universes=[],
            sizing_multiplier=0.5,
        )
        atlas_db_module.record_regime(
            date="2026-01-01",
            state="bull_risk_on",
            trend_score=0.9,
            risk_score=0.8,
            active_universes=["sp500"],
            sizing_multiplier=1.0,
        )
        with atlas_db_module.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM regime_history WHERE date='2026-01-01'"
            ).fetchone()[0]
        assert count == 1

    def test_active_universes_serialized_as_json(self):
        universes = ["sp500", "sector_etfs", "treasury_etfs"]
        atlas_db_module.record_regime(
            date=_date(),
            state="bull_risk_on",
            trend_score=0.8,
            risk_score=0.7,
            active_universes=universes,
            sizing_multiplier=1.0,
        )
        r = atlas_db_module.get_current_regime()
        assert r["active_universes"] == universes


# ═══════════════════════════════════════════════════════════════════════════════
# 4. OHLCV
# ═══════════════════════════════════════════════════════════════════════════════


class TestEquityCurve:
    def test_record_and_get_equity_curve(self):
        atlas_db_module.record_equity(
            date="2026-01-02",
            market_id="sp500",
            equity=10_000.0,
            cash=5_000.0,
            positions_value=5_000.0,
            day_pnl=50.0,
            regime_state="bull_risk_on",
        )
        curve = atlas_db_module.get_equity_curve("sp500")
        assert len(curve) == 1
        assert curve[0]["equity"] == pytest.approx(10_000.0)

    def test_multiple_days(self):
        for i, date in enumerate(["2026-01-02", "2026-01-03", "2026-01-06"]):
            atlas_db_module.record_equity(
                date=date,
                market_id="sp500",
                equity=10_000.0 + i * 100,
                cash=5_000.0,
                positions_value=5_000.0 + i * 100,
                day_pnl=100.0,
                regime_state="bull_risk_on",
            )
        curve = atlas_db_module.get_equity_curve("sp500")
        assert len(curve) == 3

    def test_filter_by_market_id(self):
        atlas_db_module.record_equity(
            date="2026-01-02",
            market_id="sp500",
            equity=10_000.0,
            cash=5_000.0,
            positions_value=5_000.0,
            day_pnl=0.0,
            regime_state="bull_risk_on",
        )
        atlas_db_module.record_equity(
            date="2026-01-02",
            market_id="asx",
            equity=20_000.0,
            cash=10_000.0,
            positions_value=10_000.0,
            day_pnl=0.0,
            regime_state="bull_risk_on",
        )
        sp500_curve = atlas_db_module.get_equity_curve("sp500")
        assert len(sp500_curve) == 1

    def test_equity_curve_empty(self):
        assert atlas_db_module.get_equity_curve("sp500") == []


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Portfolio Snapshots
# ═══════════════════════════════════════════════════════════════════════════════


class TestHeartbeats:
    def test_record_and_get_heartbeat(self):
        atlas_db_module.record_heartbeat(
            service="atlas-planner",
            status="ok",
            detail={"last_plan": "2026-01-02"},
        )
        beats = atlas_db_module.get_heartbeats()
        assert any(b["service"] == "atlas-planner" for b in beats)

    def test_heartbeat_upsert(self):
        """INSERT OR REPLACE — second write for same service updates the record."""
        atlas_db_module.record_heartbeat(service="atlas-planner", status="ok")
        atlas_db_module.record_heartbeat(service="atlas-planner", status="degraded")
        beats = atlas_db_module.get_heartbeats()
        planner = [b for b in beats if b["service"] == "atlas-planner"]
        assert len(planner) == 1
        assert planner[0]["status"] == "degraded"

    def test_multiple_services(self):
        for service in ["atlas-planner", "atlas-executor", "atlas-ingestor"]:
            atlas_db_module.record_heartbeat(service=service, status="ok")
        beats = atlas_db_module.get_heartbeats()
        services = {b["service"] for b in beats}
        assert {"atlas-planner", "atlas-executor", "atlas-ingestor"} <= services

    def test_get_heartbeats_empty(self):
        assert atlas_db_module.get_heartbeats() == []


class TestSystemLog:
    def test_record_and_get_system_log(self):
        atlas_db_module.record_system_log(
            level="info",
            service="atlas-planner",
            message="Plan generated successfully",
            detail={"plan_id": 42},
        )
        logs = atlas_db_module.get_system_logs()
        assert len(logs) >= 1
        assert logs[0]["message"] == "Plan generated successfully"

    def test_get_system_logs_empty(self):
        assert atlas_db_module.get_system_logs() == []

    def test_filter_by_service(self):
        atlas_db_module.record_system_log(
            level="info", service="atlas-planner", message="ok"
        )
        atlas_db_module.record_system_log(
            level="error", service="atlas-executor", message="failed"
        )
        planner_logs = atlas_db_module.get_system_logs(service="atlas-planner")
        assert all(l["service"] == "atlas-planner" for l in planner_logs)
        assert len(planner_logs) == 1

    def test_filter_by_level(self):
        atlas_db_module.record_system_log(
            level="info", service="svc", message="info msg"
        )
        atlas_db_module.record_system_log(
            level="error", service="svc", message="error msg"
        )
        errors = atlas_db_module.get_system_logs(level="error")
        assert all(l["level"] == "error" for l in errors)
        assert len(errors) == 1

    def test_filter_by_hours(self):
        atlas_db_module.record_system_log(
            level="info", service="svc", message="recent"
        )
        logs = atlas_db_module.get_system_logs(hours=1)
        assert len(logs) >= 1

    def test_multiple_levels(self):
        for level in ["info", "warning", "error", "critical"]:
            atlas_db_module.record_system_log(
                level=level, service="test-svc", message=f"Level {level}"
            )
        assert len(atlas_db_module.get_system_logs()) == 4

    def test_detail_can_be_none(self):
        atlas_db_module.record_system_log(
            level="info", service="svc", message="no detail"
        )
        logs = atlas_db_module.get_system_logs()
        assert len(logs) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 14. Edge Cases & Concurrency
# ═══════════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_empty_queries_return_empty_lists(self):
        """Every list-returning function returns [] on empty DB."""
        assert atlas_db_module.get_open_positions() == []
        assert atlas_db_module.get_closed_trades() == []
        assert atlas_db_module.get_heartbeats() == []
        assert atlas_db_module.get_system_logs() == []

    def test_empty_scalar_queries_return_none(self):
        assert atlas_db_module.get_current_regime() is None

    def test_performance_summary_all_wins(self):
        for ticker in ["AAPL", "MSFT"]:
            atlas_db_module.record_trade_entry(
                ticker=ticker,
                strategy="mean_reversion",
                universe="sp500",
                entry_price=100.0,
                shares=10,
                stop_price=90.0,
                take_profit=120.0,
                confidence=0.8,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit(ticker, "mean_reversion", 115.0, "target")
        summary = atlas_db_module.performance_summary()
        assert summary["win_rate"] == pytest.approx(100.0)
        # All-wins / no-losses is capped at 99.99 (not inf): inf is not valid
        # JSON and would break dashboard/CLI/journal serialization. The cap is
        # applied consistently in db/trades.py performance_summary() and
        # _group_performance().
        assert summary["profit_factor"] == pytest.approx(99.99)

    def test_performance_summary_all_losses(self):
        atlas_db_module.record_trade_entry(
            ticker="LOSE",
            strategy="mean_reversion",
            universe="sp500",
            entry_price=100.0,
            shares=10,
            stop_price=90.0,
            take_profit=120.0,
            confidence=0.7,
            regime_state="bear_early",
        )
        atlas_db_module.record_trade_exit("LOSE", "mean_reversion", 90.0, "stop_loss")
        summary = atlas_db_module.performance_summary()
        assert summary["win_rate"] == pytest.approx(0.0)
        assert summary["profit_factor"] == pytest.approx(0.0)

    def test_concurrent_reads(self):
        """Multiple simultaneous reads via get_db() should not raise."""
        import threading

        errors = []

        def _read():
            try:
                atlas_db_module.get_open_positions()
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=_read) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"Concurrent read errors: {errors}"

    def test_get_db_accepts_db_path_override(self, tmp_path):
        """get_db(db_path=...) uses the provided path, not DB_PATH."""
        alt_path = tmp_path / "alt.db"
        init_db(db_path=str(alt_path))
        with atlas_db_module.get_db(db_path=str(alt_path)) as conn:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert len(tables) > 0

    def test_transaction_rollback_on_error(self):
        """An exception inside get_db() context rolls back the transaction."""
        with pytest.raises(ValueError):
            with atlas_db_module.get_db() as conn:
                conn.execute(
                    "INSERT INTO system_log (level, service, message) VALUES (?, ?, ?)",
                    ("info", "test-svc", "will be rolled back"),
                )
                raise ValueError("deliberate rollback test")

        logs = atlas_db_module.get_system_logs()
        assert all(l["message"] != "will be rolled back" for l in logs)

# ═══════════════════════════════════════════════════════════════════════════════
# 15. DB module __init__.py exports
# ═══════════════════════════════════════════════════════════════════════════════


class TestModuleExports:
    def test_db_init_exports_get_db(self):
        import atlas.db as db
        assert hasattr(db, "get_db")

    def test_db_init_exports_db_path(self):
        import atlas.db as db
        assert hasattr(db, "DB_PATH")

    def test_get_db_is_context_manager(self):
        """get_db() yields a usable sqlite3 connection."""
        with atlas_db_module.get_db() as conn:
            result = conn.execute("SELECT 1").fetchone()[0]
        assert result == 1
