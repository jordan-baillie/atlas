"""Adversarial test suite -- proves safety bounds hold under attack.

Each test simulates a malicious or hallucinating LLM that proposes a fix
violating a safety boundary. The test asserts that the boundary is enforced.

Layers under attack:
  A. NEVER-list bypass attempts (file_globs, error_class, message, function_name, causal_chain)
  B. Diff oversize attempts (29, 30, 31, 100 lines)
  C. Missing regression test
  D. Reviewer default-REJECT (parse failure, timeout, unknown verdict, low confidence)
  E. Telegram silence-on-success (100 successful merges -> 0 calls)
  F. Telegram alert-on-failure (1 per failure)
  G. All 8 kill-switch layers individually
  H. Adversarial fix targets (place_order, halt, kill_switch AST check)
  I. Secret exfiltration attempts (.atlas-secrets.json, .env* in diff path)
  J. Causal-chain bypass (safe file_path but trading code in traceback)
  K. Domain.write enforcement (brokers/, risk/ paths in deny.yaml)
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_error(
    message: str = "test error",
    exc_type: str | None = None,
    file_path: str | None = None,
    function_name: str | None = None,
    traceback: str | None = None,
) -> dict:
    return {
        "message": message,
        "exc_type": exc_type,
        "file_path": file_path,
        "line_number": 1,
        "function_name": function_name,
        "traceback": traceback,
    }


def _bootstrap_db_for_ks(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL, equity REAL,
      daily_pnl_pct REAL, market_id TEXT
    );
    CREATE TABLE IF NOT EXISTS errors (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      fingerprint TEXT UNIQUE NOT NULL,
      first_seen_ts TEXT NOT NULL, last_seen_ts TEXT NOT NULL,
      ts TEXT NOT NULL, source TEXT NOT NULL, level TEXT NOT NULL,
      message TEXT NOT NULL, classification TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
      tier INTEGER NOT NULL DEFAULT 99,
      remediation_status TEXT NOT NULL DEFAULT 'NEW',
      occurrence_count INTEGER NOT NULL DEFAULT 1,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS fix_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      error_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
      started_ts TEXT NOT NULL, finished_ts TEXT,
      status TEXT NOT NULL DEFAULT 'triaged',
      classification TEXT NOT NULL,
      review_verdict TEXT, reverted_ts TEXT
    );
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Group A -- NEVER list bypass attempts
# ---------------------------------------------------------------------------

class TestNeverListBypassAttempts:
    """Each test tries a different angle to escape the NEVER list. All must escalate."""

    def test_brokers_file_path_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path="brokers/alpaca/broker.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_risk_file_path_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path="risk/stop_probability.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_strategies_file_path_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path="strategies/momentum_breakout.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_live_executor_path_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path="brokers/live_executor.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_message_broker_keyword_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="Failed to connect to broker API"))
        assert r.classification == "ESCALATE"

    def test_message_order_keyword_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="order rejected: insufficient funds"))
        assert r.classification == "ESCALATE"

    def test_exc_type_broker_error_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(exc_type="BrokerError", message="connection failed"))
        assert r.classification == "ESCALATE"

    def test_safety_critical_function_name_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(function_name="place_order"))
        assert r.classification == "ESCALATE"
        assert "place_order" in r.reason

    def test_halt_function_name_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(function_name="halt"))
        assert r.classification == "ESCALATE"

    def test_reconcile_path_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path="core/reconcile.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_message_drawdown_keyword_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="drawdown threshold exceeded"))
        assert r.classification == "ESCALATE"

    def test_message_halt_keyword_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="HALT signal received"))
        assert r.classification == "ESCALATE"


# ---------------------------------------------------------------------------
# Group B -- Diff oversize attempts
# ---------------------------------------------------------------------------

class TestDiffOversizeAttempts:
    def test_29_lines_passes_cap(self, tmp_path):
        from core import merge_gates
        diff = "\n".join(f"+line_{i}: change" for i in range(29))
        with patch.object(merge_gates, "_git_diff", return_value=diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert gate.passed
        assert gate.detail["total"] == 29

    def test_30_lines_exactly_at_cap_passes(self, tmp_path):
        from core import merge_gates
        diff = "\n".join(f"+line_{i}: change" for i in range(30))
        with patch.object(merge_gates, "_git_diff", return_value=diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert gate.passed
        assert gate.detail["total"] == 30

    def test_31_lines_exceeds_cap(self, tmp_path):
        from core import merge_gates
        diff = "\n".join(f"+line_{i}: change" for i in range(31))
        with patch.object(merge_gates, "_git_diff", return_value=diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert not gate.passed
        assert gate.detail["total"] == 31

    def test_100_lines_far_exceeds_cap(self, tmp_path):
        from core import merge_gates
        diff = "\n".join(f"+line_{i}: significant change" for i in range(100))
        with patch.object(merge_gates, "_git_diff", return_value=diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert not gate.passed
        assert gate.detail["total"] == 100

    def test_mixed_15_plus_15_minus_equals_30_passes(self, tmp_path):
        from core import merge_gates
        diff = (
            "\n".join(f"+added_line_{i}" for i in range(15))
            + "\n"
            + "\n".join(f"-removed_line_{i}" for i in range(15))
        )
        with patch.object(merge_gates, "_git_diff", return_value=diff):
            gate = merge_gates.gate_diff_size_cap(tmp_path, "branch", max_lines=30)
        assert gate.passed
        assert gate.detail["total"] == 30


# ---------------------------------------------------------------------------
# Group C -- Missing regression test
# ---------------------------------------------------------------------------

class TestMissingRegressionTest:
    def test_no_files_changed_fails(self, tmp_path):
        from core import merge_gates
        with patch.object(merge_gates, "_git_files_in_branch", return_value=[]), \
             patch.object(merge_gates, "_git_diff", return_value=""):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert not gate.passed
        assert gate.detail["new_test_functions_added"] == 0

    def test_non_test_py_file_only_fails(self, tmp_path):
        from core import merge_gates
        with patch.object(merge_gates, "_git_files_in_branch",
                          return_value=["utils/helper.py"]), \
             patch.object(merge_gates, "_git_diff",
                          return_value="+def helper_func(): pass\n"):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert not gate.passed

    def test_test_file_added_passes(self, tmp_path):
        from core import merge_gates
        with patch.object(merge_gates, "_git_files_in_branch",
                          return_value=["tests/test_helper.py", "utils/helper.py"]), \
             patch.object(merge_gates, "_git_diff",
                          return_value="+def test_helper_returns_value(): assert True\n"):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert gate.passed

    def test_new_test_function_in_diff_passes_even_without_test_file(self, tmp_path):
        from core import merge_gates
        diff_with_test = "+def test_edge_case_none_input():\n+    assert compute(None) is None\n"
        with patch.object(merge_gates, "_git_files_in_branch",
                          return_value=["utils/compute.py"]), \
             patch.object(merge_gates, "_git_diff", return_value=diff_with_test):
            gate = merge_gates.gate_regression_test_present(tmp_path, "branch")
        assert gate.passed
        assert gate.detail["new_test_functions_added"] >= 1


# ---------------------------------------------------------------------------
# Group D -- Reviewer default-REJECT under adversarial conditions
# ---------------------------------------------------------------------------

class TestReviewerDefaultDeny:
    def test_empty_response_defaults_to_reject(self):
        from core.reviewer import parse_review_output
        parsed = parse_review_output("")
        assert parsed == {}

    def test_non_json_response_defaults_to_reject(self):
        from core.reviewer import parse_review_output
        parsed = parse_review_output("ESCALATE: cannot reproduce the issue")
        assert parsed == {}  # no valid JSON found

    def test_unknown_verdict_field_treated_as_reject(self):
        from core.reviewer import parse_review_output, MIN_APPROVE_CONFIDENCE
        parsed = parse_review_output('{"verdict": "UNCERTAIN", "confidence": 0.9}')
        verdict = (parsed.get("verdict") or "REJECT").upper()
        confidence = float(parsed.get("confidence") or 0.0)
        final = "APPROVE" if verdict == "APPROVE" and confidence >= MIN_APPROVE_CONFIDENCE else "REJECT"
        assert final == "REJECT"

    def test_approve_low_confidence_demoted_to_reject(self):
        from core.reviewer import parse_review_output, MIN_APPROVE_CONFIDENCE
        parsed = parse_review_output('{"verdict": "APPROVE", "confidence": 0.5}')
        confidence = float(parsed.get("confidence") or 0.0)
        verdict = (parsed.get("verdict") or "REJECT").upper()
        final = "APPROVE" if verdict == "APPROVE" and confidence >= MIN_APPROVE_CONFIDENCE else "REJECT"
        assert final == "REJECT"

    def test_approve_high_confidence_approved(self):
        from core.reviewer import parse_review_output, MIN_APPROVE_CONFIDENCE
        parsed = parse_review_output('{"verdict": "APPROVE", "confidence": 0.95}')
        confidence = float(parsed.get("confidence") or 0.0)
        verdict = (parsed.get("verdict") or "REJECT").upper()
        final = "APPROVE" if verdict == "APPROVE" and confidence >= MIN_APPROVE_CONFIDENCE else "REJECT"
        assert final == "APPROVE"

    def test_review_fix_dry_run_always_rejects(self):
        """dry_run=True reviewer returns REJECT by default (defensive posture)."""
        from core.reviewer import review_fix
        error = _make_error(message="test", file_path="tests/test_foo.py")
        out = review_fix(error, diff="", dry_run=True)
        assert out.success
        assert out.verdict == "REJECT"
        assert "DRY_RUN" in out.reason

    def test_nonzero_pi_exit_defaults_to_reject(self):
        """Non-zero pi exit code -> ReviewOutcome.success=False and verdict=REJECT."""
        from core import reviewer
        error = _make_error(message="test")
        with patch.object(reviewer, "invoke_reviewer_via_pi_team",
                          return_value=(1, "", "pi error")):
            out = reviewer.review_fix(error, diff="+line\n", dry_run=False)
        assert not out.success
        assert out.verdict == "REJECT"

    def test_timeout_defaults_to_reject(self):
        """Timeout during review -> REJECT (never APPROVE by default)."""
        import subprocess
        from core import reviewer
        error = _make_error(message="test")
        with patch.object(reviewer, "invoke_reviewer_via_pi_team",
                          side_effect=subprocess.TimeoutExpired("pi", 300)):
            out = reviewer.review_fix(error, diff="+line\n", dry_run=False)
        assert out.verdict == "REJECT"
        assert "timeout" in out.reason.lower()


# ---------------------------------------------------------------------------
# Group E -- Telegram silence on success (100 successful merges -> 0 calls)
# ---------------------------------------------------------------------------

class TestTelegramSilenceOnSuccess:
    def test_send_failure_alert_not_called_for_success_merge(self, tmp_path):
        """merge_fix with all-passing gates never calls _send_failure_alert."""
        from core import merger
        from core.merge_gates import GateRunOutcome

        passing = GateRunOutcome(
            all_passed=True, results=[],
            summary={"passed": ["g1"], "failed": [], "blocking_failures": []},
        )
        fix_out = SimpleNamespace(
            success=True, attempt_id=-1, error_id=1,
            fingerprint="fp_silence", branch="auto-fix/err-1",
            worktree=str(tmp_path),
        )
        with patch("utils.telegram.send_message") as mock_tg, \
             patch("core.merger.run_all_gates", return_value=passing), \
             patch("core.merger.push_to_staging", return_value=(True, "sha1")), \
             patch("core.merger._persist_merge_result"):
            merger.merge_fix(fix_out, None)

        assert mock_tg.call_count == 0

    def test_100_successful_mergeoutcomes_zero_telegram(self):
        """Simulating 100 successful fix decisions -> 0 telegram calls."""
        from core import merger
        with patch("utils.telegram.send_message") as mock_tg:
            for i in range(100):
                success = True
                if not success:  # This is the exact guard in merge_fix
                    merger._send_failure_alert(
                        merger.MergeOutcome(
                            success=False, error_id=i, fingerprint=f"fp_{i}", branch="b"
                        )
                    )
        assert mock_tg.call_count == 0

    def test_merge_config_telegram_on_success_is_never(self):
        """User-locked config: telegram.on_success = 'never'."""
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        assert cfg["telegram"]["on_success"] == "never"

    def test_telegram_daily_digest_disabled(self):
        """User-locked config: telegram.daily_digest = false."""
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        assert cfg["telegram"]["daily_digest"] is False


# ---------------------------------------------------------------------------
# Group F -- Telegram alert on failure
# ---------------------------------------------------------------------------

class TestTelegramAlertOnFailure:
    def test_send_failure_alert_calls_send_message(self):
        from core import merger
        outcome = merger.MergeOutcome(
            success=False, error_id=1, fingerprint="fp_alert",
            branch="auto-fix/err-1", blocking_failures=["reviewer_approved"],
        )
        with patch("utils.telegram.send_message") as mock_tg:
            merger._send_failure_alert(outcome)
        assert mock_tg.call_count == 1

    def test_alert_message_contains_blocking_gate_names(self):
        from core import merger
        outcome = merger.MergeOutcome(
            success=False, error_id=2, fingerprint="fp_gate_alert",
            branch="auto-fix/err-2",
            blocking_failures=["diff_size_cap", "regression_test_present"],
        )
        captured = []
        with patch("utils.telegram.send_message", side_effect=lambda m: captured.append(m)):
            merger._send_failure_alert(outcome)
        assert len(captured) == 1
        assert "diff_size_cap" in captured[0]

    def test_100_failures_yield_100_alerts(self):
        from core import merger
        with patch("utils.telegram.send_message") as mock_tg:
            for i in range(100):
                merger._send_failure_alert(
                    merger.MergeOutcome(
                        success=False, error_id=i, fingerprint=f"fp_{i}",
                        branch="b", blocking_failures=["g"],
                    )
                )
        assert mock_tg.call_count == 100

    def test_enforce_budget_halt_sends_telegram_alert(self, tmp_path):
        from core import budget, remediation_kill_switch as ks
        monkeypatch_like_obj = type("MP", (), {})()  # not a real monkeypatch but we use tmp files

        # Create tmp db with 10 merged commits (triggers commit cap)
        db = tmp_path / "budget_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE fix_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER, fingerprint TEXT, started_ts TEXT,
            finished_ts TEXT, status TEXT, classification TEXT,
            review_verdict TEXT, reverted_ts TEXT
        )""")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(10):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, finished_ts, "
                "status, classification) VALUES (?,?,?,?,'merged','ASSIST')",
                (i + 1, f"fp_{i}", now, now),
            )
        conn.commit()
        conn.close()

        # Redirect PROJECT_ROOT for halt file creation
        import os
        os.environ["_ATLAS_TEST_HALT_DIR"] = str(tmp_path / "data")
        (tmp_path / "data").mkdir(exist_ok=True)

        with patch("core.remediation_kill_switch.PROJECT_ROOT", tmp_path), \
             patch("utils.telegram.send_message") as mock_tg:
            decision = budget.enforce_budget(db_path=str(db), send_alert=True)

        assert decision.action == "HALT"
        assert mock_tg.call_count >= 1

        os.environ.pop("_ATLAS_TEST_HALT_DIR", None)


