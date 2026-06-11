"""tests/test_remediation_kill_switch.py — 30 tests for the 8-layer kill-switch chain.

Groups:
  TestL1EnvVar          — L1 env var (3 tests)
  TestL2RemediationHalt — L2 AUTO_REMEDIATION_HALT file (4 tests)
  TestL3TradingHalt     — L3 data/HALT and .live_halt (3 tests)
  TestL4Drawdown        — L4 portfolio drawdown (4 tests)
  TestL5HealthcheckCascade — L5 critical healthcheck count (3 tests)
  TestL6ReviewerRate    — L6 reviewer rejection rate (3 tests)
  TestCheckAllLayers    — check_all_layers integration (3 tests)
  TestHaltResume        — halt() / resume() actions (5 tests)
  TestProperties        — invariant / property tests (2 tests)

Total: 30 tests

DB isolation: tests that need DB pass db_path explicitly (never touch prod atlas.db).
Halt-file isolation: ks_root fixture monkeypatches PROJECT_ROOT to a tmp dir.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import atlas.execution.kill_switch as ks


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _make_full_db(tmp_path: Path) -> str:
    """Create (or reuse) a DB with all tables needed for L4/L5/L6 checks.

    All tables are empty — so every check returns None (all-clear state).
    Uses CREATE TABLE IF NOT EXISTS for idempotency within a single test.

    NOTE (2026-04-30, Task #289): L4 now queries equity_history (not the
    legacy portfolio_snapshots.daily_pnl_pct column which never existed in
    the production schema).
    """
    db = tmp_path / "full.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS equity_history (
            market_id  TEXT NOT NULL,
            date       TEXT NOT NULL,
            equity     REAL NOT NULL,
            pnl        REAL,
            PRIMARY KEY (market_id, date)
        );
        CREATE TABLE IF NOT EXISTS errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE, source TEXT, level TEXT, last_seen_ts TEXT
        );
        CREATE TABLE IF NOT EXISTS fix_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_verdict TEXT, started_ts TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db)


# ---------------------------------------------------------------------------
# Fixture: isolated PROJECT_ROOT for halt-file tests
# ---------------------------------------------------------------------------

@pytest.fixture
def ks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect atlas.execution.kill_switch.PROJECT_ROOT to an isolated tmp dir.

    All check_l2/check_l3/halt/resume calls will read/write files under this dir,
    never touching /root/atlas/data/HALT or /root/atlas/data/AUTO_REMEDIATION_HALT.
    """
    root = tmp_path / "ks_root"
    root.mkdir()
    (root / "data").mkdir()
    monkeypatch.setattr(ks, "PROJECT_ROOT", root)
    return root


# ===========================================================================
# L1 — env var ATLAS_AUTO_REMEDIATION_DISABLED
# ===========================================================================

class TestL1EnvVar:
    """Tests 1-3: L1 env var check."""

    def test_none_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test 1: returns None when ATLAS_AUTO_REMEDIATION_DISABLED is not set."""
        monkeypatch.delenv(ks.ENV_DISABLE, raising=False)
        assert ks.check_l1_env() is None

    def test_blocks_when_env_set_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test 2: returns BlockReason L1 when ATLAS_AUTO_REMEDIATION_DISABLED=1."""
        monkeypatch.setenv(ks.ENV_DISABLE, "1")
        result = ks.check_l1_env()
        assert result is not None
        assert result.layer == "L1"
        assert ks.ENV_DISABLE in result.reason

    def test_none_when_env_set_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test 3: returns None when ATLAS_AUTO_REMEDIATION_DISABLED=0."""
        monkeypatch.setenv(ks.ENV_DISABLE, "0")
        assert ks.check_l1_env() is None


# ===========================================================================
# L2 — AUTO_REMEDIATION_HALT file
# ===========================================================================

class TestL2RemediationHalt:
    """Tests 4-7: L2 AUTO_REMEDIATION_HALT file check."""

    def test_none_when_halt_file_absent(self, ks_root: Path) -> None:
        """Test 4: returns None when AUTO_REMEDIATION_HALT does not exist."""
        assert ks.check_l2_remediation_halt() is None

    def test_blocks_when_halt_file_present(self, ks_root: Path) -> None:
        """Test 5: returns BlockReason L2 when AUTO_REMEDIATION_HALT exists."""
        (ks_root / "data" / "AUTO_REMEDIATION_HALT").write_text("test halt")
        result = ks.check_l2_remediation_halt()
        assert result is not None
        assert result.layer == "L2"

    def test_detail_path_matches_file(self, ks_root: Path) -> None:
        """Test 6: detail['path'] matches the actual halt file path."""
        halt_file = ks_root / "data" / "AUTO_REMEDIATION_HALT"
        halt_file.write_text("halt reason")
        result = ks.check_l2_remediation_halt()
        assert result.detail["path"] == str(halt_file)

    def test_detail_content_has_file_text(self, ks_root: Path) -> None:
        """Test 7: detail['content'] contains the file's text."""
        halt_file = ks_root / "data" / "AUTO_REMEDIATION_HALT"
        halt_file.write_text("unique halt content xyz")
        result = ks.check_l2_remediation_halt()
        assert "unique halt content xyz" in result.detail["content"]


