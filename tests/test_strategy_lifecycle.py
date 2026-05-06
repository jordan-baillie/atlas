"""Tests for the strategy promotion lifecycle state machine.

Covers:
  1. Schema — strategy_lifecycle and strategy_lifecycle_history tables exist
  2. Initial seed — transition to RESEARCH creates a tracked row
  3. Allowed transitions — RESEARCH→PAPER ok; RESEARCH→LIVE raises for system
  4. History — 3 transitions produce 3 history rows in order
  5. Idempotency — repeated same-state transition DOES create an additional
     history row (documented behaviour: transitions are events, not state-only
     upserts; each call is intentional and auditable)
  6. List by state — filter works correctly
  7. Paper start/end dates — RESEARCH→PAPER sets paper_start_date;
     PAPER→LIVE sets paper_end_date
  8. Disallowed transitions raise ValueError
  9. Migration script — seeds correctly from tmp config + tmp DB

Run:
    cd /root/atlas && python3 -m pytest tests/test_strategy_lifecycle.py -v --timeout=30
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "migrations" / "2026-05-06-seed-strategy-lifecycle.py"
)


def _load_migration():
    """Load the migration module by file path (filename has dashes — not importable normally)."""
    spec = importlib.util.spec_from_file_location("migration_strategy_lifecycle", MIGRATION_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import init_db, get_db
from monitor.strategy_lifecycle import (
    PromotionState,
    ALLOWED_TRANSITIONS,
    get_state,
    transition,
    is_live,
    is_paper,
    list_state,
)


# ─── Test 1: Schema ───────────────────────────────────────────────────────────

class TestSchema:
    """strategy_lifecycle and strategy_lifecycle_history tables exist after init_db."""

    def test_strategy_lifecycle_table_exists(self):
        with get_db() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_lifecycle'"
            ).fetchone()
        assert row is not None, "strategy_lifecycle table not found"

    def test_strategy_lifecycle_history_table_exists(self):
        with get_db() as db:
            row = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_lifecycle_history'"
            ).fetchone()
        assert row is not None, "strategy_lifecycle_history table not found"

    def test_lifecycle_columns(self):
        with get_db() as db:
            cols = {
                r["name"]
                for r in db.execute("PRAGMA table_info(strategy_lifecycle)").fetchall()
            }
        expected = {
            "strategy", "universe", "state", "entered_state_at",
            "prev_state", "transition_reason", "paper_start_date",
            "paper_end_date", "auto_promotion_id", "notes",
        }
        assert expected <= cols, f"Missing columns: {expected - cols}"

    def test_lifecycle_history_columns(self):
        with get_db() as db:
            cols = {
                r["name"]
                for r in db.execute("PRAGMA table_info(strategy_lifecycle_history)").fetchall()
            }
        expected = {
            "id", "strategy", "universe", "from_state", "to_state",
            "transitioned_at", "reason", "auto_promotion_id", "operator",
        }
        assert expected <= cols, f"Missing columns: {expected - cols}"

    def test_state_check_constraint(self):
        """strategy_lifecycle.state must be one of RESEARCH/PAPER/LIVE/RETIRED."""
        with pytest.raises(Exception):  # sqlite3.IntegrityError
            with get_db() as db:
                db.execute(
                    "INSERT INTO strategy_lifecycle "
                    "(strategy, universe, state, entered_state_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    ("test_strat", "sp500", "INVALID"),
                )


# ─── Test 2: Initial seed ─────────────────────────────────────────────────────

class TestInitialSeed:
    def test_transition_research_creates_row(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        assert get_state("mr", "sp500") == PromotionState.RESEARCH

    def test_transition_live_creates_row(self):
        transition("mb", "sp500", PromotionState.LIVE, reason="legacy live seed")
        assert get_state("mb", "sp500") == PromotionState.LIVE

    def test_untracked_returns_none(self):
        assert get_state("not_tracked", "sp500") is None

    def test_is_live_false_for_research(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        assert not is_live("mr", "sp500")

    def test_is_paper_false_for_research(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        assert not is_paper("mr", "sp500")


# ─── Test 3: Allowed transitions ─────────────────────────────────────────────

class TestAllowedTransitions:
    def test_research_to_paper_allowed(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="promoting to paper")
        assert get_state("mr", "sp500") == PromotionState.PAPER

    def test_research_to_live_forbidden_for_system(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        with pytest.raises(ValueError, match="Disallowed system transition"):
            transition("mr", "sp500", PromotionState.LIVE, reason="skip paper — should fail")

    def test_research_to_live_allowed_manual(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        # Manual override bypasses graph — should NOT raise
        transition(
            "mr", "sp500", PromotionState.LIVE,
            reason="emergency activation",
            operator="manual",
        )
        assert get_state("mr", "sp500") == PromotionState.LIVE

    def test_paper_to_live_allowed(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="paper phase")
        transition("mr", "sp500", PromotionState.LIVE, reason="passed paper gates")
        assert get_state("mr", "sp500") == PromotionState.LIVE

    def test_paper_to_research_rollback(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="paper phase")
        transition("mr", "sp500", PromotionState.RESEARCH, reason="failed paper — rollback")
        assert get_state("mr", "sp500") == PromotionState.RESEARCH

    def test_live_to_retired(self):
        transition("mr", "sp500", PromotionState.LIVE, reason="legacy live")
        transition("mr", "sp500", PromotionState.RETIRED, reason="decommission")
        assert get_state("mr", "sp500") == PromotionState.RETIRED

    def test_retired_to_research_revival(self):
        transition("mr", "sp500", PromotionState.LIVE, reason="legacy live")
        transition("mr", "sp500", PromotionState.RETIRED, reason="decommission")
        transition("mr", "sp500", PromotionState.RESEARCH, reason="revival")
        assert get_state("mr", "sp500") == PromotionState.RESEARCH


# ─── Test 4: History ──────────────────────────────────────────────────────────

class TestHistory:
    def test_three_transitions_produce_three_history_rows(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="paper")
        transition("mr", "sp500", PromotionState.LIVE, reason="live")

        with get_db() as db:
            rows = db.execute(
                "SELECT from_state, to_state FROM strategy_lifecycle_history "
                "WHERE strategy='mr' AND universe='sp500' "
                "ORDER BY transitioned_at, id",
            ).fetchall()

        assert len(rows) == 3
        assert rows[0]["from_state"] is None
        assert rows[0]["to_state"] == "RESEARCH"
        assert rows[1]["from_state"] == "RESEARCH"
        assert rows[1]["to_state"] == "PAPER"
        assert rows[2]["from_state"] == "PAPER"
        assert rows[2]["to_state"] == "LIVE"

    def test_history_stores_operator(self):
        transition("mr", "sp500", PromotionState.LIVE, reason="legacy", operator="system")
        transition("mr", "sp500", PromotionState.RETIRED, reason="manual decom", operator="alice")

        with get_db() as db:
            rows = db.execute(
                "SELECT operator FROM strategy_lifecycle_history "
                "WHERE strategy='mr' AND universe='sp500' ORDER BY id",
            ).fetchall()

        assert rows[0]["operator"] == "system"
        assert rows[1]["operator"] == "alice"


# ─── Test 5: Idempotency ──────────────────────────────────────────────────────

class TestIdempotency:
    def test_same_state_transition_creates_second_history_row(self):
        """Each call to transition() is a logged event, even if state doesn't change.

        Design decision: transitions are audit events, not state-only upserts.
        Callers should check get_state() before calling transition() if they
        want to avoid duplicate history entries.
        """
        transition("mr", "sp500", PromotionState.RESEARCH, reason="first seed")
        transition("mr", "sp500", PromotionState.RESEARCH, reason="idempotent re-seed", operator="manual")

        with get_db() as db:
            count = db.execute(
                "SELECT COUNT(*) as c FROM strategy_lifecycle_history "
                "WHERE strategy='mr' AND universe='sp500'"
            ).fetchone()["c"]

        # Two history rows — both calls are recorded (audit trail)
        assert count == 2

    def test_final_state_is_last_written(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="first")
        transition("mr", "sp500", PromotionState.RESEARCH, reason="idempotent", operator="manual")
        assert get_state("mr", "sp500") == PromotionState.RESEARCH


# ─── Test 6: List by state ────────────────────────────────────────────────────

class TestListByState:
    def test_list_research_returns_two(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mb", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("gap", "sp500", PromotionState.LIVE, reason="legacy")

        research_rows = list_state(PromotionState.RESEARCH)
        live_rows = list_state(PromotionState.LIVE)

        assert len(research_rows) == 2
        assert len(live_rows) == 1
        strats = {r["strategy"] for r in research_rows}
        assert strats == {"mr", "mb"}

    def test_list_returns_dict_keys(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        rows = list_state(PromotionState.RESEARCH)
        assert len(rows) == 1
        assert "strategy" in rows[0]
        assert "universe" in rows[0]
        assert "state" in rows[0]
        assert "entered_state_at" in rows[0]


# ─── Test 7: Paper dates ──────────────────────────────────────────────────────

class TestPaperDates:
    def _get_row(self, strategy: str, universe: str) -> dict:
        with get_db() as db:
            r = db.execute(
                "SELECT * FROM strategy_lifecycle WHERE strategy=? AND universe=?",
                (strategy, universe),
            ).fetchone()
        return dict(r) if r else {}

    def test_paper_start_date_set_on_research_to_paper(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        assert self._get_row("mr", "sp500")["paper_start_date"] is None

        transition("mr", "sp500", PromotionState.PAPER, reason="paper start")
        row = self._get_row("mr", "sp500")
        assert row["paper_start_date"] is not None, "paper_start_date should be set"

    def test_paper_end_date_set_on_paper_to_live(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="paper start")
        assert self._get_row("mr", "sp500")["paper_end_date"] is None

        transition("mr", "sp500", PromotionState.LIVE, reason="promoted")
        row = self._get_row("mr", "sp500")
        assert row["paper_end_date"] is not None, "paper_end_date should be set on PAPER→LIVE"

    def test_paper_start_date_preserved_after_live(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        transition("mr", "sp500", PromotionState.PAPER, reason="paper start")
        start = self._get_row("mr", "sp500")["paper_start_date"]
        transition("mr", "sp500", PromotionState.LIVE, reason="promoted")
        assert self._get_row("mr", "sp500")["paper_start_date"] == start


# ─── Test 8: Disallowed transitions raise ────────────────────────────────────

class TestDisallowedTransitions:
    def test_research_to_live_raises(self):
        transition("mr", "sp500", PromotionState.RESEARCH, reason="seed")
        with pytest.raises(ValueError, match="Disallowed system transition"):
            transition("mr", "sp500", PromotionState.LIVE)

    def test_none_to_paper_raises(self):
        """Initial seed to PAPER is not allowed (must go RESEARCH first)."""
        with pytest.raises(ValueError, match="Disallowed system transition"):
            transition("mr", "sp500", PromotionState.PAPER, reason="bad seed")

    def test_live_to_research_raises(self):
        transition("mr", "sp500", PromotionState.LIVE, reason="legacy")
        with pytest.raises(ValueError, match="Disallowed system transition"):
            transition("mr", "sp500", PromotionState.RESEARCH, reason="should fail")

    def test_retired_to_live_raises(self):
        transition("mr", "sp500", PromotionState.LIVE, reason="legacy")
        transition("mr", "sp500", PromotionState.RETIRED, reason="decom")
        with pytest.raises(ValueError, match="Disallowed system transition"):
            transition("mr", "sp500", PromotionState.LIVE, reason="skip research — should fail")


# ─── Test 9: Migration script ─────────────────────────────────────────────────

class TestMigrationScript:
    """Migration seeds LIVE from config, RESEARCH from research_best."""

    @pytest.fixture
    def tmp_config_dir(self, tmp_path: Path) -> Path:
        config_dir = tmp_path / "active"
        config_dir.mkdir()
        # sp500: momentum_breakout=enabled, connors_rsi2=enabled, mean_reversion=disabled
        (config_dir / "sp500.json").write_text(json.dumps({
            "strategies": {
                "momentum_breakout": {"enabled": True},
                "connors_rsi2": {"enabled": True},
                "mean_reversion": {"enabled": False},
            }
        }))
        # commodity_etfs: nothing enabled (simulate disabled universe)
        (config_dir / "commodity_etfs.json").write_text(json.dumps({
            "strategies": {
                "momentum_breakout": {"enabled": False},
            }
        }))
        return config_dir

    @pytest.fixture
    def seed_research_best(self) -> None:
        """Insert research_best rows for mean_reversion and commodity_etfs combo."""
        with get_db() as db:
            # mean_reversion/sp500 with positive sharpe
            db.execute(
                """INSERT OR IGNORE INTO research_best
                   (strategy, universe, sharpe, trades, max_dd_pct, params, metric_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("mean_reversion", "sp500", 1.2, 50, 15.0, "{}", "unknown"),
            )
            # commodity_etfs/momentum_breakout with positive sharpe
            db.execute(
                """INSERT OR IGNORE INTO research_best
                   (strategy, universe, sharpe, trades, max_dd_pct, params, metric_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("momentum_breakout", "commodity_etfs", 0.8, 30, 20.0, "{}", "unknown"),
            )
            # negative sharpe — should NOT be seeded
            db.execute(
                """INSERT OR IGNORE INTO research_best
                   (strategy, universe, sharpe, trades, max_dd_pct, params, metric_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                ("trend_following", "sp500", -0.5, 20, 25.0, "{}", "unknown"),
            )

    def test_migration_dry_run_does_not_seed(self, tmp_config_dir: Path, seed_research_best):
        mig = _load_migration()
        result = mig.run_migration(apply=False, active_config_dir=tmp_config_dir)
        # Dry-run: nothing in DB
        with get_db() as db:
            count = db.execute("SELECT COUNT(*) AS c FROM strategy_lifecycle").fetchone()["c"]
        assert count == 0
        assert result["live"] >= 2  # momentum_breakout + connors_rsi2

    def test_migration_apply_seeds_live(self, tmp_config_dir: Path, seed_research_best):
        mig = _load_migration()
        result = mig.run_migration(apply=True, active_config_dir=tmp_config_dir)

        # momentum_breakout/sp500 and connors_rsi2/sp500 → LIVE
        assert get_state("momentum_breakout", "sp500") == PromotionState.LIVE
        assert get_state("connors_rsi2", "sp500") == PromotionState.LIVE
        assert result["live"] == 2

    def test_migration_apply_seeds_research(self, tmp_config_dir: Path, seed_research_best):
        mig = _load_migration()
        mig.run_migration(apply=True, active_config_dir=tmp_config_dir)

        # mean_reversion/sp500: in research_best but NOT enabled → RESEARCH
        assert get_state("mean_reversion", "sp500") == PromotionState.RESEARCH
        # commodity_etfs/momentum_breakout: in research_best but not enabled → RESEARCH
        assert get_state("momentum_breakout", "commodity_etfs") == PromotionState.RESEARCH

    def test_migration_skips_negative_sharpe(self, tmp_config_dir: Path, seed_research_best):
        mig = _load_migration()
        mig.run_migration(apply=True, active_config_dir=tmp_config_dir)
        # trend_following/sp500 has sharpe=-0.5 → NOT seeded
        assert get_state("trend_following", "sp500") is None

    def test_migration_is_idempotent(self, tmp_config_dir: Path, seed_research_best):
        mig = _load_migration()
        first_result = mig.run_migration(apply=True, active_config_dir=tmp_config_dir)
        second_result = mig.run_migration(apply=True, active_config_dir=tmp_config_dir)

        # Second run: all rows already exist → skipped=all, live=0, research=0
        assert second_result["live"] == 0
        assert second_result["research"] == 0
        assert second_result["skipped"] == (first_result["live"] + first_result["research"])

        with get_db() as db:
            states = {
                (r["strategy"], r["universe"]): r["state"]
                for r in db.execute("SELECT strategy, universe, state FROM strategy_lifecycle").fetchall()
            }
        assert states[("momentum_breakout", "sp500")] == "LIVE"
        assert states[("connors_rsi2", "sp500")] == "LIVE"
        assert states[("mean_reversion", "sp500")] == "RESEARCH"
