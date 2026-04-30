"""Regression test: atlas-reconcile-shadow systemd timer installation.

Verifies:
1. Unit files exist in the repo under systemd/
2. OnCalendar matches the B.2 spec (every 30min, UTC 00-07, Tue-Sat)
3. /etc/systemd/system/ symlinks point back to the repo source
4. systemctl reports the timer as enabled
"""
import os
import subprocess
import configparser
import pytest

REPO_SYSTEMD = "/root/atlas/systemd"
ETC_SYSTEMD  = "/etc/systemd/system"
TIMER_NAME   = "atlas-reconcile-shadow.timer"
SERVICE_NAME = "atlas-reconcile-shadow.service"

TIMER_SRC    = os.path.join(REPO_SYSTEMD, TIMER_NAME)
SERVICE_SRC  = os.path.join(REPO_SYSTEMD, SERVICE_NAME)
TIMER_LINK   = os.path.join(ETC_SYSTEMD,  TIMER_NAME)
SERVICE_LINK = os.path.join(ETC_SYSTEMD,  SERVICE_NAME)

EXPECTED_ON_CALENDAR = "Tue..Sat *-*-* 00,01,02,03,04,05,06,07:00,30 UTC"


# ── 1. Unit files exist and are parseable ────────────────────────────────────

def test_timer_file_exists():
    assert os.path.isfile(TIMER_SRC), f"Missing: {TIMER_SRC}"


def test_service_file_exists():
    assert os.path.isfile(SERVICE_SRC), f"Missing: {SERVICE_SRC}"


def test_timer_parseable():
    """configparser can read both sections of the timer file."""
    cfg = configparser.RawConfigParser(strict=False)
    # systemd uses [Unit] / [Timer] / [Install] headers — configparser handles these fine
    cfg.read(TIMER_SRC)
    assert cfg.has_section("Timer"), "timer file missing [Timer] section"
    assert cfg.has_section("Install"), "timer file missing [Install] section"


def test_service_parseable():
    cfg = configparser.RawConfigParser(strict=False)
    cfg.read(SERVICE_SRC)
    assert cfg.has_section("Service"), "service file missing [Service] section"


# ── 2. OnCalendar matches spec ───────────────────────────────────────────────

def test_on_calendar_value():
    """The OnCalendar pattern must exactly match the B.2 spec."""
    cfg = configparser.RawConfigParser(strict=False)
    cfg.read(TIMER_SRC)
    on_calendar = cfg.get("Timer", "OnCalendar")
    assert on_calendar == EXPECTED_ON_CALENDAR, (
        f"OnCalendar mismatch.\n  got:      {on_calendar!r}\n"
        f"  expected: {EXPECTED_ON_CALENDAR!r}"
    )


# ── 3. /etc/systemd/system symlinks are correct ──────────────────────────────

def test_timer_symlink_exists():
    assert os.path.islink(TIMER_LINK), (
        f"{TIMER_LINK} is not a symlink (not installed)"
    )


def test_timer_symlink_target():
    target = os.readlink(TIMER_LINK)
    assert target == TIMER_SRC, (
        f"Symlink target wrong.\n  got:      {target!r}\n"
        f"  expected: {TIMER_SRC!r}"
    )


def test_service_symlink_exists():
    assert os.path.islink(SERVICE_LINK), (
        f"{SERVICE_LINK} is not a symlink (not installed)"
    )


def test_service_symlink_target():
    target = os.readlink(SERVICE_LINK)
    assert target == SERVICE_SRC, (
        f"Symlink target wrong.\n  got:      {target!r}\n"
        f"  expected: {SERVICE_SRC!r}"
    )


# ── 4. systemctl is-enabled reports "enabled" ────────────────────────────────

def test_timer_is_enabled():
    result = subprocess.run(
        ["systemctl", "is-enabled", TIMER_NAME],
        capture_output=True, text=True, timeout=10
    )
    state = result.stdout.strip()
    assert state == "enabled", (
        f"Timer not enabled. systemctl is-enabled returned: {state!r}\n"
        f"stderr: {result.stderr.strip()}"
    )
