"""Regression test: sector_etfs config must have live_enabled=True.

Context (P1-8):
    XLK and XLY are live positions at Alpaca with matching SQLite trade rows.
    EOD settlement skips markets where trading.live_enabled=False, which caused
    stale snapshots for these live positions.  The operator decision is implicit
    — positions exist and are being actively traded.

    This test prevents a silent regression where someone flips live_enabled
    back to False (which would cause EOD settlement to skip the market and
    leave live positions unmonitored).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ── Project root ──────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

CONFIG_PATH = PROJECT / "config" / "active" / "sector_etfs.json"


@pytest.fixture(scope="module")
def sector_etfs_config() -> dict:
    """Load sector_etfs config once for the module."""
    assert CONFIG_PATH.exists(), f"Config file not found: {CONFIG_PATH}"
    with open(CONFIG_PATH) as f:
        return json.load(f)


class TestSectorEtfsLiveEnabled:
    """Prevent accidental disabling of live trading for sector_etfs."""

    def test_live_enabled_is_true(self, sector_etfs_config: dict) -> None:
        """trading.live_enabled must be True.

        Flipping this to False causes EOD settlement to skip sector_etfs,
        leaving XLK/XLY live positions without stop-loss monitoring or
        daily equity snapshots.
        """
        trading = sector_etfs_config["trading"]
        assert trading["live_enabled"] is True, (
            "sector_etfs trading.live_enabled is False!  "
            "XLK and XLY are LIVE positions — EOD settlement will skip this "
            "market and leave stops unmonitored.  Set live_enabled=true in "
            "config/active/sector_etfs.json."
        )

    def test_trading_mode_is_expected(self, sector_etfs_config: dict) -> None:
        """trading.mode must match the current consolidation-phase config.

        History:
          2026-04-27 (audit-fix-5): mode set to 'live' — XLI/XLK/XLY confirmed active.
          2026-05-05 (consolidation): positions wound down; mode reverted to 'passive'.
            Version pinned as v1.0.3-consolidation-passive to document intent.

        The critical safety invariant is trading.live_enabled=True (tested separately),
        not the mode string.  Mode='passive' during consolidation is intentional — it
        prevents execute_approved.py from opening NEW positions while existing ones settle.

        To re-enable: bump version, set mode='live', update this assertion.
        """
        mode = sector_etfs_config["trading"]["mode"]
        # v1.0.3-consolidation-passive: intentional passive phase while positions close out.
        # Change this assertion when mode is promoted back to 'live'.
        assert mode == "passive", (
            f"sector_etfs trading.mode is {mode!r}!  "
            "Expected 'passive' (consolidation phase, v1.0.3-consolidation-passive). "
            "If re-enabling live trading: set mode='live', bump config version, "
            "and update this assertion with a rationale comment."
        )

    def test_config_file_parses_as_valid_json(self) -> None:
        """Config file must be valid JSON (guards against edit corruption)."""
        content = CONFIG_PATH.read_text()
        parsed = json.loads(content)
        assert isinstance(parsed, dict), "Config must be a JSON object"

    def test_market_is_sector_etfs(self, sector_etfs_config: dict) -> None:
        """Config market field must match the filename."""
        assert sector_etfs_config.get("market") == "sector_etfs", (
            "Config 'market' field does not match 'sector_etfs'"
        )
