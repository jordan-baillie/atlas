"""Tests for B3 canary promotion of top-3 sp500 candidates.

Four tests:
  1. test_audit_top3_includes_expected_candidates — audit_promotion_backlog.main()
     captures stdout; asserts all 3 strategy names appear in output table.
  2. test_canary_respects_gate4_reject — Gate 4 rejection via patched
     _run_oos_validation; asserts no research_best write + no pending entry.
  3. test_canary_telegram_notification_fires — Gate 1 rejection; asserts
     _notify mock called with promoted=False and strategy name.
  4. test_pending_promotion_entry_shape — all gates pass (mocked); asserts
     pending_promotions.json entry has required keys with status='pending'.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
import io
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_good_regression_result() -> dict:
    """Minimal regression check result that passes all criteria."""
    m = {
        "sharpe": 1.0,
        "cagr_pct": 15.0,
        "sortino": 1.5,
        "profit_factor": 1.8,
        "win_rate_pct": 55.0,
        "max_drawdown_pct": 10.0,
        "total_trades": 150,
    }
    comparisons = {
        metric: {"baseline": v, "candidate": v, "delta": 0.0, "pct_change": 0.0}
        for metric, v in m.items()
        if metric != "max_drawdown_pct"
    }
    comparisons["max_drawdown_pct"] = {
        "baseline": 10.0, "candidate": 10.0, "delta": 0.0
    }
    comparisons["total_trades"] = {
        "baseline": 150, "candidate": 150, "delta": 0, "pct_change": 0.0
    }
    return {
        "pass": True,
        "baseline_metrics": m,
        "candidate_metrics": m,
        "comparisons": comparisons,
    }


def _make_good_oos_result() -> dict:
    """Minimal OOS result that passes all Gate 4 criteria."""
    return {
        "pass": True,
        "reason": "OOS validation passed",
        "sharpe_oos": 0.85,
        "profit_factor_oos": 1.75,
        "cagr_degradation_pct": 20.0,
        "perturbation_pass_rate": 0.9,
        "raw": {},
    }


# ─── Test 1: audit reflects post-RCA reality (B3 canary RCA, 2026-04-28) ─────


class TestAuditTop3Candidates:
    """Post-RCA assertions about audit_promotion_backlog.main() output.

    The B3 canary (2026-04-28) ran on top-3 sp500 candidates:
    sector_rotation, opening_gap, mean_reversion. RCA revealed that the audit
    was conflating description='baseline' rows (whole-portfolio measurements)
    with strategy parameter improvements. After the filter fix:

    - sector_rotation/sp500: ALL kept rows were baselines -> strategy absent
      from audit (was a 100% artifact, no real parameter improvements exist)
    - opening_gap/sp500: still present but reclassified to fail client gate
      (true delta < 0.05)
    - mean_reversion/sp500: still YES with a genuine +0.7140 delta
    """

    def test_audit_post_rca_strategy_classifications(self) -> None:
        """Audit output reflects post-baseline-filter classifications."""
        from scripts.audit_promotion_backlog import main

        buf = io.StringIO()
        with redirect_stdout(buf):
            main()
        output = buf.getvalue()

        # mean_reversion/sp500 must still be in output and marked YES
        mr_lines = [
            ln for ln in output.splitlines()
            if "mean_reversion" in ln and "sp500" in ln
        ]
        assert mr_lines, (
            f"Expected mean_reversion/sp500 row in audit output.\n"
            f"Full output:\n{output}"
        )
        assert "YES" in mr_lines[0], (
            f"mean_reversion/sp500 should still be promote-eligible "
            f"(genuine +0.7140 delta after baseline filter). Got: {mr_lines[0]}"
        )

        # opening_gap/sp500 must still appear in output but NOT be YES
        og_lines = [
            ln for ln in output.splitlines()
            if "opening_gap" in ln and "sp500" in ln
        ]
        assert og_lines, (
            f"Expected opening_gap/sp500 row in audit output.\n"
            f"Full output:\n{output}"
        )
        assert "YES" not in og_lines[0], (
            f"opening_gap/sp500 was a pre-RCA artifact — true delta is "
            f"below the 0.05 client gate. Should NOT be promote-eligible. "
            f"Got: {og_lines[0]}"
        )

        # sector_rotation/sp500 must NOT appear in output at all
        # (every kept row was description='baseline' — pure artifact)
        sr_sp500_lines = [
            ln for ln in output.splitlines()
            if "sector_rotation" in ln and "sp500" in ln
        ]
        assert not sr_sp500_lines, (
            f"sector_rotation/sp500 should be ABSENT from audit output "
            f"(all kept rows were description='baseline' artifacts). "
            f"Got line(s): {sr_sp500_lines}"
        )


# ─── Test 2: Gate 4 rejection path ───────────────────────────────────────────


class TestCandidateGate4Reject:
    """When Gate 4 (_run_oos_validation) fails: no research_best write,
    no pending_promotions.json entry."""

    _SYNTH_PARAMS = {"rsi_period": 14, "rsi_oversold": 52, "ibs_max": 1.05}

    def test_canary_respects_gate4_reject(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import research.promoter as promoter

        # Redirect pending promotions file to a tmp location
        tmp_pending = tmp_path / "pending_promotions.json"
        monkeypatch.setattr(promoter, "PENDING_PROMOTIONS_PATH", tmp_pending)

        # Redirect OOS cache dir to tmp so no stale cache pollutes
        tmp_oos_dir = tmp_path / "oos_cache"
        monkeypatch.setattr(promoter, "OOS_CACHE_DIR", tmp_oos_dir)

        # Gate 1: pass (cooldown clear)
        monkeypatch.setattr(promoter, "_check_cooldown", lambda _: True)

        # Gate 2: pass (regression OK, good metrics)
        monkeypatch.setattr(promoter, "_regression_check", lambda c, m: _make_good_regression_result())

        # Gate 3: pass (sanity OK — driven by candidate_metrics from gate 2 result)

        # Gate 4: FAIL — mock OOS validation returning failure
        monkeypatch.setattr(
            promoter, "_run_oos_validation",
            lambda c, m: {"pass": False, "reason": "mock OOS fail"},
        )

        # Suppress Telegram side-effects
        notified: list[dict] = []
        monkeypatch.setattr(promoter, "_notify", lambda r: notified.append(r))

        # Also suppress brain write
        monkeypatch.setattr(
            "research.brain.writer.record_promotion",
            lambda **_kw: None,
            raising=False,
        )

        result = promoter.auto_promote(
            strategy="mean_reversion",
            improved_params=self._SYNTH_PARAMS,
            initial_sharpe=0.2691,
            final_sharpe=0.9831,
            improvements=["canary test: +0.714"],
            market="sp500",
        )

        # Should be rejected
        assert result["promoted"] is False
        assert result.get("pending") is not True
        assert "OOS" in result["reason"] or "mock OOS fail" in result["reason"]

        # Pending promotions file should NOT exist (or have no new entries)
        if tmp_pending.exists():
            entries = json.loads(tmp_pending.read_text())
            sp500_entries = [
                e for e in entries
                if e.get("market") == "sp500" and e.get("strategy") == "mean_reversion"
            ]
            assert len(sp500_entries) == 0, (
                f"Expected no pending entry after Gate 4 rejection, got: {sp500_entries}"
            )

        # _notify was called with promoted=False
        assert len(notified) >= 1
        assert notified[-1]["promoted"] is False


# ─── Test 3: Gate 1 (cooldown) notification fires ────────────────────────────


class TestCandidateTelegramNotification:
    """When Gate 1 (cooldown) fires, _notify is called with promoted=False
    and the strategy name in the payload."""

    def test_canary_telegram_notification_fires(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import research.promoter as promoter

        notify_calls: list[dict] = []

        # Force Gate 1 to fail: cooldown active
        monkeypatch.setattr(promoter, "_check_cooldown", lambda _: False)

        # Replace _notify with a list-appending mock
        monkeypatch.setattr(
            promoter, "_notify",
            lambda r: notify_calls.append(dict(r)),
        )

        result = promoter.auto_promote(
            strategy="sector_rotation",
            improved_params={"weight": 0.15},
            initial_sharpe=0.0442,
            final_sharpe=0.9099,
            improvements=["canary test"],
            market="sp500",
        )

        assert result["promoted"] is False
        assert "cooldown" in result["reason"].lower()

        # Notification must have been fired
        assert len(notify_calls) >= 1, "Expected _notify to be called at Gate 1 failure"

        last = notify_calls[-1]
        assert last["promoted"] is False, f"Expected promoted=False, got {last}"
        assert last.get("strategy") == "sector_rotation", (
            f"Expected strategy='sector_rotation', got {last.get('strategy')}"
        )


# ─── Test 4: Pending promotion entry shape ───────────────────────────────────


class TestPendingPromotionEntryShape:
    """When all 4 gates pass, pending_promotions.json gets an entry with
    keys: pending_id, strategy, market, timestamp, status='pending'."""

    _SYNTH_PARAMS = {"gap_threshold": -0.0, "ibs_confirm": 0.52, "rsi14_max": 44}

    def test_pending_promotion_entry_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import research.promoter as promoter

        # Redirect pending promotions to tmp file
        tmp_pending = tmp_path / "pending_promotions.json"
        monkeypatch.setattr(promoter, "PENDING_PROMOTIONS_PATH", tmp_pending)

        # Redirect OOS cache dir
        tmp_oos_dir = tmp_path / "oos_cache"
        monkeypatch.setattr(promoter, "OOS_CACHE_DIR", tmp_oos_dir)

        # Gate 1: pass
        monkeypatch.setattr(promoter, "_check_cooldown", lambda _: True)

        # Gate 2: pass
        monkeypatch.setattr(promoter, "_regression_check", lambda c, m: _make_good_regression_result())

        # Gate 3: pass — driven by gate 2 candidate_metrics which has sharpe=1.0 etc.

        # Gate 4: pass
        monkeypatch.setattr(
            promoter, "_run_oos_validation",
            lambda c, m: _make_good_oos_result(),
        )

        # Suppress Telegram side-effects (approval request)
        monkeypatch.setattr(promoter, "_notify_approval_request", lambda *a, **kw: None)

        # Suppress DSR stats (uses research.loop which touches DB)
        def _noop_dsr():
            return {"num_experiments": 0, "variance_of_sharpes": 0.0}

        monkeypatch.setattr(
            "research.loop._get_dsr_stats",
            _noop_dsr,
            raising=False,
        )

        # Suppress brain write
        monkeypatch.setattr(
            "research.brain.writer.record_promotion",
            lambda **_kw: None,
            raising=False,
        )

        result = promoter.auto_promote(
            strategy="opening_gap",
            improved_params=self._SYNTH_PARAMS,
            initial_sharpe=0.0989,
            final_sharpe=0.9099,
            improvements=["canary test: +0.811"],
            market="sp500",
        )

        # All gates passed → pending approval
        assert result.get("pending") is True, (
            f"Expected pending=True (all gates passed), got: {result}"
        )
        assert result["promoted"] is False  # not promoted until human approves

        # Check pending_promotions.json entry shape
        assert tmp_pending.exists(), "pending_promotions.json was not created"
        entries = json.loads(tmp_pending.read_text())
        assert len(entries) >= 1, "Expected at least one pending entry"

        entry = entries[-1]  # newest entry

        required_keys = {"pending_id", "strategy", "market", "timestamp", "status"}
        missing = required_keys - set(entry.keys())
        assert not missing, f"Pending entry missing keys: {missing}. Entry: {entry}"

        assert entry["strategy"] == "opening_gap"
        assert entry["market"] == "sp500"
        assert entry["status"] == "pending"
        assert isinstance(entry["pending_id"], str) and len(entry["pending_id"]) > 0
        assert isinstance(entry["timestamp"], str) and "T" in entry["timestamp"]


# ─── Test 5: audit script filters baseline rows ─────────────────────────────


class TestAuditBaselineFilter:
    """Regression test for the baseline-filter fix (B3 canary RCA, 2026-04-28).

    The audit's main aggregate query MUST exclude rows where
    description='baseline' OR params_changed IS NULL — these are
    whole-portfolio measurements, not strategy parameter improvements.
    """

    def test_audit_query_filters_baseline_rows(self) -> None:
        """Static check: audit script source contains the baseline filter."""
        script = (ATLAS_ROOT / "scripts" / "audit_promotion_backlog.py").read_text()
        assert "description != 'baseline'" in script, (
            "audit_promotion_backlog.py must filter out description='baseline' rows"
        )
        assert "params_changed IS NOT NULL" in script, (
            "audit_promotion_backlog.py must filter out NULL params_changed rows"
        )

    def test_audit_output_excludes_baseline_aggregates(self) -> None:
        """Functional check: re-run audit and confirm baseline rows do not
        contribute to any group's n_kept (compare counts vs raw DB query)."""
        import sqlite3

        db_path = ATLAS_ROOT / "data" / "atlas.db"
        conn = sqlite3.connect(str(db_path))

        # Total kept rows in window (no filter)
        total_unfiltered = conn.execute(
            """SELECT COUNT(*) FROM research_experiments
               WHERE status='kept' AND created_at >= '2026-04-13'"""
        ).fetchone()[0]

        # Total with baseline filter applied (matches the audit query)
        total_filtered = conn.execute(
            """SELECT COUNT(*) FROM research_experiments
               WHERE status='kept' AND created_at >= '2026-04-13'
                 AND description != 'baseline'
                 AND params_changed IS NOT NULL"""
        ).fetchone()[0]
        conn.close()

        # The filter MUST drop a meaningful number of rows (baseline rows exist)
        assert total_filtered < total_unfiltered, (
            f"Expected filter to drop rows, but unfiltered={total_unfiltered} "
            f"and filtered={total_filtered}"
        )

        # Run the audit; sum n_kept from the table; confirm it equals filtered count
        from scripts.audit_promotion_backlog import main as audit_main

        buf = io.StringIO()
        with redirect_stdout(buf):
            audit_main()
        output = buf.getvalue()

        # Parse the BACKLOG summary line
        backlog_lines = [ln for ln in output.splitlines() if ln.startswith("BACKLOG:")]
        assert backlog_lines, f"No BACKLOG summary line in audit output:\n{output}"
        # Form: "BACKLOG: <N> kept experiments across <M> ..."
        import re
        m = re.search(r"BACKLOG:\s+(\d+)\s+kept experiments", backlog_lines[0])
        assert m, f"Could not parse BACKLOG line: {backlog_lines[0]}"
        reported_total = int(m.group(1))

        assert reported_total == total_filtered, (
            f"Audit reports {reported_total} kept experiments; filtered DB count "
            f"is {total_filtered}. Baseline filter is not active."
        )
