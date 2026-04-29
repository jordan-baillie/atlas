"""tests/test_runbook_invariants.py

Sanity checks that docs/auto-remediation-runbook.md exists, is complete,
and contains all the required sections / references.

These tests run in CI and prevent the runbook from drifting out of sync
with the actual system (e.g., if someone adds a new kill-switch layer
without updating the doc, or removes the Phase 3 activation section).

13 tests, all read-only (no network, no DB, no side effects).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUNBOOK_PATH = Path(__file__).resolve().parent.parent / "docs" / "auto-remediation-runbook.md"


@pytest.fixture(scope="module")
def runbook_text() -> str:
    """Return the full runbook content as a string. Cached per test module."""
    assert RUNBOOK_PATH.exists(), (
        f"Runbook not found at {RUNBOOK_PATH}. "
        "Run Phase 3 runbook creation task first."
    )
    return RUNBOOK_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: File existence
# ---------------------------------------------------------------------------

def test_runbook_file_exists():
    """docs/auto-remediation-runbook.md must exist."""
    assert RUNBOOK_PATH.exists(), (
        f"Runbook file missing: {RUNBOOK_PATH}"
    )


# ---------------------------------------------------------------------------
# Test 2: File size (>5KB)
# ---------------------------------------------------------------------------

def test_runbook_size_gt_5kb():
    """Runbook must be >5KB (guards against empty/stub file)."""
    size = RUNBOOK_PATH.stat().st_size
    assert size > 5_000, (
        f"Runbook is only {size} bytes — expected >5000. "
        "File may be a stub or was accidentally truncated."
    )


# ---------------------------------------------------------------------------
# Test 3: All 8 kill-switch layers mentioned (L1–L8)
# ---------------------------------------------------------------------------

def test_all_8_kill_switch_layers_mentioned(runbook_text: str):
    """All 8 layers (L1, L2, …, L8) must be referenced in the runbook."""
    missing = []
    for n in range(1, 9):
        pattern = rf"\bL{n}\b"
        if not re.search(pattern, runbook_text):
            missing.append(f"L{n}")
    assert not missing, (
        f"Runbook is missing kill-switch layer references: {missing}. "
        "Section §3.3 must document all 8 layers."
    )


# ---------------------------------------------------------------------------
# Test 4: All 6 Day-1 whitelist classes mentioned by name
# ---------------------------------------------------------------------------

_DAY1_CLASSES = [
    "test_import_error",
    "stale_fixture_datetime",
    "lint_non_trading_files",
    "markdown_typos",
    "dashboard_react_build_errors",
    "healthz_section_logic",
]


@pytest.mark.parametrize("class_name", _DAY1_CLASSES)
def test_whitelist_class_mentioned(runbook_text: str, class_name: str):
    """Each Day-1 AUTO_FIX whitelist class must appear in the runbook by name."""
    assert class_name in runbook_text, (
        f"Day-1 whitelist class '{class_name}' not found in runbook. "
        "Section §5.1 must list all 6 whitelist classes."
    )


# ---------------------------------------------------------------------------
# Test 5: phase_3_enabled flag referenced
# ---------------------------------------------------------------------------

def test_phase_3_enabled_flag_mentioned(runbook_text: str):
    """The phase_3_enabled config flag must appear in the runbook."""
    assert "phase_3_enabled" in runbook_text, (
        "Runbook does not mention 'phase_3_enabled'. "
        "Section §5.2 must describe how to flip this flag."
    )


# ---------------------------------------------------------------------------
# Test 6: AUTO_REMEDIATION_HALT file path referenced
# ---------------------------------------------------------------------------

def test_auto_remediation_halt_path_mentioned(runbook_text: str):
    """The AUTO_REMEDIATION_HALT sentinel file path must appear in the runbook."""
    assert "AUTO_REMEDIATION_HALT" in runbook_text, (
        "Runbook does not mention AUTO_REMEDIATION_HALT. "
        "This is the primary operator halt mechanism (§3.1)."
    )
    # Also verify the full path appears (not just the name)
    assert "data/AUTO_REMEDIATION_HALT" in runbook_text, (
        "Runbook must include the full path data/AUTO_REMEDIATION_HALT."
    )


# ---------------------------------------------------------------------------
# Test 7: At least 3 SQL code blocks
# ---------------------------------------------------------------------------

def test_at_least_3_sql_blocks(runbook_text: str):
    """Runbook must contain at least 3 fenced ```sql code blocks."""
    sql_blocks = re.findall(r"```sql", runbook_text, re.IGNORECASE)
    assert len(sql_blocks) >= 3, (
        f"Found only {len(sql_blocks)} ```sql blocks. "
        "Runbook must include >=3 SQL examples (§7.2 audit log queries)."
    )


