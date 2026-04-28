"""
Tests for the 2026-04-28-backfill-good-friday-regime migration.

Runs the migration logic against a fresh in-memory (or temp-file) SQLite
database seeded with the minimal schema so the tests are fully isolated
from the production atlas.db.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
MIGRATION = (
    ATLAS_ROOT
    / "scripts"
    / "migrations"
    / "2026-04-28-backfill-good-friday-regime.py"
)


# ---------------------------------------------------------------------------
# Helper: load the migration as a module without importing it globally
# ---------------------------------------------------------------------------

def _load_migration() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("_gf_migration", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fixture: isolated tmp SQLite with regime_history + trades seeded
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    """
    Create a temporary atlas.db with only the tables the migration needs,
    seeded with the Apr-01 and Apr-02 rows (matching prod data shapes).
    """
    db_path = tmp_path / "atlas.db"
    conn = sqlite3.connect(str(db_path))

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS regime_history (
            date             TEXT PRIMARY KEY,
            regime_state     TEXT NOT NULL,
            trend_score      REAL,
            risk_score       REAL,
            active_universes TEXT,
            sizing_multiplier REAL DEFAULT 1.0,
            enabled_strategies TEXT,
            reasoning        TEXT,
            model_version    TEXT
        );

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY,
            ticker          TEXT NOT NULL,
            strategy        TEXT NOT NULL DEFAULT 'unknown',
            entry_date      TEXT NOT NULL,
            entry_price     REAL NOT NULL DEFAULT 0.0,
            shares          INTEGER NOT NULL DEFAULT 0,
            status          TEXT DEFAULT 'open',
            regime_at_entry TEXT,
            regime_at_exit  TEXT,
            superseded      INTEGER NOT NULL DEFAULT 0
        );
        """
    )

    # Seed Apr-01 and Apr-02 regime rows (matching prod values)
    conn.executemany(
        "INSERT INTO regime_history "
        "(date, regime_state, trend_score, risk_score, active_universes, "
        " sizing_multiplier, enabled_strategies, reasoning, model_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "2026-04-01",
                "transition_uncertain",
                -0.415581747741047,
                0.203483306929058,
                '["sp500", "sector_etfs", "treasury_etfs", "gold_etfs"]',
                0.5,
                '["all"]',
                "transition_uncertain: SPY below 200 DMA (trend -0.42), "
                "VIX low/calm (risk +0.36), credit tight (credit +1.00), "
                "yield curve normal (+0.44). Composite: +0.13",
                "v1",
            ),
            (
                "2026-04-02",
                "transition_uncertain",
                -0.418373601189502,
                0.359170037855781,
                '["sp500", "sector_etfs", "treasury_etfs", "gold_etfs"]',
                0.5,
                '["all"]',
                "transition_uncertain: SPY below 200 DMA (trend -0.42), "
                "VIX low/calm (risk +0.36), credit tight (credit +1.00), "
                "yield curve normal (+0.44). Composite: +0.13",
                "v1",
            ),
        ],
    )

    # Seed trade #127 with empty regime_at_entry (matches prod before migration)
    conn.execute(
        "INSERT INTO trades (id, ticker, strategy, entry_date, entry_price, shares, "
        "status, regime_at_entry, regime_at_exit) "
        "VALUES (127, 'MRVL', 'momentum_breakout', '2026-04-03', 99.12, 3, "
        "'closed', '', NULL)",
    )

    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Helper: monkey-patch the migration's DB_PATH so it uses our tmp db
# ---------------------------------------------------------------------------

@pytest.fixture()
def migration_on_isolated_db(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    """Loads the migration module with DB_PATH pointed at the tmp db."""
    mod = _load_migration()
    monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    # Also patch BACKUPS_DIR so backup writes go to tmp
    monkeypatch.setattr(mod, "BACKUPS_DIR", isolated_db.parent / "backups")
    return mod, isolated_db


# ===========================================================================
# Test 1 — migration is idempotent
# ===========================================================================

def test_migration_is_idempotent(migration_on_isolated_db) -> None:
    """
    Running the migration twice should not insert a duplicate row or raise
    an error.  The second run detects the existing row and exits cleanly.
    """
    mod, db_path = migration_on_isolated_db

    # First run — should succeed and insert 1 row
    rc1 = mod.run(apply=True)
    assert rc1 == 0, f"First run returned non-zero: {rc1}"

    conn = sqlite3.connect(str(db_path))
    count_after_first = conn.execute(
        "SELECT COUNT(*) FROM regime_history WHERE date='2026-04-03'"
    ).fetchone()[0]
    conn.close()
    assert count_after_first == 1, "Expected 1 row for 2026-04-03 after first run"

    # Second run — should be no-op
    rc2 = mod.run(apply=True)
    assert rc2 == 0, f"Second run returned non-zero: {rc2}"

    conn = sqlite3.connect(str(db_path))
    count_after_second = conn.execute(
        "SELECT COUNT(*) FROM regime_history WHERE date='2026-04-03'"
    ).fetchone()[0]
    conn.close()
    assert count_after_second == 1, (
        f"Expected exactly 1 row for 2026-04-03 after second run, got {count_after_second}"
    )


# ===========================================================================
# Test 2 — correct regime state carried forward from 2026-04-02
# ===========================================================================

def test_backfill_inserts_correct_regime_state(migration_on_isolated_db) -> None:
    """
    After the migration runs, regime_history must have a row for 2026-04-03
    with regime_state='transition_uncertain' (carried from 2026-04-02), and
    the reasoning must contain the carry-forward annotation.
    """
    mod, db_path = migration_on_isolated_db

    rc = mod.run(apply=True)
    assert rc == 0, f"Migration run returned: {rc}"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT regime_state, reasoning FROM regime_history WHERE date='2026-04-03'"
    ).fetchone()
    conn.close()

    assert row is not None, "No regime_history row for 2026-04-03 after migration"
    regime_state, reasoning = row

    assert regime_state == "transition_uncertain", (
        f"Expected regime_state='transition_uncertain', got '{regime_state}'"
    )

    carry_marker = "carry-forward from 2026-04-02"
    assert carry_marker in reasoning, (
        f"Expected reasoning to contain '{carry_marker}', got:\n{reasoning}"
    )


# ===========================================================================
# Test 3 — trade #127 regime_at_entry is patched
# ===========================================================================

def test_trade_127_regime_at_entry_patched(migration_on_isolated_db) -> None:
    """
    Trade #127 (MRVL, entry 2026-04-03) had an empty regime_at_entry.
    After migration, regime_at_entry should be 'transition_uncertain'.
    """
    mod, db_path = migration_on_isolated_db

    rc = mod.run(apply=True)
    assert rc == 0, f"Migration run returned: {rc}"

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT regime_at_entry FROM trades WHERE id=127"
    ).fetchone()
    conn.close()

    assert row is not None, "Trade #127 not found after migration"
    assert row[0] == "transition_uncertain", (
        f"Expected regime_at_entry='transition_uncertain', got '{row[0]}'"
    )