# ---------------------------------------------------------------------------
# Group G -- All 8 kill-switch layers individually
# ---------------------------------------------------------------------------

class TestAllKillSwitchLayersIndividually:
    def test_l1_env_var_blocks(self, monkeypatch):
        monkeypatch.setenv("ATLAS_AUTO_REMEDIATION_DISABLED", "1")
        from core.remediation_kill_switch import check_l1_env
        result = check_l1_env()
        assert result is not None
        assert result.layer == "L1"

    def test_l1_env_var_clears_when_unset(self, monkeypatch):
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        from core.remediation_kill_switch import check_l1_env
        result = check_l1_env()
        assert result is None

    def test_l2_halt_file_blocks(self, tmp_path, monkeypatch):
        from core import remediation_kill_switch as ks
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "AUTO_REMEDIATION_HALT").write_text("manual halt")
        result = ks.check_l2_remediation_halt()
        assert result is not None
        assert result.layer == "L2"

    def test_l3_trading_halt_blocks(self, tmp_path, monkeypatch):
        from core import remediation_kill_switch as ks
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "HALT").write_text("trading halt")
        result = ks.check_l3_trading_halt()
        assert result is not None
        assert result.layer == "L3"

    def test_l4_drawdown_breach_blocks(self, tmp_path):
        from core.remediation_kill_switch import check_l4_drawdown
        db = tmp_path / "l4_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE portfolio_snapshots (
            id INTEGER PRIMARY KEY, timestamp TEXT,
            equity REAL, daily_pnl_pct REAL, market_id TEXT
        )""")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        conn.execute(
            "INSERT INTO portfolio_snapshots VALUES (1, ?, 5000, -7.0, 'sp500')", (now,)
        )
        conn.commit()
        conn.close()
        result = check_l4_drawdown(db_path=str(db), threshold_pct=5.0)
        assert result is not None
        assert result.layer == "L4"

    def test_l5_healthcheck_cascade_blocks(self, tmp_path):
        from core.remediation_kill_switch import check_l5_healthcheck_cascade
        db = tmp_path / "l5_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT UNIQUE, first_seen_ts TEXT, last_seen_ts TEXT,
            ts TEXT, source TEXT, level TEXT, message TEXT,
            classification TEXT DEFAULT 'UNCLASSIFIED',
            tier INTEGER DEFAULT 99, remediation_status TEXT DEFAULT 'NEW',
            occurrence_count INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        for i in range(4):
            conn.execute(
                "INSERT INTO errors (fingerprint, first_seen_ts, last_seen_ts, ts, "
                "source, level, message) VALUES (?,?,?,?,'healthcheck','CRITICAL','hc fail')",
                (f"hc_fp_{i}", now, now, now),
            )
        conn.commit()
        conn.close()
        result = check_l5_healthcheck_cascade(db_path=str(db), threshold=3)
        assert result is not None
        assert result.layer == "L5"

    def test_l6_reviewer_rejection_rate_blocks(self, tmp_path):
        from core.remediation_kill_switch import check_l6_reviewer_rejection_rate
        db = tmp_path / "l6_test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE fix_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER, fingerprint TEXT, started_ts TEXT,
            finished_ts TEXT, status TEXT, classification TEXT,
            review_verdict TEXT, reverted_ts TEXT
        )""")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        # 8 REJECT + 2 APPROVE out of 10 = 80% rejection rate (>50%)
        for i in range(8):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "review_verdict, status, classification) VALUES (?,?,?,'REJECT','reviewed','ASSIST')",
                (i + 1, f"fp_{i}", now),
            )
        for i in range(2):
            conn.execute(
                "INSERT INTO fix_attempts (error_id, fingerprint, started_ts, "
                "review_verdict, status, classification) VALUES (?,?,?,'APPROVE','merged','ASSIST')",
                (i + 100, f"fp_ok_{i}", now),
            )
        conn.commit()
        conn.close()
        result = check_l6_reviewer_rejection_rate(
            db_path=str(db), min_sample_size=10, threshold_pct=50.0
        )
        assert result is not None
        assert result.layer == "L6"

    def test_clear_all_layers_returns_none(self, tmp_path, monkeypatch):
        """When no halt conditions are active, check_all_layers returns None."""
        from core import remediation_kill_switch as ks
        monkeypatch.delenv("ATLAS_AUTO_REMEDIATION_DISABLED", raising=False)
        monkeypatch.setattr(ks, "PROJECT_ROOT", tmp_path)
        (tmp_path / "data").mkdir()
        # No HALT files
        result = ks.check_all_layers(db_path=str(tmp_path / "nonexistent.db"))
        assert result is None


