#!/usr/bin/env python3
"""Unit tests for fix_equity_history_divergences_2026-05-14.py.

Tests use tmp_path for all DB and JSON files — no production data touched.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load the fix script as a module
# ---------------------------------------------------------------------------

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "fix_equity_history_divergences_2026-05-14.py"


def _load_fix_module():
    spec = importlib.util.spec_from_file_location(
        "fix_equity_history_divergences", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FIX = _load_fix_module()


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _make_db(path: Path) -> Path:
    """Create a minimal equity_history SQLite DB at *path*."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE equity_history (
               market_id TEXT NOT NULL,
               date      TEXT NOT NULL,
               equity    REAL NOT NULL,
               pnl       REAL,
               PRIMARY KEY (market_id, date)
           )"""
    )
    conn.commit()
    conn.close()
    return path


def _insert_row(db_path: Path, market_id: str, date: str, equity: float) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT OR REPLACE INTO equity_history (market_id, date, equity) VALUES (?,?,?)",
        (market_id, date, equity),
    )
    conn.commit()
    conn.close()


def _read_sqlite_equity(db_path: Path, market_id: str, date: str) -> float | None:
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT equity FROM equity_history WHERE market_id=? AND date=?",
        (market_id, date),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _make_state_json(state_dir: Path, market_id: str, rows: list[dict]) -> Path:
    """Write a minimal live_{market_id}.json with given equity_history rows."""
    path = state_dir / f"live_{market_id}.json"
    path.write_text(json.dumps({"equity_history": rows}))
    return path


def _read_json_equity_history(state_dir: Path, market_id: str) -> list[dict]:
    path = state_dir / f"live_{market_id}.json"
    return json.loads(path.read_text()).get("equity_history", [])


# ---------------------------------------------------------------------------
# Test 1 — dry_run_no_mutation
# ---------------------------------------------------------------------------

class TestDryRunNoMutation:
    def test_dry_run_no_mutation(self, tmp_path: Path) -> None:
        """Dry run must not write to SQLite or JSON state files."""
        db_path = _make_db(tmp_path / "atlas.db")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        audit_log = tmp_path / "audit.json"

        # SQLite: add a diverged row for commodity_etfs 2026-05-05
        _insert_row(db_path, "commodity_etfs", "2026-05-05", 944.82)
        # JSON: no 2026-05-01 row
        _make_state_json(state_dir, "commodity_etfs", [
            {"date": "2026-05-05", "equity": 944.82},
        ])
        _make_state_json(state_dir, "sector_etfs", [
            {"date": "2026-05-05", "equity": 3191.73},
        ])

        # Record mtime before
        db_mtime_before = db_path.stat().st_mtime
        json_mtime_before = (state_dir / "live_commodity_etfs.json").stat().st_mtime

        audit = FIX.run_fix(
            dry_run=True,
            db_path=db_path,
            state_dir=state_dir,
            audit_log_path=audit_log,
        )

        # SQLite row unchanged
        assert _read_sqlite_equity(db_path, "commodity_etfs", "2026-05-05") == 944.82
        # JSON file unchanged
        assert (state_dir / "live_commodity_etfs.json").stat().st_mtime == json_mtime_before
        # Actions show dry_run suffix
        action_types = {a["action"] for a in audit["market_id_changes"]}
        assert all("dry_run" in t or "skipped" in t for t in action_types), action_types
        assert audit["dry_run"] is True


# ---------------------------------------------------------------------------
# Test 2 — apply_updates_sqlite_divergence
# ---------------------------------------------------------------------------

class TestApplyUpdatesSqliteDivergence:
    def test_apply_updates_sqlite_divergence(self, tmp_path: Path) -> None:
        """--apply must UPDATE stale SQLite values to JSON truth."""
        db_path = _make_db(tmp_path / "atlas.db")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        audit_log = tmp_path / "audit.json"

        # Set up diverged SQLite rows (stale values)
        _insert_row(db_path, "commodity_etfs", "2026-05-05", 944.82)
        _insert_row(db_path, "sector_etfs", "2026-05-05", 3191.73)
        # No 2026-05-01 SQLite rows (nothing to backfill JSON with)
        # JSON state files — minimal, no 2026-05-01
        _make_state_json(state_dir, "commodity_etfs", [
            {"date": "2026-05-05", "equity": 944.82},
        ])
        _make_state_json(state_dir, "sector_etfs", [
            {"date": "2026-05-05", "equity": 3191.73},
        ])

        audit = FIX.run_fix(
            dry_run=False,
            db_path=db_path,
            state_dir=state_dir,
            audit_log_path=audit_log,
        )

        # SQLite should now show the JSON-truth target values
        assert _read_sqlite_equity(db_path, "commodity_etfs", "2026-05-05") == pytest.approx(956.82, abs=0.01)
        assert _read_sqlite_equity(db_path, "sector_etfs", "2026-05-05") == pytest.approx(3202.08, abs=0.01)
        # Status: ok or partial (partial when JSON appends are skipped because no SQLite 2026-05-01 rows)
        assert audit["status"] in ("ok", "partial")
        # Find sqlite_update actions
        updates = [a for a in audit["market_id_changes"] if a["action"] == "sqlite_update"]
        assert len(updates) == 2


# ---------------------------------------------------------------------------
# Test 3 — apply_backfills_json_with_sqlite_value
# ---------------------------------------------------------------------------

class TestApplyBackfillsJsonWithSqliteValue:
    def test_apply_backfills_json_with_sqlite_value(self, tmp_path: Path) -> None:
        """--apply must APPEND 2026-05-01 rows to JSON from SQLite source."""
        db_path = _make_db(tmp_path / "atlas.db")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        audit_log = tmp_path / "audit.json"

        # SQLite has 2026-05-01 rows but not 2026-05-05 (no divergence for this test)
        _insert_row(db_path, "commodity_etfs", "2026-05-01", 956.58)
        _insert_row(db_path, "sector_etfs", "2026-05-01", 3204.93)
        # SQLite also has correct 2026-05-05 values (no UPDATE needed)
        _insert_row(db_path, "commodity_etfs", "2026-05-05", 956.82)
        _insert_row(db_path, "sector_etfs", "2026-05-05", 3202.08)

        # JSON files DON'T have 2026-05-01
        _make_state_json(state_dir, "commodity_etfs", [
            {"date": "2026-05-05", "equity": 956.82},
        ])
        _make_state_json(state_dir, "sector_etfs", [
            {"date": "2026-05-05", "equity": 3202.08},
        ])

        audit = FIX.run_fix(
            dry_run=False,
            db_path=db_path,
            state_dir=state_dir,
            audit_log_path=audit_log,
        )

        # JSON should now have 2026-05-01 rows
        commodity_rows = _read_json_equity_history(state_dir, "commodity_etfs")
        dates = [r["date"] for r in commodity_rows]
        assert "2026-05-01" in dates, f"Expected 2026-05-01 in {dates}"
        # Value matches SQLite
        row_01 = next(r for r in commodity_rows if r["date"] == "2026-05-01")
        assert row_01["equity"] == pytest.approx(956.58, abs=0.01)

        sector_rows = _read_json_equity_history(state_dir, "sector_etfs")
        sector_dates = [r["date"] for r in sector_rows]
        assert "2026-05-01" in sector_dates
        row_s01 = next(r for r in sector_rows if r["date"] == "2026-05-01")
        assert row_s01["equity"] == pytest.approx(3204.93, abs=0.01)

        # json_append actions
        appends = [a for a in audit["market_id_changes"] if a["action"] == "json_append"]
        assert len(appends) == 2


# ---------------------------------------------------------------------------
# Test 4 — idempotent_second_run
# ---------------------------------------------------------------------------

class TestIdempotentSecondRun:
    def test_idempotent_second_run(self, tmp_path: Path) -> None:
        """Applying the fix twice must be a no-op on the second run."""
        db_path = _make_db(tmp_path / "atlas.db")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        audit_log = tmp_path / "audit.json"

        # Full divergence setup
        _insert_row(db_path, "commodity_etfs", "2026-05-05", 944.82)
        _insert_row(db_path, "sector_etfs", "2026-05-05", 3191.73)
        _insert_row(db_path, "commodity_etfs", "2026-05-01", 956.58)
        _insert_row(db_path, "sector_etfs", "2026-05-01", 3204.93)
        _make_state_json(state_dir, "commodity_etfs", [
            {"date": "2026-05-05", "equity": 944.82},
        ])
        _make_state_json(state_dir, "sector_etfs", [
            {"date": "2026-05-05", "equity": 3191.73},
        ])

        # First run
        audit1 = FIX.run_fix(
            dry_run=False,
            db_path=db_path,
            state_dir=state_dir,
            audit_log_path=audit_log,
        )
        assert audit1["status"] == "ok"

        # Record state after first run
        ce_sqlite = _read_sqlite_equity(db_path, "commodity_etfs", "2026-05-05")
        se_sqlite = _read_sqlite_equity(db_path, "sector_etfs", "2026-05-05")
        ce_json_count = len(_read_json_equity_history(state_dir, "commodity_etfs"))
        se_json_count = len(_read_json_equity_history(state_dir, "sector_etfs"))

        # Second run — should be no-op
        audit2 = FIX.run_fix(
            dry_run=False,
            db_path=db_path,
            state_dir=state_dir,
            audit_log_path=audit_log,
        )
        assert audit2["status"] == "ok"

        # Values unchanged
        assert _read_sqlite_equity(db_path, "commodity_etfs", "2026-05-05") == ce_sqlite
        assert _read_sqlite_equity(db_path, "sector_etfs", "2026-05-05") == se_sqlite
        # Row counts unchanged (no duplicate appends)
        assert len(_read_json_equity_history(state_dir, "commodity_etfs")) == ce_json_count
        assert len(_read_json_equity_history(state_dir, "sector_etfs")) == se_json_count

        # All second-run actions should be "skipped_idempotent"
        action_types = {a["action"] for a in audit2["market_id_changes"]}
        assert all("skipped_idempotent" in t for t in action_types), action_types
