"""tests/test_remediation_budget.py — 23 tests for core/budget.py.

Groups:
  TestCheckBudgetProceed   — PROCEED paths (6 tests)
  TestCheckBudgetHalt      — HALT paths via all 3 layers (5 tests)
  TestCheckBudgetAlert     — ALERT paths and small-sample guard (4 tests)
  TestEnforceBudget        — enforce_budget side-effects (6 tests)
  TestHelpers              — count_commits_24h, count_reverts_24h, revert_rate_24h (3 tests)
  TestLoadBudgetConfig     — _load_budget_config fallback and real config (2 tests)

Total: 23 tests (spec requires ≥15)

DB isolation: every test creates its own tmp DB via _make_db_with_schema(tmp_path).
Telegram isolation: tests that call enforce_budget mock utils.telegram.send_message.
Halt-file isolation: tests that need AUTO_REMEDIATION_HALT use the ks_root fixture.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from core.budget import (
    BudgetDecision,
    _load_budget_config,
    check_budget,
    count_commits_24h,
    count_reverts_24h,
    enforce_budget,
    revert_rate_24h,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ago_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _make_db_with_schema(tmp_path: Path) -> str:
    """Create a tmp DB with the fix_attempts schema (as per spec)."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE errors (id INTEGER PRIMARY KEY, fingerprint TEXT);
        CREATE TABLE fix_attempts (
            id INTEGER PRIMARY KEY, error_id INTEGER, fingerprint TEXT,
            started_ts TEXT, finished_ts TEXT, reverted_ts TEXT,
            status TEXT, classification TEXT, review_verdict TEXT
        );
    """)
    conn.commit()
    conn.close()
    return str(db)


def _insert_merged(conn: sqlite3.Connection, n_hours_ago: float = 0) -> None:
    """Insert a merged fix_attempt within the last 24h (or older if n_hours_ago > 24)."""
    ts = _ago_iso(n_hours_ago)
    conn.execute(
        """INSERT INTO fix_attempts (started_ts, finished_ts, status)
           VALUES (?, ?, 'merged')""",
        (ts, ts),
    )


def _insert_reverted(conn: sqlite3.Connection, n_hours_ago: float = 0) -> None:
    """Insert a reverted fix_attempt."""
    ts = _ago_iso(n_hours_ago)
    conn.execute(
        """INSERT INTO fix_attempts (started_ts, finished_ts, reverted_ts, status)
           VALUES (?, ?, ?, 'reverted')""",
        (ts, ts, ts),
    )


# ---------------------------------------------------------------------------
# Fixture: test YAML config with user-locked values
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "auto_remediation.yaml"
    cfg.write_text(
        "budget:\n"
        "  max_commits_per_day: 10\n"
        "  reverts_to_halt: 2\n"
        "  revert_rate_alert_pct: 15\n"
        "  revert_rate_halt_pct: 25\n"
    )
    return cfg


# ---------------------------------------------------------------------------
# Fixture: isolated AUTO_REMEDIATION_HALT location
# ---------------------------------------------------------------------------

@pytest.fixture
def ks_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect core.remediation_kill_switch.PROJECT_ROOT to tmp dir."""
    import core.remediation_kill_switch as _ks
    root = tmp_path / "ks_root"
    root.mkdir()
    (root / "data").mkdir()
    monkeypatch.setattr(_ks, "PROJECT_ROOT", root)
    return root


# ===========================================================================
# TestCheckBudgetProceed
# ===========================================================================