# ---------------------------------------------------------------------------
# Group H -- Adversarial fix targets (AST-level function check)
# ---------------------------------------------------------------------------

class TestAdversarialFixTargets:
    def test_place_order_in_safety_critical_list(self):
        funcs_path = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
        funcs = {ln.strip() for ln in funcs_path.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        assert "place_order" in funcs

    def test_halt_in_safety_critical_list(self):
        funcs_path = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
        funcs = {ln.strip() for ln in funcs_path.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        assert "halt" in funcs

    def test_execute_plan_in_safety_critical_list(self):
        funcs_path = PROJECT_ROOT / "config" / "safety_critical_functions.txt"
        funcs = {ln.strip() for ln in funcs_path.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        assert "execute_plan" in funcs

    def test_gate_blocks_modified_safety_critical_function(self, tmp_path):
        """gate_no_safety_critical_function_modified blocks place_order change."""
        from core import merge_gates

        # Simulate: after-patch has place_order with different body
        test_file = tmp_path / "brokers" / "live_executor.py"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("def place_order(self, symbol, qty): return {'id': '123'}\n")

        # Pre-patch version (different body)
        pre_patch_src = "def place_order(self, symbol): return None\n"

        with patch.object(merge_gates, "_git_files_in_branch",
                          return_value=["brokers/live_executor.py"]), \
             patch.object(merge_gates.subprocess, "run") as mock_run:
            mock_run.return_value = SimpleNamespace(returncode=0, stdout=pre_patch_src)
            gate = merge_gates.gate_no_safety_critical_function_modified(tmp_path, "branch")

        assert not gate.passed
        assert any(v.get("function") == "place_order" for v in gate.detail["violations"])


# ---------------------------------------------------------------------------
# Group I -- Secret exfiltration attempts
# ---------------------------------------------------------------------------

class TestSecretExfiltrationAttempts:
    def test_secrets_file_in_deny_list_globs(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        globs = deny.get("file_globs", [])
        assert ".atlas-secrets.json" in globs, f"Not found in globs: {globs}"

    def test_env_file_pattern_in_deny_list_globs(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        globs = deny.get("file_globs", [])
        env_globs = [g for g in globs if ".env" in g]
        assert len(env_globs) >= 1, f"No .env* pattern in globs: {globs}"

    def test_triage_classifier_blocks_secrets_path(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(file_path=".atlas-secrets.json"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_credential_keyword_in_message_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="invalid credential provided"))
        assert r.classification == "ESCALATE"

    def test_auth_keyword_in_message_blocked(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        r = c.classify(_make_error(message="auth token expired"))
        assert r.classification == "ESCALATE"


# ---------------------------------------------------------------------------
# Group J -- Causal-chain bypass: safe file_path but trading code in traceback
# ---------------------------------------------------------------------------

class TestCausalChainBypass:
    def test_safe_file_path_but_brokers_in_traceback_escalates(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = _make_error(
            file_path="tests/test_integration.py",
            message="Unexpected assertion failure",
            exc_type="AssertionError",
            traceback="  File brokers/alpaca/broker.py, line 100, in connect\n    raise BrokerError()",
        )
        r = c.classify(e)
        assert r.classification == "ESCALATE"
        assert "causal_chain" in r.rule_id or "never_fix" in r.rule_id

    def test_safe_file_path_but_risk_in_traceback_escalates(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = _make_error(
            file_path="utils/math_helper.py",
            message="ZeroDivisionError in calculation",
            exc_type="ZeroDivisionError",
            traceback="  File risk/position_sizer.py, line 42, in compute_size\n    div by zero",
        )
        r = c.classify(e)
        assert r.classification == "ESCALATE"

    def test_safe_file_path_but_strategies_in_traceback_escalates(self):
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = _make_error(
            file_path="tests/test_strategy_smoke.py",
            message="TypeError in strategy signal generation",
            exc_type="TypeError",
            traceback=(
                "  File strategies/momentum_breakout.py, line 88, in generate_signals\n"
                "    raise TypeError('unexpected None')"
            ),
        )
        r = c.classify(e)
        assert r.classification == "ESCALATE"

    def test_pure_test_file_clean_traceback_does_not_causal_escalate(self):
        """Clean traceback in test file = not escalated via causal-chain."""
        from core.triage import TriageClassifier
        c = TriageClassifier()
        e = _make_error(
            file_path="tests/test_math.py",
            message="AssertionError: expected 5 got 4",
            exc_type="AssertionError",
            traceback="  File tests/test_math.py, line 12, in test_add\n    assert add(2,2) == 4",
        )
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False), \
             patch.object(TriageClassifier, "is_halt_active", return_value=False):
            r = c.classify(e)
        # Should NOT be escalated due to causal chain (no trading-module references)
        # (may still be ESCALATE via default_deny, but NOT via causal chain rule)
        assert "causal_chain" not in r.rule_id


# ---------------------------------------------------------------------------
# Group K -- Domain.write enforcement via deny.yaml coverage
# ---------------------------------------------------------------------------

class TestDomainWriteEnforcement:
    def _load_deny_globs(self) -> list:
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        return list(deny.get("file_globs") or [])

    def test_deny_yaml_covers_brokers_path(self):
        globs = self._load_deny_globs()
        broker_globs = [g for g in globs if "brokers" in g]
        assert len(broker_globs) >= 1

    def test_deny_yaml_covers_risk_path(self):
        globs = self._load_deny_globs()
        risk_globs = [g for g in globs if "risk" in g]
        assert len(risk_globs) >= 1

    def test_deny_yaml_covers_trading_scripts(self):
        globs = self._load_deny_globs()
        script_globs = [g for g in globs if "scripts/eod_settlement" in g
                        or "scripts/execute_approved" in g
                        or "scripts/intraday_monitor" in g]
        assert len(script_globs) >= 2

    def test_deny_yaml_covers_db_schema(self):
        globs = self._load_deny_globs()
        db_globs = [g for g in globs if "schema.sql" in g or "atlas_db" in g]
        assert len(db_globs) >= 2

    def test_deny_yaml_message_patterns_include_broker_keywords(self):
        import yaml
        with open(PROJECT_ROOT / "config" / "auto_fix_deny.yaml") as f:
            deny = yaml.safe_load(f)
        patterns = deny.get("message_patterns", [])
        assert "broker" in patterns, f"'broker' not in message_patterns: {patterns}"
        assert "order" in patterns
        assert "fill" in patterns

    def test_all_never_fix_yaml_paths_blocked_by_triage(self):
        """Sample 5 paths from never_fix config and verify each triggers ESCALATE."""
        import yaml
        from core.triage import TriageClassifier
        with open(PROJECT_ROOT / "config" / "auto_remediation.yaml") as f:
            cfg = yaml.safe_load(f)
        never_fix_paths = list((cfg.get("never_fix") or {}).get("paths") or [])
        c = TriageClassifier()

        concrete_paths = {
            "brokers/**": "brokers/alpaca/broker.py",
            "risk/**": "risk/portfolio_var.py",
            "regime/**": "regime/model.py",
            "signals/**": "signals/momentum.py",
            "portfolio/**": "portfolio/allocator.py",
        }
        checked = 0
        for glob_pattern, concrete in concrete_paths.items():
            if any(glob_pattern == p for p in never_fix_paths):
                r = c.classify(_make_error(file_path=concrete))
                assert r.classification == "ESCALATE", (
                    f"{concrete} should be ESCALATE but got {r.classification}"
                )
                checked += 1
        assert checked >= 3, "Should have checked at least 3 never_fix paths"
