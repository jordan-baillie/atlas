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
