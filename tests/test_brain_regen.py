"""Tests for scripts/regen_brain_strategies.py (D1)
and a self-test for scripts/data_integrity_monitor.py (D3).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

from scripts.regen_brain_strategies import _build_metrics, _build_params, _load_research_best, regen_all
from scripts.data_integrity_monitor import query_suspicious


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def tmp_db(tmp_path: Path) -> Path:
    """In-memory SQLite with a minimal research_best table."""
    db_path = tmp_path / "test_atlas.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE research_best (
                strategy  TEXT NOT NULL,
                universe  TEXT NOT NULL,
                params    TEXT NOT NULL DEFAULT '{}',
                sharpe    REAL,
                trades    INTEGER,
                max_dd_pct REAL,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (strategy, universe)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE research_experiments (
                id       TEXT PRIMARY KEY,
                strategy TEXT NOT NULL,
                universe TEXT NOT NULL DEFAULT 'sp500',
                sharpe   REAL,
                trades   INTEGER,
                max_dd_pct REAL,
                profit_factor REAL,
                cagr_pct REAL,
                status   TEXT DEFAULT 'running',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()
    return db_path


def _insert_best(db_path: Path, rows: list[tuple]) -> None:
    """Insert rows into research_best: (strategy, universe, params, sharpe, trades, max_dd)."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            "INSERT INTO research_best (strategy, universe, params, sharpe, trades, max_dd_pct) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()


