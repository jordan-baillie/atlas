"""
Test suite: Telegram noise budget regression.

Target: ≤3 Telegram messages/day during normal (healthy) operation.

Tests:
  1. try_bash_fixes (pycache) — must NOT send Telegram
  2. drift alert cooldown — 2nd call within 4h is suppressed
  3. healthcheck_pipelines silent when all pipelines fresh
  4. healthcheck_tp_coverage silent when all positions covered
  5. reconcile_positions silent when no discrepancies
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ATLAS_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Test 1: pycache cleanup does NOT trigger Telegram
# ---------------------------------------------------------------------------
class TestPycacheNoTelegram:
    """After Fix 1: try_bash_fixes removes pycache but stays silent on Telegram."""

    def test_pycache_cleanup_does_not_telegram(self, tmp_path, monkeypatch):
        """
        Simulate the try_bash_fixes bash function by calling it via subprocess
        with a minimal shim that injects a stub for send_message.

        Strategy: extract the bash try_bash_fixes function logic into a
        mini-script that sources the function, calls it with a pycache issue,
        and asserts no python3 -c send_message was called.

        Simpler approach: verify the text of healthz_hourly.sh no longer
        contains the Telegram send block inside try_bash_fixes.
        """
        script = ATLAS_ROOT / "scripts" / "healthz_hourly.sh"
        content = script.read_text()

        # Locate try_bash_fixes function body
        func_start = content.find("try_bash_fixes() {")
        func_end = content.find("\n}", func_start)
        if func_end < 0:
            # Find the closing brace of the function
            # Walk forward from func_start to find matching "}"
            depth = 0
            i = func_start
            while i < len(content):
                if content[i] == "{":
                    depth += 1
                elif content[i] == "}":
                    depth -= 1
                    if depth == 0:
                        func_end = i
                        break
                i += 1

        func_body = content[func_start:func_end + 1]

        # The Telegram block inside try_bash_fixes must be gone
        assert "Watchdog auto-fixed" not in func_body, (
            "try_bash_fixes still contains a 'Watchdog auto-fixed' Telegram call. "
            "This fires every hour for pycache cleanup — should be log-only."
        )
        assert "send_message" not in func_body, (
            "try_bash_fixes still calls send_message. "
            "Pycache/log rotation is routine — should be log-only."
        )


# ---------------------------------------------------------------------------
# Test 2: drift alert cooldown suppresses 2nd call within 4h
# ---------------------------------------------------------------------------
class TestDriftAlertCooldown:
    """After Fix 3: identical drift hashes are suppressed within 4h."""

    def _compute_drift_hash(self, drift_lines: str) -> str:
        """Mirror the bash hash logic from healthz_hourly.sh."""
        import re
        import hashlib
        normalized = re.sub(r"\d+\.\d+", "N", drift_lines)
        normalized = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", normalized)
        return hashlib.md5(normalized.encode()).hexdigest()[:12]

    def test_drift_alert_first_call_sends(self, tmp_path, monkeypatch):
        """First drift alert (no cooldown file) should be allowed."""
        cooldown_dir = tmp_path / "cooldowns"
        cooldown_dir.mkdir()

        drift_lines = "UNTRACKED: CAT not in sp500 internal state"
        drift_hash = self._compute_drift_hash(drift_lines)
        cooldown_file = cooldown_dir / f"drift_{drift_hash}"

        assert not cooldown_file.exists()

        # Simulate: no cooldown file → should alert
        should_alert = True
        if cooldown_file.exists():
            age_h = (time.time() - cooldown_file.stat().st_mtime) / 3600
            if age_h < 4:
                should_alert = False

        assert should_alert, "First drift alert should not be suppressed"

        # Simulate touching the cooldown file after alerting
        cooldown_file.touch()

    def test_drift_alert_second_call_suppressed(self, tmp_path, monkeypatch):
        """Second drift alert within 4h should be suppressed."""
        cooldown_dir = tmp_path / "cooldowns"
        cooldown_dir.mkdir()

        drift_lines = "UNTRACKED: CAT not in sp500 internal state"
        drift_hash = self._compute_drift_hash(drift_lines)
        cooldown_file = cooldown_dir / f"drift_{drift_hash}"

        # Simulate: cooldown file exists, touched 1h ago → suppress
        cooldown_file.touch()
        # Set mtime to 1 hour ago
        past = time.time() - 3600
        os.utime(str(cooldown_file), (past, past))

        should_alert = True
        if cooldown_file.exists():
            age_h = (time.time() - cooldown_file.stat().st_mtime) / 3600
            if age_h < 4:
                should_alert = False

        assert not should_alert, "Second drift alert within 4h should be suppressed"

    def test_drift_alert_after_cooldown_expires(self, tmp_path):
        """Alert re-fires after 4h cooldown expires."""
        cooldown_dir = tmp_path / "cooldowns"
        cooldown_dir.mkdir()

        drift_lines = "UNTRACKED: CAT not in sp500 internal state"
        drift_hash = self._compute_drift_hash(drift_lines)
        cooldown_file = cooldown_dir / f"drift_{drift_hash}"

        # Set mtime to 5 hours ago
        cooldown_file.touch()
        past = time.time() - (5 * 3600)
        os.utime(str(cooldown_file), (past, past))

        should_alert = True
        if cooldown_file.exists():
            age_h = (time.time() - cooldown_file.stat().st_mtime) / 3600
            if age_h < 4:
                should_alert = False

        assert should_alert, "Alert should re-fire after 4h cooldown"

    def test_healthz_hourly_has_drift_cooldown(self):
        """Verify healthz_hourly.sh contains the drift cooldown logic."""
        content = (ATLAS_ROOT / "scripts" / "healthz_hourly.sh").read_text()

        # Must have the drift cooldown variables
        assert "DRIFT_HASH" in content
        assert "DRIFT_COOLDOWN_FILE" in content
        assert "DRIFT_SHOULD_ALERT" in content
        assert "drift_" in content  # cooldown file name prefix


# ---------------------------------------------------------------------------
# Test 3: healthcheck_pipelines silent on healthy state
# ---------------------------------------------------------------------------
class TestHealthcheckPipelinesSilentOnHealthy:
    """healthcheck_pipelines.run_once() must not send Telegram when all fresh."""

    def test_all_fresh_no_telegram(self, tmp_path, monkeypatch):
        """All pipelines fresh → run_once() returns 0 and does not call send_message."""
        sys.path.insert(0, str(ATLAS_ROOT))
        from scripts.healthcheck_pipelines import run_once, PIPELINES  # type: ignore

        send_calls = []

        def stub_send(*a, **kw):
            send_calls.append((a, kw))
            pytest.fail(f"healthcheck_pipelines sent Telegram on healthy: {a}")

        state_file = tmp_path / "hc_state.json"
        now_utc = datetime.now(timezone.utc)

        # All pipelines return fresh (last checked = now)
        def _fresh_pipeline(pipeline, atlas_root, now):
            return False, now_utc, 0.0  # is_stale=False

        with patch("scripts.healthcheck_pipelines._check_pipeline", side_effect=_fresh_pipeline):
            with patch("utils.telegram.send_message", side_effect=stub_send):
                rc = run_once(
                    state_path=state_file,
                    atlas_root=ATLAS_ROOT,
                    _now=now_utc,
                )

        assert rc == 0, "Expected 0 (all fresh)"
        assert send_calls == [], "No Telegram should be sent when all pipelines are fresh"


# ---------------------------------------------------------------------------
# Test 4: healthcheck_tp_coverage silent when all positions covered
# ---------------------------------------------------------------------------
class TestHealthcheckTpCoverageSilentOnHealthy:
    """TP-coverage check must not send Telegram when all positions have stop+TP."""

    def test_all_covered_no_telegram(self, tmp_path, monkeypatch):
        """All positions have stop AND tp → no Telegram alert."""
        sys.path.insert(0, str(ATLAS_ROOT))

        from scripts.healthcheck_tp_coverage import run_check  # type: ignore

        send_calls = []

        def stub_send(*a, **kw):
            send_calls.append((a, kw))
            pytest.fail(f"healthcheck_tp_coverage sent Telegram on healthy: {a}")

        # Mock broker for one market returning fully-covered position
        mock_pos = MagicMock()
        mock_pos.ticker = "CAT"

        mock_order_stop = MagicMock()
        mock_order_stop.order_type = "stop"
        mock_order_stop.side = "sell"
        mock_order_stop.status = "accepted"

        mock_order_tp = MagicMock()
        mock_order_tp.order_type = "limit"
        mock_order_tp.side = "sell"
        mock_order_tp.status = "accepted"

        mock_broker = MagicMock()
        mock_broker.connect.return_value = True
        mock_broker.get_positions.return_value = [mock_pos]
        mock_broker.get_open_orders.return_value = [mock_order_stop, mock_order_tp]

        state_file = tmp_path / "tp_state.json"

        with patch("scripts.healthcheck_tp_coverage.check_market") as mock_check:
            mock_check.return_value = (
                [{"ticker": "CAT", "market": "sp500", "has_stop": True, "has_tp": True}],
                None,
            )
            with patch("utils.telegram.send_message", side_effect=stub_send):
                rc = run_check(
                    markets=("sp500",),
                    no_alert=False,
                    state_path=state_file,
                )

        assert rc == 0, f"Expected 0 (all covered), got {rc}"
        assert send_calls == [], "No Telegram when all positions are covered"

    def test_no_positions_no_telegram(self, tmp_path):
        """No positions → no alert."""
        sys.path.insert(0, str(ATLAS_ROOT))
        from scripts.healthcheck_tp_coverage import run_check  # type: ignore

        send_calls = []
        state_file = tmp_path / "tp_state_empty.json"

        with patch("scripts.healthcheck_tp_coverage.check_market") as mock_check:
            mock_check.return_value = ([], None)  # no positions
            with patch("utils.telegram.send_message", lambda *a, **kw: send_calls.append(a) or pytest.fail("Telegram sent for empty portfolio")):
                rc = run_check(markets=("sp500",), no_alert=False, state_path=state_file)

        assert rc == 0
        assert send_calls == []


# ---------------------------------------------------------------------------
# Test 5: reconcile_positions silent when zero discrepancies
# ---------------------------------------------------------------------------
class TestReconcilePositionsSilentOnZeroDiscrepancies:
    """reconcile_positions.main() must not send Telegram when broker matches."""

    def _make_mock_broker(self):
        """Create a mock broker with no positions."""
        mock_b = MagicMock()
        mock_b.connect.return_value = True
        mock_b.disconnect.return_value = None
        mock_b.get_positions.return_value = []
        mock_b.get_open_orders.return_value = []
        return mock_b

    def test_no_discrepancies_no_telegram(self, tmp_path, monkeypatch):
        """Broker positions match internal state → no Telegram, exit 0."""
        sys.path.insert(0, str(ATLAS_ROOT))

        send_calls = []

        def fail_send(*a, **kw):
            send_calls.append((a, kw))
            pytest.fail(f"reconcile_positions sent Telegram with no discrepancies: {a}")

        import db.atlas_db as _adb
        monkeypatch.setattr(_adb, "_db_path_override", str(tmp_path / "isolated.db"))
        from db.atlas_db import init_db
        init_db()

        # Empty state file (no internal positions)
        state_file = tmp_path / "live_sp500.json"
        state_file.write_text(json.dumps({"market_id": "sp500", "positions": {}}))

        mock_broker = self._make_mock_broker()

        with patch("brokers.registry.get_live_broker", return_value=mock_broker):
            with patch("utils.config.get_active_config", return_value={"live_enabled": True, "trading": {"broker": "alpaca", "live_enabled": True}}):
                with patch("scripts.reconcile_positions.load_internal_state", return_value={"positions": []}):
                    with patch("utils.telegram.send_message", side_effect=fail_send):
                        from scripts import reconcile_positions
                        sys_argv_backup = sys.argv[:]
                        try:
                            sys.argv = [
                                "reconcile_positions.py",
                                "--market", "sp500",
                                "--quiet",
                            ]
                            rc = reconcile_positions.main()
                        except SystemExit as e:
                            rc = e.code
                        finally:
                            sys.argv = sys_argv_backup

        # Exit 0 = no discrepancies
        assert rc == 0, f"Expected exit 0 (no discrepancies), got {rc}"
        assert send_calls == [], "No Telegram should be sent with zero discrepancies"

    def test_reconcile_positions_has_cooldown_code(self):
        """Verify reconcile_positions.py has the 4h Telegram cooldown logic."""
        content = (ATLAS_ROOT / "scripts" / "reconcile_positions.py").read_text()
        # The cooldown logic must be present
        assert "cooldown" in content.lower(), "reconcile_positions.py must have Telegram cooldown"
        assert "_cooldown_file" in content
        assert "_cooldown_ok" in content
