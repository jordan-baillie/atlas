"""
tests/test_alpaca_paper_consistency.py

A2 regression guard — ensures all market configs with an `alpaca` block
have `paper` consistent with ~/.atlas-secrets.json:ALPACA_PAPER, and that
the audit script and lessons file are in place.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ATLAS_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ATLAS_ROOT / "config" / "active"
SECRETS_FILE = Path.home() / ".atlas-secrets.json"


def _secrets_paper() -> bool:
    """Return the expected paper flag value from ~/.atlas-secrets.json.

    The secrets file stores ALPACA_PAPER as a JSON string ("false"/"true")
    or a JSON boolean; normalise both to a Python bool.
    """
    data = json.loads(SECRETS_FILE.read_text())
    val = data.get("ALPACA_PAPER", False)
    if isinstance(val, bool):
        return val
    # String form: "false" → False, "true" → True (case-insensitive)
    return str(val).lower() == "true"


def _market_configs_with_alpaca_block() -> list[tuple[str, dict]]:
    """Return list of (market_name, config_dict) for configs that have an alpaca block."""
    results = []
    for path in sorted(CONFIG_DIR.glob("*.json")):
        cfg = json.loads(path.read_text())
        if "alpaca" in cfg and "paper" in cfg["alpaca"]:
            results.append((path.stem, cfg))
    return results


# ---------------------------------------------------------------------------
# Test 1: no drift in any config that declares alpaca.paper
# ---------------------------------------------------------------------------


def test_no_drift_in_all_configs() -> None:
    """Every config/active/*.json with an alpaca.paper key must equal ALPACA_PAPER from secrets."""
    if not SECRETS_FILE.exists():
        pytest.skip(f"Secrets file not found at {SECRETS_FILE}")

    expected = _secrets_paper()
    configs = _market_configs_with_alpaca_block()

    assert configs, "Expected at least one market config with an alpaca block"

    drifted = []
    for market, cfg in configs:
        actual = cfg["alpaca"]["paper"]
        if actual != expected:
            drifted.append(f"{market}: paper={actual} (expected {expected})")

    assert not drifted, (
        f"alpaca.paper drift detected in {len(drifted)} config(s):\n"
        + "\n".join(f"  {d}" for d in drifted)
    )


# ---------------------------------------------------------------------------
# Test 2: lessons file exists and contains the key patterns
# ---------------------------------------------------------------------------


def test_lessons_file_exists_with_pattern() -> None:
    """tasks/atlas-lessons.md must exist and document the 40110000 error pattern."""
    lessons_path = ATLAS_ROOT / "tasks" / "atlas-lessons.md"
    assert lessons_path.exists(), (
        f"Lessons file not found at {lessons_path}; "
        "create tasks/atlas-lessons.md with alpaca.paper guidance"
    )
    content = lessons_path.read_text()
    assert "alpaca.paper" in content, (
        "tasks/atlas-lessons.md must contain 'alpaca.paper' section"
    )
    assert "40110000" in content, (
        "tasks/atlas-lessons.md must document the '40110000' Alpaca error code"
    )


# ---------------------------------------------------------------------------
# Test 3: audit script exits 0 on the current (clean) state
# ---------------------------------------------------------------------------


def test_audit_script_clean() -> None:
    """scripts/audit_alpaca_paper.sh must be executable and exit 0 (no drift)."""
    script = ATLAS_ROOT / "scripts" / "audit_alpaca_paper.sh"
    assert script.exists(), f"Audit script not found at {script}"
    assert script.stat().st_mode & 0o111, f"Audit script is not executable: {script}"

    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"audit_alpaca_paper.sh exited {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "no drift" in result.stdout.lower() or "consistent" in result.stdout.lower(), (
        f"Expected clean output from audit script, got:\n{result.stdout}"
    )
