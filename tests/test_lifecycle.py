"""Tests for monitor.lifecycle — StrategyLifecycleManager state machine.

Run with:
    cd /root/atlas && pytest tests/test_lifecycle.py -v --tb=short
"""

from __future__ import annotations

import json
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import pytest

# Ensure project root is on path
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from monitor.lifecycle import LifecycleState, LifecycleRecord, StrategyLifecycleManager


# ── Helpers & fixtures ────────────────────────────────────────────────────────

def _make_config(pool_cap: int = 3, strategies: Optional[dict] = None) -> dict:
    """Build a minimal Atlas config that satisfies StrategyLifecycleManager."""
    if strategies is None:
        strategies = {
            "mean_reversion":    {"enabled": True},
            "momentum_breakout": {"enabled": True},
            "trend_following":   {"enabled": True},
        }
    return {
        "allocation": {
            "enabled": True,
            "mode": "hard_pool",
            "pools": {
                "mean_reversion":    {"max_positions": pool_cap},
                "momentum_breakout": {"max_positions": pool_cap},
                "trend_following":   {"max_positions": pool_cap},
            },
        },
        "strategies": strategies,
    }


@dataclass
class MockAssessment:
    strategy: str
    status: str  # HEALTHY, WARNING, DEGRADED, INSUFFICIENT_DATA


@dataclass
class MockHealthReport:
    assessments: List[MockAssessment] = field(default_factory=list)


def _make_report(*pairs: tuple) -> MockHealthReport:
    """Create a MockHealthReport from (strategy, status) pairs."""
    return MockHealthReport(
        assessments=[MockAssessment(strategy=s, status=st) for s, st in pairs]
    )


# ── LifecycleState enum ───────────────────────────────────────────────────────

class TestLifecycleState:
    def test_all_values_defined(self):
        expected = {"RAMP_UP", "ACTIVE", "WATCH", "PROBATION", "SUSPENDED"}
        actual = {s.value for s in LifecycleState}
        assert actual == expected

    def test_str_enum(self):
        assert LifecycleState.ACTIVE == "ACTIVE"
        assert LifecycleState.WATCH == "WATCH"

    def test_from_string(self):
        assert LifecycleState("PROBATION") is LifecycleState.PROBATION


# ── Initialization ────────────────────────────────────────────────────────────

