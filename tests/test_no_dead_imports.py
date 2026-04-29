"""
Guard test: Phase A.6.c

Ensures:
1. Every *enabled* strategy in active configs maps to an importable strategies/ file
2. No test file directly imports a strategy that no longer has a file
3. scripts/tools/archive/ files are not imported anywhere outside archive/

These tests prevent the silent failure pattern:
  "delete a strategy file, forget to update config → ImportError at runtime"
"""
from __future__ import annotations

import importlib
import json
import re
from pathlib import Path

import pytest

# ── Constants ────────────────────────────────────────────────────────────────

ATLAS_ROOT = Path(__file__).parent.parent
ACTIVE_CONFIG_DIR = ATLAS_ROOT / "config" / "active"
STRATEGIES_DIR = ATLAS_ROOT / "strategies"
TESTS_DIR = ATLAS_ROOT / "tests"
ARCHIVE_DIR = ATLAS_ROOT / "scripts" / "tools" / "archive"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _enabled_strategy_names() -> set[str]:
    """Return set of strategy names that are *enabled* in any active config."""
    names: set[str] = set()
    for cfg_path in ACTIVE_CONFIG_DIR.glob("*.json"):
        try:
            data = json.loads(cfg_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        for name, opts in data.get("strategies", {}).items():
            if opts.get("enabled", False):
                names.add(name)
    return names


def _available_strategy_files() -> set[str]:
    """Return set of strategy module stems present in strategies/."""
    skip = {"__init__", "base", "entry_optimizer"}
    return {
        f.stem
        for f in STRATEGIES_DIR.glob("*.py")
        if f.stem not in skip
    }


def _archive_stems() -> set[str]:
    """Return stems of Python files in scripts/tools/archive/."""
    if not ARCHIVE_DIR.exists():
        return set()
    return {f.stem for f in ARCHIVE_DIR.glob("*.py")}


# ── Test 1: Enabled config strategies are importable ─────────────────────────


class TestActiveConfigStrategiesImportable:
    """Every *enabled* strategy in active configs must have a file in strategies/."""

    def test_no_missing_enabled_strategy_files(self) -> None:
        """Enabled strategy names in active configs must have a strategies/<name>.py."""
        enabled = _enabled_strategy_names()
        available = _available_strategy_files()

        missing = enabled - available
        assert not missing, (
            "Active configs have ENABLED strategies with no corresponding file:\n"
            + "\n".join(f"  - strategies/{name}.py (MISSING)" for name in sorted(missing))
            + "\n\nFix: either create the strategy file or set enabled=false in config/active/*.json"
        )

    def test_enabled_strategy_files_importable(self) -> None:
        """Every enabled strategy file must be importable without errors."""
        enabled = _enabled_strategy_names()
        available = _available_strategy_files()
        to_check = enabled & available  # only check ones that have files

        errors: list[str] = []
        for name in sorted(to_check):
            try:
                importlib.import_module(f"strategies.{name}")
            except Exception as e:
                errors.append(f"  strategies.{name}: {type(e).__name__}: {e}")

        assert not errors, (
            "Some enabled strategy modules failed to import:\n" + "\n".join(errors)
        )

    def test_disabled_strategies_with_missing_files_are_known(self) -> None:
        """
        Disabled strategies that lack a file are noted here.

        These are strategies configured as enabled=False (or weight=0) that
        have no implementation file. This is acceptable, but we track them
        so the list doesn't grow silently.
        """
        known_disabled_missing = {
            "dividend_capture",  # Planned strategy, never implemented; disabled in all configs
        }

        all_config_names: set[str] = set()
        for cfg_path in ACTIVE_CONFIG_DIR.glob("*.json"):
            try:
                data = json.loads(cfg_path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            all_config_names |= set(data.get("strategies", {}).keys())

        available = _available_strategy_files()
        disabled_missing = (all_config_names - _enabled_strategy_names()) - available

        unexpected = disabled_missing - known_disabled_missing
        assert not unexpected, (
            "Disabled strategies with missing files (not in known list):\n"
            + "\n".join(f"  - {name}" for name in sorted(unexpected))
            + "\n\nIf intentional, add to known_disabled_missing in this test."
        )


# ── Test 2: Test files don't import deleted strategy names ───────────────────


class TestNoDeletedStrategyImports:
    """Test files must not import strategy names that have been removed."""

    # Update this list when strategies are deleted.
    # Phase A.6: bb_squeeze, mtf_momentum, trend_following all KEPT (active imports found).
    DELETED_STRATEGY_NAMES: list[str] = [
        # e.g. "old_strategy",
    ]

    def test_no_test_imports_deleted_strategy(self) -> None:
        """No test file should import a deleted strategy by name."""
        if not self.DELETED_STRATEGY_NAMES:
            pytest.skip("No strategies deleted yet — test is a placeholder")

        violations: list[str] = []
        for test_file in TESTS_DIR.rglob("test_*.py"):
            try:
                content = test_file.read_text(errors="replace")
            except OSError:
                continue
            for deleted in self.DELETED_STRATEGY_NAMES:
                pattern = rf"(from strategies\.{deleted}|import strategies\.{deleted})"
                if re.search(pattern, content):
                    violations.append(
                        f"  {test_file.relative_to(ATLAS_ROOT)}: references '{deleted}'"
                    )

        assert not violations, (
            "Test files import deleted strategy modules:\n" + "\n".join(violations)
        )


# ── Test 3: Archive files are not imported outside archive ───────────────────


class TestArchivedScriptsNotImported:
    """scripts/tools/archive/ files must not be imported by active code."""

    def test_archive_scripts_have_no_active_importers(self) -> None:
        """Archived scripts should have zero importers outside the archive dir."""
        archived = _archive_stems()
        if not archived:
            return  # nothing archived yet

        violations: list[str] = []
        for py_file in ATLAS_ROOT.rglob("*.py"):
            if ARCHIVE_DIR in py_file.parents:
                continue  # skip archive itself
            if ".git" in py_file.parts:
                continue
            try:
                content = py_file.read_text(errors="replace")
            except OSError:
                continue
            for stem in archived:
                pattern = (
                    rf"(from scripts\.tools\.archive\.{stem}"
                    rf"|import scripts\.tools\.archive\.{stem}"
                    rf"|from scripts\.{stem}\b"
                    rf"|import scripts\.{stem}\b)"
                )
                if re.search(pattern, content):
                    rel = py_file.relative_to(ATLAS_ROOT)
                    violations.append(
                        f"  {rel}: imports archived script '{stem}'"
                    )

        assert not violations, (
            "Active code imports archived scripts:\n" + "\n".join(violations)
            + "\n\nFix: either un-archive the script:\n"
            + "  git mv scripts/tools/archive/<f>.py scripts/<f>.py\n"
            + "Or remove the import from the active file."
        )

    def test_archive_directory_exists(self) -> None:
        """scripts/tools/archive/ must exist."""
        assert ARCHIVE_DIR.exists(), f"Archive dir missing: {ARCHIVE_DIR}"

    def test_archive_has_readme(self) -> None:
        """scripts/tools/archive/ must have a README describing what's there."""
        readme = ARCHIVE_DIR / "README.md"
        assert readme.exists(), f"Archive README missing: {readme}"

    def test_archived_files_count(self) -> None:
        """Smoke test: at least 1 archived script must exist (Phase A.6 baseline)."""
        archived = _archive_stems()
        assert len(archived) >= 1, (
            "Expected at least 1 archived script in scripts/tools/archive/. "
            "Did someone accidentally un-archive everything?"
        )
