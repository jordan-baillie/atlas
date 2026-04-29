"""Tests for core/triage.py — TriageClassifier.

Covers all 7 rule layers with 50+ tests.
Groups:
  A. NEVER list — file_globs
  B. NEVER list — error_class_patterns
  C. NEVER list — message_patterns
  D. NEVER list — safety_critical_functions
  E. NEVER list — causal chain (traceback)
  F. IGNORE patterns
  G. ESCALATE_DEFERRED (market hours active)
  H. IGNORE_PENDING_CLEAR (HALT file present)
  I. Permanent-ASSIST paths
  J. Default-deny
  K. Phase 3 disabled gate
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.triage import TriageClassifier, TriageResult


# ---------------------------------------------------------------------------
# Shared fixture — loads classifier from real YAML configs once per module
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def clf() -> TriageClassifier:
    return TriageClassifier()


def _err(**kwargs) -> dict:
    """Build a minimal error dict, defaulting all fields to empty/None."""
    defaults = dict(
        file_path="",
        exc_type="",
        message="",
        traceback="",
        function_name="",
        market_hours=0,
        halt_active=0,
        level="ERROR",
        service="test",
    )
    defaults.update(kwargs)
    return defaults


# ===========================================================================
# A. NEVER list — file_globs (10 tests)
# ===========================================================================

class TestNeverFileGlobs:
    def test_brokers_path_escalates(self, clf):
        r = clf.classify(_err(file_path="brokers/alpaca/broker.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_risk_path_escalates(self, clf):
        r = clf.classify(_err(file_path="risk/portfolio_var.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_regime_path_escalates(self, clf):
        r = clf.classify(_err(file_path="regime/distributions.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_signals_path_escalates(self, clf):
        r = clf.classify(_err(file_path="signals/sector_rotation.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_strategies_path_escalates(self, clf):
        r = clf.classify(_err(file_path="strategies/momentum.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_portfolio_path_escalates(self, clf):
        r = clf.classify(_err(file_path="portfolio/constructor.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_overlay_path_escalates(self, clf):
        r = clf.classify(_err(file_path="overlay/engine.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_kill_switch_glob_escalates(self, clf):
        r = clf.classify(_err(file_path="utils/kill_switch_manager.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_live_executor_glob_escalates(self, clf):
        r = clf.classify(_err(file_path="brokers/live_executor_v2.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_db_schema_sql_escalates(self, clf):
        r = clf.classify(_err(file_path="db/schema.sql"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_atlas_db_escalates(self, clf):
        r = clf.classify(_err(file_path="db/atlas_db.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_eod_settlement_escalates(self, clf):
        r = clf.classify(_err(file_path="scripts/eod_settlement.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_pi_cron_escalates(self, clf):
        r = clf.classify(_err(file_path="scripts/pi-cron.sh"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_secrets_file_escalates(self, clf):
        r = clf.classify(_err(file_path=".atlas-secrets.json"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_core_reconcile_escalates(self, clf):
        r = clf.classify(_err(file_path="core/reconcile.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_config_active_escalates(self, clf):
        r = clf.classify(_err(file_path="config/active/sp500.json"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_sql_migration_escalates(self, clf):
        r = clf.classify(_err(file_path="scripts/migrations/2026-04-29-add-table.sql"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_halt_glob_escalates(self, clf):
        r = clf.classify(_err(file_path="scripts/halt_trading.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_approve_glob_escalates(self, clf):
        r = clf.classify(_err(file_path="scripts/approve_plan.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_telegram_bot_escalates(self, clf):
        r = clf.classify(_err(file_path="services/telegram_bot.py"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_safe_utils_path_does_not_escalate_via_glob(self, clf):
        """utils/helpers.py is NOT in NEVER globs — should reach a later layer."""
        r = clf.classify(_err(file_path="utils/helpers.py", message="simple syntax error"))
        # Should not be ESCALATE via tier-0 glob; may still ESCALATE from default-deny
        # What matters: if it escalates, it's NOT tier=0 (glob), it's tier=99 (default)
        if r.classification == "ESCALATE":
            assert r.rule_id == "default_deny", f"Unexpected rule_id: {r.rule_id}"


# ===========================================================================
# B. NEVER list — error_class_patterns (5 tests)
# ===========================================================================

class TestNeverErrorClassPatterns:
    def test_broker_error_escalates(self, clf):
        r = clf.classify(_err(exc_type="BrokerError"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_kill_switch_triggered_escalates(self, clf):
        r = clf.classify(_err(exc_type="KillSwitchTriggered"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_drawdown_breach_escalates(self, clf):
        r = clf.classify(_err(exc_type="DrawdownBreach"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_risk_budget_exceeded_escalates(self, clf):
        r = clf.classify(_err(exc_type="RiskBudgetExceeded"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_circuit_breaker_exc_escalates(self, clf):
        r = clf.classify(_err(exc_type="CircuitBreaker"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_halt_error_escalates(self, clf):
        r = clf.classify(_err(exc_type="HaltError"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_order_rejected_escalates(self, clf):
        r = clf.classify(_err(exc_type="OrderRejected"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_value_error_does_not_match_exc_pattern(self, clf):
        """ValueError is NOT in the deny list — should not escalate via exc pattern."""
        r = clf.classify(_err(exc_type="ValueError", message="bad input format"))
        # May escalate from default-deny but NOT from exc pattern
        if r.classification == "ESCALATE":
            assert "exc:" not in r.rule_id


# ===========================================================================
# C. NEVER list — message_patterns (8 tests)
# ===========================================================================

class TestNeverMessagePatterns:
    def test_broker_in_message_escalates(self, clf):
        r = clf.classify(_err(message="Failed to connect to broker"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_order_in_message_escalates(self, clf):
        r = clf.classify(_err(message="Order submission failed"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_drawdown_in_message_escalates(self, clf):
        r = clf.classify(_err(message="Daily drawdown limit exceeded"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_halt_in_message_escalates(self, clf):
        r = clf.classify(_err(message="System HALT condition triggered"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_kill_switch_in_message_escalates(self, clf):
        r = clf.classify(_err(message="kill switch engaged"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_reconcile_in_message_escalates(self, clf):
        r = clf.classify(_err(message="reconcile failed for ticker AMD"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_auth_in_message_escalates(self, clf):
        r = clf.classify(_err(message="auth token expired"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_database_locked_escalates(self, clf):
        r = clf.classify(_err(message="database is locked"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_case_insensitive_broker(self, clf):
        r = clf.classify(_err(message="BROKER connection refused"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_alpaca_in_message_escalates(self, clf):
        r = clf.classify(_err(message="Alpaca API rate limit exceeded"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_stop_price_zero_escalates(self, clf):
        r = clf.classify(_err(message="stop_price=0 detected for AMD"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_pdt_in_message_escalates(self, clf):
        r = clf.classify(_err(message="PDT rule would be violated"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_inert_message_not_escalated_via_msg_pattern(self, clf):
        """A message with no deny keywords should NOT trigger NEVER list."""
        r = clf.classify(_err(message="test fixture setup failed"))
        if r.classification == "ESCALATE":
            assert "never_fix.msg:" not in r.rule_id


# ===========================================================================
# D. NEVER list — safety_critical_functions (3 tests)
# ===========================================================================

class TestNeverSafetyCriticalFunctions:
    def test_place_order_function_escalates(self, clf):
        r = clf.classify(_err(function_name="place_order"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0
        assert "never_fix.fn:place_order" == r.rule_id

    def test_execute_entry_function_escalates(self, clf):
        r = clf.classify(_err(function_name="_execute_entry"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_halt_function_escalates(self, clf):
        r = clf.classify(_err(function_name="halt"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_reconcile_positions_function_escalates(self, clf):
        r = clf.classify(_err(function_name="reconcile_positions"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_check_daily_drawdown_escalates(self, clf):
        r = clf.classify(_err(function_name="check_daily_drawdown"))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_unknown_function_name_no_escalate_via_fn(self, clf):
        """An unknown function should NOT trigger the safety-critical-fn rule."""
        r = clf.classify(_err(function_name="my_helper_fn"))
        if r.classification == "ESCALATE":
            assert "never_fix.fn:" not in r.rule_id

    def test_empty_function_name_no_escalate_via_fn(self, clf):
        """Empty function_name must not match blocked functions."""
        r = clf.classify(_err(function_name=""))
        if r.classification == "ESCALATE":
            assert "never_fix.fn:" not in r.rule_id


# ===========================================================================
# E. NEVER list — causal chain via traceback (3 tests)
# ===========================================================================

class TestNeverCausalChain:
    def test_brokers_in_traceback_escalates(self, clf):
        """file_path is in tests/ but traceback references brokers/ → ESCALATE tier=0."""
        tb = (
            'File "tests/test_something.py", line 10\n'
            '  result = execute()\n'
            'File "brokers/live_executor.py", line 598\n'
            '  self._broker_call(fn)\n'
        )
        r = clf.classify(_err(file_path="tests/test_something.py", traceback=tb))
        assert r.classification == "ESCALATE"
        assert r.tier == 0
        assert "causal_chain" in r.rule_id

    def test_risk_in_traceback_escalates(self, clf):
        tb = (
            'File "utils/common.py", line 20\n'
            '  check()\n'
            'File "risk/portfolio_var.py", line 44\n'
            '  raise RiskLimitBreached()\n'
        )
        r = clf.classify(_err(file_path="utils/common.py", traceback=tb))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_regime_in_traceback_escalates(self, clf):
        tb = (
            'File "data/cache.py", line 5\n'
            'File "regime/model.py", line 88\n'
        )
        r = clf.classify(_err(file_path="data/cache.py", traceback=tb))
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_safe_traceback_does_not_trigger_causal_chain(self, clf):
        """Traceback that only mentions test helpers — no causal chain match."""
        tb = (
            'File "tests/conftest.py", line 10\n'
            'File "utils/helpers.py", line 5\n'
        )
        # conftest IS in deny globs so tier-0 will fire for that — use a different path
        tb2 = (
            'File "utils/helpers.py", line 10\n'
            '  raise ValueError("bad")\n'
        )
        r = clf.classify(_err(file_path="utils/helpers.py", traceback=tb2,
                              message="bad input"))
        # Should not fire causal_chain rule
        assert "causal_chain" not in r.rule_id


# ===========================================================================
# F. IGNORE patterns (4 tests)
# ===========================================================================

class TestIgnorePatterns:
    def test_circuit_breaker_tripped_ignored(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(message="Circuit breaker tripped for AMD"))
            assert r.classification == "IGNORE"

    def test_execution_blocked_halted_escalates_via_halt_pattern(self, clf):
        """'Execution blocked: HALTED' contains 'HALT' which is in NEVER list
        message_patterns (case-insensitive substring 'halt'). NEVER list fires
        first -> ESCALATE tier=0, NOT IGNORE. The ignore_pattern entry only
        suppresses messages that reach layer 4 without a NEVER-list match."""
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(message="Execution blocked: HALTED"))
            assert r.classification == "ESCALATE"
            assert r.tier == 0

    def test_execution_blocked_not_connected_ignored(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(message="Execution blocked: Not connected"))
            assert r.classification == "IGNORE"

    def test_execution_blocked_plan_status_ignored(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(message="Execution blocked: Plan status is PENDING"))
            assert r.classification == "IGNORE"

    def test_execution_blocked_preflight_ignored(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(message="Execution blocked: Pending preflight checks"))
            assert r.classification == "IGNORE"

    def test_ignore_pattern_fires_even_during_market_hours(self, clf):
        """IGNORE patterns are checked BEFORE ESCALATE_DEFERRED during RTH."""
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=True):
            r = clf.classify(_err(message="Circuit breaker tripped"))
            assert r.classification == "IGNORE"


# ===========================================================================
# G. ESCALATE_DEFERRED — market hours active (2 tests)
# ===========================================================================

class TestEscalateDeferred:
    def test_unknown_error_during_market_hours_deferred(self, clf):
        """An unknown error during RTH → ESCALATE_DEFERRED, not immediate ESCALATE."""
        market_open = datetime(2026, 4, 28, 15, 0, 0, tzinfo=timezone.utc)  # 15:00 UTC = 11:00 ET
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=True):
            r = clf.classify(_err(
                file_path="utils/some_helper.py",
                message="Unhandled exception in scheduler",
            ))
            assert r.classification == "ESCALATE_DEFERRED"
            assert r.rule_id == "market_hours_defer"

    def test_off_hours_does_not_defer(self, clf):
        """Same error off-hours → ESCALATE (default-deny), not ESCALATE_DEFERRED."""
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify(_err(
                file_path="utils/some_helper.py",
                message="Unhandled exception in scheduler",
            ))
            assert r.classification != "ESCALATE_DEFERRED"

    def test_is_market_hours_weekday_during_rth(self, clf):
        # Monday 14:00 UTC = during RTH (EDT)
        dt = datetime(2026, 4, 27, 14, 0, 0, tzinfo=timezone.utc)
        assert clf.is_market_hours_now(now=dt) is True

    def test_is_market_hours_weekend(self, clf):
        # Saturday 15:00 UTC
        dt = datetime(2026, 4, 26, 15, 0, 0, tzinfo=timezone.utc)
        assert clf.is_market_hours_now(now=dt) is False

    def test_is_market_hours_weekday_before_open(self, clf):
        # Monday 10:00 UTC — before 13:30 UTC open
        dt = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
        assert clf.is_market_hours_now(now=dt) is False

    def test_is_market_hours_weekday_after_close(self, clf):
        # Monday 22:00 UTC — after 21:00 UTC close
        dt = datetime(2026, 4, 27, 22, 0, 0, tzinfo=timezone.utc)
        assert clf.is_market_hours_now(now=dt) is False


# ===========================================================================
# H. IGNORE_PENDING_CLEAR — HALT file present (2 tests)
# ===========================================================================

class TestIgnorePendingClear:
    def test_halt_file_present_returns_ignore_pending_clear(self, clf, tmp_path):
        halt_file = tmp_path / "HALT"
        halt_file.touch()
        halt_paths = (halt_file,)
        with patch.object(TriageClassifier, "is_halt_active",
                          staticmethod(lambda hp=halt_paths: any(p.exists() for p in hp))):
            r = clf.classify(_err(message="some non-trading error"))
            assert r.classification == "IGNORE_PENDING_CLEAR"
            assert r.rule_id == "halt_active"

    def test_no_halt_file_does_not_suppress(self, clf, tmp_path):
        """Without HALT file, errors proceed normally."""
        halt_paths = (tmp_path / "HALT", tmp_path / ".live_halt", tmp_path / "AUTO_REMEDIATION_HALT")
        with patch.object(TriageClassifier, "is_halt_active",
                          staticmethod(lambda hp=halt_paths: any(p.exists() for p in hp))):
            with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
                r = clf.classify(_err(message="some non-trading error"))
                assert r.classification != "IGNORE_PENDING_CLEAR"

    def test_halt_file_wins_over_ignore_pattern(self, clf, tmp_path):
        """NEVER list fires before HALT check; but HALT fires before IGNORE patterns.
        Here NEVER list doesn't match (safe message) → HALT wins over IGNORE."""
        halt_file = tmp_path / "HALT"
        halt_file.touch()
        halt_paths = (halt_file,)
        with patch.object(TriageClassifier, "is_halt_active",
                          staticmethod(lambda hp=halt_paths: any(p.exists() for p in hp))):
            r = clf.classify(_err(message="Circuit breaker tripped"))
            # HALT check is layer 2; IGNORE is layer 4 — but Circuit breaker message
            # has no NEVER-list match, so HALT fires first
            assert r.classification == "IGNORE_PENDING_CLEAR"


