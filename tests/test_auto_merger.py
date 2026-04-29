"""Tests for core/auto_merger.py — Phase 3 AUTO_FIX merge path."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# Import under test
from core.auto_merger import (
    AutoMergeOutcome,
    _load_classes,
    _load_phase_3_state,
    auto_merge,
    match_auto_fix_class,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@dataclass
class _FakeFixOutcome:
    branch: str = "auto-fix/test-001"
    worktree_path: str = "/tmp/test_worktree"
    diff_lines: int = 5
    duration_seconds: float = 1.2
    success: bool = True
    attempt_id: int = 1
    error_id: int = 10


@dataclass
class _FakeReviewOutcome:
    success: bool = True
    verdict: str = "APPROVE"
    confidence: float = 0.90
    reason: str = "Looks safe — test import fix only"


def _import_error() -> dict:
    return {
        "id": 42,
        "fingerprint": "abc123def456789a",
        "message": "ImportError: No module named 'tests.helpers'",
        "exc_type": "ImportError",
        "file_path": "tests/test_something.py",
        "function_name": "test_foo",
        "traceback": "",
    }


def _markdown_error() -> dict:
    return {
        "id": 43,
        "fingerprint": "deadbeef12345678",
        "message": "broken link in docs/setup.md",
        "exc_type": "",
        "file_path": "docs/setup.md",
        "function_name": "",
        "traceback": "",
    }


def _trading_error() -> dict:
    return {
        "id": 99,
        "fingerprint": "ffffffffffffffff",
        "message": "KeyError: position not found",
        "exc_type": "KeyError",
        "file_path": "brokers/live_executor.py",
        "function_name": "execute_plan",
        "traceback": "",
    }


def _healthz_broker_error() -> dict:
    """healthz_section_logic but file path contains 'broker' — should be blocked."""
    return {
        "id": 77,
        "fingerprint": "7777777777777777",
        "message": "healthz broker failed threshold exceeded",
        "exc_type": "",
        "file_path": "scripts/healthz_broker_check.sh",
        "function_name": "",
        "traceback": "",
    }


@pytest.fixture()
def _tmp_db(tmp_path) -> str:
    """Create a minimal SQLite DB with fix_attempts + fix_audit_log tables."""
    db = str(tmp_path / "test_auto_merger.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fix_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            error_id INTEGER,
            fingerprint TEXT,
            started_ts TEXT,
            finished_ts TEXT,
            status TEXT,
            classification TEXT,
            fix_branch TEXT,
            fix_commit_sha TEXT,
            fix_diff_lines INTEGER DEFAULT 0,
            review_verdict TEXT,
            review_confidence REAL,
            review_reason TEXT,
            gates_passed_json TEXT,
            gates_failed_json TEXT,
            monitor_outcome TEXT,
            total_wall_seconds REAL,
            notes TEXT
        );
        CREATE TABLE IF NOT EXISTS fix_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            attempt_id INTEGER,
            error_id INTEGER,
            ts TEXT,
            phase TEXT NOT NULL,
            actor TEXT NOT NULL,
            decision TEXT,
            reasoning TEXT,
            payload_json TEXT,
            result_status TEXT
        );
    """)
    conn.commit()
    conn.close()
    return db


# ── _load_phase_3_state tests ───────────────────────────────────────────────


def test_load_phase3_env_true(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    assert _load_phase_3_state() is True


def test_load_phase3_env_false(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "false")
    assert _load_phase_3_state() is False


def test_load_phase3_env_true_uppercase(monkeypatch):
    """env var matching is case-insensitive (lowercased)."""
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "TRUE")
    assert _load_phase_3_state() is True


def test_load_phase3_env_unset_config_not_found(monkeypatch, tmp_path):
    monkeypatch.delenv("AUTO_REMEDIATION_PHASE_3_ENABLED", raising=False)
    with patch("core.auto_merger.CFG_PATH", tmp_path / "nonexistent.yaml"):
        assert _load_phase_3_state() is False


