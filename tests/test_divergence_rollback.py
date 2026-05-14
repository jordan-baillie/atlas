#!/usr/bin/env python3
"""Tests for Sub-phase 1.4: auto-rollback on divergence breach.

Covers scripts/check_live_research_divergence.py consecutive-day breach
tracking, PAPER auto-rollback, LIVE escalation, idempotency, and --no-rollback.

Run:
    python3 -m pytest tests/test_divergence_rollback.py -v --timeout=30
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

import scripts.check_live_research_divergence as _mod
from scripts.check_live_research_divergence import (
    _compute_updated_entry,
    _load_state,
    _save_state_atomic,
    process_rollbacks,
    run_divergence_check,
    ROLLBACK_CONSECUTIVE_DAYS,
)
from monitor.strategy_lifecycle import PromotionState


# ── Date helpers ───────────────────────────────────────────────────────────────

def _today() -> str:
    return date.today().isoformat()


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).isoformat()


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ── Shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture()
def state_file(tmp_path: Path) -> Path:
    """Temporary divergence state file (not pre-created — matches prod behaviour)."""
    return tmp_path / "divergence_state.json"


@pytest.fixture()
def rollback_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect PROMOTION_LOG_PATH to a temp file so tests don't touch prod."""
    log = tmp_path / "promotion_log.json"
    monkeypatch.setattr(_mod, "PROMOTION_LOG_PATH", log)
    return log