# ---------------------------------------------------------------------------
# Test 8: At least 2 INI/systemd unit code blocks
# ---------------------------------------------------------------------------

def test_at_least_2_ini_blocks(runbook_text: str):
    """Runbook must contain at least 2 fenced ```ini blocks (systemd units)."""
    ini_blocks = re.findall(r"```ini", runbook_text, re.IGNORECASE)
    assert len(ini_blocks) >= 2, (
        f"Found only {len(ini_blocks)} ```ini blocks. "
        "Runbook must include >=2 systemd unit templates (§8.2, §8.3)."
    )


# ---------------------------------------------------------------------------
# Test 9: validate_classifier_30day.py script reference
# ---------------------------------------------------------------------------

def test_validate_classifier_30day_script_mentioned(runbook_text: str):
    """validate_classifier_30day.py must be referenced in the runbook."""
    assert "validate_classifier_30day.py" in runbook_text, (
        "Runbook does not reference validate_classifier_30day.py. "
        "This script enforces the 94% IGNORE mandate (§10.3, §12 quick ref)."
    )


# ---------------------------------------------------------------------------
# Test 10: run_graduation_engine.py script reference
# ---------------------------------------------------------------------------

def test_run_graduation_engine_script_mentioned(runbook_text: str):
    """run_graduation_engine.py must be referenced in the runbook."""
    assert "run_graduation_engine.py" in runbook_text, (
        "Runbook does not reference run_graduation_engine.py. "
        "This script drives Phase 3 class promotion (§5.1, §5.2)."
    )


# ---------------------------------------------------------------------------
# Test 11: Phase 3 activation section exists
# ---------------------------------------------------------------------------

_PHASE3_MARKERS = [
    "Phase 3 Activation",
    "Pre-conditions for activating Phase 3",
    "The flip procedure",
]


def test_phase_3_activation_section_exists(runbook_text: str):
    """The runbook must contain a Phase 3 activation section with key subsections."""
    missing = [m for m in _PHASE3_MARKERS if m not in runbook_text]
    assert not missing, (
        f"Runbook is missing Phase 3 activation content: {missing}. "
        "Section §5 must cover the full Phase 3 flip procedure."
    )


# ---------------------------------------------------------------------------
# Test 12: Disaster recovery section exists
# ---------------------------------------------------------------------------

_DR_MARKERS = [
    "Disaster Recovery",
    "Bad fix landed in main",
    "Capture broken",
    "Classifier returning wrong tiers",
    "OAuth subscription window exhausted",
]


def test_disaster_recovery_section_exists(runbook_text: str):
    """Disaster recovery section must cover the major failure scenarios."""
    missing = [m for m in _DR_MARKERS if m not in runbook_text]
    assert not missing, (
        f"Runbook disaster recovery section is missing: {missing}. "
        "Section §10 must cover all major incident scenarios."
    )


# ---------------------------------------------------------------------------
# Test 13: Audit log phases appendix exists with required phases
# ---------------------------------------------------------------------------

_AUDIT_PHASES = [
    "capture",
    "triage",
    "reproduce",
    "diagnose",
    "review",
    "gate_check",
    "merge",
    "monitor",
    "revert",
    "halt",
    "resume",
    "graduation",
    "demotion",
]


def test_audit_log_phases_appendix_exists(runbook_text: str):
    """Appendix A must exist and list all audit log phases."""
    assert "Appendix A" in runbook_text, (
        "Runbook is missing Appendix A (Audit Log Phases). "
        "This appendix is referenced throughout the doc."
    )
    missing_phases = [p for p in _AUDIT_PHASES if p not in runbook_text]
    assert not missing_phases, (
        f"Appendix A is missing audit log phases: {missing_phases}. "
        "All 13 canonical phases must be documented."
    )