def test_load_phase3_env_unset_reads_config_false(monkeypatch, tmp_path):
    """When env unset and config has phase_3_enabled=false → False."""
    monkeypatch.delenv("AUTO_REMEDIATION_PHASE_3_ENABLED", raising=False)
    cfg = tmp_path / "auto_remediation.yaml"
    cfg.write_text("phase:\n  phase_3_enabled: false\n")
    with patch("core.auto_merger.CFG_PATH", cfg):
        assert _load_phase_3_state() is False


def test_load_phase3_env_unset_reads_config_true(monkeypatch, tmp_path):
    """When env unset and config has phase_3_enabled=true → True."""
    monkeypatch.delenv("AUTO_REMEDIATION_PHASE_3_ENABLED", raising=False)
    cfg = tmp_path / "auto_remediation.yaml"
    cfg.write_text("phase:\n  phase_3_enabled: true\n")
    with patch("core.auto_merger.CFG_PATH", cfg):
        assert _load_phase_3_state() is True


def test_load_phase3_env_overrides_config(monkeypatch, tmp_path):
    """env=false should win even if config says true."""
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "false")
    cfg = tmp_path / "auto_remediation.yaml"
    cfg.write_text("phase:\n  phase_3_enabled: true\n")
    with patch("core.auto_merger.CFG_PATH", cfg):
        assert _load_phase_3_state() is False


# ── match_auto_fix_class tests ──────────────────────────────────────────────


def test_match_test_import_error():
    cls = match_auto_fix_class(_import_error())
    assert cls is not None
    assert cls["name"] == "test_import_error"


def test_match_markdown_typos():
    cls = match_auto_fix_class(_markdown_error())
    assert cls is not None
    assert cls["name"] == "markdown_typos"


def test_match_returns_none_for_trading_path():
    """A KeyError in live_executor.py should NOT match any whitelist class."""
    cls = match_auto_fix_class(_trading_error())
    assert cls is None


def test_match_respects_file_path_block_globs():
    """healthz_section_logic with 'broker' in filename should be blocked."""
    cls = match_auto_fix_class(_healthz_broker_error())
    # The message matches healthz_section_logic pattern but path is blocked
    assert cls is None


def test_match_lint_non_trading_files_tests_dir():
    err = {
        "id": 50,
        "fingerprint": "5555555555555555",
        "message": "SyntaxError: invalid syntax in test file",
        "exc_type": "SyntaxError",
        "file_path": "tests/test_something.py",
        "traceback": "",
    }
    cls = match_auto_fix_class(err)
    assert cls is not None
    assert cls["name"] == "lint_non_trading_files"


def test_match_dashboard_react_build():
    err = {
        "id": 55,
        "fingerprint": "5555abcd5555abcd",
        "message": "TS2345: Argument of type string is not assignable",
        "exc_type": "",
        "file_path": "dashboard-ui/src/components/Chart.tsx",
        "traceback": "",
    }
    cls = match_auto_fix_class(err)
    assert cls is not None
    assert cls["name"] == "dashboard_react_build_errors"


# ── auto_merge failure path tests ───────────────────────────────────────────


def test_auto_merge_fails_when_phase3_disabled(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "false")
    out = auto_merge(_FakeFixOutcome(), _FakeReviewOutcome(), error=_import_error())
    assert out.success is False
    assert "phase_3_enabled=false" in out.error