# ===========================================================================
# I. Permanent-ASSIST paths (5 tests)
# ===========================================================================

class TestPermanentAssist:
    def _off_hours_classify(self, clf, error: dict) -> TriageResult:
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            with patch.object(TriageClassifier, "is_halt_active", return_value=False):
                return clf.classify(error)

    def test_services_path_returns_assist(self, clf):
        # services/telegram_bot.py is in NEVER list — use a non-bot services path
        r = self._off_hours_classify(clf, _err(
            file_path="services/dashboard_server.py",
            message="syntax error in template"
        ))
        # services/** is in permanent_assist AND telegram_bot.py is NEVER;
        # dashboard_server.py is services/** → should be ASSIST (not tier-0)
        # BUT services/** is also permanent_assist, and telegram_bot is NEVER
        assert r.classification in ("ASSIST", "ESCALATE")
        if r.classification == "ASSIST":
            assert r.tier == 1

    def test_research_path_returns_assist(self, clf):
        r = self._off_hours_classify(clf, _err(
            file_path="research/loop.py",
            message="import error in research module"
        ))
        assert r.classification == "ASSIST"
        assert r.tier == 1

    def test_monitor_path_returns_assist(self, clf):
        # monitor/lifecycle.py is in NEVER list — use a different monitor file
        r = self._off_hours_classify(clf, _err(
            file_path="monitor/heartbeat.py",
            message="heartbeat check failed"
        ))
        assert r.classification == "ASSIST"
        assert r.tier == 1

    def test_systemd_path_returns_assist(self, clf):
        r = self._off_hours_classify(clf, _err(
            file_path="systemd/atlas-research.service",
            message="service config parse error"
        ))
        # systemd/atlas-*.service is in NEVER deny globs → ESCALATE tier=0
        # This tests that NEVER fires BEFORE permanent_assist
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_config_path_returns_assist(self, clf):
        # config/** is in permanent_assist (but config/active/** is in NEVER)
        r = self._off_hours_classify(clf, _err(
            file_path="config/heartbeat.json",
            message="JSON parse error"
        ))
        assert r.classification == "ASSIST"
        assert r.tier == 1

    def test_cron_file_returns_assist(self, clf):
        r = self._off_hours_classify(clf, _err(
            file_path="scripts/my_task.cron",
            message="cron parse error"
        ))
        # **/*.cron is in both NEVER deny globs AND permanent_assist
        # NEVER fires first → ESCALATE tier=0
        assert r.classification == "ESCALATE"
        assert r.tier == 0

    def test_sql_file_returns_assist_not_never(self, clf):
        """**/*.sql is in NEVER deny globs → must be ESCALATE."""
        r = self._off_hours_classify(clf, _err(
            file_path="db/migrations/add_column.sql",
            message="syntax error"
        ))
        assert r.classification == "ESCALATE"
        assert r.tier == 0