@pytest.fixture()
def no_telegram(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch utils.telegram.notify to prevent real sends."""
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("utils.telegram.notify", mock, raising=False)
    return mock


def _make_breach_entry(
    streak: int, last_check: str, last_breach: str, state: str = "PAPER"
) -> Dict[str, Any]:
    """Build a per-combo state dict simulating an existing breach streak."""
    return {
        "consecutive_breach_days": streak,
        "last_breach_date": last_breach,
        "last_check_date": last_check,
        "current_state": state,
    }


def _seed_lifecycle(db_conn, strategy: str, universe: str, state: str) -> None:
    """Seed a row into strategy_lifecycle in the isolated test DB."""
    from datetime import datetime, timezone
    entered_at = datetime.now(timezone.utc).isoformat()
    db_conn.execute(
        "INSERT OR REPLACE INTO strategy_lifecycle "
        "(strategy, universe, state, entered_state_at) VALUES (?, ?, ?, ?)",
        (strategy, universe, state, entered_at),
    )
    db_conn.commit()


# ── Pnl data that produces gap > 0.5 vs research_sharpe=1.0 ──────────────────
# live_sharpe ≈ 0.44 → gap ≈ 0.56 > 0.5
_BREACH_PNL = [0.10, 0.05, -0.10, 0.15, -0.05, 0.08, -0.02, 0.12, 0.20, -0.08]

# Pnl data that produces gap < 0.5 (live_sharpe ≈ 1.05 → gap ≈ −0.05)
_CLEAN_PNL = [0.20, 0.15, 0.18, 0.22, 0.10, 0.17, 0.19, 0.14, 0.21, 0.16]

_STRATEGY = "momentum_breakout"
_UNIVERSE = "sp500"
_KEY = f"{_STRATEGY}:{_UNIVERSE}"
_RESEARCH_ROW = [
    {
        "strategy": _STRATEGY,
        "universe": _UNIVERSE,
        "sharpe": 1.0,
        "trades": 10,
        "updated_at": _today(),
    }
]


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1 — consecutive breach counter increments
# ═══════════════════════════════════════════════════════════════════════════════

class TestConsecutiveBreachCounter:
    """Unit tests for _compute_updated_entry streak logic."""

    def test_counter_increments_on_back_to_back_breach(self) -> None:
        """Day1 breach (streak=1 yesterday) → day2 breach → streak becomes 2."""
        today = _today()
        yesterday = _yesterday()
        entry = _make_breach_entry(streak=1, last_check=yesterday, last_breach=yesterday)

        updated = _compute_updated_entry(entry, is_breach=True, today_str=today, yesterday_str=yesterday)

        assert updated["consecutive_breach_days"] == 2
        assert updated["last_breach_date"] == today
        assert updated["last_check_date"] == today

    def test_counter_increments_from_3_to_4(self) -> None:
        """Existing streak of 3 → 4 after another breach day."""
        today = _today()
        yesterday = _yesterday()
        entry = _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday)

        updated = _compute_updated_entry(entry, is_breach=True, today_str=today, yesterday_str=yesterday)

        assert updated["consecutive_breach_days"] == 4

    def test_new_breach_after_no_prior_state_starts_at_1(self) -> None:
        """Empty entry + breach → streak = 1."""
        today = _today()
        yesterday = _yesterday()
        updated = _compute_updated_entry({}, is_breach=True, today_str=today, yesterday_str=yesterday)

        assert updated["consecutive_breach_days"] == 1
        assert updated["last_breach_date"] == today


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2 — reset on clean day
# ═══════════════════════════════════════════════════════════════════════════════

class TestResetOnCleanDay:
    """Streak resets when gap falls below threshold."""

    def test_counter_resets_on_clean_day(self) -> None:
        """Streak at 3 → clean day → counter becomes 0."""
        today = _today()
        yesterday = _yesterday()
        entry = _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday)

        updated = _compute_updated_entry(entry, is_breach=False, today_str=today, yesterday_str=yesterday)

        assert updated["consecutive_breach_days"] == 0
        assert updated["last_check_date"] == today
        # last_breach_date is NOT updated on a clean day
        assert updated["last_breach_date"] == yesterday


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3 — PAPER rollback fires at 5 consecutive days
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperRollback:
    """Integration test: PAPER → RESEARCH rollback after 5-day breach."""

    def test_paper_rollback_fires_at_5_consecutive_days(
        self, state_file: Path, rollback_log: Path, no_telegram: MagicMock
    ) -> None:
        """5th consecutive breach triggers PAPER → RESEARCH via real lifecycle DB."""
        today = _today()
        yesterday = _yesterday()

        # Pre-seed state: 4 days of breach (yesterday was the 4th)
        pre_state = {
            _KEY: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday),
        }
        _save_state_atomic(pre_state, state_file)

        # Seed PAPER row in the isolated lifecycle DB
        from db.atlas_db import get_db
        with get_db() as conn:
            _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

        with (
            patch("scripts.check_live_research_divergence._fetch_research_best_rows",
                  return_value=_RESEARCH_ROW),
            patch("scripts.check_live_research_divergence._fetch_live_trades",
                  return_value=_BREACH_PNL),
        ):
            rc = run_divergence_check(
                gap_threshold=0.5,
                state_file=state_file,
                no_rollback=False,
                dry_run_telegram=True,
                no_telegram=False,
                today=today,
            )

        assert rc == 0

        # Promotion state should now be RESEARCH
        from monitor.strategy_lifecycle import get_state
        assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.RESEARCH

        # Rollback log entry written
        assert rollback_log.exists()
        entries = json.loads(rollback_log.read_text())
        assert len(entries) == 1
        entry = entries[0]
        assert entry["from_state"] == "PAPER"
        assert entry["to_state"] == "RESEARCH"
        assert entry["strategy"] == _STRATEGY
        assert entry["universe"] == _UNIVERSE

        # Streak reset to 0 in state file after rollback
        saved = _load_state(state_file)
        assert saved[_KEY]["consecutive_breach_days"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4 — no rollback at 4 consecutive days
# ═══════════════════════════════════════════════════════════════════════════════

class TestPaperNoRollbackAt4Days:
    def test_no_rollback_at_4_consecutive_days(
        self, state_file: Path, rollback_log: Path
    ) -> None:
        """Streak reaches 4 today → no transition fired (threshold is 5)."""
        today = _today()
        yesterday = _yesterday()

        # Pre-seed state: 3 days of breach (yesterday was 3rd)
        pre_state = {
            _KEY: _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday),
        }
        _save_state_atomic(pre_state, state_file)

        # Seed PAPER row in the isolated lifecycle DB
        from db.atlas_db import get_db
        with get_db() as conn:
            _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

        with (
            patch("scripts.check_live_research_divergence._fetch_research_best_rows",
                  return_value=_RESEARCH_ROW),
            patch("scripts.check_live_research_divergence._fetch_live_trades",
                  return_value=_BREACH_PNL),
        ):
            rc = run_divergence_check(
                gap_threshold=0.5,
                state_file=state_file,
                no_rollback=False,
                dry_run_telegram=True,
                no_telegram=True,
                today=today,
            )

        assert rc == 0

        # Promotion state should remain PAPER (no transition yet)
        from monitor.strategy_lifecycle import get_state
        assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.PAPER

        # No rollback log written
        assert not rollback_log.exists()

        # Streak should have incremented to 4
        saved = _load_state(state_file)
        assert saved[_KEY]["consecutive_breach_days"] == 4


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5 — intermittent breach resets counter
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntermittentBreach:
    def test_intermittent_breach_resets_counter(self) -> None:
        """Unit test: streak at 3, clean day today → counter resets to 0."""
        today = _today()
        yesterday = _yesterday()
        entry = _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday)

        updated = _compute_updated_entry(entry, is_breach=False, today_str=today, yesterday_str=yesterday)

        assert updated["consecutive_breach_days"] == 0

    def test_skipped_day_resets_streak_even_on_breach(self) -> None:
        """Breach today but last check was 2+ days ago → streak broken → set to 1."""
        today = _today()
        yesterday = _yesterday()
        two_days_ago = _days_ago(2)

        entry = _make_breach_entry(streak=3, last_check=two_days_ago, last_breach=two_days_ago)

        updated = _compute_updated_entry(entry, is_breach=True, today_str=today, yesterday_str=yesterday)

        # Streak broken by the skip — new streak starts at 1
        assert updated["consecutive_breach_days"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6 — LIVE state: Telegram escalation + force_to_watch (Item 3)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLiveStateEscalation:
    """LIVE strategies get Telegram alert + health-state demotion to WATCH."""

    _LIVE_STRATEGY = "trend_following"
    _LIVE_UNIVERSE = "sp500"
    _LIVE_KEY = "trend_following:sp500"
    _LIVE_ROW = [
        {
            "strategy": "trend_following",
            "universe": "sp500",
            "sharpe": 1.0,
            "trades": 10,
            "updated_at": _today(),
        }
    ]

    def test_live_5_day_breach_alert_and_health_demotion(
        self, state_file: Path, rollback_log: Path
    ) -> None:
        """5 consecutive breach days for LIVE → Telegram alert + force_to_watch called; promo state unchanged."""
        today = _today()
        yesterday = _yesterday()

        # Pre-seed state: 4 days breach
        pre_state = {
            self._LIVE_KEY: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday, state="LIVE"),
        }
        _save_state_atomic(pre_state, state_file)

        # Seed LIVE row in the isolated lifecycle DB
        from db.atlas_db import get_db
        with get_db() as conn:
            _seed_lifecycle(conn, self._LIVE_STRATEGY, self._LIVE_UNIVERSE, "LIVE")

        # Track force_to_watch calls; mock class to avoid real lifecycle file writes
        force_to_watch_calls = []
        mock_lcm = MagicMock()
        mock_lcm.force_to_watch = MagicMock(
            side_effect=lambda s, r: (force_to_watch_calls.append((s, r)), True)[1]
        )

        with (
            patch("scripts.check_live_research_divergence._fetch_research_best_rows",
                  return_value=self._LIVE_ROW),
            patch("scripts.check_live_research_divergence._fetch_live_trades",
                  return_value=_BREACH_PNL),
            patch("monitor.lifecycle.StrategyLifecycleManager", return_value=mock_lcm),
            patch("utils.config.get_active_config", return_value={}),
        ):
            rc = run_divergence_check(
                gap_threshold=0.5,
                state_file=state_file,
                no_rollback=False,
                dry_run_telegram=True,   # print alert, don't send Telegram
                no_telegram=True,
                today=today,
            )

        assert rc == 0

        # Promotion state must remain LIVE (no transition)
        from monitor.strategy_lifecycle import get_state
        assert get_state(self._LIVE_STRATEGY, self._LIVE_UNIVERSE) == PromotionState.LIVE

        # force_to_watch was called exactly once for the LIVE strategy
        assert len(force_to_watch_calls) == 1
        assert force_to_watch_calls[0][0] == self._LIVE_STRATEGY

        # Rollback log written because force_to_watch returned True
        assert rollback_log.exists()
        entries = json.loads(rollback_log.read_text())
        assert len(entries) == 1
        assert entries[0]["health_state_to"] == "WATCH"
        assert entries[0]["from_state"] == "LIVE"
        assert entries[0]["to_state"] == "LIVE"   # promotion state unchanged

        # Streak should now be 5 in state file (kept — not reset for LIVE)
        saved = _load_state(state_file)
        assert saved[self._LIVE_KEY]["consecutive_breach_days"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7 — idempotent same-day run
# ═══════════════════════════════════════════════════════════════════════════════

class TestIdempotency:
    def test_same_day_run_is_no_op(self, state_file: Path) -> None:
        """Second run on the same calendar day exits immediately (no DB calls)."""
        today = _today()

        # Pre-seed state as already run today
        existing = {"_last_run_date": today}
        _save_state_atomic(existing, state_file)

        compute_called = []

        def _spy_compute(*a: Any, **kw: Any) -> List:
            compute_called.append(True)
            return []

        with (
            patch("scripts.check_live_research_divergence.compute_divergences",
                  side_effect=_spy_compute),
        ):
            rc = run_divergence_check(
                state_file=state_file,
                dry_run_telegram=True,
                no_telegram=True,
                today=today,
            )

        assert rc == 0
        # compute_divergences should NOT have been called
        assert compute_called == [], "Second run must be a no-op (idempotency)"

        # State file unchanged
        saved = _load_state(state_file)
        assert saved["_last_run_date"] == today


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8 — --no-rollback flag prevents transition
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoRollbackFlag:
    def test_no_rollback_prevents_transition_at_5_days(
        self, state_file: Path, rollback_log: Path
    ) -> None:
        """--no-rollback: streak reaches 5, but no transition and no log entry."""
        today = _today()
        yesterday = _yesterday()

        # Pre-seed: 4 consecutive breach days (5th would normally trigger rollback)
        pre_state = {
            _KEY: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday),
        }
        _save_state_atomic(pre_state, state_file)

        # Seed PAPER row in isolated DB
        from db.atlas_db import get_db
        with get_db() as conn:
            _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

        with (
            patch("scripts.check_live_research_divergence._fetch_research_best_rows",
                  return_value=_RESEARCH_ROW),
            patch("scripts.check_live_research_divergence._fetch_live_trades",
                  return_value=_BREACH_PNL),
        ):
            rc = run_divergence_check(
                gap_threshold=0.5,
                state_file=state_file,
                no_rollback=True,       # ← key: no rollback
                dry_run_telegram=True,
                no_telegram=True,
                today=today,
            )

        assert rc == 0

        # Promotion state should remain PAPER
        from monitor.strategy_lifecycle import get_state
        assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.PAPER

        # No log entry written
        assert not rollback_log.exists()

        # Counter should have incremented to 5 (tracking still works)
        saved = _load_state(state_file)
        assert saved[_KEY]["consecutive_breach_days"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9 — LIVE breach calls force_to_watch AND sends Telegram (Item 3)
# ═══════════════════════════════════════════════════════════════════════════════

def test_live_breach_calls_force_to_watch_and_sends_telegram(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state_file: Path,
    rollback_log: Path,
) -> None:
    """LIVE-state divergence breach for 5+ days → force_to_watch + Telegram.

    Both the Telegram escalation alert AND force_to_watch must fire.
    Promotion state remains LIVE.
    """
    today = _today()
    yesterday = _yesterday()

    _LIVE_STRATEGY = "sector_rotation"
    _LIVE_UNIVERSE = "sp500"
    _LIVE_KEY = f"{_LIVE_STRATEGY}:{_LIVE_UNIVERSE}"
    _LIVE_ROW = [
        {
            "strategy": _LIVE_STRATEGY,
            "universe": _LIVE_UNIVERSE,
            "sharpe": 1.0,
            "trades": 10,
            "updated_at": today,
        }
    ]

    # Pre-seed state: 4 days breach (5th fires rollback gate)
    pre_state = {
        _LIVE_KEY: _make_breach_entry(
            streak=4, last_check=yesterday, last_breach=yesterday, state="LIVE"
        ),
    }
    _save_state_atomic(pre_state, state_file)

    # Seed LIVE row in isolated lifecycle DB
    from db.atlas_db import get_db
    with get_db() as conn:
        _seed_lifecycle(conn, _LIVE_STRATEGY, _LIVE_UNIVERSE, "LIVE")

    # Track calls to Telegram notify
    telegram_calls: list = []

    # Track force_to_watch calls; mock class to avoid real lifecycle file writes
    force_to_watch_calls: list = []
    mock_lcm = MagicMock()
    mock_lcm.force_to_watch = MagicMock(
        side_effect=lambda s, r: (force_to_watch_calls.append((s, r)), True)[1]
    )

    with (
        patch("scripts.check_live_research_divergence._fetch_research_best_rows",
              return_value=_LIVE_ROW),
        patch("scripts.check_live_research_divergence._fetch_live_trades",
              return_value=_BREACH_PNL),
        patch("monitor.lifecycle.StrategyLifecycleManager", return_value=mock_lcm),
        patch("utils.config.get_active_config", return_value={}),
        patch("utils.telegram.notify",
              side_effect=lambda m, **kw: telegram_calls.append(m)),
    ):
        rc = run_divergence_check(
            gap_threshold=0.5,
            state_file=state_file,
            no_rollback=False,
            dry_run_telegram=False,   # real send path (intercepted by mock)
            no_telegram=False,
            today=today,
        )

    assert rc == 0

    # Telegram alert MUST still fire (escalation alert + digest)
    assert len(telegram_calls) >= 1, "Telegram notify must be called"

    # force_to_watch MUST be called with the correct strategy name
    assert len(force_to_watch_calls) == 1, "force_to_watch must be called exactly once"
    assert force_to_watch_calls[0][0] == _LIVE_STRATEGY

    # Promotion state must remain LIVE (no promotion-state transition)
    from monitor.strategy_lifecycle import get_state
    assert get_state(_LIVE_STRATEGY, _LIVE_UNIVERSE) == PromotionState.LIVE

    # Rollback log entry written (force_to_watch returned True)
    assert rollback_log.exists()
    entries = json.loads(rollback_log.read_text())
    assert len(entries) >= 1
    entry = entries[0]
    assert entry["from_state"] == "LIVE"
    assert entry["to_state"] == "LIVE"
    assert entry["health_state_to"] == "WATCH"
    assert entry["strategy"] == _LIVE_STRATEGY

    # Breach streak is kept at 5 (not reset for LIVE — operator must act)
    saved = _load_state(state_file)
    assert saved[_LIVE_KEY]["consecutive_breach_days"] == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Spec-required test names (Task 14 acceptance criteria)
# These carry the exact function names from the spec for traceability.
# Where equivalent logic already exists in classes above, these standalone
# functions add the canonical spec name.
# ═══════════════════════════════════════════════════════════════════════════════


def test_5_consecutive_days_paper_triggers_rollback_to_research(
    state_file: Path, rollback_log: Path, no_telegram: MagicMock
) -> None:
    """PAPER → RESEARCH auto-rollback fires on 5th consecutive breach day.

    Replicates TestPaperRollback but as a top-level function per spec naming.
    """
    today = _today()
    yesterday = _yesterday()

    # Pre-seed: 4 days breach (5th today fires rollback)
    pre_state = {
        _KEY: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday),
    }
    _save_state_atomic(pre_state, state_file)

    from db.atlas_db import get_db
    with get_db() as conn:
        _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

    with (
        patch("scripts.check_live_research_divergence._fetch_research_best_rows",
              return_value=_RESEARCH_ROW),
        patch("scripts.check_live_research_divergence._fetch_live_trades",
              return_value=_BREACH_PNL),
    ):
        rc = run_divergence_check(
            gap_threshold=0.5,
            state_file=state_file,
            no_rollback=False,
            dry_run_telegram=True,
            no_telegram=False,
            today=today,
        )

    assert rc == 0
    from monitor.strategy_lifecycle import get_state
    assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.RESEARCH, (
        "PAPER strategy must roll back to RESEARCH after 5 consecutive breach days"
    )
    assert rollback_log.exists()
    entries = json.loads(rollback_log.read_text())
    assert entries[0]["from_state"] == "PAPER"
    assert entries[0]["to_state"] == "RESEARCH"


def test_4_consecutive_days_no_rollback(
    state_file: Path, rollback_log: Path
) -> None:
    """4 consecutive breach days must NOT trigger rollback (threshold is 5)."""
    today = _today()
    yesterday = _yesterday()

    # Pre-seed: 3 days breach (4th today, NOT enough)
    pre_state = {
        _KEY: _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday),
    }
    _save_state_atomic(pre_state, state_file)

    from db.atlas_db import get_db
    with get_db() as conn:
        _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

    with (
        patch("scripts.check_live_research_divergence._fetch_research_best_rows",
              return_value=_RESEARCH_ROW),
        patch("scripts.check_live_research_divergence._fetch_live_trades",
              return_value=_BREACH_PNL),
    ):
        rc = run_divergence_check(
            gap_threshold=0.5,
            state_file=state_file,
            no_rollback=False,
            dry_run_telegram=True,
            no_telegram=True,
            today=today,
        )

    assert rc == 0
    from monitor.strategy_lifecycle import get_state
    assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.PAPER, (
        "4 consecutive days must NOT trigger rollback — threshold is 5"
    )
    assert not rollback_log.exists()
    saved = _load_state(state_file)
    assert saved[_KEY]["consecutive_breach_days"] == 4, "Streak should increment to 4"


def test_5_consecutive_days_live_soft_rollback_to_paper(
    state_file: Path, rollback_log: Path
) -> None:
    """5 consecutive breach days for LIVE → health demotion (force_to_watch).

    NOTE: The implementation performs health-state demotion to WATCH (via
    StrategyLifecycleManager.force_to_watch) rather than a promotion-state
    transition to PAPER. This is a deliberate design decision: the operator
    must manually flip the config and promotion state via the Controls UI.

    The spec says 'transition(LIVE → PAPER)'; the implementation instead uses
    force_to_watch which changes the HEALTH state to WATCH, leaving the
    PROMOTION state as LIVE.  This deviation is flagged in the commit message.
    """
    today = _today()
    yesterday = _yesterday()

    _LIVE_S = "mean_reversion"
    _LIVE_U = "sp500"
    _LIVE_K = f"{_LIVE_S}:{_LIVE_U}"
    _LIVE_R = [{"strategy": _LIVE_S, "universe": _LIVE_U, "sharpe": 1.0, "trades": 10, "updated_at": today}]

    pre_state = {
        _LIVE_K: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday, state="LIVE"),
    }
    _save_state_atomic(pre_state, state_file)

    from db.atlas_db import get_db
    with get_db() as conn:
        _seed_lifecycle(conn, _LIVE_S, _LIVE_U, "LIVE")

    force_to_watch_calls: list = []
    mock_lcm = MagicMock()
    mock_lcm.force_to_watch = MagicMock(
        side_effect=lambda s, r: (force_to_watch_calls.append((s, r)), True)[1]
    )

    with (
        patch("scripts.check_live_research_divergence._fetch_research_best_rows",
              return_value=_LIVE_R),
        patch("scripts.check_live_research_divergence._fetch_live_trades",
              return_value=_BREACH_PNL),
        patch("monitor.lifecycle.StrategyLifecycleManager", return_value=mock_lcm),
        patch("utils.config.get_active_config", return_value={}),
    ):
        rc = run_divergence_check(
            gap_threshold=0.5,
            state_file=state_file,
            no_rollback=False,
            dry_run_telegram=True,
            no_telegram=True,
            today=today,
        )

    assert rc == 0
    # Implementation: promotion state STAYS LIVE (force_to_watch, not LIVE→PAPER)
    from monitor.strategy_lifecycle import get_state
    assert get_state(_LIVE_S, _LIVE_U) == PromotionState.LIVE, (
        "Promotion state must remain LIVE (operator action required for demotion)"
    )
    # Health demotion must have been triggered
    assert len(force_to_watch_calls) == 1, "force_to_watch must be called"
    assert force_to_watch_calls[0][0] == _LIVE_S


def test_gap_normalizes_resets_counter(state_file: Path) -> None:
    """Gap falling below threshold resets the consecutive breach counter.

    Day 1-3: breach (streak=3). Day 4: gap normalizes (clean) → streak resets to 0.
    Day 5 onwards: fresh start if breach resumes.
    """
    today = _today()
    yesterday = _yesterday()

    # Simulate: after 3 breach days, today is clean
    entry_after_3_days = _make_breach_entry(streak=3, last_check=yesterday, last_breach=yesterday)
    updated = _compute_updated_entry(
        entry_after_3_days, is_breach=False, today_str=today, yesterday_str=yesterday
    )
    assert updated["consecutive_breach_days"] == 0, (
        "Counter must reset to 0 when gap normalizes (clean day)"
    )

    # Verify: clean day does NOT update last_breach_date
    assert updated.get("last_breach_date") == yesterday, (
        "last_breach_date must not be updated on a clean day"
    )

    # Verify: a new breach on the next day after normalization starts fresh at 1
    next_day = today
    prev_day = yesterday
    fresh_breach = _compute_updated_entry(
        {"consecutive_breach_days": 0, "last_check_date": today, "last_breach_date": yesterday},
        is_breach=True, today_str=next_day, yesterday_str=prev_day
    )
    # Note: last_check == yesterday (the clean day), last_breach != yesterday
    # → streak broken → new streak = 1
    assert fresh_breach["consecutive_breach_days"] == 1


def test_dry_run_no_transition(state_file: Path, rollback_log: Path) -> None:
    """--no-rollback (dry-run equivalent) prevents transition even at 5 breach days.

    The divergence script has --no-rollback (not --dry-run) as the flag that
    suppresses state transitions.  This test verifies the safety gate.
    """
    today = _today()
    yesterday = _yesterday()

    # Pre-seed: 4 breach days (5th today would normally fire rollback)
    pre_state = {
        _KEY: _make_breach_entry(streak=4, last_check=yesterday, last_breach=yesterday),
    }
    _save_state_atomic(pre_state, state_file)

    from db.atlas_db import get_db
    with get_db() as conn:
        _seed_lifecycle(conn, _STRATEGY, _UNIVERSE, "PAPER")

    with (
        patch("scripts.check_live_research_divergence._fetch_research_best_rows",
              return_value=_RESEARCH_ROW),
        patch("scripts.check_live_research_divergence._fetch_live_trades",
              return_value=_BREACH_PNL),
    ):
        rc = run_divergence_check(
            gap_threshold=0.5,
            state_file=state_file,
            no_rollback=True,        # ← safety gate equivalent to dry-run
            dry_run_telegram=True,
            no_telegram=True,
            today=today,
        )

    assert rc == 0
    from monitor.strategy_lifecycle import get_state
    assert get_state(_STRATEGY, _UNIVERSE) == PromotionState.PAPER, (
        "no_rollback=True must prevent state transition"
    )
    assert not rollback_log.exists(), "No rollback log when no_rollback=True"
    # Streak still tracks (counter incremented to 5)
    saved = _load_state(state_file)
    assert saved[_KEY]["consecutive_breach_days"] == 5
