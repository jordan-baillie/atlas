"""Tests for config/auto_fix_classes.yaml — structure and content validation."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CLASSES_PATH = Path(__file__).resolve().parent.parent / "config" / "auto_fix_classes.yaml"
EXPECTED_NAMES = {
    "test_import_error",
    "stale_fixture_datetime",
    "lint_non_trading_files",
    "markdown_typos",
    "dashboard_react_build_errors",
    "healthz_section_logic",
}
REQUIRED_FIELDS = {
    "name",
    "message_regex",
    "file_path_globs",
    "max_diff_lines",
    "requires_test_file_added",
}


@pytest.fixture(scope="module")
def cfg() -> dict:
    return yaml.safe_load(CLASSES_PATH.read_text())


@pytest.fixture(scope="module")
def classes(cfg) -> list:
    return cfg["classes"]


# ── Structural tests ────────────────────────────────────────────────────────


def test_yaml_parses_without_error(cfg):
    """File must parse cleanly as YAML."""
    assert cfg is not None
    assert isinstance(cfg, dict)


def test_has_exactly_six_classes(classes):
    assert len(classes) == 6, f"Expected 6 classes, got {len(classes)}: {[c['name'] for c in classes]}"


def test_all_required_fields_present(classes):
    """Every class must carry all 5 required fields."""
    for cls in classes:
        missing = REQUIRED_FIELDS - set(cls.keys())
        assert not missing, f"Class {cls.get('name', '?')} missing fields: {missing}"


def test_class_names_match_spec(classes):
    actual = {c["name"] for c in classes}
    assert actual == EXPECTED_NAMES, f"Name mismatch. Got: {actual}"


def test_all_classes_have_max_diff_lines_30(classes):
    for cls in classes:
        assert cls["max_diff_lines"] == 30, (
            f"Class {cls['name']} has max_diff_lines={cls['max_diff_lines']}, expected 30"
        )


def test_file_path_globs_are_non_empty_lists(classes):
    for cls in classes:
        globs = cls["file_path_globs"]
        assert isinstance(globs, list) and len(globs) > 0, (
            f"Class {cls['name']} has empty file_path_globs"
        )


def test_requires_test_file_added_is_bool(classes):
    for cls in classes:
        assert isinstance(cls["requires_test_file_added"], bool), (
            f"Class {cls['name']} requires_test_file_added is not bool"
        )


def test_defaults_section_present(cfg):
    assert "defaults" in cfg, "defaults section missing"


def test_defaults_reviewer_confidence_threshold(cfg):
    assert cfg["defaults"]["reviewer_confidence_threshold"] == 0.75


def test_defaults_max_diff_lines(cfg):
    assert cfg["defaults"]["max_diff_lines"] == 30


def test_defaults_post_merge_monitor_minutes(cfg):
    assert cfg["defaults"]["post_merge_monitor_minutes"] == 30


def test_healthz_section_logic_has_block_globs(classes):
    cls = next(c for c in classes if c["name"] == "healthz_section_logic")
    block_globs = cls.get("file_path_block_globs") or []
    assert len(block_globs) >= 4, f"Expected >=4 block globs, got {block_globs}"
    # Verify the 4 expected broker/risk/reconcile/kill patterns are all present
    block_str = " ".join(block_globs)
    for keyword in ("broker", "risk", "reconcile", "kill"):
        assert keyword in block_str, f"Missing block glob for {keyword!r}"


def test_each_class_has_non_empty_message_regex(classes):
    for cls in classes:
        assert cls["message_regex"], f"Class {cls['name']} has empty message_regex"


def test_each_class_has_notes(classes):
    """notes field should be present and non-empty (documentation convention)."""
    for cls in classes:
        assert cls.get("notes"), f"Class {cls['name']} missing notes"


def test_sample_match_test_import_error(classes):
    """Smoke: test_import_error should match a sample ImportError message."""
    import re

    cls = next(c for c in classes if c["name"] == "test_import_error")
    pattern = cls["message_regex"]
    assert re.search(pattern, "ImportError: No module named 'foo'", re.IGNORECASE)
    assert re.search(pattern, "ModuleNotFoundError: No module named 'bar'", re.IGNORECASE)


def test_sample_match_markdown_typos(classes):
    """Smoke: markdown_typos should match a broken-link message."""
    import re

    cls = next(c for c in classes if c["name"] == "markdown_typos")
    pattern = cls["message_regex"]
    assert re.search(pattern, "broken link found in README.md", re.IGNORECASE)
    assert re.search(pattern, "typo in docs/setup.md", re.IGNORECASE)


def test_sample_match_dashboard_react_build_errors(classes):
    """Smoke: dashboard_react_build_errors should match TypeScript error codes."""
    import re

    cls = next(c for c in classes if c["name"] == "dashboard_react_build_errors")
    pattern = cls["message_regex"]
    assert re.search(pattern, "TS2345: Argument of type 'string'...", re.IGNORECASE)
    assert re.search(pattern, "vite build failed", re.IGNORECASE)
