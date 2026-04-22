"""Regression test: log_experiment() must tag new rows with regime_state.

Fixture creates an in-memory DB (via ATLAS_DB_PATH env override) with:
- research_experiments table including regime_state column
- regime_history table with a row for today

Then calls log_experiment() and verifies the inserted row has regime_state populated.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temp SQLite DB with required tables, point atlas_db at it."""
    db_path = tmp_path / "test_atlas.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    # Minimal research_experiments schema (includes regime_state)
    conn.execute("""
        CREATE TABLE research_experiments (
            id TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            universe TEXT DEFAULT 'sp500',
            experiment_type TEXT,
            params_changed TEXT,
            description TEXT,
            sharpe REAL,
            trades INTEGER,
            max_dd_pct REAL,
            profit_factor REAL,
            cagr_pct REAL,
            status TEXT DEFAULT 'running',
            recommendation TEXT,
            agent_id TEXT,
            completed_at TEXT,
            window_coverage_pct REAL,
            regime_state TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Minimal research_sessions schema (needed by log_session)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            mode TEXT,
            strategy TEXT,
            status TEXT DEFAULT 'running',
            ended_at TEXT,
            experiments_run INTEGER DEFAULT 0,
            experiments_kept INTEGER DEFAULT 0,
            duration_minutes REAL
        )
    """)

    # regime_history with today → "bull_risk_on"
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    conn.execute("""
        CREATE TABLE regime_history (
            date TEXT PRIMARY KEY,
            regime_state TEXT NOT NULL,
            trend_score REAL,
            risk_score REAL,
            active_universes TEXT,
            sizing_multiplier REAL DEFAULT 1.0,
            enabled_strategies TEXT,
            reasoning TEXT,
            model_version TEXT
        )
    """)
    conn.execute(
        "INSERT INTO regime_history (date, regime_state) VALUES (?, ?)",
        (today, "bull_risk_on"),
    )
    conn.commit()
    conn.close()

    # Override atlas_db to use our temp file
    monkeypatch.setenv("ATLAS_DB_PATH", str(db_path))

    # Force reload of any cached connection/path in atlas_db
    try:
        import db.atlas_db as atlas_db_mod
        monkeypatch.setattr(atlas_db_mod, "_db_path_override", db_path, raising=False)
    except Exception:
        pass

    return db_path


# ── Tests ──────────────────────────────────────────────────────────────────────

def test_log_experiment_tags_regime_state(temp_db: Path) -> None:
    """log_experiment() must insert regime_state = 'bull_risk_on' for today."""
    from research.db import log_experiment

    log_experiment(
        strategy="momentum_breakout",
        metrics={
            "sharpe": 1.1,
            "total_trades": 50,
            "max_drawdown_pct": -8.5,
            "profit_factor": 1.6,
            "cagr_pct": 12.3,
            "window_coverage_pct": 98.0,
        },
        params_changed='{"rsi_period": 7}',
        status="keep",
        description="Test experiment — regime tagging",
        source="test",
        market="sp500",
        stage="solo",
    )

    conn = sqlite3.connect(str(temp_db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM research_experiments ORDER BY created_at DESC LIMIT 1"
    ).fetchall()
    conn.close()

    assert len(rows) == 1, "Expected exactly 1 experiment row after log_experiment()"
    row = rows[0]

    assert row["strategy"] == "momentum_breakout"
    assert row["status"] == "kept"  # 'keep' → 'kept' mapping
    assert row["regime_state"] == "bull_risk_on", (
        f"Expected regime_state='bull_risk_on', got {row['regime_state']!r}"
    )


def test_log_experiment_no_regime_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """log_experiment() must NOT crash if regime_history is empty (returns NULL)."""
    db_path = tmp_path / "test_empty_regime.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE research_experiments (
            id TEXT PRIMARY KEY,
            strategy TEXT NOT NULL,
            universe TEXT DEFAULT 'sp500',
            experiment_type TEXT,
            params_changed TEXT,
            description TEXT,
            sharpe REAL,
            trades INTEGER,
            max_dd_pct REAL,
            profit_factor REAL,
            cagr_pct REAL,
            status TEXT DEFAULT 'running',
            recommendation TEXT,
            agent_id TEXT,
            completed_at TEXT,
            window_coverage_pct REAL,
            regime_state TEXT DEFAULT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT, mode TEXT, strategy TEXT, status TEXT DEFAULT 'running',
            ended_at TEXT, experiments_run INTEGER DEFAULT 0,
            experiments_kept INTEGER DEFAULT 0, duration_minutes REAL
        )
    """)
    conn.execute("""
        CREATE TABLE regime_history (
            date TEXT PRIMARY KEY,
            regime_state TEXT NOT NULL
        )
    """)
    # NOTE: no rows inserted → regime_state lookup returns None
    conn.commit()
    conn.close()

    monkeypatch.setenv("ATLAS_DB_PATH", str(db_path))
    try:
        import db.atlas_db as atlas_db_mod
        monkeypatch.setattr(atlas_db_mod, "_db_path_override", db_path, raising=False)
    except Exception:
        pass

    from research.db import log_experiment

    # Should not raise
    log_experiment(
        strategy="mean_reversion",
        metrics={"sharpe": 0.8, "total_trades": 30},
        params_changed=None,
        status="discard",
        description="No regime history test",
        source="test",
    )

    conn2 = sqlite3.connect(str(db_path))
    conn2.row_factory = sqlite3.Row
    rows = conn2.execute("SELECT regime_state FROM research_experiments").fetchall()
    conn2.close()

    assert len(rows) == 1
    assert rows[0]["regime_state"] is None, (
        f"Expected NULL regime_state when history is empty, got {rows[0]['regime_state']!r}"
    )