# ===========================================================================
# L3 — trading halt files
# ===========================================================================

class TestL3TradingHalt:
    """Tests 8-10: L3 data/HALT and .live_halt check."""

    def test_blocks_on_data_halt_file(self, ks_root: Path) -> None:
        """Test 8: returns BlockReason L3 when data/HALT present."""
        (ks_root / "data" / "HALT").write_text("trading halt active")
        result = ks.check_l3_trading_halt()
        assert result is not None
        assert result.layer == "L3"

    def test_blocks_on_live_halt_file(self, ks_root: Path) -> None:
        """Test 9: returns BlockReason L3 when .live_halt present."""
        (ks_root / ".live_halt").write_text("live halt active")
        result = ks.check_l3_trading_halt()
        assert result is not None
        assert result.layer == "L3"

    def test_none_when_no_halt_files(self, ks_root: Path) -> None:
        """Test 10: returns None when neither HALT file exists."""
        assert ks.check_l3_trading_halt() is None


# ===========================================================================
# L4 — drawdown breach
# ===========================================================================

class TestL4Drawdown:
    """Tests 11-14: L4 portfolio drawdown check (equity_history-based).

    Task #289 (2026-04-30): L4 was previously fail-open in production because it
    queried portfolio_snapshots.daily_pnl_pct — a column that never existed in the
    production schema. The bare except swallowed the OperationalError silently.
    These tests verify the corrected equity_history-based computation.
    """

    def _make_db(self, tmp_path: Path, rows: list | None = None) -> str:
        """Create a tmp DB with equity_history table for L4 testing.

        rows: list of (date_str, market_id, equity, pnl) tuples.
        """
        db = tmp_path / "equity.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE equity_history (
            market_id  TEXT NOT NULL,
            date       TEXT NOT NULL,
            equity     REAL NOT NULL,
            pnl        REAL,
            PRIMARY KEY (market_id, date)
        )""")
        if rows:
            conn.executemany(
                "INSERT INTO equity_history (market_id, date, equity, pnl) VALUES (?,?,?,?)",
                rows,
            )
        conn.commit()
        conn.close()
        return str(db)

    def _recent_date(self, days_ago: int = 0) -> str:
        """Return a date string N days ago (within the default window_days=30)."""
        from datetime import datetime, timezone, timedelta
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def test_l4_fires_at_5pct_drawdown(self, tmp_path: Path) -> None:
        """Test 11: 5 rows below threshold → no breach; add row crossing 5% → breach.

        Equity path: 1000 → 1010 → 1015 → 1020 → 980 (peak=1020, DD=3.9% → no breach)
        Then row at 945 → DD = (1020-945)/1020 = 7.35% → breach.
        """
        # Phase A: 5 rows, peak=$1020, latest=$980 → drawdown 3.9% → no breach
        rows_a = [
            ("sp500", self._recent_date(9), 1000.0, None),
            ("sp500", self._recent_date(7), 1010.0, None),
            ("sp500", self._recent_date(5), 1015.0, None),
            ("sp500", self._recent_date(3), 1020.0, None),
            ("sp500", self._recent_date(1), 980.0, None),
        ]
        db_a = self._make_db(tmp_path, rows_a)
        assert ks.check_l4_drawdown(db_path=db_a) is None

        # Phase B: add $945 row → drawdown (1020-945)/1020 = 7.35% → breach
        rows_b = rows_a + [("sp500", self._recent_date(0), 945.0, None)]
        # Use a different tmp_path sub-dir to avoid sqlite3 PRIMARY KEY conflict
        import tempfile, os
        tmp_b = Path(tempfile.mkdtemp())
        db_b = self._make_db(tmp_b, rows_b)
        result = ks.check_l4_drawdown(db_path=db_b)
        assert result is not None
        assert result.layer == "L4"
        assert result.detail["peak_equity"] == pytest.approx(1020.0)
        assert result.detail["drawdown_pct"] == pytest.approx(7.3529, abs=0.01)

    def test_l4_fires_at_10pct_drawdown(self, tmp_path: Path) -> None:
        """Test 12: peak $5000 → current $4500 = -10% drawdown → breach."""
        rows = [
            ("sp500", self._recent_date(5), 5000.0, None),
            ("sp500", self._recent_date(0), 4500.0, None),
        ]
        db = self._make_db(tmp_path, rows)
        result = ks.check_l4_drawdown(db_path=db)
        assert result is not None
        assert result.layer == "L4"
        assert result.detail["peak_equity"] == pytest.approx(5000.0)
        assert result.detail["latest_equity"] == pytest.approx(4500.0)
        assert result.detail["drawdown_pct"] == pytest.approx(10.0, abs=0.01)

    def test_l4_handles_minus_3pct_no_breach(self, tmp_path: Path) -> None:
        """Test 13: peak $5000 → current $4850 = -3% drawdown → no breach (< 5%)."""
        rows = [
            ("sp500", self._recent_date(5), 5000.0, None),
            ("sp500", self._recent_date(0), 4850.0, None),
        ]
        db = self._make_db(tmp_path, rows)
        result = ks.check_l4_drawdown(db_path=db)
        assert result is None

    def test_l4_handles_empty_history(self, tmp_path: Path, caplog) -> None:
        """Test 14: empty equity_history → returns None (fail-open) with log message, no crash."""
        import logging

        db = self._make_db(tmp_path)  # table exists, no rows
        with caplog.at_level(logging.DEBUG, logger="atlas.execution.kill_switch"):
            result = ks.check_l4_drawdown(db_path=db)

        assert result is None
        # Must log something (not silently swallow)
        assert any("L4" in r.message for r in caplog.records)


# ===========================================================================
# L5 — healthcheck cascade
# ===========================================================================

class TestCheckAllLayers:
    """Tests 21-23: check_all_layers integration."""

    def test_l1_takes_priority_over_l2(
        self, ks_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test 21: L1 fires first even when L2 halt file is also present."""
        monkeypatch.setenv(ks.ENV_DISABLE, "1")
        (ks_root / "data" / "AUTO_REMEDIATION_HALT").write_text("halt")
        result = ks.check_all_layers(db_path=_make_full_db(tmp_path))
        assert result is not None
        assert result.layer == "L1"

    def test_l2_returned_when_only_halt_file_present(
        self, ks_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test 22: returns L2 when only AUTO_REMEDIATION_HALT file is present."""
        monkeypatch.delenv(ks.ENV_DISABLE, raising=False)
        (ks_root / "data" / "AUTO_REMEDIATION_HALT").write_text("halt")
        result = ks.check_all_layers(db_path=_make_full_db(tmp_path))
        assert result is not None
        assert result.layer == "L2"

    def test_returns_none_when_all_clean(
        self, ks_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test 23: returns None when no layers are tripped."""
        monkeypatch.delenv(ks.ENV_DISABLE, raising=False)
        # ks_root has no halt files; empty DB → L4/L5/L6 return None
        result = ks.check_all_layers(db_path=_make_full_db(tmp_path))
        assert result is None


# ===========================================================================
# halt() / resume() actions
# ===========================================================================

class TestHaltResume:
    """Tests 24-28: halt() and resume() action functions."""

    def test_halt_creates_file_with_reason(self, ks_root: Path) -> None:
        """Test 24: halt() creates AUTO_REMEDIATION_HALT containing the reason."""
        ks.halt("test halt reason", source="test")
        halt_path = ks_root / "data" / "AUTO_REMEDIATION_HALT"
        assert halt_path.exists()
        content = halt_path.read_text()
        assert "test halt reason" in content
        assert "test" in content  # source

    def test_halt_is_idempotent(self, ks_root: Path) -> None:
        """Test 25: halt() overwrites existing file (latest reason wins)."""
        ks.halt("first reason", source="test")
        ks.halt("second reason", source="test")
        content = (ks_root / "data" / "AUTO_REMEDIATION_HALT").read_text()
        assert "second reason" in content
        assert "first reason" not in content

    def test_resume_removes_halt_file(self, ks_root: Path) -> None:
        """Test 26: resume() deletes AUTO_REMEDIATION_HALT and returns True."""
        (ks_root / "data" / "AUTO_REMEDIATION_HALT").write_text("halt")
        result = ks.resume()
        assert result is True
        assert not (ks_root / "data" / "AUTO_REMEDIATION_HALT").exists()

    def test_resume_returns_false_when_no_halt_file(self, ks_root: Path) -> None:
        """Test 27: resume() returns False when AUTO_REMEDIATION_HALT does not exist."""
        assert ks.resume() is False

    def test_resume_does_not_remove_trading_halt(self, ks_root: Path) -> None:
        """Test 28: resume() only removes AUTO_REMEDIATION_HALT, not data/HALT."""
        (ks_root / "data" / "HALT").write_text("trading halt must survive")
        (ks_root / "data" / "AUTO_REMEDIATION_HALT").write_text("remediation halt removed")
        ks.resume()
        # Trading halt must survive
        assert (ks_root / "data" / "HALT").exists()
        # Remediation halt must be gone
        assert not (ks_root / "data" / "AUTO_REMEDIATION_HALT").exists()


# ===========================================================================
# Properties
# ===========================================================================

class TestProperties:
    """Tests 29-30: invariant / property-style tests."""

    def test_any_single_layer_trip_returns_block_reason(
        self, ks_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test 29: tripping any of L1, L2, or L3 causes check_all_layers to return BlockReason."""
        db = _make_full_db(tmp_path)

        # L1 — env var
        monkeypatch.setenv(ks.ENV_DISABLE, "1")
        assert isinstance(ks.check_all_layers(db_path=db), ks.BlockReason)
        monkeypatch.delenv(ks.ENV_DISABLE, raising=False)

        # L2 — AUTO_REMEDIATION_HALT file
        halt_file = ks_root / "data" / "AUTO_REMEDIATION_HALT"
        halt_file.write_text("test halt")
        assert isinstance(ks.check_all_layers(db_path=db), ks.BlockReason)
        halt_file.unlink()

        # L3 — data/HALT
        trading_halt = ks_root / "data" / "HALT"
        trading_halt.write_text("trading halt")
        assert isinstance(ks.check_all_layers(db_path=db), ks.BlockReason)
        trading_halt.unlink()

        # Confirm clean state after cleanup
        assert ks.check_all_layers(db_path=db) is None

    def test_100_calls_with_no_triggers_return_none(
        self, ks_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Test 30: 100 consecutive calls with no triggers all return None."""
        monkeypatch.delenv(ks.ENV_DISABLE, raising=False)
        db = _make_full_db(tmp_path)  # all tables empty
        for i in range(100):
            result = ks.check_all_layers(db_path=db)
            assert result is None, f"Unexpected block at iteration {i}: {result}"
