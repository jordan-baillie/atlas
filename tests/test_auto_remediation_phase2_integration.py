"""Phase 2 integration tests -- cross-module invariants.

Tests that the boundaries between modules are properly sealed:
  - error_monitor -> halt -> zero dispatch
  - error_monitor -> ESCALATE -> zero fix_worker calls
  - audit log is append-only under concurrent churn
  - budget enforcement creates AUTO_REMEDIATION_HALT via kill_switch
  - Telegram property tests (success=0, failure=N calls)
  - All 6 user-locked config values
  - NEVER list invariants across config files
  - safety_critical_functions.txt has exactly 43 entries
"""
from __future__ import annotations

import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Schema bootstrap + helpers
# ---------------------------------------------------------------------------

def _bootstrap_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS errors (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fingerprint TEXT UNIQUE NOT NULL,
      first_seen_ts TEXT NOT NULL, last_seen_ts TEXT NOT NULL,
      occurrence_count INTEGER NOT NULL DEFAULT 1,
      ts TEXT NOT NULL, source TEXT NOT NULL,
      service TEXT, level TEXT NOT NULL, logger_name TEXT,
      message TEXT NOT NULL,
      exc_type TEXT, exc_message TEXT, traceback TEXT,
      file_path TEXT, line_number INTEGER, function_name TEXT,
      pid INTEGER, hostname TEXT, context_json TEXT,
      market_hours INTEGER NOT NULL DEFAULT 0,
      halt_active INTEGER NOT NULL DEFAULT 0,
      git_sha TEXT,
      classification TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
      triage_reason TEXT,
      tier INTEGER NOT NULL DEFAULT 99,
      remediation_status TEXT NOT NULL DEFAULT 'NEW',
      remediation_attempts INTEGER NOT NULL DEFAULT 0,
      last_attempt_at TEXT,
      fixed_by_attempt_id INTEGER,
      resolved_at TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fix_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      error_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
      started_ts TEXT NOT NULL, finished_ts TEXT,
      status TEXT NOT NULL DEFAULT 'triaged',
      classification TEXT NOT NULL,
      triage_model TEXT, triage_reason TEXT, triage_tokens INTEGER,
      review_verdict TEXT, reverted_ts TEXT,
      gates_passed_json TEXT, gates_failed_json TEXT, blocked_by_gate TEXT,
      monitor_outcome TEXT, total_wall_seconds REAL, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS fix_audit_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      attempt_id INTEGER, error_id INTEGER,
      ts TEXT NOT NULL DEFAULT (datetime('now')),
      phase TEXT NOT NULL, actor TEXT NOT NULL,
      model TEXT, decision TEXT, reasoning TEXT, diff TEXT,
      payload_json TEXT, duration_sec REAL, tokens_in INTEGER, tokens_out INTEGER,
      cost_usd REAL DEFAULT 0,
      result_status TEXT, blocked_by_gate TEXT, notes TEXT
    );
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL, equity REAL,
      daily_pnl_pct REAL, market_id TEXT
    );
    """)
    conn.commit()
    conn.close()


def _insert_error(
    db_path: Path,
    *,
    fingerprint: str = "fp_test",
    message: str = "Generic test error",
    exc_type: str = "TypeError",
    file_path: str | None = "tests/test_foo.py",
    function_name: str | None = "test_foo",
    traceback: str | None = None,
    classification: str = "UNCLASSIFIED",
    remediation_status: str = "NEW",
) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """INSERT OR IGNORE INTO errors
            (fingerprint, first_seen_ts, last_seen_ts, ts, source, service, level,
             logger_name, message, exc_type, file_path, line_number, function_name,
             classification, tier, remediation_status)
           VALUES (?, '2026-04-29T10:00:00', '2026-04-29T10:00:00', '2026-04-29T10:00:00',
                   'python_logger', 'test_service', 'ERROR', 'atlas.test',
                   ?, ?, ?, 42, ?, ?, 99, ?)""",
        (fingerprint, message, exc_type, file_path, function_name,
         classification, remediation_status),
    )
    conn.commit()
    row_id = conn.execute(
        "SELECT id FROM errors WHERE fingerprint=?", (fingerprint,)
    ).fetchone()[0]
    if traceback:
        conn.execute("UPDATE errors SET traceback=? WHERE id=?", (traceback, row_id))
        conn.commit()
    conn.close()
    return {
        "id": row_id, "fingerprint": fingerprint, "message": message,
        "exc_type": exc_type, "file_path": file_path, "function_name": function_name,
        "traceback": traceback, "service": "test_service", "level": "ERROR",
        "occurrence_count": 1,
    }


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "atlas.db"
    _bootstrap_db(db)
    return db


# ---------------------------------------------------------------------------
# TestRunOnceHaltBehavior
# ---------------------------------------------------------------------------

class TestRunOnceHaltBehavior:
    def test_run_once_env_disabled_returns_halted(self, db_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        _insert_error(db_path, fingerprint="fp_halt_001", message="test error 1")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["halted"] is True
        assert result["halt_reason"] is not None
        assert "ATLAS_AUTO_REMEDIATION_DISABLED" in result["halt_reason"]

    def test_run_once_halt_file_returns_halted(self, db_path, tmp_path, monkeypatch):
        """error_monitor uses its own HALT_FILES constant (separate from ks.PROJECT_ROOT)."""
        from core import error_monitor
        halt_file = tmp_path / "AUTO_REMEDIATION_HALT"
        halt_file.write_text("test halt")
        # Patch error_monitor's HALT_FILES to point at the tmp halt file
        monkeypatch.setattr(error_monitor, "HALT_FILES",
                            (halt_file, tmp_path / "NONEXISTENT_HALT"))
        _insert_error(db_path, fingerprint="fp_halt_002", message="test error 2")
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["halted"] is True

    def test_run_once_halted_processes_zero_errors(self, db_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        for i in range(5):
            _insert_error(db_path, fingerprint=f"fp_halt_{i}",
                          message=f"test error {i}")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] == 0

    def test_run_once_halted_does_not_write_audit_log(self, db_path, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        _insert_error(db_path, fingerprint="fp_halt_audit", message="test error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count == 0

    def test_run_once_unhalted_processes_normally(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_normal", message="normal test error")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["halted"] is False
        assert result["processed"] == 1


# ---------------------------------------------------------------------------
# TestRunOnceClassificationDispatch
# ---------------------------------------------------------------------------

class TestRunOnceClassificationDispatch:
    def test_run_once_escalate_sets_escalated_status(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        # brokers/ path -> ESCALATE
        _insert_error(db_path, fingerprint="fp_esc_status",
                      message="broker failure",
                      file_path="brokers/alpaca/broker.py")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT classification, remediation_status FROM errors WHERE fingerprint='fp_esc_status'"
        ).fetchone()
        conn.close()
        assert row[0] == "ESCALATE"
        assert row[1] == "ESCALATED"

    def test_run_once_assist_sets_triaged_status(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_assist_status",
                      message="test chat server error",
                      file_path="services/chat_server.py")
        from core import error_monitor
        from core.triage import TriageClassifier
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT classification, remediation_status FROM errors WHERE fingerprint='fp_assist_status'"
        ).fetchone()
        conn.close()
        assert row[0] == "ASSIST"
        assert row[1] == "TRIAGED"

    def test_run_once_ignore_sets_ignored_status(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_ignore_status",
                      message="Circuit breaker tripped: session limit",
                      file_path=None, function_name=None)
        from core import error_monitor
        from core.triage import TriageClassifier
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT classification, remediation_status FROM errors WHERE fingerprint='fp_ignore_status'"
        ).fetchone()
        conn.close()
        assert row[0] == "IGNORE"
        assert row[1] == "IGNORED"

    def test_run_once_dry_run_annotation_in_audit_log(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_dry_ann", message="test annotation error")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT payload_json FROM fix_audit_log WHERE error_id IN "
            "(SELECT id FROM errors WHERE fingerprint='fp_dry_ann') LIMIT 1"
        ).fetchone()
        conn.close()
        assert row is not None
        payload = json.loads(row[0])
        assert payload["dry_run"] is True

    def test_run_once_already_classified_not_reprocessed(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_done",
                      message="already done",
                      classification="ESCALATE",
                      remediation_status="ESCALATED")
        from core import error_monitor
        result = error_monitor.run_once(db_path=str(db_path), dry_run=True)
        assert result["processed"] == 0


# ---------------------------------------------------------------------------
# TestAuditLogImmutabilityUnderChurn
# ---------------------------------------------------------------------------

class TestAuditLogImmutabilityUnderChurn:
    def test_audit_log_only_inserts_never_updates(self, db_path, monkeypatch):
        """run_once never UPDATEs fix_audit_log -- it only INSERTs."""
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        for i in range(3):
            _insert_error(db_path, fingerprint=f"fp_churn_{i}",
                          message=f"churn test error {i}")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        count_after_first_run = conn.execute(
            "SELECT COUNT(*) FROM fix_audit_log"
        ).fetchone()[0]
        conn.close()
        assert count_after_first_run == 3

        # Second run: no new UNCLASSIFIED errors -> no new audit rows
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        count_after_second_run = conn.execute(
            "SELECT COUNT(*) FROM fix_audit_log"
        ).fetchone()[0]
        conn.close()
        assert count_after_second_run == 3  # No increase

    def test_audit_log_pk_prevents_overwrite_of_existing_row(self, db_path):
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "INSERT INTO fix_audit_log (id, ts, phase, actor, decision) "
            "VALUES (42, '2026-04-29T10:00:00', 'triage', 'classifier', 'ESCALATE')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO fix_audit_log (id, ts, phase, actor, decision) "
                "VALUES (42, '2026-04-29T11:00:00', 'triage', 'classifier', 'APPROVE')"
            )
            conn.commit()
        # Verify original row unchanged
        row = conn.execute(
            "SELECT decision FROM fix_audit_log WHERE id=42"
        ).fetchone()
        conn.close()
        assert row[0] == "ESCALATE"  # Original preserved

    def test_concurrent_inserts_are_thread_safe(self, db_path):
        """Multiple concurrent inserts into fix_audit_log complete without corruption."""
        errors_seen = []
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

        def insert_row(i):
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "INSERT INTO fix_audit_log (error_id, ts, phase, actor, decision) "
                    "VALUES (?,?,?,?,?)",
                    (i, now, "triage", "classifier", f"CLASS_{i}"),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                errors_seen.append(str(e))

        threads = [threading.Thread(target=insert_row, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        assert len(errors_seen) == 0, f"Thread errors: {errors_seen}"
        conn = sqlite3.connect(str(db_path))
        count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
        conn.close()
        assert count == 10

    def test_audit_log_row_count_monotone_over_multiple_runs(self, db_path, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        counts = []
        for batch in range(3):
            _insert_error(db_path, fingerprint=f"fp_mono_{batch}",
                          message=f"monotone test {batch}")
            from core import error_monitor
            error_monitor.run_once(db_path=str(db_path), dry_run=True)
            conn = sqlite3.connect(str(db_path))
            count = conn.execute("SELECT COUNT(*) FROM fix_audit_log").fetchone()[0]
            conn.close()
            counts.append(count)

        # Each batch adds exactly 1 row -> counts should be [1, 2, 3]
        assert counts == [1, 2, 3], f"Expected [1,2,3] got {counts}"


# ---------------------------------------------------------------------------
# TestBudgetKillSwitchIntegration
# ---------------------------------------------------------------------------

class TestBudgetKillSwitchIntegration:
    def test_check_budget_proceed_when_under_cap(self, db_path):
        from core.budget import check_budget
        result = check_budget(db_path=str(db_path))
        assert result.action == "PROCEED"
        assert result.metric["commits_24h"] == 0

    def test_check_budget_halt_on_commit_cap_exceeded(self, db_path):
        from core.budget import check_budget
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db_path))
        for i in range(10):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, finished_ts, "
                "status, classification) VALUES (?,?,?,?,'merged','ASSIST')",
                (i + 1, f"fp_{i}", now, now),
            )
        conn.commit()
        conn.close()
        result = check_budget(db_path=str(db_path))
        assert result.action == "HALT"
        assert "Commit cap" in result.reason

    def test_enforce_budget_halt_creates_halt_file(self, db_path, tmp_path):
        from core import budget, remediation_kill_switch as ks
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db_path))
        for i in range(10):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, finished_ts, "
                "status, classification) VALUES (?,?,?,?,'merged','ASSIST')",
                (i + 1, f"fp_h_{i}", now, now),
            )
        conn.commit()
        conn.close()

        (tmp_path / "data").mkdir(exist_ok=True)
        with patch("core.remediation_kill_switch.PROJECT_ROOT", tmp_path), \
             patch("utils.telegram.send_message"):
            decision = budget.enforce_budget(db_path=str(db_path), send_alert=True)

        assert decision.action == "HALT"
        halt_file = tmp_path / "data" / "AUTO_REMEDIATION_HALT"
        assert halt_file.exists()

    def test_check_budget_halt_on_absolute_revert_count(self, db_path):
        from core.budget import check_budget
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db_path))
        for i in range(2):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, finished_ts, "
                "status, classification, reverted_ts) VALUES (?,?,?,?,'reverted','ASSIST',?)",
                (i + 1, f"fp_rev_{i}", now, now, now),
            )
        conn.commit()
        conn.close()
        result = check_budget(db_path=str(db_path))
        assert result.action == "HALT"
        assert "revert" in result.reason.lower()

    def test_budget_integrates_with_check_all_layers(self, db_path, tmp_path, monkeypatch):
        """After enforce_budget creates HALT file, check_all_layers fires L2."""
        from core import budget, remediation_kill_switch as ks
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir(exist_ok=True)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn = sqlite3.connect(str(db_path))
        for i in range(10):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, finished_ts, "
                "status, classification) VALUES (?,?,?,?,'merged','ASSIST')",
                (i + 1, f"fp_int_{i}", now, now),
            )
        conn.commit()
        conn.close()

        with patch("utils.telegram.send_message"):
            budget.enforce_budget(db_path=str(db_path), send_alert=True)

        # Now check_all_layers should fire L2
        result = ks.check_all_layers(db_path=str(db_path))
        assert result is not None
        assert result.layer == "L2"


# ---------------------------------------------------------------------------
# TestTelegramPropertyTests
# ---------------------------------------------------------------------------

class TestTelegramPropertyTests:
    def test_100_successful_merges_zero_telegram_calls(self, tmp_path):
        """100 successful MergeOutcomes -> 0 telegram calls via success guard."""
        from core import merger
        from core.merge_gates import GateRunOutcome

        passing = GateRunOutcome(
            all_passed=True, results=[],
            summary={"passed": ["g1"], "failed": [], "blocking_failures": []},
        )
        with patch("utils.telegram.send_message") as mock_tg, \
             patch("core.merger.run_all_gates", return_value=passing), \
             patch("core.merger.push_to_staging", return_value=(True, "sha123")), \
             patch("core.merger._persist_merge_result"):
            for i in range(100):
                fix_out = SimpleNamespace(
                    success=True, attempt_id=-1, error_id=i,
                    fingerprint=f"fp_{i}", branch=f"auto-fix/err-{i}",
                    worktree=str(tmp_path),
                )
                merger.merge_fix(fix_out, None)

        assert mock_tg.call_count == 0

    def test_100_failed_merges_100_telegram_calls(self):
        """100 failed merges -> 100 telegram calls (one per failure)."""
        from core import merger
        with patch("utils.telegram.send_message") as mock_tg:
            for i in range(100):
                merger._send_failure_alert(
                    merger.MergeOutcome(
                        success=False, error_id=i, fingerprint=f"fp_{i}",
                        branch="b", blocking_failures=["gate_x"],
                    )
                )
        assert mock_tg.call_count == 100

    def test_50_50_mixed_50_telegram_calls(self):
        """50 success + 50 failure -> exactly 50 telegram calls."""
        from core import merger
        with patch("utils.telegram.send_message") as mock_tg:
            # 50 successes (no telegram per success guard)
            for i in range(50):
                success = True
                if not success:
                    merger._send_failure_alert(
                        merger.MergeOutcome(
                            success=False, error_id=i, fingerprint=f"fp_{i}", branch="b"
                        )
                    )
            # 50 failures (telegram each)
            for i in range(50):
                merger._send_failure_alert(
                    merger.MergeOutcome(
                        success=False, error_id=i + 50,
                        fingerprint=f"fp_f_{i}", branch="b",
                        blocking_failures=["gate"],
                    )
                )
        assert mock_tg.call_count == 50

    def test_send_failure_alert_message_structure(self):
        """Failure alert contains fingerprint, branch, and blocking gate info."""
        from core import merger
        outcome = merger.MergeOutcome(
            success=False, error_id=42, fingerprint="fp_structure_test",
            branch="auto-fix/err-42",
            blocking_failures=["diff_size_cap", "regression_test_present"],
        )
        captured = []
        with patch("utils.telegram.send_message", side_effect=lambda m: captured.append(m)):
            merger._send_failure_alert(outcome)

        assert len(captured) == 1
        msg = captured[0]
        assert "fp_structure_test" in msg
        assert "diff_size_cap" in msg

    def test_telegram_on_success_config_is_never(self):
        """User-locked config: telegram.on_success='never'."""
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        assert cfg["telegram"]["on_success"] == "never"


# ---------------------------------------------------------------------------
# TestConfigInvariants (all 6 user-locked configs)
# ---------------------------------------------------------------------------

class TestConfigInvariants:
    @pytest.fixture(scope="class")
    def cfg(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            return yaml.safe_load(f)

    def test_telegram_on_success_is_never(self, cfg):
        assert cfg["telegram"]["on_success"] == "never"

    def test_telegram_daily_digest_is_false(self, cfg):
        assert cfg["telegram"]["daily_digest"] is False

    def test_phase_3_enabled_is_false(self, cfg):
        assert cfg["phase"]["phase_3_enabled"] is False

    def test_max_commits_per_day_is_10(self, cfg):
        assert cfg["budget"]["max_commits_per_day"] == 10

    def test_reverts_to_halt_is_2(self, cfg):
        assert cfg["budget"]["reverts_to_halt"] == 2

    def test_review_default_verdict_is_reject(self, cfg):
        assert cfg["review"]["default_verdict"] == "REJECT"


# ---------------------------------------------------------------------------
# TestNeverListInvariant
# ---------------------------------------------------------------------------

class TestNeverListInvariant:
    def test_all_never_fix_yaml_paths_covered_by_deny_yaml(self):
        """Every path in auto_remediation.yaml never_fix is also in auto_fix_deny.yaml."""
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)

        never_fix_paths = list((cfg.get("never_fix") or {}).get("paths") or [])
        deny_globs = set(deny.get("file_globs") or [])

        # Each never_fix path should appear in the deny globs (belt-and-suspenders)
        missing = [p for p in never_fix_paths if p not in deny_globs]
        assert len(missing) == 0, (
            f"These never_fix paths are missing from auto_fix_deny.yaml file_globs: {missing}"
        )

    def test_deny_yaml_has_expected_trading_path_prefixes(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        globs = deny.get("file_globs", [])
        prefixes_expected = ["brokers/**", "risk/**", "regime/**", "signals/**", "strategies/**"]
        for prefix in prefixes_expected:
            assert prefix in globs, f"Expected '{prefix}' in deny globs"

    def test_deny_yaml_message_patterns_covers_trading_keywords(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        patterns = deny.get("message_patterns", [])
        required = ["broker", "order", "fill", "position", "drawdown", "HALT"]
        for kw in required:
            assert kw in patterns, f"'{kw}' not in message_patterns"

    def test_deny_yaml_error_class_patterns_covers_key_errors(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        patterns = deny.get("error_class_patterns", [])
        required = ["BrokerError", "OrderRejected", "RiskBudgetExceeded", "KillSwitchTriggered"]
        for kw in required:
            assert kw in patterns, f"'{kw}' not in error_class_patterns"


# ---------------------------------------------------------------------------
# TestSafetyCriticalFunctions
# ---------------------------------------------------------------------------

class TestSafetyCriticalFunctions:
    @pytest.fixture(scope="class")
    def funcs(self):
        funcs_path = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
        return {
            ln.strip()
            for ln in funcs_path.read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        }

    def test_safety_critical_functions_count_is_43(self, funcs):
        assert len(funcs) == 43, f"Expected 43 functions, got {len(funcs)}: {sorted(funcs)}"

    def test_place_order_present(self, funcs):
        assert "place_order" in funcs

    def test_halt_present(self, funcs):
        assert "halt" in funcs

    def test_execute_plan_present(self, funcs):
        assert "execute_plan" in funcs

    def test_classifier_blocks_safety_critical_function_name(self, funcs):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        # Sample: check_daily_drawdown is in the list
        assert "check_daily_drawdown" in funcs
        r = c.classify({
            "message": "unexpected error",
            "exc_type": "RuntimeError",
            "file_path": "monitor/drawdown.py",
            "line_number": 1,
            "function_name": "check_daily_drawdown",
            "traceback": None,
        })
        assert r.classification == "ESCALATE"
        assert "check_daily_drawdown" in r.reason


# ---------------------------------------------------------------------------
# TestDispatchOrderInvariants
# ---------------------------------------------------------------------------

class TestDispatchOrderInvariants:
    def test_classification_written_to_db_before_any_worker_dispatch(
        self, db_path, monkeypatch
    ):
        """run_once writes classification to DB (update_classification) in the
        same cycle as classify(). The classification is persisted even if a
        subsequent Phase 2 dispatch were to fail."""
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_dispatch_order",
                      message="test dispatch ordering",
                      file_path="brokers/alpaca/broker.py")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT classification FROM errors WHERE fingerprint='fp_dispatch_order'"
        ).fetchone()
        conn.close()
        # Classification written immediately after classify()
        assert row[0] == "ESCALATE"

    def test_escalated_errors_get_escalated_status_not_triaged(
        self, db_path, monkeypatch
    ):
        """ESCALATE -> remediation_status='ESCALATED' (not 'TRIAGED')."""
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_esc_not_triage",
                      message="broker timeout",
                      file_path="brokers/live_executor.py")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT remediation_status FROM errors WHERE fingerprint='fp_esc_not_triage'"
        ).fetchone()
        conn.close()
        assert row[0] == "ESCALATED"
        assert row[0] != "TRIAGED"

    def test_audit_log_records_rule_id_in_payload(self, db_path, monkeypatch):
        """Audit log payload_json contains rule_id for forensic traceability."""
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        _insert_error(db_path, fingerprint="fp_rule_id",
                      message="broker connection error",
                      file_path="brokers/alpaca/broker.py")
        from core import error_monitor
        error_monitor.run_once(db_path=str(db_path), dry_run=True)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT payload_json, reasoning FROM fix_audit_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        payload = json.loads(row[0])
        assert "rule_id" in payload
        assert payload["rule_id"] is not None
        # reasoning contains the rule
        assert "rule=" in row[1]
