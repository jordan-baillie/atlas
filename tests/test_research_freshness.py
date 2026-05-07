"""Tests for research/freshness.py freshness guard.

Covers 8 acceptance criteria from the C1 spec.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _no_existing(strategy, universe):
    """Stub: no existing research_best row."""
    return []


def _existing_at(updated_at_str: str):
    """Stub factory: one existing row with given updated_at."""
    def _stub(strategy, universe):
        return [{"updated_at": updated_at_str, "params": {}, "sharpe": 1.0}]
    return _stub


# ── 1. Fresh candidate accepted ──────────────────────────────────────────────

def test_fresh_candidate_accepted():
    """Current-time candidate → returns (True, 'fresh')."""
    from research.freshness import check_freshness

    # Patch the module-level attribute in freshness.py
    with patch("research.freshness.get_research_best", _no_existing):
        allow, reason = check_freshness("momentum", "sp500", notify=False)

    assert allow is True
    assert reason == "fresh"


# ── 2. Stale candidate rejected ──────────────────────────────────────────────

def test_stale_candidate_rejected():
    """Candidate 30d old with 14d threshold → rejected."""
    from research.freshness import check_freshness

    stale_ts = datetime.now(timezone.utc) - timedelta(days=30)

    with patch("research.freshness.get_research_best", _no_existing):
        allow, reason = check_freshness(
            "momentum", "sp500",
            candidate_timestamp=stale_ts,
            freshness_days=14,
            notify=False,
        )

    assert allow is False
    assert "freshness reject" in reason


# ── 3. Older than existing row rejected ──────────────────────────────────────

def test_older_than_existing_rejected():
    """Existing row updated 1h ago; candidate timestamp 2h ago → rejected."""
    from research.freshness import check_freshness

    now = datetime.now(timezone.utc)
    existing_ts = now - timedelta(hours=1)
    candidate_ts = now - timedelta(hours=2)

    with patch(
        "research.freshness.get_research_best",
        _existing_at(existing_ts.isoformat()),
    ):
        allow, reason = check_freshness(
            "momentum", "sp500",
            candidate_timestamp=candidate_ts,
            notify=False,
        )

    assert allow is False
    assert "older than existing" in reason


# ── 4. Configurable threshold ─────────────────────────────────────────────────

def test_configurable_threshold():
    """30d-old candidate with freshness_days=60 → accepted (within window)."""
    from research.freshness import check_freshness

    stale_ts = datetime.now(timezone.utc) - timedelta(days=30)

    with patch("research.freshness.get_research_best", _no_existing):
        allow, reason = check_freshness(
            "momentum", "sp500",
            candidate_timestamp=stale_ts,
            freshness_days=60,
            notify=False,
        )

    assert allow is True
    assert reason == "fresh"


# ── 5. Telegram alert sent on rejection ──────────────────────────────────────

def test_telegram_alert_sent_on_rejection():
    """get_alert_manager().send() called once containing 'freshness' on rejection.

    freshness.py was migrated to AlertManager (#7).  The patchable module-level
    name is now ``research.freshness.get_alert_manager`` (was ``send_message``).
    """
    from research.freshness import check_freshness

    stale_ts = datetime.now(timezone.utc) - timedelta(days=30)
    alerts = []

    mock_am = MagicMock()
    mock_am.send.side_effect = lambda msg, **kw: alerts.append(msg) or True

    with patch("research.freshness.get_research_best", _no_existing), \
         patch("research.freshness.get_alert_manager", return_value=mock_am):
        allow, reason = check_freshness(
            "momentum", "sp500",
            candidate_timestamp=stale_ts,
            freshness_days=14,
            notify=True,
        )

    assert allow is False
    assert mock_am.send.call_count == 1
    assert "freshness" in alerts[0].lower()


# ── 6. save_best invokes freshness guard ─────────────────────────────────────

def test_save_best_invokes_freshness_guard(tmp_path):
    """Integration: save_best is blocked when guard returns (False, ...)."""
    import research.loop as loop_mod

    calls = []

    def _mock_guard(strategy, universe, **kwargs):
        calls.append((strategy, universe))
        return (False, "test reject")

    upsert_calls = []

    def _mock_upsert(**kwargs):
        upsert_calls.append(kwargs)

    best_dir_orig = loop_mod.BEST_DIR
    try:
        loop_mod.BEST_DIR = tmp_path / "best"

        # Patch at the module-level names in research.loop
        with patch("research.loop.check_freshness", _mock_guard), \
             patch("research.loop.upsert_research_best", _mock_upsert):
            loop_mod.save_best(
                strategy="test_strat",
                market="test_universe",
                params={"rsi": 14},
                metrics={"sharpe": 1.2, "total_trades": 50},
            )
    finally:
        loop_mod.BEST_DIR = best_dir_orig

    # Guard was called
    assert len(calls) == 1
    assert calls[0] == ("test_strat", "test_universe")

    # No JSON file written
    best_files = list(tmp_path.glob("**/*.json"))
    assert best_files == [], f"Expected no JSON files, found: {best_files}"

    # upsert_research_best NOT called
    assert upsert_calls == []


# ── 7. _promote_session_result invokes freshness guard ───────────────────────

def test_promote_session_result_invokes_freshness_guard():
    """Integration: _promote_session_result is blocked when freshness guard rejects."""
    import research.autoresearch_runner as runner_mod

    # Patch research.freshness.check_freshness so when the runner does
    # `from research.freshness import check_freshness as _cf` it gets our mock
    guard_calls = []

    def _mock_guard(strategy, universe, **kwargs):
        guard_calls.append((strategy, universe))
        return (False, "promo test reject")

    # Stub get_research_best at the db level so the runner reaches the guard
    def _mock_grb(strategy, universe):
        return [{"params": {"rsi": 14}, "sharpe": 1.5, "updated_at": None}]

    with patch("research.freshness.check_freshness", _mock_guard), \
         patch("db.atlas_db.get_research_best", _mock_grb):
        result = runner_mod._promote_session_result(
            strategy="mean_reversion",
            market="sp500",
            universe="sp500",
            kept=3,
            starting_sharpe=1.0,
            final_sharpe=1.2,
        )

    # Result is rejected
    assert result is not None
    assert result.get("promoted") is False
    assert "promo test reject" in result.get("reason", "")


# ── 8. Both paths reference research.freshness ───────────────────────────────

def test_shared_helper_used_by_both_paths():
    """Verify that both save_best and _promote_session_result reference research.freshness."""
    import research.loop
    import research.autoresearch_runner
    import inspect

    loop_src = inspect.getsource(research.loop.save_best)
    assert "freshness" in loop_src, "save_best must reference research.freshness"

    runner_src = inspect.getsource(research.autoresearch_runner._promote_session_result)
    assert "freshness" in runner_src, "_promote_session_result must reference research.freshness"