class TestCheckBudgetProceed:
    """Tests 1-2, 5: check_budget returns PROCEED in safe scenarios."""

    def test_proceed_when_empty_db(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 1: PROCEED when no commits and no reverts."""
        db = _make_db_with_schema(tmp_path)
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"
        assert decision.metric["commits_24h"] == 0
        assert decision.metric["reverted_24h"] == 0

    def test_proceed_when_5_commits_no_reverts(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 2: PROCEED when 5 merged commits, 0 reverts (well under caps)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"
        assert decision.metric["commits_24h"] == 5

    def test_proceed_when_9_commits(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 5: PROCEED when 9 merged commits (under cap=10)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(9):
            _insert_merged(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"

    def test_proceed_when_1_revert(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 7: PROCEED when 1 revert (under reverts_to_halt=2)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(3):
            _insert_merged(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"

    def test_proceed_when_4_merged_no_reverts(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 10: PROCEED when 4 merged, 0 reverts — rate check has no trigger."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(4):
            _insert_merged(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"

    def test_proceed_when_3_merged_1_revert_small_sample(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 12: PROCEED when 3 merged + 1 reverted — rate=33% but sample<4."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(3):
            _insert_merged(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"
        # Rate computed but suppressed due to small sample
        assert decision.metric["revert_rate_pct"] == pytest.approx(33.33, abs=0.1)


# ===========================================================================
# TestCheckBudgetHalt
# ===========================================================================

class TestCheckBudgetHalt:
    """Tests 3-4, 6, 9: check_budget returns HALT."""

    def test_halt_when_commit_cap_exactly_reached(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 3: HALT when exactly 10 merged commits in 24h (max_commits_per_day=10)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(10):
            _insert_merged(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "HALT"
        assert "commit cap" in decision.reason.lower()
        assert decision.metric["commits_24h"] == 10

    def test_halt_when_11_commits(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 4: HALT when 11 merged commits in 24h (over cap)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(11):
            _insert_merged(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "HALT"

    def test_halt_when_2_reverts(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 6: HALT when 2 reverts in 24h (reverts_to_halt=2)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        _insert_merged(conn)  # 1 merged so rate computation has denominator
        _insert_reverted(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "HALT"
        assert "revert count" in decision.reason.lower()

    def test_halt_when_2_reverts_with_high_rate(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 9: HALT when 5 merged + 2 reverts — absolute count fires first."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        _insert_reverted(conn)
        _insert_reverted(conn)  # 2nd revert → absolute count halt
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        # HALT must be returned — either absolute count or rate triggered
        assert decision.action == "HALT"

    def test_halt_when_revert_rate_exceeds_halt_threshold(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Extra: HALT when rate ≥25% with only 1 revert (under absolute count threshold)."""
        # 4 merged + 1 reverted = 25% which is exactly rate_halt_pct=25 → HALT
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(4):
            _insert_merged(conn)
        _insert_reverted(conn)  # 25% rate, 1 revert < 2 threshold → rate-halt
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "HALT"
        assert "halt threshold" in decision.reason.lower()


# ===========================================================================
# TestCheckBudgetAlert
# ===========================================================================

class TestCheckBudgetAlert:
    """Tests 8, 11-12: check_budget returns ALERT and small-sample guard."""

    def test_alert_when_revert_rate_between_thresholds(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 8: ALERT when 5 merged + 1 reverted = 20% (alert=15%, halt=25%)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        _insert_reverted(conn)  # 1/5 = 20% > alert threshold
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "ALERT"
        assert decision.metric["revert_rate_pct"] == pytest.approx(20.0, abs=0.1)
        assert "alert threshold" in decision.reason.lower()

    def test_alert_requires_min_4_merged_sample(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 11: ALERT only fires when merged_24h >= 4 (small-sample guard)."""
        # 3 merged + 1 reverted = 33% — above alert threshold but sample < 4
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(3):
            _insert_merged(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"  # suppressed by small-sample guard

    def test_no_alert_when_revert_rate_below_alert_threshold(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 11b: PROCEED when rate < alert_threshold (14% < 15%)."""
        # 7 merged + 1 reverted = ~14.3% → just below alert
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(7):
            _insert_merged(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "PROCEED"
        assert decision.metric["revert_rate_pct"] == pytest.approx(14.29, abs=0.1)

    def test_alert_vs_halt_boundary(self, tmp_path: Path, cfg_path: Path) -> None:
        """Extra: 5 merged + 1 reverted = 20% is ALERT not HALT (< halt=25%)."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        _insert_reverted(conn)
        conn.commit()
        conn.close()
        decision = check_budget(db_path=db, cfg_path=cfg_path)
        assert decision.action == "ALERT"  # not HALT


# ===========================================================================
# TestEnforceBudget
# ===========================================================================

class TestEnforceBudget:
    """Tests 13-18: enforce_budget side-effects (halt file creation, Telegram)."""

    def test_halt_decision_creates_halt_file(
        self, tmp_path: Path, cfg_path: Path, ks_root: Path
    ) -> None:
        """Test 13: enforce_budget with HALT decision creates AUTO_REMEDIATION_HALT."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(10):
            _insert_merged(conn)
        conn.commit()
        conn.close()

        enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=False)

        assert (ks_root / "data" / "AUTO_REMEDIATION_HALT").exists()

    def test_alert_decision_does_not_create_halt_file(
        self, tmp_path: Path, cfg_path: Path, ks_root: Path
    ) -> None:
        """Test 14: enforce_budget with ALERT decision does NOT create halt file."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        _insert_reverted(conn)  # 20% → ALERT
        conn.commit()
        conn.close()

        enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=False)

        assert not (ks_root / "data" / "AUTO_REMEDIATION_HALT").exists()

    def test_proceed_decision_does_not_create_halt_file(
        self, tmp_path: Path, cfg_path: Path, ks_root: Path
    ) -> None:
        """Test 15: enforce_budget with PROCEED decision does NOT create halt file."""
        db = _make_db_with_schema(tmp_path)

        enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=False)

        assert not (ks_root / "data" / "AUTO_REMEDIATION_HALT").exists()

    def test_alert_calls_telegram_once(self, tmp_path: Path, cfg_path: Path) -> None:
        """Test 16: enforce_budget ALERT calls send_message exactly once."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(5):
            _insert_merged(conn)
        _insert_reverted(conn)  # 20% → ALERT
        conn.commit()
        conn.close()

        with patch("utils.telegram.send_message") as mock_tg:
            enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=True)
        assert mock_tg.call_count == 1

    def test_halt_calls_telegram_once(
        self, tmp_path: Path, cfg_path: Path, ks_root: Path
    ) -> None:
        """Test 17: enforce_budget HALT calls send_message exactly once."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        for _ in range(10):
            _insert_merged(conn)
        conn.commit()
        conn.close()

        with patch("utils.telegram.send_message") as mock_tg:
            enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=True)
        assert mock_tg.call_count == 1

    def test_proceed_does_not_call_telegram(
        self, tmp_path: Path, cfg_path: Path
    ) -> None:
        """Test 18: enforce_budget PROCEED does NOT call send_message."""
        db = _make_db_with_schema(tmp_path)

        with patch("utils.telegram.send_message") as mock_tg:
            enforce_budget(db_path=db, cfg_path=cfg_path, send_alert=True)
        assert mock_tg.call_count == 0


# ===========================================================================
# TestHelpers
# ===========================================================================

class TestHelpers:
    """Tests 19-21: count_commits_24h, count_reverts_24h, revert_rate_24h."""

    def test_count_commits_excludes_old_commits(self, tmp_path: Path) -> None:
        """Test 19: count_commits_24h only counts commits with finished_ts within 24h."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        # 3 recent
        for _ in range(3):
            _insert_merged(conn, n_hours_ago=0)
        # 2 old (> 24h)
        for _ in range(2):
            _insert_merged(conn, n_hours_ago=25)
        conn.commit()
        count = count_commits_24h(conn)
        conn.close()
        assert count == 3

    def test_count_reverts_excludes_old_reverts(self, tmp_path: Path) -> None:
        """Test 20: count_reverts_24h only counts reverts with reverted_ts within 24h."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        _insert_reverted(conn, n_hours_ago=0)   # recent
        _insert_reverted(conn, n_hours_ago=25)  # old
        conn.commit()
        count = count_reverts_24h(conn)
        conn.close()
        assert count == 1

    def test_revert_rate_24h_zero_when_no_merged(self, tmp_path: Path) -> None:
        """Test 21: revert_rate_24h returns (0, 0, 0.0) when no merged rows exist."""
        db = _make_db_with_schema(tmp_path)
        conn = sqlite3.connect(db)
        merged, reverted, rate = revert_rate_24h(conn)
        conn.close()
        assert merged == 0
        assert reverted == 0
        assert rate == 0.0


# ===========================================================================
# TestLoadBudgetConfig
# ===========================================================================

class TestLoadBudgetConfig:
    """Tests 22-23: _load_budget_config fallback and real config."""

    def test_falls_back_to_defaults_when_yaml_missing(self, tmp_path: Path) -> None:
        """Test 22: _load_budget_config returns hardcoded defaults when YAML not found."""
        missing = tmp_path / "does_not_exist.yaml"
        cfg = _load_budget_config(cfg_path=missing)
        assert cfg["max_commits_per_day"] == 10
        assert cfg["reverts_to_halt"] == 2
        assert cfg["revert_rate_alert_pct"] == 15
        assert cfg["revert_rate_halt_pct"] == 25

    def test_loads_user_locked_values_from_real_config(self) -> None:
        """Test 23: real config/auto_remediation.yaml has the user-locked values 10/2/15/25."""
        cfg = _load_budget_config()  # Uses CFG_PATH = PROJECT_ROOT/config/auto_remediation.yaml
        assert cfg["max_commits_per_day"] == 10
        assert cfg["reverts_to_halt"] == 2
        assert cfg["revert_rate_alert_pct"] == 15
        assert cfg["revert_rate_halt_pct"] == 25