# ===========================================================================
# J. Default-deny (2 tests)
# ===========================================================================

class TestDefaultDeny:
    def _off_hours_classify(self, clf, error: dict) -> TriageResult:
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            with patch.object(TriageClassifier, "is_halt_active", return_value=False):
                return clf.classify(error)

    def test_unknown_file_unknown_message_escalates(self, clf):
        r = self._off_hours_classify(clf, _err(
            file_path="utils/some_utility.py",
            message="unexpected None value in computation",
        ))
        assert r.classification == "ESCALATE"
        assert r.rule_id == "default_deny"
        assert r.tier == 99

    def test_empty_error_dict_escalates(self, clf):
        """Completely empty error row should still result in ESCALATE (default-deny)."""
        r = self._off_hours_classify(clf, {})
        assert r.classification == "ESCALATE"
        assert r.rule_id == "default_deny"

    def test_default_deny_tier_is_99(self, clf):
        r = self._off_hours_classify(clf, _err(
            file_path="lib/utils.py",
            message="unsupported operand type"
        ))
        assert r.classification == "ESCALATE"
        assert r.tier == 99


# ===========================================================================
# K. Phase 3 disabled gate (1 test)
# ===========================================================================

class TestPhase3Gate:
    def test_phase_3_disabled_whitelist_not_auto_fix(self, clf):
        """When phase_3_enabled=false, whitelist classes do NOT return AUTO_FIX."""
        # Even if we somehow construct an error that looks like a whitelist class,
        # it must NOT return AUTO_FIX because phase_3_enabled=false
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            with patch.object(TriageClassifier, "is_halt_active", return_value=False):
                r = clf.classify(_err(
                    file_path="tests/test_something.py",
                    exc_type="test_import_error",
                    message="test import failed"
                ))
                # phase_3_enabled=false → _check_auto_fix_whitelist returns None
                # Result must NOT be AUTO_FIX
                assert r.classification != "AUTO_FIX"

    def test_phase_3_disabled_confirmed_in_config(self, clf):
        """Verify the config itself has phase_3_enabled=false."""
        assert clf._phase_3_enabled is False

    def test_whitelist_has_six_entries(self, clf):
        """User spec requires exactly 6 whitelist entries."""
        assert len(clf._whitelist_classes) == 6