def test_auto_merge_fails_when_no_whitelist_match(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    out = auto_merge(_FakeFixOutcome(), _FakeReviewOutcome(), error=_trading_error())
    assert out.success is False
    assert "whitelist" in out.error.lower()


def test_auto_merge_fails_when_reviewer_reject(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    rv = _FakeReviewOutcome(verdict="REJECT")
    out = auto_merge(_FakeFixOutcome(), rv, error=_import_error())
    assert out.success is False
    assert "APPROVE" in out.error


def test_auto_merge_fails_when_reviewer_success_false(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    rv = _FakeReviewOutcome(success=False, verdict="APPROVE")
    out = auto_merge(_FakeFixOutcome(), rv, error=_import_error())
    assert out.success is False


def test_auto_merge_fails_when_confidence_too_low(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    rv = _FakeReviewOutcome(confidence=0.50)
    out = auto_merge(_FakeFixOutcome(), rv, error=_import_error())
    assert out.success is False
    assert "confidence" in out.error


def test_auto_merge_fails_when_diff_too_large(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    fx = _FakeFixOutcome(diff_lines=50)  # class cap is 30
    out = auto_merge(fx, _FakeReviewOutcome(), error=_import_error())
    assert out.success is False
    assert "diff" in out.error


def test_auto_merge_fails_when_gates_crash(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    with patch("core.merge_gates.run_all_gates", side_effect=RuntimeError("exploded")):
        out = auto_merge(_FakeFixOutcome(), _FakeReviewOutcome(), error=_import_error())
    assert out.success is False
    assert "gate run crashed" in out.error


def test_auto_merge_fails_when_gates_blocked(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    mock_outcome = MagicMock()
    mock_outcome.all_passed = False
    mock_outcome.summary = {
        "passed": ["gate1"],
        "failed": ["gate2"],
        "blocking_failures": ["gate2"],
    }
    with patch("core.merge_gates.run_all_gates", return_value=mock_outcome):
        out = auto_merge(_FakeFixOutcome(), _FakeReviewOutcome(), error=_import_error())
    assert out.success is False
    assert "gates blocked" in out.error


# ── auto_merge success + persistence tests ──────────────────────────────────


def _make_passing_gate_outcome():
    mock = MagicMock()
    mock.all_passed = True
    mock.summary = {
        "passed": [f"gate{i}" for i in range(15)],
        "failed": [],
        "blocking_failures": [],
    }
    return mock


def test_auto_merge_persists_fix_attempts_row(monkeypatch, _tmp_db):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    with (
        patch("core.merge_gates.run_all_gates", return_value=_make_passing_gate_outcome()),
        patch("core.merger.push_to_staging", return_value=(True, "abc123sha456789")),
    ):
        out = auto_merge(
            _FakeFixOutcome(),
            _FakeReviewOutcome(),
            error=_import_error(),
            db_path=_tmp_db,
        )
    assert out.success is True, f"auto_merge failed: {out.error}"
    conn = sqlite3.connect(_tmp_db)
    rows = conn.execute("SELECT * FROM fix_attempts").fetchall()
    conn.close()
    assert len(rows) == 1
    row = rows[0]
    # status=merged, classification=AUTO_FIX, monitor_outcome=pending
    assert row[5] == "merged"        # status
    assert row[6] == "AUTO_FIX"      # classification
    assert row[15] == "pending"       # monitor_outcome


def test_auto_merge_persists_fix_audit_log_entry(monkeypatch, _tmp_db):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    with (
        patch("core.merge_gates.run_all_gates", return_value=_make_passing_gate_outcome()),
        patch("core.merger.push_to_staging", return_value=(True, "abc123sha456789")),
    ):
        out = auto_merge(
            _FakeFixOutcome(),
            _FakeReviewOutcome(),
            error=_import_error(),
            db_path=_tmp_db,
        )
    assert out.success is True
    conn = sqlite3.connect(_tmp_db)
    rows = conn.execute(
        "SELECT phase, actor, decision, result_status FROM fix_audit_log"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    phase, actor, decision, result_status = rows[0]
    assert phase == "merge"
    assert actor == "auto_merger"
    assert decision == "AUTO_FIX_STAGED"
    assert result_status == "success"


def test_auto_merge_sets_matched_class_on_outcome(monkeypatch):
    monkeypatch.setenv("AUTO_REMEDIATION_PHASE_3_ENABLED", "true")
    with (
        patch("core.merge_gates.run_all_gates", return_value=_make_passing_gate_outcome()),
        patch("core.merger.push_to_staging", return_value=(True, "sha123")),
    ):
        out = auto_merge(_FakeFixOutcome(), _FakeReviewOutcome(), error=_import_error())
    assert out.matched_class == "test_import_error"
    assert out.staging_sha == "sha123"
    assert out.success is True
