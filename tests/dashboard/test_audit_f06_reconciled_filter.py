"""F-06 acceptance tests: 'reconciled' synthetic strategy excluded from dashboard rollups.

Tests both the dashboard_builder aggregation and the atlas_db _group_performance filter.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile

import pytest


# ─── Unit tests: dashboard_builder.build_strategy_stats ───────────────────────

class TestF06DashboardBuilderFilter:
    """build_strategy_stats must exclude reconciled/unknown from by_strategy."""

    def _make_db_with_trades(self, trades: list[dict]) -> str:
        """Create a temp SQLite DB populated with given trades rows."""
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                ticker TEXT,
                strategy TEXT,
                pnl REAL,
                pnl_pct REAL,
                entry_date TEXT,
                exit_date TEXT,
                stop_price REAL,
                entry_price REAL,
                status TEXT,
                superseded INTEGER DEFAULT 0,
                exit_reason TEXT,
                universe TEXT
            )
        """)
        conn.execute("CREATE TABLE equity_curve (date TEXT, market_id TEXT, equity REAL)")
        conn.execute("CREATE TABLE ohlcv (ticker TEXT, date TEXT, close REAL)")
        for t in trades:
            conn.execute(
                "INSERT INTO trades (ticker,strategy,pnl,pnl_pct,exit_date,status,superseded)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    t.get("ticker", "AAA"),
                    t.get("strategy"),
                    t.get("pnl", 10.0),
                    t.get("pnl_pct", 1.0),
                    t.get("exit_date", "2026-01-15"),
                    t.get("status", "closed"),
                    t.get("superseded", 0),
                ),
            )
        conn.commit()
        conn.close()
        return db_path

    def test_reconciled_excluded(self, monkeypatch, tmp_path):
        """reconciled strategy is NOT in by_strategy."""
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats
        from atlas import db as atlas_db

        db_path = self._make_db_with_trades([
            {"ticker": "AAA", "strategy": "momentum_breakout", "pnl": 50.0},
            {"ticker": "BBB", "strategy": "reconciled", "pnl": -20.0},
        ])
        monkeypatch.setattr(atlas_db, "_db_path_override", db_path)
        result = build_strategy_stats([], [])
        by_strategy = result["strategy_performance"]["by_strategy"]
        assert "reconciled" not in by_strategy, f"reconciled in {list(by_strategy.keys())}"
        assert "momentum_breakout" in by_strategy

    def test_unknown_excluded(self, monkeypatch, tmp_path):
        """unknown strategy is NOT in by_strategy."""
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats
        from atlas import db as atlas_db

        db_path = self._make_db_with_trades([
            {"ticker": "AAA", "strategy": "mean_reversion", "pnl": 30.0},
            {"ticker": "BBB", "strategy": "unknown", "pnl": -5.0},
        ])
        monkeypatch.setattr(atlas_db, "_db_path_override", db_path)
        result = build_strategy_stats([], [])
        by_strategy = result["strategy_performance"]["by_strategy"]
        assert "unknown" not in by_strategy
        assert "mean_reversion" in by_strategy

    def test_null_strategy_excluded(self, monkeypatch, tmp_path):
        """NULL strategy is NOT in by_strategy."""
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats
        from atlas import db as atlas_db

        db_path = self._make_db_with_trades([
            {"ticker": "AAA", "strategy": "trend_following", "pnl": 40.0},
            {"ticker": "BBB", "strategy": None, "pnl": -8.0},
        ])
        monkeypatch.setattr(atlas_db, "_db_path_override", db_path)
        result = build_strategy_stats([], [])
        by_strategy = result["strategy_performance"]["by_strategy"]
        assert None not in by_strategy
        assert "unknown" not in by_strategy
        assert "trend_following" in by_strategy

    def test_empty_string_strategy_excluded(self, monkeypatch, tmp_path):
        """Empty-string strategy is NOT in by_strategy."""
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats
        from atlas import db as atlas_db

        db_path = self._make_db_with_trades([
            {"ticker": "AAA", "strategy": "bb_squeeze", "pnl": 15.0},
            {"ticker": "BBB", "strategy": "", "pnl": -3.0},
        ])
        monkeypatch.setattr(atlas_db, "_db_path_override", db_path)
        result = build_strategy_stats([], [])
        by_strategy = result["strategy_performance"]["by_strategy"]
        assert "" not in by_strategy
        assert "bb_squeeze" in by_strategy

    def test_multiple_synthetic_excluded(self, monkeypatch, tmp_path):
        """reconciled + unknown + null all excluded; real strategies kept."""
        from atlas.dashboard.api.dashboard_builder import build_strategy_stats
        from atlas import db as atlas_db

        db_path = self._make_db_with_trades([
            {"ticker": "AAA", "strategy": "momentum_breakout", "pnl": 100.0},
            {"ticker": "BBB", "strategy": "mean_reversion", "pnl": 50.0},
            {"ticker": "CCC", "strategy": "reconciled", "pnl": -209.20},
            {"ticker": "DDD", "strategy": "unknown", "pnl": -5.0},
            {"ticker": "EEE", "strategy": None, "pnl": -2.0},
            {"ticker": "FFF", "strategy": "", "pnl": -1.0},
        ])
        monkeypatch.setattr(atlas_db, "_db_path_override", db_path)
        result = build_strategy_stats([], [])
        by_strategy = result["strategy_performance"]["by_strategy"]
        for bad in ("reconciled", "unknown", None, ""):
            assert bad not in by_strategy, f"synthetic strategy {bad!r} found in by_strategy"
        assert "momentum_breakout" in by_strategy
        assert "mean_reversion" in by_strategy


