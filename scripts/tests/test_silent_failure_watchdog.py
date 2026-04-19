"""Tests for scripts/silent_failure_watchdog.py — check_autoresearch_logs().

Covers:
  1. Genuine failure  — zero-byte today-dated log, no rotated sibling → alert fires.
  2. Rotation stub (sibling with content) — zero-byte yesterday-dated log + non-empty
     sibling → no alert.
  3. Past-date stub, no sibling — zero-byte old-dated log, no sibling → no alert
     (past-date guard fires).
  4. Old mtime  — zero-byte file whose mtime is >24 h old → no alert (existing
     cutoff behaviour preserved).
  5. Mixed batch — 2 genuine failures + 3 stubs → alert lists only the 2 failures.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── resolve the watchdog module path ──────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
_ATLAS_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_ATLAS_ROOT))

# Import the module under test (lazy, so we can reload after monkey-patching)
import scripts.silent_failure_watchdog as wdog  # noqa: E402


# ─── helpers ──────────────────────────────────────────────────────────────────

def _make_log(
    logs_dir: Path,
    strategy: str,
    date_str: str,  # YYYYMMDD
    size: int = 0,
    mtime_offset: float = -3600,  # seconds from now (negative = in the past)
) -> Path:
    """Create a fake autoresearch log file inside *logs_dir*."""
    p = logs_dir / f"autoresearch_{strategy}_{date_str}.log"
    p.write_bytes(b"x" * size)
    new_mtime = time.time() + mtime_offset
    os.utime(p, (new_mtime, new_mtime))
    return p


def _make_sibling(log_file: Path, rotate_date: str, size: int = 1024) -> Path:
    """Create a rotated sibling like autoresearch_<strat>_<d>.log-YYYYMMDD."""
    sib = log_file.parent / (log_file.name + f"-{rotate_date}")
    sib.write_bytes(b"y" * size)
    return sib


def _today() -> str:
    return date.today().strftime("%Y%m%d")


def _yesterday() -> str:
    return (date.today() - timedelta(days=1)).strftime("%Y%m%d")


def _old_date() -> str:
    return (date.today() - timedelta(days=30)).strftime("%Y%m%d")


# ─── Test 1 — genuine failure ─────────────────────────────────────────────────

def test_genuine_failure_fires_alert(tmp_path: Path) -> None:
    """Zero-byte today-dated log with no sibling → alert is sent."""
    _make_log(tmp_path, "trend_following", _today(), size=0, mtime_offset=-3600)

    mock_alert = MagicMock()
    with (
        patch.object(wdog, "LOGS_DIR", tmp_path),
        patch.object(wdog, "_alert", mock_alert),
    ):
        wdog.check_autoresearch_logs(dry_run=False)

    mock_alert.assert_called_once()
    call_text: str = mock_alert.call_args[0][0]
    assert "autoresearch" in call_text.lower()
    assert "autoresearch parameter-sweep runner produced no output" in call_text
    assert "logrotate stubs filtered" in call_text


# ─── Test 2 — rotation stub with non-empty sibling → no alert ────────────────

def test_rotation_stub_with_sibling_no_alert(tmp_path: Path) -> None:
    """Zero-byte yesterday-dated log + non-empty rotated sibling → no alert."""
    log = _make_log(tmp_path, "mean_reversion", _yesterday(), size=0, mtime_offset=-3600)
    _make_sibling(log, _today(), size=2394)  # sibling written by logrotate post-rename

    mock_alert = MagicMock()
    with (
        patch.object(wdog, "LOGS_DIR", tmp_path),
        patch.object(wdog, "_alert", mock_alert),
    ):
        wdog.check_autoresearch_logs(dry_run=False)

    mock_alert.assert_not_called()


# ─── Test 3 — past-date stub, no sibling → no alert (date guard) ─────────────

def test_past_date_stub_no_sibling_no_alert(tmp_path: Path) -> None:
    """Zero-byte old-dated log (30 days ago), no sibling, recent mtime → no alert.

    The past-date guard in _is_rotation_stub() should catch this even though
    the file was recently touched (e.g. logrotate touched but didn't rename).
    """
    _make_log(tmp_path, "opening_gap", _old_date(), size=0, mtime_offset=-3600)

    mock_alert = MagicMock()
    with (
        patch.object(wdog, "LOGS_DIR", tmp_path),
        patch.object(wdog, "_alert", mock_alert),
    ):
        wdog.check_autoresearch_logs(dry_run=False)

    mock_alert.assert_not_called()


# ─── Test 4 — old mtime → no alert (existing cutoff behaviour) ───────────────

def test_old_mtime_no_alert(tmp_path: Path) -> None:
    """Zero-byte today-dated log whose mtime is >24 h old → no alert."""
    _make_log(
        tmp_path,
        "momentum_breakout",
        _today(),
        size=0,
        mtime_offset=-(86400 + 3600),  # 25 h ago
    )

    mock_alert = MagicMock()
    with (
        patch.object(wdog, "LOGS_DIR", tmp_path),
        patch.object(wdog, "_alert", mock_alert),
    ):
        wdog.check_autoresearch_logs(dry_run=False)

    mock_alert.assert_not_called()


# ─── Test 5 — mixed batch ─────────────────────────────────────────────────────

def test_mixed_batch_only_real_failures_alerted(tmp_path: Path) -> None:
    """2 genuine failures + 3 stubs → alert lists exactly 2 failures."""
    today = _today()
    yest = _yesterday()

    # ── 2 genuine failures ──
    _make_log(tmp_path, "trend_following", today, size=0, mtime_offset=-3600)
    _make_log(tmp_path, "opening_gap", today, size=0, mtime_offset=-7200)

    # ── Stub 1: sibling with content ──
    stub1 = _make_log(tmp_path, "mean_reversion", yest, size=0, mtime_offset=-1800)
    _make_sibling(stub1, today, size=2394)

    # ── Stub 2: past-date, no sibling ──
    _make_log(tmp_path, "sector_rotation", _old_date(), size=0, mtime_offset=-600)

    # ── Stub 3: yesterday-dated + sibling ──
    stub3 = _make_log(tmp_path, "momentum_breakout", yest, size=0, mtime_offset=-900)
    _make_sibling(stub3, today, size=942)

    mock_alert = MagicMock()
    with (
        patch.object(wdog, "LOGS_DIR", tmp_path),
        patch.object(wdog, "_alert", mock_alert),
    ):
        wdog.check_autoresearch_logs(dry_run=False)

    mock_alert.assert_called_once()
    call_text: str = mock_alert.call_args[0][0]

    # Both real failures mentioned
    assert f"autoresearch_trend_following_{today}.log" in call_text
    assert f"autoresearch_opening_gap_{today}.log" in call_text

    # Count marker
    assert "2 zero-byte" in call_text

    # Stubs not mentioned
    assert "mean_reversion" not in call_text
    assert "sector_rotation" not in call_text
    assert "momentum_breakout" not in call_text


# ─── _is_rotation_stub unit tests ─────────────────────────────────────────────

def test_is_rotation_stub_sibling_with_content(tmp_path: Path) -> None:
    """_is_rotation_stub returns True when a non-empty sibling exists."""
    log = tmp_path / f"autoresearch_trend_following_{_yesterday()}.log"
    log.write_bytes(b"")
    sib = tmp_path / (log.name + f"-{_today()}")
    sib.write_bytes(b"x" * 100)

    assert wdog._is_rotation_stub(log) is True


def test_is_rotation_stub_empty_sibling_only(tmp_path: Path) -> None:
    """_is_rotation_stub checks sibling SIZE — empty sibling doesn't count."""
    log = tmp_path / f"autoresearch_trend_following_{_today()}.log"
    log.write_bytes(b"")
    sib = tmp_path / (log.name + f"-{_today()}")
    sib.write_bytes(b"")  # sibling also empty

    # sibling check fails; but TODAY filename matches today → not past-date
    assert wdog._is_rotation_stub(log) is False


def test_is_rotation_stub_past_date_no_sibling(tmp_path: Path) -> None:
    """_is_rotation_stub returns True for past-dated file with no sibling."""
    log = tmp_path / f"autoresearch_mean_reversion_{_old_date()}.log"
    log.write_bytes(b"")

    assert wdog._is_rotation_stub(log) is True


def test_is_rotation_stub_today_no_sibling(tmp_path: Path) -> None:
    """_is_rotation_stub returns False for today-dated file with no sibling."""
    log = tmp_path / f"autoresearch_mean_reversion_{_today()}.log"
    log.write_bytes(b"")

    assert wdog._is_rotation_stub(log) is False
