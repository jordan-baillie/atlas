"""Regression tests for the 2026-05-04 consolidation mode change.

sector_etfs and commodity_etfs are flipped to mode=passive with
live_enabled=True.  This is an intentional combination:
  - mode=passive  →  execute_approved.py sees mode != "live" and skips
                     (no new entries placed at US open)
  - live_enabled=True →  sync_protective_orders, intraday_monitor, and
                          eod_settlement all continue running so the
                          remaining open positions (GLD, XLE, XLI) keep
                          their OCO brackets maintained until the Phase-2
                          closure script executes them.

DO NOT "fix" this by setting live_enabled=False — that would orphan the
protective stops on the remaining positions.
"""

from __future__ import annotations

import json
import sys
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

# ── paths ────────────────────────────────────────────────────────────────────
PROJECT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT / "config" / "active"

_REQUIRED_CONSOLIDATION_KEYS = {
    "paused_at",
    "decision",
    "open_positions_at_pause",
    "closure_plan",
    "reenable_criteria",
    "reversibility",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(market: str) -> dict:
    return json.loads((CONFIG_DIR / f"{market}.json").read_text())


# ---------------------------------------------------------------------------
# 1. Mode + live_enabled combination for paused markets
# ---------------------------------------------------------------------------

class TestPausedMarketsConfig:
    """commodity_etfs and sector_etfs must be passive + live_enabled=True."""

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_trading_mode_is_passive(self, market: str) -> None:
        cfg = _load(market)
        assert cfg["trading"]["mode"] == "passive", (
            f"{market}: expected mode=passive, got {cfg['trading']['mode']!r}"
        )

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_live_enabled_is_true(self, market: str) -> None:
        # INTENTIONAL: live_enabled=True keeps sync_protective_orders,
        # intraday_monitor, and eod_settlement running for open positions.
        cfg = _load(market)
        assert cfg["trading"]["live_enabled"] is True, (
            f"{market}: live_enabled must stay True so protective OCO brackets "
            "keep running — do not 'fix' this"
        )

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_auto_approve_is_false(self, market: str) -> None:
        cfg = _load(market)
        assert cfg["trading"]["auto_approve"] is False, (
            f"{market}: auto_approve must be False (belt-and-suspenders)"
        )


# ---------------------------------------------------------------------------
# 2. _consolidation_note structure
# ---------------------------------------------------------------------------

class TestConsolidationNote:
    """Both paused markets must have a _consolidation_note with all keys."""

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_has_consolidation_note(self, market: str) -> None:
        cfg = _load(market)
        assert "_consolidation_note" in cfg, (
            f"{market}: missing top-level _consolidation_note"
        )

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_consolidation_note_required_keys(self, market: str) -> None:
        cfg = _load(market)
        note = cfg["_consolidation_note"]
        missing = _REQUIRED_CONSOLIDATION_KEYS - set(note.keys())
        assert not missing, (
            f"{market}: _consolidation_note missing keys: {missing}"
        )

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_reenable_criteria_is_list(self, market: str) -> None:
        note = _load(market)["_consolidation_note"]
        assert isinstance(note["reenable_criteria"], list)
        assert len(note["reenable_criteria"]) >= 1

    @pytest.mark.parametrize("market", ["commodity_etfs", "sector_etfs"])
    def test_open_positions_at_pause_is_non_empty_list(self, market: str) -> None:
        note = _load(market)["_consolidation_note"]
        assert isinstance(note["open_positions_at_pause"], list)
        assert len(note["open_positions_at_pause"]) >= 1

    def test_commodity_etfs_gld_position_recorded(self) -> None:
        note = _load("commodity_etfs")["_consolidation_note"]
        positions_str = " ".join(note["open_positions_at_pause"])
        assert "GLD" in positions_str

    def test_sector_etfs_xle_xli_positions_recorded(self) -> None:
        note = _load("sector_etfs")["_consolidation_note"]
        positions_str = " ".join(note["open_positions_at_pause"])
        assert "XLE" in positions_str
        assert "XLI" in positions_str


# ---------------------------------------------------------------------------
# 3. sp500 remains live and untouched
# ---------------------------------------------------------------------------

class TestSp500Unchanged:
    """sp500 must stay live and must NOT have a _consolidation_note."""

    def test_sp500_mode_is_live(self) -> None:
        cfg = _load("sp500")
        assert cfg["trading"]["mode"] == "live"

    def test_sp500_live_enabled_is_true(self) -> None:
        cfg = _load("sp500")
        assert cfg["trading"]["live_enabled"] is True

    def test_sp500_no_consolidation_note(self) -> None:
        cfg = _load("sp500")
        assert "_consolidation_note" not in cfg, (
            "sp500 should NOT have a _consolidation_note — it was not paused"
        )


# ---------------------------------------------------------------------------
# 4. execute_approved.py skips when mode != "live"
# ---------------------------------------------------------------------------

class TestExecuteApprovedPassiveGate:
    """execute_approved.main() must return early when mode=passive.

    get_active_config is imported locally inside main() via
    ``from utils.config import get_active_config``, so the correct
    patch target is ``utils.config.get_active_config`` — not a module
    attribute on execute_approved itself.
    """

    _PASSIVE_CONFIG = {
        "trading": {
            "mode": "passive",
            "live_enabled": True,
            "auto_approve": False,
        }
    }

    def test_passive_mode_causes_early_return(self, caplog: pytest.LogCaptureFixture) -> None:
        """main() logs 'not live — skipping' and returns without touching broker."""
        if str(PROJECT) not in sys.path:
            sys.path.insert(0, str(PROJECT))

        import scripts.execute_approved as ea
        import utils.config as _uc

        with patch.object(_uc, "get_active_config", return_value=self._PASSIVE_CONFIG), \
             patch("sys.argv", ["execute_approved.py", "--market", "commodity_etfs", "--dry-run"]):
            with caplog.at_level(logging.INFO, logger="execute_approved"):
                try:
                    ea.main()
                except SystemExit:
                    pass  # argparse exit is fine

        skip_logged = any(
            ("not 'live'" in r.message or "skipping" in r.message)
            for r in caplog.records
        )
        assert skip_logged, (
            "Expected execute_approved to log a skip message for mode=passive. "
            f"Captured records: {[r.message for r in caplog.records]}"
        )

    def test_passive_mode_does_not_reach_broker(self) -> None:
        """execute_approved must not instantiate a broker for mode=passive."""
        if str(PROJECT) not in sys.path:
            sys.path.insert(0, str(PROJECT))

        import scripts.execute_approved as ea
        import utils.config as _uc

        broker_calls: list[str] = []

        def _fake_get_live_broker(*args, **kwargs):  # noqa: ANN001
            broker_calls.append("get_live_broker called")
            raise AssertionError("Broker should never be reached for mode=passive")

        with patch.object(_uc, "get_active_config", return_value=self._PASSIVE_CONFIG), \
             patch("brokers.registry.get_live_broker", side_effect=_fake_get_live_broker), \
             patch("sys.argv", ["execute_approved.py", "--market", "sector_etfs", "--dry-run"]):
            try:
                ea.main()
            except SystemExit:
                pass

        assert broker_calls == [], (
            "Broker was reached despite mode=passive — the early-return gate is broken"
        )
