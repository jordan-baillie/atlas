"""Tests for equity_history dedup-on-load in LivePortfolio._load_local_state().

Verifies that:
1. Duplicate-date rows in a legacy state file are collapsed to one (last-wins).
2. Unique-date rows are all preserved.
3. An empty equity_history list causes no error.

These tests exercise the real _load_local_state() path (no mock).
The autouse _isolate_live_portfolio_state fixture (conftest.py) redirects
brokers.live_portfolio._STATE_DIR to tmp_path/lp_state for each test,
so the production state files are never touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from brokers.live_portfolio import LivePortfolio


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_config(market_id: str = "commodity_etfs", starting_equity: float = 1000.0) -> dict:
    return {
        "market_id": market_id,
        "risk": {
            "starting_equity": starting_equity,
            "max_risk_per_trade_pct": 0.005,
            "max_open_positions": 10,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": 0.02,
            "leverage": 1.0,
        },
        "fees": {},
    }


def _write_state(state_dir: Path, market_id: str, equity_history: list[dict]) -> None:
    """Write a minimal live_{market_id}.json state file into state_dir."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"live_{market_id}.json"
    state_file.write_text(
        json.dumps({
            "closed_trades": [],
            "closed_trades_quarantine": [],
            "equity_history": equity_history,
            "daily_high_water": 1000.0,
            "daily_high_water_date": None,
            "halted": False,
            "halt_reason": "",
            "positions": [],
        }),
        encoding="utf-8",
    )


def _make_eq_row(date: str, equity: float) -> dict:
    """Return a minimal equity_history row dict."""
    return {
        "date": date,
        "equity": equity,
        "num_positions": 0,
        "total_realized_pnl": 0.0,
        "total_closed_trades": 0,
        "positions": [],
    }


# ═════════════════════════════════════════════════════════════════════════════
# Test class
# ═════════════════════════════════════════════════════════════════════════════

class TestLoadLocalStateEquityHistoryDedup:
    """_load_local_state() deduplicates equity_history rows by date."""

    def test_load_local_state_dedups_equity_history_by_date(
        self, tmp_path: Path
    ) -> None:
        """3 rows for 2026-05-05 → collapsed to 1, last value wins ($956.82)."""
        market_id = "commodity_etfs"
        state_dir = tmp_path / "lp_state"  # matches autouse fixture path

        raw_rows = [
            _make_eq_row("2026-05-05", 944.82),
            _make_eq_row("2026-05-05", 944.82),
            _make_eq_row("2026-05-05", 956.82),  # last — must survive
        ]
        _write_state(state_dir, market_id, raw_rows)

        # Instantiate without patching _load_local_state — exercises the real path.
        lp = LivePortfolio(_make_config(market_id), market_id=market_id)

        assert len(lp.equity_history) == 1, (
            f"expected 1 row after dedup, got {len(lp.equity_history)}"
        )
        assert lp.equity_history[0]["equity"] == 956.82, (
            f"expected last-wins equity=956.82, got {lp.equity_history[0]['equity']}"
        )
        assert lp.equity_history[0]["date"] == "2026-05-05"

    def test_load_local_state_preserves_unique_dates(
        self, tmp_path: Path
    ) -> None:
        """5 rows with distinct dates → all 5 preserved, no data loss."""
        market_id = "sector_etfs"
        state_dir = tmp_path / "lp_state"

        dates = [
            "2026-05-01",
            "2026-05-02",
            "2026-05-03",
            "2026-05-04",
            "2026-05-05",
        ]
        raw_rows = [_make_eq_row(d, 1000.0 + i * 10) for i, d in enumerate(dates)]
        _write_state(state_dir, market_id, raw_rows)

        lp = LivePortfolio(_make_config(market_id), market_id=market_id)

        assert len(lp.equity_history) == 5, (
            f"expected 5 rows, got {len(lp.equity_history)}: {lp.equity_history}"
        )
        loaded_dates = [r["date"] for r in lp.equity_history]
        assert sorted(loaded_dates) == sorted(dates), (
            f"date mismatch: {loaded_dates}"
        )

    def test_load_local_state_no_history_no_error(
        self, tmp_path: Path
    ) -> None:
        """equity_history: [] in state file → empty list, no exception raised."""
        market_id = "sp500"
        state_dir = tmp_path / "lp_state"

        _write_state(state_dir, market_id, [])

        lp = LivePortfolio(_make_config(market_id), market_id=market_id)

        assert lp.equity_history == [], (
            f"expected empty list, got {lp.equity_history}"
        )
