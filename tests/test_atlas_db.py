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
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as atlas_db_module
from db.atlas_db import init_db


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
        for strategy in ["mean_reversion", "momentum_breakout"]:
            atlas_db_module.record_trade_entry(
                ticker="AAPL",
                strategy=strategy,
                universe="sp500",
                entry_price=150.0,
                shares=5,
                stop_price=140.0,
                take_profit=170.0,
                confidence=0.75,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit("AAPL", strategy, 165.0, "target")
        mr = atlas_db_module.get_closed_trades(strategy="mean_reversion")
        assert all(t["strategy"] == "mean_reversion" for t in mr)
        assert len(mr) >= 1

    def test_get_closed_trades_filter_by_universe(self):
        for universe in ["sp500", "sector_etfs"]:
            atlas_db_module.record_trade_entry(
                ticker="XLK",
                strategy="sector_rotation",
                universe=universe,
                entry_price=50.0,
                shares=10,
                stop_price=47.0,
                take_profit=56.0,
                confidence=0.70,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit("XLK", "sector_rotation", 54.0, "target")
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
        for strategy in ["mean_reversion", "mean_reversion", "momentum_breakout"]:
            atlas_db_module.record_trade_entry(
                ticker="TEST",
                strategy=strategy,
                universe="sp500",
                entry_price=100.0,
                shares=5,
                stop_price=90.0,
                take_profit=115.0,
                confidence=0.72,
                regime_state="bull_risk_on",
            )
            atlas_db_module.record_trade_exit("TEST", strategy, 110.0, "target")
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


class TestOHLCV:
    def _insert_aapl(self):
        atlas_db_module.upsert_ohlcv(
            ticker="AAPL",
            date="2026-01-02",
            o=180.0,
            h=185.0,
            l=178.0,
            c=183.0,
            adj=183.0,
            vol=50_000_000,
            universe="sp500",
        )

    def test_upsert_and_get_ohlcv(self):
        self._insert_aapl()
        df = atlas_db_module.get_ohlcv("AAPL")
        assert isinstance(df, pd.DataFrame)
        assert not df.empty
        assert "close" in df.columns

    def test_get_ohlcv_date_index(self):
        self._insert_aapl()
        df = atlas_db_module.get_ohlcv("AAPL")
        assert df.index.name == "date" or df.index.dtype == "datetime64[ns]" or df.index.name == "date"

    def test_upsert_overwrites_same_key(self):
        self._insert_aapl()
        atlas_db_module.upsert_ohlcv(
            ticker="AAPL",
            date="2026-01-02",
            o=181.0,
            h=186.0,
            l=179.0,
            c=184.0,
            adj=184.0,
            vol=55_000_000,
            universe="sp500",
        )
        df = atlas_db_module.get_ohlcv("AAPL")
        assert len(df) == 1
        # Updated close
        assert df["close"].iloc[0] == pytest.approx(184.0)

    def test_get_ohlcv_with_date_filter(self):
        for i, date in enumerate(["2026-01-02", "2026-01-03", "2026-01-05"]):
            atlas_db_module.upsert_ohlcv(
                ticker="AAPL",
                date=date,
                o=180.0 + i,
                h=185.0 + i,
                l=178.0 + i,
                c=183.0 + i,
                adj=183.0 + i,
                vol=50_000_000,
                universe="sp500",
            )
        df = atlas_db_module.get_ohlcv("AAPL", start_date="2026-01-03")
        assert len(df) == 2

    def test_get_ohlcv_unknown_ticker_returns_empty(self):
        df = atlas_db_module.get_ohlcv("UNKNOWN")
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_get_universe_data(self):
        for ticker, universe in [("AAPL", "sp500"), ("MSFT", "sp500"), ("XLK", "sector_etfs")]:
            atlas_db_module.upsert_ohlcv(
                ticker=ticker,
                date="2026-01-02",
                o=100.0,
                h=105.0,
                l=98.0,
                c=103.0,
                adj=103.0,
                vol=1_000_000,
                universe=universe,
            )
        data = atlas_db_module.get_universe_data("sp500")
        assert isinstance(data, dict)
        assert "AAPL" in data
        assert "MSFT" in data
        assert "XLK" not in data

    def test_upsert_ohlcv_custom_source(self):
        atlas_db_module.upsert_ohlcv(
            ticker="SPY",
            date="2026-01-02",
            o=500.0,
            h=505.0,
            l=498.0,
            c=503.0,
            adj=503.0,
            vol=80_000_000,
            universe="sp500",
            source="yfinance",
        )
        with atlas_db_module.get_db() as conn:
            row = conn.execute(
                "SELECT source FROM ohlcv WHERE ticker='SPY'"
            ).fetchone()
        assert row["source"] == "yfinance"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Signals
# ═══════════════════════════════════════════════════════════════════════════════


class TestSignals:
    def _record(self, ticker="AAPL", strategy="mean_reversion", action="accepted",
                market_id="sp500"):
        atlas_db_module.record_signal(
            timestamp=_iso(),
            ticker=ticker,
            strategy=strategy,
            universe="sp500",
            direction="long",
            entry_price=150.0,
            stop_price=143.0,
            take_profit=165.0,
            position_size=10,
            position_value=1500.0,
            risk_amount=70.0,
            confidence=0.76,
            rationale="RSI oversold",
            features={"rsi": 28.0, "zscore": -2.3},
            sector="Technology",
            regime_state="bull_risk_on",
            action=action,
            market_id=market_id,
        )

    def test_record_and_get_signals(self):
        self._record()
        signals = atlas_db_module.get_signals()
        assert len(signals) == 1
        assert signals[0]["ticker"] == "AAPL"

    def test_get_signals_empty(self):
        assert atlas_db_module.get_signals() == []

    def test_filter_by_strategy(self):
        self._record(ticker="AAPL", strategy="mean_reversion")
        self._record(ticker="MSFT", strategy="momentum_breakout")
        result = atlas_db_module.get_signals(strategy="mean_reversion")
        assert all(s["strategy"] == "mean_reversion" for s in result)
        assert len(result) == 1

    def test_filter_by_ticker(self):
        self._record(ticker="AAPL")
        self._record(ticker="MSFT")
        result = atlas_db_module.get_signals(ticker="AAPL")
        assert all(s["ticker"] == "AAPL" for s in result)
        assert len(result) == 1

    def test_features_deserialized(self):
        self._record()
        signals = atlas_db_module.get_signals()
        features = signals[0].get("features")
        if isinstance(features, str):
            features = json.loads(features)
        assert isinstance(features, dict)
        assert "rsi" in features

    def test_filter_by_days(self):
        self._record()
        result_all = atlas_db_module.get_signals(days=30)
        result_none = atlas_db_module.get_signals(days=0)
        # All of today's signals should appear in a 30-day window
        assert len(result_all) >= 1
        assert len(result_none) == 0 or len(result_none) <= len(result_all)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Plans
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlans:
    def _record(self, date=None, market_id="sp500", status="pending"):
        d = date or _date()
        atlas_db_module.record_plan(
            date=d,
            market_id=market_id,
            plan_data={"signals": [{"ticker": "AAPL"}], "risk_summary": {}},
            regime_state="bull_risk_on",
            active_universes=["sp500"],
            sizing_multiplier=1.0,
        )
        return d

    def test_record_and_get_plan(self):
        d = self._record()
        plan = atlas_db_module.get_plan(d, "sp500")
        assert plan is not None
        assert plan["market_id"] == "sp500"

    def test_get_plan_not_found(self):
        result = atlas_db_module.get_plan("2000-01-01", "sp500")
        assert result is None

    def test_get_plans_all(self):
        self._record(date="2026-01-01")
        self._record(date="2026-01-02")
        plans = atlas_db_module.get_plans()
        assert len(plans) >= 2

    def test_update_plan_status(self):
        d = self._record()
        plan = atlas_db_module.get_plan(d, "sp500")
        atlas_db_module.update_plan_status(plan["id"], "approved")
        updated = atlas_db_module.get_plan(d, "sp500")
        assert updated["status"] == "approved"

    def test_get_plans_filter_by_status(self):
        self._record(date="2026-01-03")
        plan = atlas_db_module.get_plan("2026-01-03", "sp500")
        atlas_db_module.update_plan_status(plan["id"], "approved")
        self._record(date="2026-01-04")

        approved = atlas_db_module.get_plans(status="approved")
        assert all(p["status"] == "approved" for p in approved)
        assert len(approved) >= 1

    def test_plan_data_serialized(self):
        self._record()
        plan = atlas_db_module.get_plan(_date(), "sp500")
        plan_data = plan.get("plan_data")
        if isinstance(plan_data, str):
            plan_data = json.loads(plan_data)
        assert isinstance(plan_data, dict)
        assert "signals" in plan_data

    def test_get_plans_empty(self):
        assert atlas_db_module.get_plans() == []


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Equity Curve
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


class TestPortfolioSnapshots:
    def _record(self, total_equity=10_000.0):
        atlas_db_module.record_snapshot(
            timestamp=_iso(),
            total_equity=total_equity,
            cash=5_000.0,
            positions=[{"ticker": "AAPL", "shares": 10, "value": 5_000.0}],
            exposure_by_universe={"sp500": 0.5},
            exposure_by_sector={"technology": 0.5},
            regime_state="bull_risk_on",
            source="eod",
        )

    def test_record_and_get_latest(self):
        self._record(total_equity=10_000.0)
        snap = atlas_db_module.get_latest_snapshot()
        assert snap is not None
        assert snap["total_equity"] == pytest.approx(10_000.0)

    def test_get_latest_returns_most_recent(self):
        self._record(total_equity=10_000.0)
        time.sleep(0.01)
        self._record(total_equity=11_000.0)
        snap = atlas_db_module.get_latest_snapshot()
        assert snap["total_equity"] == pytest.approx(11_000.0)

    def test_get_snapshots_multiple(self):
        self._record(10_000.0)
        self._record(11_000.0)
        snaps = atlas_db_module.get_snapshots()
        assert len(snaps) >= 2

    def test_positions_deserialized(self):
        self._record()
        snap = atlas_db_module.get_latest_snapshot()
        positions = snap.get("positions")
        if isinstance(positions, str):
            positions = json.loads(positions)
        assert isinstance(positions, list)

    def test_get_latest_empty(self):
        assert atlas_db_module.get_latest_snapshot() is None

    def test_get_snapshots_empty(self):
        assert atlas_db_module.get_snapshots() == []


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Overlay Decisions
# ═══════════════════════════════════════════════════════════════════════════════


class TestOverlayDecisions:
    def _record(self, action="no_change"):
        atlas_db_module.record_overlay_decision(
            timestamp=_iso(),
            regime_state="bull_risk_on",
            action=action,
            sizing_override=None,
            universes_deactivated=[],
            tickers_avoided=["TSLA"],
            reasoning="High risk detected",
            confidence=0.8,
            data_sources={"news": True, "vix": 22.5},
        )

    def test_record_and_get_overlay(self):
        self._record()
        decisions = atlas_db_module.get_overlay_decisions()
        assert len(decisions) == 1
        assert decisions[0]["action"] == "no_change"

    def test_get_overlay_empty(self):
        assert atlas_db_module.get_overlay_decisions() == []

    def test_record_tighten_action(self):
        self._record(action="tighten")
        decisions = atlas_db_module.get_overlay_decisions()
        assert decisions[0]["action"] == "tighten"

    def test_update_overlay_outcome(self):
        self._record()
        decisions = atlas_db_module.get_overlay_decisions()
        overlay_id = decisions[0]["id"]
        atlas_db_module.update_overlay_outcome(
            overlay_id=overlay_id,
            outcome_correct=1,
            outcome_notes="Correctly avoided TSLA drawdown",
        )
        updated = atlas_db_module.get_overlay_decisions()
        record = next(r for r in updated if r["id"] == overlay_id)
        assert record["outcome_evaluated"] == 1
        assert record["outcome_correct"] == 1

    def test_filter_by_days(self):
        self._record()
        result = atlas_db_module.get_overlay_decisions(days=30)
        assert len(result) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Ceasefire (Geopolitical Monitor)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCeasefire:
    def test_upsert_and_get_ceasefire_factor(self):
        atlas_db_module.upsert_ceasefire_factor(
            factor_id="iran_nuclear_talks",
            category="ceasefire",
            description="Iran nuclear negotiations ongoing",
            weight=0.3,
            active=1,
            confidence="medium",
            source="reuters",
            last_updated=_iso(),
        )
        factors = atlas_db_module.get_ceasefire_factors()
        assert len(factors) == 1
        assert factors[0]["id"] == "iran_nuclear_talks"

    def test_upsert_overwrites_factor(self):
        for weight in [0.3, 0.5]:
            atlas_db_module.upsert_ceasefire_factor(
                factor_id="iran_nuclear_talks",
                category="ceasefire",
                description="Iran nuclear negotiations",
                weight=weight,
                active=1,
            )
        factors = atlas_db_module.get_ceasefire_factors()
        assert len(factors) == 1
        assert factors[0]["weight"] == pytest.approx(0.5)

    def test_get_ceasefire_factors_empty(self):
        assert atlas_db_module.get_ceasefire_factors() == []

    def test_record_ceasefire_history(self):
        atlas_db_module.record_ceasefire_history(
            timestamp=_iso(),
            probability=0.35,
            active_factors=["iran_nuclear_talks"],
            change_log="New factor added",
        )
        history = atlas_db_module.get_ceasefire_history()
        assert len(history) == 1
        assert history[0]["probability"] == pytest.approx(0.35)

    def test_get_ceasefire_history_empty(self):
        assert atlas_db_module.get_ceasefire_history() == []

    def test_ceasefire_history_active_factors_deserialized(self):
        atlas_db_module.record_ceasefire_history(
            timestamp=_iso(),
            probability=0.4,
            active_factors=["factor_a", "factor_b"],
        )
        history = atlas_db_module.get_ceasefire_history()
        factors = history[0].get("active_factors")
        if isinstance(factors, str):
            factors = json.loads(factors)
        assert isinstance(factors, list)

    def test_multiple_ceasefire_factors(self):
        for fid, cat in [("a", "ceasefire"), ("b", "escalation"), ("c", "ceasefire")]:
            atlas_db_module.upsert_ceasefire_factor(
                factor_id=fid,
                category=cat,
                description=f"Factor {fid}",
                weight=0.2,
            )
        assert len(atlas_db_module.get_ceasefire_factors()) == 3


# ═══════════════════════════════════════════════════════════════════════════════
# 11. News Intel
# ═══════════════════════════════════════════════════════════════════════════════


class TestNewsIntel:
    def _record(self, category="macro", timestamp=None):
        atlas_db_module.record_news(
            timestamp=timestamp or _iso(),
            source="brave",
            headline="Fed holds rates steady",
            url="https://example.com/news/1",
            relevance_score=0.9,
            category=category,
            summary="Federal Reserve maintains interest rates at current levels.",
        )

    def test_record_and_get_news(self):
        self._record()
        news = atlas_db_module.get_news()
        assert len(news) == 1
        assert news[0]["headline"] == "Fed holds rates steady"

    def test_get_news_empty(self):
        assert atlas_db_module.get_news() == []

    def test_filter_by_category(self):
        self._record(category="macro")
        self._record(category="iran")
        self._record(category="earnings")
        macro = atlas_db_module.get_news(category="macro")
        assert all(n["category"] == "macro" for n in macro)
        assert len(macro) == 1

    def test_filter_by_days(self):
        self._record()
        result = atlas_db_module.get_news(days=7)
        assert len(result) >= 1

    def test_multiple_categories(self):
        for category in ["macro", "iran", "earnings", "fed"]:
            self._record(category=category)
        assert len(atlas_db_module.get_news()) == 4


# ═══════════════════════════════════════════════════════════════════════════════
# 12. Research Experiments + Best
# ═══════════════════════════════════════════════════════════════════════════════


class TestResearch:
    def _record_exp(self, exp_id="exp-001", strategy="mean_reversion",
                    status="running", sharpe=None):
        atlas_db_module.record_experiment(
            experiment_id=exp_id,
            strategy=strategy,
            universe="sp500",
            experiment_type="param_sweep",
            params_changed={"rsi_period": 14, "zscore_entry": -2.0},
            description="RSI period sweep",
            sharpe=sharpe,
            trades=45,
            max_dd_pct=8.5,
            profit_factor=1.4,
            cagr_pct=12.3,
            status=status,
            baseline_sharpe=0.85,
        )

    def test_record_and_get_experiments(self):
        self._record_exp()
        exps = atlas_db_module.get_experiments()
        assert len(exps) == 1
        assert exps[0]["strategy"] == "mean_reversion"

    def test_get_experiments_empty(self):
        assert atlas_db_module.get_experiments() == []

    def test_filter_by_strategy(self):
        self._record_exp(exp_id="e1", strategy="mean_reversion")
        self._record_exp(exp_id="e2", strategy="momentum_breakout")
        result = atlas_db_module.get_experiments(strategy="mean_reversion")
        assert all(e["strategy"] == "mean_reversion" for e in result)
        assert len(result) == 1

    def test_filter_by_status(self):
        self._record_exp(exp_id="e1", status="running")
        self._record_exp(exp_id="e2", status="kept")
        kept = atlas_db_module.get_experiments(status="kept")
        assert all(e["status"] == "kept" for e in kept)
        assert len(kept) == 1

    def test_update_experiment_status(self):
        self._record_exp(exp_id="e1")
        atlas_db_module.update_experiment_status(
            experiment_id="e1",
            status="kept",
            recommendation="Use rsi_period=14 for sp500",
            sharpe=1.05,
        )
        exps = atlas_db_module.get_experiments(status="kept")
        assert len(exps) == 1
        assert exps[0]["recommendation"] == "Use rsi_period=14 for sp500"
        assert exps[0]["sharpe"] == pytest.approx(1.05)

    def test_upsert_research_best(self):
        atlas_db_module.upsert_research_best(
            strategy="mean_reversion",
            universe="sp500",
            params={"rsi_period": 14, "zscore_entry": -2.0},
            sharpe=1.05,
            trades=45,
            max_dd_pct=8.5,
        )
        best = atlas_db_module.get_research_best()
        assert len(best) >= 1

    def test_upsert_research_best_overwrites(self):
        for sharpe in [0.85, 1.10]:
            atlas_db_module.upsert_research_best(
                strategy="mean_reversion",
                universe="sp500",
                params={"rsi_period": 14},
                sharpe=sharpe,
            )
        with atlas_db_module.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM research_best "
                "WHERE strategy='mean_reversion' AND universe='sp500'"
            ).fetchone()[0]
        assert count == 1

    def test_get_research_best_filter_by_strategy(self):
        atlas_db_module.upsert_research_best(
            strategy="mean_reversion",
            universe="sp500",
            params={},
            sharpe=1.0,
        )
        atlas_db_module.upsert_research_best(
            strategy="momentum_breakout",
            universe="sp500",
            params={},
            sharpe=0.9,
        )
        result = atlas_db_module.get_research_best(strategy="mean_reversion")
        assert all(r["strategy"] == "mean_reversion" for r in result)

    def test_get_research_best_filter_by_universe(self):
        atlas_db_module.upsert_research_best(
            strategy="mean_reversion",
            universe="sp500",
            params={},
            sharpe=1.0,
        )
        atlas_db_module.upsert_research_best(
            strategy="mean_reversion",
            universe="sector_etfs",
            params={},
            sharpe=0.8,
        )
        result = atlas_db_module.get_research_best(universe="sp500")
        assert all(r["universe"] == "sp500" for r in result)

    def test_params_deserialized(self):
        atlas_db_module.upsert_research_best(
            strategy="mean_reversion",
            universe="sp500",
            params={"rsi_period": 14, "zscore_entry": -2.0},
            sharpe=1.0,
        )
        best = atlas_db_module.get_research_best(
            strategy="mean_reversion", universe="sp500"
        )
        assert len(best) >= 1
        params = best[0].get("params")
        if isinstance(params, str):
            params = json.loads(params)
        assert isinstance(params, dict)

    def test_get_experiments_limit(self):
        for i in range(10):
            self._record_exp(exp_id=f"exp-{i:03d}")
        result = atlas_db_module.get_experiments(limit=5)
        assert len(result) <= 5


# ═══════════════════════════════════════════════════════════════════════════════
# 13. System — Heartbeats + System Log
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
        assert atlas_db_module.get_signals() == []
        assert atlas_db_module.get_plans() == []
        assert atlas_db_module.get_snapshots() == []
        assert atlas_db_module.get_overlay_decisions() == []
        assert atlas_db_module.get_ceasefire_factors() == []
        assert atlas_db_module.get_ceasefire_history() == []
        assert atlas_db_module.get_news() == []
        assert atlas_db_module.get_experiments() == []
        assert atlas_db_module.get_research_best() == []
        assert atlas_db_module.get_heartbeats() == []
        assert atlas_db_module.get_system_logs() == []

    def test_empty_scalar_queries_return_none(self):
        assert atlas_db_module.get_current_regime() is None
        assert atlas_db_module.get_latest_snapshot() is None

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
        assert summary["profit_factor"] == float("inf")

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

    def test_ohlcv_primary_key_constraint(self):
        """Upserting same (ticker, date) twice keeps only one row."""
        for close in [100.0, 105.0]:
            atlas_db_module.upsert_ohlcv(
                ticker="AAPL",
                date="2026-01-02",
                o=99.0,
                h=106.0,
                l=98.0,
                c=close,
                adj=close,
                vol=1_000_000,
                universe="sp500",
            )
        with atlas_db_module.get_db() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM ohlcv WHERE ticker='AAPL' AND date='2026-01-02'"
            ).fetchone()[0]
        assert count == 1

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

    def test_json_round_trip_features(self):
        """JSON features are serialized and deserialized correctly."""
        features = {"rsi": 28.3, "zscore": -2.1, "nested": {"a": 1}}
        atlas_db_module.record_signal(
            timestamp=_iso(),
            ticker="AAPL",
            strategy="mean_reversion",
            universe="sp500",
            direction="long",
            entry_price=150.0,
            stop_price=143.0,
            take_profit=165.0,
            position_size=10,
            position_value=1500.0,
            risk_amount=70.0,
            confidence=0.76,
            rationale="Test JSON round-trip",
            features=features,
            sector="Technology",
            regime_state="bull_risk_on",
            action="accepted",
        )
        signals = atlas_db_module.get_signals()
        stored_features = signals[0].get("features")
        if isinstance(stored_features, str):
            stored_features = json.loads(stored_features)
        assert stored_features == features


# ═══════════════════════════════════════════════════════════════════════════════
# 15. DB module __init__.py exports
# ═══════════════════════════════════════════════════════════════════════════════


class TestModuleExports:
    def test_db_init_exports_get_db(self):
        import db
        assert hasattr(db, "get_db")

    def test_db_init_exports_db_path(self):
        import db
        assert hasattr(db, "DB_PATH")

    def test_get_db_is_context_manager(self):
        """get_db() yields a usable sqlite3 connection."""
        with atlas_db_module.get_db() as conn:
            result = conn.execute("SELECT 1").fetchone()[0]
        assert result == 1