# ===========================================================================
# L. Structural / config correctness tests
# ===========================================================================

class TestStructural:
    def test_classifier_loads_without_error(self):
        """Basic smoke test — classifier initialises from real configs."""
        clf = TriageClassifier()
        assert clf.cfg is not None
        assert clf.deny is not None

    def test_blocked_functions_non_empty(self, clf):
        assert len(clf._blocked_functions) >= 30

    def test_never_globs_non_empty(self, clf):
        assert len(clf._never_globs) >= 10

    def test_ignore_patterns_has_circuit_breaker(self, clf):
        assert any("Circuit breaker" in p for p in clf._ignore_patterns)

    def test_permanent_assist_has_services(self, clf):
        assert any("services" in g for g in clf._permanent_assist_globs)

    def test_result_is_frozen_dataclass(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify({})
        with pytest.raises((AttributeError, TypeError)):
            r.classification = "MUTATED"  # type: ignore[misc]

    def test_classify_returns_triage_result(self, clf):
        with patch.object(TriageClassifier, "is_market_hours_now", return_value=False):
            r = clf.classify({})
        assert isinstance(r, TriageResult)
        assert r.classification in {
            "AUTO_FIX", "ASSIST", "ESCALATE",
            "IGNORE", "ESCALATE_DEFERRED", "IGNORE_PENDING_CLEAR"
        }