class TestInitialization:
    def test_all_strategies_start_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        config = _make_config()
        mgr = StrategyLifecycleManager(config)

        states = mgr.get_all_states()
        assert states["mean_reversion"]    == "ACTIVE"
        assert states["momentum_breakout"] == "ACTIVE"
        assert states["trend_following"]   == "ACTIVE"

    def test_disabled_strategies_not_tracked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        config = _make_config(strategies={
            "mean_reversion":    {"enabled": True},
            "opening_gap":       {"enabled": False},
        })
        mgr = StrategyLifecycleManager(config)
        states = mgr.get_all_states()
        assert "mean_reversion" in states
        assert "opening_gap" not in states

    def test_get_state_returns_active_for_unknown(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        assert mgr.get_state("nonexistent_strategy") == LifecycleState.ACTIVE


# ── ACTIVE → WATCH ────────────────────────────────────────────────────────────

class TestActiveToWatch:
    def _manager(self, tmp_path, monkeypatch, pool_cap=3):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        return StrategyLifecycleManager(_make_config(pool_cap=pool_cap))

    def test_warning_triggers_watch(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        report = _make_report(("mean_reversion", "WARNING"))
        transitions = mgr.process_health_report(report)

        assert len(transitions) == 1
        assert transitions[0]["strategy"] == "mean_reversion"
        assert transitions[0]["from"] == "ACTIVE"
        assert transitions[0]["to"] == "WATCH"

    def test_single_degraded_triggers_watch(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        report = _make_report(("mean_reversion", "DEGRADED"))
        transitions = mgr.process_health_report(report)

        assert len(transitions) == 1
        assert transitions[0]["to"] == "WATCH"

    def test_pool_cap_reduced_on_watch(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch, pool_cap=3)
        report = _make_report(("mean_reversion", "WARNING"))
        mgr.process_health_report(report)

        # pool_cap_override = max(1, 3-1) = 2
        override = mgr.get_effective_pool_cap("mean_reversion")
        assert override == 2

    def test_pool_cap_floor_is_1(self, tmp_path, monkeypatch):
        """Even if default cap is 1, override should be at least 1."""
        mgr = self._manager(tmp_path, monkeypatch, pool_cap=1)
        report = _make_report(("mean_reversion", "WARNING"))
        mgr.process_health_report(report)
        assert mgr.get_effective_pool_cap("mean_reversion") == 1

    def test_insufficient_data_skipped(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        report = _make_report(("mean_reversion", "INSUFFICIENT_DATA"))
        transitions = mgr.process_health_report(report)
        assert transitions == []
        assert mgr.get_state("mean_reversion") == LifecycleState.ACTIVE


# ── WATCH → ACTIVE recovery ───────────────────────────────────────────────────

class TestWatchToActiveRecovery:
    def _manager_in_watch(self, tmp_path, monkeypatch) -> StrategyLifecycleManager:
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        # Drive into WATCH
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))
        assert mgr.get_state("mean_reversion") == LifecycleState.WATCH
        return mgr

    def test_one_healthy_not_enough(self, tmp_path, monkeypatch):
        mgr = self._manager_in_watch(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        assert mgr.get_state("mean_reversion") == LifecycleState.WATCH

    def test_two_consecutive_healthy_recovers(self, tmp_path, monkeypatch):
        mgr = self._manager_in_watch(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))

        assert mgr.get_state("mean_reversion") == LifecycleState.ACTIVE
        assert any(t["to"] == "ACTIVE" for t in transitions)

    def test_pool_cap_reset_on_recovery(self, tmp_path, monkeypatch):
        mgr = self._manager_in_watch(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))

        # pool_cap_override should be None after recovery
        assert mgr.get_effective_pool_cap("mean_reversion") is None

    def test_interrupted_recovery_resets_counter(self, tmp_path, monkeypatch):
        mgr = self._manager_in_watch(tmp_path, monkeypatch)
        # 1 healthy
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        # then WARNING resets recovery counter
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))
        # 1 more healthy — not enough
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        assert mgr.get_state("mean_reversion") == LifecycleState.WATCH


# ── WATCH/ACTIVE → PROBATION (3+ consecutive DEGRADED) ───────────────────────

