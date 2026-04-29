"""Tests for auto_remediation config file correctness.

Validates that all three config files parse correctly and contain the exact
user-ratified settings from 2026-04-29.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = PROJECT_ROOT / "config" / "auto_remediation.yaml"
DENY_PATH = PROJECT_ROOT / "config" / "auto_fix_deny.yaml"
FUNCS_PATH = PROJECT_ROOT / "config" / "safety_critical_functions.txt"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cfg() -> dict:
    with open(CFG_PATH) as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="module")
def deny() -> dict:
    with open(DENY_PATH) as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="module")
def funcs() -> set[str]:
    with open(FUNCS_PATH) as f:
        return {ln.strip() for ln in f if ln.strip() and not ln.startswith("#")}


# ===========================================================================
# 1. Files parse without error
# ===========================================================================

class TestFilesParse:
    def test_auto_remediation_yaml_parses(self):
        with open(CFG_PATH) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)

    def test_auto_fix_deny_yaml_parses(self):
        with open(DENY_PATH) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)

    def test_safety_critical_functions_txt_readable(self):
        content = FUNCS_PATH.read_text()
        assert len(content.strip()) > 0


# ===========================================================================
# 2. Required top-level keys present
# ===========================================================================

class TestRequiredKeys:
    def test_auto_remediation_top_level_keys(self, cfg):
        required = {
            "budget", "day1_auto_fix_whitelist", "telegram", "graduation",
            "permanent_assist", "never_fix", "defaults_applied", "phase",
            "monitor", "verify", "review", "audit_log", "ignore_patterns"
        }
        missing = required - set(cfg.keys())
        assert not missing, f"Missing top-level keys: {missing}"

    def test_deny_yaml_top_level_keys(self, deny):
        required = {"file_globs", "error_class_patterns", "message_patterns",
                    "function_names_blocked"}
        missing = required - set(deny.keys())
        assert not missing, f"Missing deny keys: {missing}"


# ===========================================================================
# 3. Day-1 whitelist has exactly 6 entries
# ===========================================================================

class TestWhitelist:
    def test_whitelist_has_exactly_six_entries(self, cfg):
        wl = cfg.get("day1_auto_fix_whitelist") or []
        assert len(wl) == 6, f"Expected 6 whitelist entries, got {len(wl)}: {wl}"

    def test_whitelist_contains_expected_classes(self, cfg):
        wl = set(cfg.get("day1_auto_fix_whitelist") or [])
        expected = {
            "test_import_error", "stale_fixture_datetime",
            "lint_non_trading_files", "markdown_typos",
            "dashboard_react_build_errors", "healthz_section_logic"
        }
        assert expected == wl, f"Whitelist mismatch: {wl}"


# ===========================================================================
# 4. permanent_assist.paths includes required globs
# ===========================================================================

class TestPermanentAssistPaths:
    @pytest.fixture(autouse=True)
    def _paths(self, cfg):
        self.paths = set(cfg.get("permanent_assist", {}).get("paths") or [])

    def test_services_present(self):
        assert "services/**" in self.paths

    def test_research_present(self):
        assert "research/**" in self.paths

    def test_monitor_present(self):
        assert "monitor/**" in self.paths

    def test_systemd_present(self):
        assert "systemd/**" in self.paths

    def test_cron_dir_present(self):
        assert "cron/**" in self.paths

    def test_cron_glob_present(self):
        assert "**/*.cron" in self.paths

    def test_config_present(self):
        assert "config/**" in self.paths

    def test_db_migrations_present(self):
        assert "db/migrations/**" in self.paths

    def test_sql_glob_present(self):
        assert "**/*.sql" in self.paths


# ===========================================================================
# 5. never_fix.paths includes required globs
# ===========================================================================

class TestNeverFixPaths:
    @pytest.fixture(autouse=True)
    def _paths(self, cfg):
        self.paths = set(cfg.get("never_fix", {}).get("paths") or [])

    def test_brokers_present(self):
        assert "brokers/**" in self.paths

    def test_risk_present(self):
        assert "risk/**" in self.paths

    def test_regime_present(self):
        assert "regime/**" in self.paths

    def test_signals_present(self):
        assert "signals/**" in self.paths

    def test_portfolio_present(self):
        assert "portfolio/**" in self.paths

    def test_overlay_present(self):
        assert "overlay/**" in self.paths

    def test_strategies_present(self):
        assert "strategies/**" in self.paths

    def test_core_reconcile_present(self):
        assert "core/reconcile.py" in self.paths


# ===========================================================================
# 6. Every glob in deny.yaml is a syntactically valid fnmatch pattern
# ===========================================================================

class TestDenyYamlGlobs:
    def test_all_file_globs_are_valid_fnmatch(self, deny):
        globs = deny.get("file_globs") or []
        assert len(globs) > 0, "file_globs must not be empty"
        invalid = []
        for g in globs:
            try:
                # fnmatch.translate will raise on truly malformed patterns
                fnmatch.translate(g)
            except Exception as exc:
                invalid.append((g, str(exc)))
        assert not invalid, f"Invalid fnmatch globs: {invalid}"

    def test_deny_has_at_least_thirty_file_globs(self, deny):
        globs = deny.get("file_globs") or []
        assert len(globs) >= 30, f"Expected ≥30 file globs, got {len(globs)}"


# ===========================================================================
# 7. safety_critical_functions.txt has ≥30 unique entries
# ===========================================================================

class TestSafetyCriticalFunctions:
    def test_at_least_thirty_unique_entries(self, funcs):
        assert len(funcs) >= 30, f"Expected ≥30 functions, got {len(funcs)}"

    def test_place_order_present(self, funcs):
        assert "place_order" in funcs

    def test_execute_entry_present(self, funcs):
        assert "_execute_entry" in funcs

    def test_halt_present(self, funcs):
        assert "halt" in funcs

    def test_no_inline_comments(self):
        """Lines in the file must not contain inline # comments (one name per line)."""
        lines = FUNCS_PATH.read_text().splitlines()
        for line in lines:
            if line.strip() and not line.strip().startswith("#"):
                assert "#" not in line, f"Inline comment found: {line!r}"

    def test_no_duplicate_entries(self):
        lines = [ln.strip() for ln in FUNCS_PATH.read_text().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        assert len(lines) == len(set(lines)), "Duplicate entries in safety_critical_functions.txt"


# ===========================================================================
# 8. Budget settings match user spec
# ===========================================================================

class TestBudgetSettings:
    @pytest.fixture(autouse=True)
    def _budget(self, cfg):
        self.budget = cfg.get("budget") or {}

    def test_max_commits_per_day_is_10(self):
        assert self.budget.get("max_commits_per_day") == 10

    def test_reverts_to_halt_is_2(self):
        assert self.budget.get("reverts_to_halt") == 2

    def test_revert_rate_alert_pct_is_15(self):
        assert self.budget.get("revert_rate_alert_pct") == 15

    def test_revert_rate_halt_pct_is_25(self):
        assert self.budget.get("revert_rate_halt_pct") == 25


# ===========================================================================
# 9. Telegram settings match user spec
# ===========================================================================

class TestTelegramSettings:
    @pytest.fixture(autouse=True)
    def _tg(self, cfg):
        self.tg = cfg.get("telegram") or {}

    def test_on_success_is_never(self):
        assert self.tg.get("on_success") == "never"

    def test_on_failure_is_immediate(self):
        assert self.tg.get("on_failure") == "immediate"

    def test_daily_digest_is_false(self):
        assert self.tg.get("daily_digest") is False


# ===========================================================================
# 10. Graduation thresholds match user spec
# ===========================================================================

class TestGraduationThresholds:
    @pytest.fixture(autouse=True)
    def _grad(self, cfg):
        self.grad = cfg.get("graduation") or {}

    def test_days_of_clean_assist_is_14(self):
        assist = self.grad.get("assist_to_auto_fix") or {}
        assert assist.get("days_of_clean_assist") == 14

    def test_min_merged_assist_fixes_is_5(self):
        assist = self.grad.get("assist_to_auto_fix") or {}
        assert assist.get("min_merged_assist_fixes") == 5


# ===========================================================================
# 11. Phase settings
# ===========================================================================

class TestPhaseSettings:
    @pytest.fixture(autouse=True)
    def _phase(self, cfg):
        self.phase = cfg.get("phase") or {}

    def test_current_phase_is_2(self):
        assert self.phase.get("current") == 2

    def test_phase_3_enabled_is_false(self):
        assert self.phase.get("phase_3_enabled") is False


# ===========================================================================
# 12. ignore_patterns non-empty and includes expected noise strings
# ===========================================================================

class TestIgnorePatterns:
    def test_ignore_patterns_non_empty(self, cfg):
        patterns = cfg.get("ignore_patterns") or []
        assert len(patterns) >= 4

    def test_circuit_breaker_in_ignore(self, cfg):
        patterns = cfg.get("ignore_patterns") or []
        assert any("Circuit breaker" in p for p in patterns)

    def test_execution_blocked_halted_in_ignore(self, cfg):
        patterns = cfg.get("ignore_patterns") or []
        assert any("Execution blocked: HALTED" in p for p in patterns)
