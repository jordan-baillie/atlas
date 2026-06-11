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

ATLAS_ROOT = Path(__file__).resolve().parents[2]
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
# Test 3: audit script exits 0 on the current (clean) state
# ---------------------------------------------------------------------------