def _insert_experiment(
    db_path: Path,
    strategy: str,
    universe: str,
    sharpe: float,
    trades: int,
    exp_id: str,
    hours_ago: int = 1,
) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            INSERT INTO research_experiments (id, strategy, universe, sharpe, trades, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', ?))
            """,
            (exp_id, strategy, universe, sharpe, trades, f"-{hours_ago} hours"),
        )
        conn.commit()


# ─── D1 Tests: regen_all ─────────────────────────────────────────────────────


class TestRegenBrainStrategies:
    """D1.1 — update_strategy called once per unique strategy in research_best."""

    def test_calls_update_strategy_once_per_strategy(self, tmp_db: Path) -> None:
        """regen_all() calls update_strategy exactly N times for N distinct strategies."""
        _insert_best(
            tmp_db,
            [
                ("momentum_breakout", "sp500", '{"lookback_days": 15}', 0.82, 1036, 12.3),
                ("mean_reversion", "sp500", '{"rsi_period": 14}', 0.47, 617, 8.5),
                ("sector_rotation", "sp500", "{}", 0.04, 647, 5.1),
            ],
        )

        mock_update = MagicMock()
        with patch("scripts.regen_brain_strategies.regen_all") as mock_regen:
            # Test the underlying logic directly with a patched update_strategy
            pass

        # Use the actual regen_all with a mocked update_strategy
        with patch("research.brain.writer.update_strategy", mock_update):
            processed, succeeded, failed = regen_all(db_path=tmp_db, dry_run=False)

        assert processed == 3, f"Expected 3 strategies processed, got {processed}"
        assert succeeded == 3, f"Expected 3 succeeded, got {succeeded}"
        assert failed == 0
        assert mock_update.call_count == 3, (
            f"update_strategy should be called 3 times, called {mock_update.call_count}"
        )
        # Verify strategy names were passed
        called_strategies = {c.args[0] for c in mock_update.call_args_list}
        assert called_strategies == {"momentum_breakout", "mean_reversion", "sector_rotation"}

    def test_multiple_universes_prefers_sp500(self, tmp_db: Path) -> None:
        """When a strategy appears in multiple universes, sp500 row is used."""
        _insert_best(
            tmp_db,
            [
                # gold_etfs comes first alphabetically
                ("mean_reversion", "gold_etfs", '{"rsi_period": 7}', 0.91, 53, 10.0),
                ("mean_reversion", "sp500", '{"rsi_period": 14}', 0.47, 617, 8.5),
                ("mean_reversion", "sector_etfs", '{"rsi_period": 10}', 0.91, 53, 10.0),
            ],
        )

        captured_params: list[dict] = []

        def fake_update(strategy, metrics, params, status="active", description=""):
            captured_params.append({"strategy": strategy, "params": params})

        with patch("research.brain.writer.update_strategy", fake_update):
            processed, succeeded, failed = regen_all(db_path=tmp_db, dry_run=False)

        assert processed == 1  # only one unique strategy
        assert len(captured_params) == 1
        # sp500 row has rsi_period=14, gold/sector have 7 and 10
        assert captured_params[0]["params"].get("rsi_period") == 14, (
            "sp500 row should be preferred for mean_reversion"
        )

    def test_dry_run_does_not_call_update_strategy(self, tmp_db: Path) -> None:
        """--dry-run mode returns counts but NEVER calls update_strategy."""
        _insert_best(
            tmp_db,
            [
                ("momentum_breakout", "sp500", '{"lookback_days": 15}', 0.82, 1036, 12.3),
                ("mean_reversion", "commodity_etfs", '{"rsi_period": 14}', 0.91, 53, 10.0),
            ],
        )

        mock_update = MagicMock()
        with patch("research.brain.writer.update_strategy", mock_update):
            processed, succeeded, failed = regen_all(db_path=tmp_db, dry_run=True)

        assert mock_update.call_count == 0, (
            "update_strategy must NOT be called in dry-run mode"
        )
        assert processed == 2, "Should report 2 strategies in dry-run"
        assert succeeded == 0  # nothing executed
        assert failed == 0

    def test_empty_research_best_returns_zeros(self, tmp_db: Path) -> None:
        """Empty research_best → (0, 0, 0)."""
        processed, succeeded, failed = regen_all(db_path=tmp_db, dry_run=False)
        assert (processed, succeeded, failed) == (0, 0, 0)

    def test_strategy_filter(self, tmp_db: Path) -> None:
        """--strategy filter limits to a single strategy."""
        _insert_best(
            tmp_db,
            [
                ("momentum_breakout", "sp500", "{}", 0.82, 1036, 12.3),
                ("mean_reversion", "sp500", "{}", 0.47, 617, 8.5),
            ],
        )

        mock_update = MagicMock()
        with patch("research.brain.writer.update_strategy", mock_update):
            processed, succeeded, failed = regen_all(
                db_path=tmp_db, strategy_filter="momentum_breakout", dry_run=False
            )

        assert processed == 1
        assert mock_update.call_count == 1
        assert mock_update.call_args.args[0] == "momentum_breakout"

    def test_failed_update_counts_as_failed(self, tmp_db: Path) -> None:
        """If update_strategy raises, the strategy is counted as failed."""
        _insert_best(
            tmp_db,
            [
                ("bad_strategy", "sp500", "{}", 0.50, 100, 5.0),
                ("good_strategy", "sp500", "{}", 0.80, 200, 4.0),
            ],
        )

        call_results: dict[str, Exception | None] = {
            "bad_strategy": RuntimeError("disk full"),
            "good_strategy": None,
        }

        def selective_update(strategy, metrics, params, **kwargs):
            exc = call_results.get(strategy)
            if exc is not None:
                raise exc

        with patch("research.brain.writer.update_strategy", selective_update):
            processed, succeeded, failed = regen_all(db_path=tmp_db, dry_run=False)

        assert processed == 2
        assert succeeded == 1
        assert failed == 1

    def test_exit_1_when_all_fail(self, tmp_db: Path) -> None:
        """main() exits 1 only when all strategies fail."""
        _insert_best(tmp_db, [("bad", "sp500", "{}", 0.5, 10, 5.0)])

        with patch(
            "research.brain.writer.update_strategy",
            side_effect=RuntimeError("disk error"),
        ):
            from scripts.regen_brain_strategies import main
            rc = main(["--db", str(tmp_db)])

        assert rc == 1

    def test_partial_success_exits_zero(self, tmp_db: Path) -> None:
        """main() exits 0 when at least one strategy succeeds."""
        _insert_best(
            tmp_db,
            [
                ("ok", "sp500", "{}", 0.8, 100, 5.0),
                ("fail", "sp500", "{}", 0.5, 10, 5.0),
            ],
        )

        def selective(strategy, metrics, params, **kwargs):
            if strategy == "fail":
                raise RuntimeError("oops")

        with patch("research.brain.writer.update_strategy", selective):
            from scripts.regen_brain_strategies import main
            rc = main(["--db", str(tmp_db)])

        assert rc == 0


# ─── Helper unit tests ────────────────────────────────────────────────────────

class TestBuildHelpers:
    def test_build_metrics(self) -> None:
        row = {"sharpe": 0.82, "trades": 100, "max_dd_pct": 12.5}
        m = _build_metrics(row)
        assert m["sharpe"] == pytest.approx(0.82)
        assert m["total_trades"] == 100
        assert m["max_drawdown_pct"] == pytest.approx(12.5)

    def test_build_params_valid_json(self) -> None:
        row = {"params": '{"rsi_period": 14, "lookback_days": 20}', "strategy": "s"}
        p = _build_params(row)
        assert p == {"rsi_period": 14, "lookback_days": 20}

    def test_build_params_invalid_json(self) -> None:
        row = {"params": "not-json", "strategy": "s"}
        p = _build_params(row)
        assert p == {}

    def test_build_params_null(self) -> None:
        row = {"params": None, "strategy": "s"}
        p = _build_params(row)
        assert p == {}


# ─── D3 Self-test: data_integrity_monitor ─────────────────────────────────────

class TestDataIntegrityMonitor:
    """Inline tests for scripts/data_integrity_monitor.py query logic."""

    def test_detects_cross_universe_leak(self, tmp_db: Path) -> None:
        """query_suspicious returns a hit when same (strategy, sharpe, trades)
        appears in ≥3 non-sp500 universes within 24h."""
        # Insert the same Sharpe/trades combo across 3 ETF universes
        for i, universe in enumerate(["sector_etfs", "gold_etfs", "treasury_etfs"]):
            _insert_experiment(tmp_db, "mean_reversion", universe, 0.9122, 53, f"exp-leak-{i}")

        hits = query_suspicious(tmp_db, window_hours=24)
        assert len(hits) >= 1, "Should detect cross-universe leak"
        strategies = {h["strategy"] for h in hits}
        assert "mean_reversion" in strategies

    def test_no_hit_for_sp500_only(self, tmp_db: Path) -> None:
        """sp500 universe is excluded — should not trigger the canary."""
        for i in range(10):
            _insert_experiment(tmp_db, "momentum_breakout", "sp500", 0.82, 100, f"exp-sp-{i}")

        hits = query_suspicious(tmp_db, window_hours=24)
        assert len(hits) == 0

    def test_no_hit_for_only_two_universes(self, tmp_db: Path) -> None:
        """Two distinct universes (≤2) should NOT trigger — threshold is >2."""
        for i, universe in enumerate(["sector_etfs", "gold_etfs"]):
            _insert_experiment(tmp_db, "connors_rsi2", universe, 0.574, 100, f"exp-two-{i}")

        hits = query_suspicious(tmp_db, window_hours=24)
        assert len(hits) == 0, "2 universes should not trigger (threshold is >2)"

    def test_window_hours_excludes_old_rows(self, tmp_db: Path) -> None:
        """Rows older than window_hours are not counted."""
        # Insert same pattern 49 hours ago
        for i, universe in enumerate(["sector_etfs", "gold_etfs", "treasury_etfs"]):
            _insert_experiment(
                tmp_db, "stale_strat", universe, 0.5000, 50, f"exp-old-{i}",
                hours_ago=49,
            )

        hits = query_suspicious(tmp_db, window_hours=24)
        assert all(h["strategy"] != "stale_strat" for h in hits)

    def test_clean_db_exits_zero(self, tmp_db: Path) -> None:
        """main() exits 0 when no suspicious patterns found."""
        from scripts.data_integrity_monitor import main
        rc = main(["--db", str(tmp_db), "--window-hours", "24"])
        assert rc == 0

    def test_contaminated_db_exits_one(self, tmp_db: Path) -> None:
        """main() exits 1 when suspicious patterns found."""
        for i, universe in enumerate(["sector_etfs", "gold_etfs", "treasury_etfs"]):
            _insert_experiment(tmp_db, "momentum_breakout", universe, 0.6949, 52, f"exp-c-{i}")

        from scripts.data_integrity_monitor import main
        rc = main(["--db", str(tmp_db), "--window-hours", "24"])
        assert rc == 1