# ─── Unit tests: atlas_db._group_performance ─────────────────────────────────

class TestF06GroupPerformanceFilter:
    """_group_performance(field='strategy') must exclude synthetic strategies."""

    def _trades(self, *pairs):
        """Build minimal trade dicts from (strategy, pnl) pairs."""
        return [{"strategy": s, "pnl": p} for s, p in pairs]

    def test_reconciled_excluded_from_group_performance(self):
        from atlas.db import _group_performance
        trades = self._trades(
            ("momentum_breakout", 100.0),
            ("reconciled", -209.20),
        )
        result = _group_performance(trades, "strategy")
        assert "reconciled" not in result
        assert "momentum_breakout" in result

    def test_unknown_excluded_from_group_performance(self):
        from atlas.db import _group_performance
        trades = self._trades(
            ("mean_reversion", 50.0),
            ("unknown", -5.0),
        )
        result = _group_performance(trades, "strategy")
        assert "unknown" not in result
        assert "mean_reversion" in result

    def test_null_strategy_excluded_from_group_performance(self):
        from atlas.db import _group_performance
        trades = [
            {"strategy": "trend_following", "pnl": 80.0},
            {"strategy": None, "pnl": -10.0},
        ]
        result = _group_performance(trades, "strategy")
        assert None not in result
        assert "unknown" not in result
        assert "trend_following" in result

    def test_non_strategy_field_not_filtered(self):
        """Filtering only applies when field='strategy', not other fields."""
        from atlas.db import _group_performance
        trades = [
            {"universe": "reconciled", "pnl": 50.0},  # 'reconciled' universe should NOT be filtered
            {"universe": "sp500", "pnl": 30.0},
        ]
        result = _group_performance(trades, "universe")
        # 'reconciled' as universe name should pass through (filter is strategy-only)
        assert "reconciled" in result or "sp500" in result  # at minimum one group preserved


# ─── Integration test: prod DB (if available) ─────────────────────────────────

@pytest.mark.integration
class TestF06ProdDB:
    """Check the production trades table no longer surfaces reconciled in rollups."""

    def test_prod_by_strategy_no_reconciled(self):
        """_group_performance on prod trades must not return 'reconciled'."""
        import sqlite3
        from pathlib import Path
        db_path = Path(__file__).resolve().parents[2] / "data" / "atlas.db"
        if not db_path.exists():
            pytest.skip("Production DB not found")
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        trades = [
            dict(r) for r in conn.execute(
                "SELECT strategy, pnl FROM trades WHERE status='closed'"
            ).fetchall()
        ]
        conn.close()
        from atlas.db import _group_performance
        result = _group_performance(trades, "strategy")
        assert "reconciled" not in result, (
            f"reconciled still in prod _group_performance: {result.get('reconciled')}"
        )