class TestActiveToProbation:
    def _manager(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        return StrategyLifecycleManager(_make_config())

    def test_three_consecutive_degraded_triggers_probation(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        for _ in range(3):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_state("mean_reversion") == LifecycleState.PROBATION

    def test_pool_cap_1_in_probation(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        for _ in range(3):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_effective_pool_cap("mean_reversion") == 1

    def test_two_degraded_stays_watch(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        for _ in range(2):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_state("mean_reversion") == LifecycleState.WATCH

    def test_degraded_then_healthy_resets_counter(self, tmp_path, monkeypatch):
        mgr = self._manager(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        # Counter reset → need 3 more degraded for probation
        mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        # Only 2 after reset → still WATCH
        assert mgr.get_state("mean_reversion") == LifecycleState.WATCH


# ── PROBATION → SUSPENDED (4+ consecutive DEGRADED) ─────────────────────────

class TestProbationToSuspended:
    def _manager_in_probation(self, tmp_path, monkeypatch) -> StrategyLifecycleManager:
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        # Drive to PROBATION: 3 consecutive DEGRADED
        for _ in range(3):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_state("mean_reversion") == LifecycleState.PROBATION
        return mgr

    def test_four_consecutive_degraded_suspends(self, tmp_path, monkeypatch):
        mgr = self._manager_in_probation(tmp_path, monkeypatch)
        # Now in PROBATION with consecutive_degraded=3; need one more to reach 4
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_state("mean_reversion") == LifecycleState.SUSPENDED
        assert any(t["to"] == "SUSPENDED" for t in transitions)

    def test_pool_cap_0_when_suspended(self, tmp_path, monkeypatch):
        mgr = self._manager_in_probation(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_effective_pool_cap("mean_reversion") == 0

    def test_three_degraded_in_probation_not_enough(self, tmp_path, monkeypatch):
        """In PROBATION, need the 4th degraded (total) to hit SUSPENDED."""
        mgr = self._manager_in_probation(tmp_path, monkeypatch)
        # Already have 3 consecutive; PROBATION entered at count=3
        # The record's consecutive_degraded = 3 at this point
        # We need one MORE → total 4
        assert mgr.get_state("mean_reversion") == LifecycleState.PROBATION


# ── SUSPENDED → PROBATION recovery ───────────────────────────────────────────

class TestSuspendedRecovery:
    def _manager_suspended(self, tmp_path, monkeypatch) -> StrategyLifecycleManager:
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        # 3 × DEGRADED → PROBATION, then 1 more → SUSPENDED
        for _ in range(4):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_state("mean_reversion") == LifecycleState.SUSPENDED
        return mgr

    def test_two_healthy_from_suspended_goes_probation(self, tmp_path, monkeypatch):
        mgr = self._manager_suspended(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))

        assert mgr.get_state("mean_reversion") == LifecycleState.PROBATION
        assert any(t["to"] == "PROBATION" for t in transitions)

    def test_pool_cap_1_after_suspended_recovery(self, tmp_path, monkeypatch):
        mgr = self._manager_suspended(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        assert mgr.get_effective_pool_cap("mean_reversion") == 1

    def test_one_healthy_from_suspended_stays_suspended(self, tmp_path, monkeypatch):
        mgr = self._manager_suspended(tmp_path, monkeypatch)
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        assert mgr.get_state("mean_reversion") == LifecycleState.SUSPENDED


# ── Pool cap override ─────────────────────────────────────────────────────────

class TestPoolCapOverride:
    def test_no_override_when_active(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        assert mgr.get_effective_pool_cap("mean_reversion") is None

    def test_override_set_on_watch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config(pool_cap=4))
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))
        assert mgr.get_effective_pool_cap("mean_reversion") == 3  # max(1, 4-1)

    def test_override_0_when_suspended(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        for _ in range(4):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        assert mgr.get_effective_pool_cap("mean_reversion") == 0


# ── State persistence ─────────────────────────────────────────────────────────

class TestStatePersistence:
    def test_save_and_load(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "lifecycle_state.json"
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        # Drive a transition and save
        config = _make_config()
        mgr1 = StrategyLifecycleManager(config)
        mgr1.process_health_report(_make_report(("mean_reversion", "WARNING")))
        assert mgr1.get_state("mean_reversion") == LifecycleState.WATCH

        # New instance reads persisted state
        mgr2 = StrategyLifecycleManager(config)
        assert mgr2.get_state("mean_reversion") == LifecycleState.WATCH

    def test_persisted_counters_survive_reload(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "lifecycle_state.json"
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        config = _make_config()
        mgr1 = StrategyLifecycleManager(config)
        mgr1.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        mgr1.process_health_report(_make_report(("mean_reversion", "DEGRADED")))

        # Reload from file
        mgr2 = StrategyLifecycleManager(config)
        rec = mgr2.records.get("mean_reversion")
        assert rec is not None
        assert rec.consecutive_degraded == 2

    def test_json_file_structure(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "lifecycle_state.json"
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        mgr = StrategyLifecycleManager(_make_config())
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))

        # File should be valid JSON with expected keys
        assert lifecycle_file.exists()
        data = json.loads(lifecycle_file.read_text())
        assert "mean_reversion" in data
        rec = data["mean_reversion"]
        assert rec["state"] == "WATCH"
        assert "entered_at" in rec
        assert "consecutive_degraded" in rec
        assert "consecutive_recovered" in rec
        assert "history" in rec
        assert len(rec["history"]) == 1

    def test_history_accumulates(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "lifecycle_state.json"
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        mgr = StrategyLifecycleManager(_make_config())
        # ACTIVE → WATCH
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))
        # WATCH (healthy x2) → ACTIVE
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))
        mgr.process_health_report(_make_report(("mean_reversion", "HEALTHY")))

        data = json.loads(lifecycle_file.read_text())
        history = data["mean_reversion"]["history"]
        assert len(history) == 2  # one down-transition + one recovery

    def test_load_handles_missing_file_gracefully(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "nonexistent_lifecycle.json"
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        # Should not raise; all strategies start ACTIVE
        mgr = StrategyLifecycleManager(_make_config())
        assert mgr.get_state("mean_reversion") == LifecycleState.ACTIVE

    def test_load_handles_corrupt_file_gracefully(self, tmp_path, monkeypatch):
        lifecycle_file = tmp_path / "lifecycle_state.json"
        lifecycle_file.write_text("{ this is not valid JSON }")
        monkeypatch.setattr(StrategyLifecycleManager, "LIFECYCLE_FILE", lifecycle_file)

        # Should fall back to ACTIVE
        mgr = StrategyLifecycleManager(_make_config())
        assert mgr.get_state("mean_reversion") == LifecycleState.ACTIVE


# ── process_health_report multi-strategy ─────────────────────────────────────

class TestMultiStrategy:
    def test_multiple_strategies_transition_independently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        report = _make_report(
            ("mean_reversion",    "WARNING"),
            ("momentum_breakout", "HEALTHY"),
            ("trend_following",   "DEGRADED"),
        )
        transitions = mgr.process_health_report(report)

        assert mgr.get_state("mean_reversion")    == LifecycleState.WATCH
        assert mgr.get_state("momentum_breakout") == LifecycleState.ACTIVE
        assert mgr.get_state("trend_following")   == LifecycleState.WATCH

        # Two transitions: mean_reversion and trend_following
        moved = {t["strategy"] for t in transitions}
        assert moved == {"mean_reversion", "trend_following"}

    def test_no_transitions_returns_empty_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        report = _make_report(
            ("mean_reversion",    "HEALTHY"),
            ("momentum_breakout", "HEALTHY"),
        )
        transitions = mgr.process_health_report(report)
        assert transitions == []

    def test_get_all_states_returns_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        states = mgr.get_all_states()
        assert set(states.keys()) == {"mean_reversion", "momentum_breakout", "trend_following"}
        assert all(v == "ACTIVE" for v in states.values())


# ── get_pool_cap integration ──────────────────────────────────────────────────

class TestGetPoolCap:
    """Test the standalone get_pool_cap() helper in utils.allocation."""

    def test_returns_config_default_without_manager(self):
        from utils.allocation import get_pool_cap
        config = _make_config(pool_cap=4)
        assert get_pool_cap("mean_reversion", config) == 4

    def test_returns_3_when_no_pool_configured(self):
        from utils.allocation import get_pool_cap
        assert get_pool_cap("unknown_strategy", {}) == 3

    def test_lifecycle_override_takes_precedence(self, tmp_path, monkeypatch):
        from utils.allocation import get_pool_cap

        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        config = _make_config(pool_cap=3)
        mgr = StrategyLifecycleManager(config)
        mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))

        result = get_pool_cap("mean_reversion", config, lifecycle_manager=mgr)
        assert result == 2  # max(1, 3-1)

    def test_lifecycle_none_uses_config(self, tmp_path, monkeypatch):
        from utils.allocation import get_pool_cap

        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        config = _make_config(pool_cap=5)
        mgr = StrategyLifecycleManager(config)  # all ACTIVE, no overrides

        result = get_pool_cap("mean_reversion", config, lifecycle_manager=mgr)
        assert result == 5

    def test_suspended_strategy_cap_0(self, tmp_path, monkeypatch):
        from utils.allocation import get_pool_cap

        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        config = _make_config(pool_cap=3)
        mgr = StrategyLifecycleManager(config)
        for _ in range(4):
            mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))

        result = get_pool_cap("mean_reversion", config, lifecycle_manager=mgr)
        assert result == 0


# ── Transition reason and timestamp ──────────────────────────────────────────

class TestTransitionMetadata:
    def test_transition_has_required_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))

        t = transitions[0]
        assert "strategy" in t
        assert "from" in t
        assert "to" in t
        assert "reason" in t
        assert "timestamp" in t

    def test_transition_reason_contains_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "WARNING")))
        assert "WARNING" in transitions[0]["reason"]

    def test_timestamp_is_valid_iso(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            StrategyLifecycleManager, "LIFECYCLE_FILE",
            tmp_path / "lifecycle_state.json",
        )
        mgr = StrategyLifecycleManager(_make_config())
        transitions = mgr.process_health_report(_make_report(("mean_reversion", "DEGRADED")))
        ts = transitions[0]["timestamp"]
        # Should parse without exception
        datetime.fromisoformat(ts)
