"""Regression tests for the phantom-HWM bug class (2026-05-06 sector_etfs incident).

Root cause: check_daily_drawdown wrote GLOBAL broker equity (~$5189) to
per-market HWM during session reset when _get_per_market_equity returned None
transiently.  Subsequent calls returned correct ~$2605 → 49.78% phantom HALT.

Fix A: session reset anchors HWM to _latest_snapshot_allocated_equity() (most
recent market_equity_history.allocated_equity row), falling back to
starting_equity.  NEVER falls back to effective_eq (which may be global broker
equity).

Fix B: stale-HWM guard in _load_local_state tightened from 5× → 1.5×
starting_equity.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from brokers.live_portfolio import LivePortfolio


# -- Config / portfolio factories ---------------------------------------------

def _make_config(
    market_id: str = "sp500",
    starting_equity: float = 1000.0,
    max_daily_dd: float = 0.10,
) -> dict:
    return {
        "market_id": market_id,
        "risk": {
            "starting_equity": starting_equity,
            "max_risk_per_trade_pct": 0.005,
            "max_open_positions": 10,
            "max_sector_concentration": 2,
            "max_daily_drawdown_pct": max_daily_dd,
            "leverage": 1.0,
        },
        "fees": {},
        "dual_write_market_state": False,
    }


def _make_portfolio(
    market_id: str,
    starting_equity: float = 1000.0,
    max_daily_dd: float = 0.10,
) -> LivePortfolio:
    """Create a LivePortfolio with no broker or state-file I/O."""
    cfg = _make_config(market_id, starting_equity, max_daily_dd)
    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio(cfg, market_id=market_id)
    lp._broker_equity = 0.0
    lp.broker_data_valid = True
    return lp


# -- DB seeding helper --------------------------------------------------------

def _seed_market_equity_history(db_path: Path, rows: list) -> None:
    """Seed market_equity_history rows into a temporary SQLite DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_equity_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT NOT NULL,
            market_id       TEXT NOT NULL,
            allocated_equity REAL,
            position_mv     REAL,
            cash_attributed REAL,
            broker_equity   REAL,
            broker_cash     REAL,
            snapshot_time   TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
        """
    )
    today = date.today().isoformat()
    for r in rows:
        conn.execute(
            """
            INSERT INTO market_equity_history
              (date, market_id, allocated_equity, position_mv, cash_attributed,
               broker_equity, broker_cash, snapshot_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r.get("date", today),
                r["market_id"],
                r["allocated_equity"],
                r.get("position_mv", 0.0),
                r.get("cash_attributed", 0.0),
                r.get("broker_equity", 0.0),
                r.get("broker_cash", 0.0),
                r.get("snapshot_time", datetime.now().isoformat()),
                r.get("created_at", datetime.now().isoformat()),
            ),
        )
    conn.commit()
    conn.close()


# =============================================================================
# Test 1: primary regression for the 2026-05-06 sector_etfs phantom HALT
# =============================================================================

class TestSessionResetUsesSnapshotAnchor:

    def test_session_reset_uses_snapshot_anchor_not_broker_eq(
        self, tmp_path, monkeypatch
    ):
        """Regression: session reset must anchor HWM to snapshot, not broker equity.

        Exact bug reproduction:
        - market_equity_history has allocated_equity=$2637.57 for sector_etfs
        - _get_per_market_equity returns None (transient failure - bug condition)
        - effective_eq falls back to broker_eq=$5189 (GLOBAL equity)
        - OLD code: daily_high_water = effective_eq = $5189  <-- BUG
        - NEW code: daily_high_water = snap_anchor = $2637.57 <-- FIXED
        - Subsequent correct per-market call returns ~$2605 -> 49.78% phantom HALT prevented
        """
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": date.today().isoformat(),
                "market_id": "sector_etfs",
                "allocated_equity": 2637.57,
                "broker_equity": 5189.0,
                "snapshot_time": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sector_etfs", starting_equity=3216.0)
        # Force session reset (new day condition)
        lp.daily_high_water_date = None
        # The bug condition: per-market returns None -> effective_eq falls back to broker_eq
        lp._broker_equity = 5189.0

        with patch.object(LivePortfolio, "_get_per_market_equity", return_value=None):
            halted, dd = lp.check_daily_drawdown()

        # HWM must be anchored to snapshot ($2637.57), NOT global broker equity ($5189)
        assert lp.daily_high_water == pytest.approx(2637.57, rel=1e-4), (
            "HWM should be snapshot anchor $2637.57, got ${:.2f}. "
            "If HWM is ~$5189, the phantom-HWM bug is not fixed.".format(lp.daily_high_water)
        )
        assert lp.daily_high_water_date == date.today().isoformat()
        # effective_eq (5189) > HWM (2637.57) -> dd is negative -> no halt
        assert halted is False, "Should NOT halt when HWM is anchored to snapshot"
        assert dd <= 0.0, (
            "dd should be <= 0 when broker_eq (5189) > HWM (2637.57), got {:.4f}".format(dd)
        )


# =============================================================================
# Test 2: brand-new market with no snapshot falls back to starting_equity
# =============================================================================

class TestSessionResetNoSnapshotFallsBackToStartingEquity:

    def test_session_reset_no_snapshot_falls_back_to_starting_equity(
        self, tmp_path, monkeypatch
    ):
        """Brand-new market (no snapshot) -> HWM falls back to starting_equity.

        Even with a ridiculously large broker_eq, the session reset must NOT
        write it to the per-market HWM.  starting_equity is the safe anchor.
        """
        # Empty DB - no market_equity_history rows
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("new_market", starting_equity=1000.0)
        lp.daily_high_water_date = None  # force session reset
        lp._broker_equity = 99999.0  # absurdly large global equity

        with patch.object(LivePortfolio, "_get_per_market_equity", return_value=None):
            halted, dd = lp.check_daily_drawdown()

        # Must anchor to starting_equity, never to broker_eq=$99999
        assert lp.daily_high_water == pytest.approx(1000.0), (
            "HWM should be starting_equity=$1000.0, got ${:.2f}. "
            "If HWM is ~$99999, the phantom-HWM bug is not fixed.".format(lp.daily_high_water)
        )
        assert halted is False


# =============================================================================
# Tests 3 & 4: Fix B: stale-HWM guard threshold (5x -> 1.5x)
# =============================================================================

class TestStaleHwmGuard:

    def test_stale_hwm_guard_fires_at_1_6x(self, tmp_path):
        """HWM at 1.6x starting_equity must be auto-corrected on _load_local_state.

        Fix B tightens the guard from 5x to 1.5x.  Any HWM > 1.5x starting_equity
        is treated as phantom (likely written from global broker equity).
        """
        starting_equity = 1000.0
        stale_hwm = starting_equity * 1.6  # 1600.0 - above 1.5x threshold

        # The autouse _isolate_live_portfolio_state fixture already redirected
        # _STATE_DIR to tmp_path / "lp_state".  Create the state file there.
        state_dir = tmp_path / "lp_state"
        state_dir.mkdir(parents=True, exist_ok=True)

        market_id = "stale_test_1"
        state_file = state_dir / ("live_" + market_id + ".json")
        state_file.write_text(json.dumps({
            "daily_high_water": stale_hwm,
            "daily_high_water_date": "2026-01-01",
            "closed_trades": [],
            "equity_history": [],
            "halted": False,
            "halt_reason": "",
        }))

        cfg = _make_config(market_id, starting_equity)
        # Do NOT patch _load_local_state - we need it to run and trigger the guard
        lp = LivePortfolio(cfg, market_id=market_id)

        # Guard must have reset HWM to starting_equity
        assert lp.daily_high_water == pytest.approx(starting_equity), (
            "HWM at 1.6x should be auto-corrected to starting_equity={}, "
            "got {}.".format(starting_equity, lp.daily_high_water)
        )
        # Guard also resets date to None to force a session-reset on first drawdown check
        assert lp.daily_high_water_date is None, (
            "daily_high_water_date should be None after stale-HWM guard fires"
        )

    def test_stale_hwm_guard_does_not_fire_at_1_4x(self, tmp_path):
        """HWM at 1.4x starting_equity must NOT be auto-corrected.

        1.4x is within the legitimate profit range - the guard threshold is 1.5x,
        so a 40% gain from starting_equity is preserved.
        """
        starting_equity = 1000.0
        legitimate_hwm = starting_equity * 1.4  # 1400.0 - below 1.5x threshold

        state_dir = tmp_path / "lp_state"
        state_dir.mkdir(parents=True, exist_ok=True)

        market_id = "stale_test_2"
        state_file = state_dir / ("live_" + market_id + ".json")
        today_str = date.today().isoformat()
        state_file.write_text(json.dumps({
            "daily_high_water": legitimate_hwm,
            "daily_high_water_date": today_str,
            "closed_trades": [],
            "equity_history": [],
            "halted": False,
            "halt_reason": "",
        }))

        cfg = _make_config(market_id, starting_equity)
        lp = LivePortfolio(cfg, market_id=market_id)

        # HWM must be preserved - it is below 1.5x and represents legitimate profit
        assert lp.daily_high_water == pytest.approx(legitimate_hwm), (
            "HWM at 1.4x should be preserved ({}), got {}.".format(
                legitimate_hwm, lp.daily_high_water
            )
        )
        # Date is preserved too (not reset)
        assert lp.daily_high_water_date == today_str


# =============================================================================
# Test 5: catastrophic 20% override still halts with the corrected HWM
# =============================================================================

class TestCatastrophicOverrideWithCorrectHwm:

    def test_catastrophic_20pct_override_still_halts_with_correct_hwm(
        self, tmp_path, monkeypatch
    ):
        """Fix A must not break the 20% catastrophic override.

        Scenario:
        1. HWM already anchored to $1000 from snapshot.
        2. Per-market equity drops to $700 (30% drawdown).
        3. 1h cooldown is bypassed (mocked _hwm_reset_at = 2h ago).
        4. dd=30% >= 20% catastrophic threshold -> HALT must fire.
        """
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": date.today().isoformat(),
                "market_id": "sp500",
                "allocated_equity": 1000.0,
                "broker_equity": 5000.0,
                "snapshot_time": (datetime.now() - timedelta(hours=2)).isoformat(),
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", starting_equity=1000.0, max_daily_dd=0.02)
        # Session already has today's date -- no reset triggered
        lp.daily_high_water = 1000.0
        lp.daily_high_water_date = date.today().isoformat()
        # Set _hwm_reset_at to 2h ago so cooldown has expired
        lp._hwm_reset_at = datetime.now() - timedelta(hours=2)
        lp._broker_equity = 5000.0

        # Per-market equity returns $700 -> 30% drawdown against HWM=$1000
        # Patch kill_switch._HALT_FILE so the file write goes to tmp, not production
        with patch.object(LivePortfolio, "_get_per_market_equity", return_value=700.0):
            with patch("brokers.kill_switch._HALT_FILE", tmp_path / "HALT"):
                halted, dd = lp.check_daily_drawdown()

        # 30% is >= 20% catastrophic override -> HALT fires even outside cooldown
        assert halted is True, (
            "30% drawdown must HALT (catastrophic override >=20%). dd={:.4f}".format(dd)
        )
        assert dd == pytest.approx(0.30, rel=1e-3), (
            "Expected dd=0.30, got {:.4f}".format(dd)
        )


# =============================================================================
# Test 6: sanity - snapshot anchor is always used, even when per-market healthy
# =============================================================================

class TestSessionResetAlwaysUsesSnapshotAnchor:

    def test_session_reset_with_normal_per_market_eq_path_works(
        self, tmp_path, monkeypatch
    ):
        """Even when per-market equity resolves normally, HWM anchors to snapshot.

        Confirms snap_anchor is the source of truth at session reset, regardless
        of what effective_eq (or _get_per_market_equity) returns.

        Scenario:
        - snapshot has allocated_equity=$2637.57
        - _get_per_market_equity returns $2800 (healthy, intraday growth)
        - effective_eq = $2800
        - Session reset -> HWM = $2637.57 (snapshot), NOT $2800 (effective_eq)
        """
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": date.today().isoformat(),
                "market_id": "sector_etfs",
                "allocated_equity": 2637.57,
                "broker_equity": 5100.0,
                "snapshot_time": (datetime.now() - timedelta(hours=1)).isoformat(),
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sector_etfs", starting_equity=3216.0)
        lp.daily_high_water_date = None  # force session reset
        lp._broker_equity = 5100.0

        # Per-market returns healthy $2800 - the reset should still use snapshot
        with patch.object(LivePortfolio, "_get_per_market_equity", return_value=2800.0):
            halted, dd = lp.check_daily_drawdown()

        # HWM must be anchored to snapshot ($2637.57), not effective_eq ($2800)
        assert lp.daily_high_water == pytest.approx(2637.57, rel=1e-4), (
            "HWM should be snapshot anchor $2637.57, got ${:.2f}. "
            "If HWM is ~$2800, session reset is using effective_eq instead of snapshot.".format(
                lp.daily_high_water
            )
        )
        assert halted is False
        # dd = (2637.57 - 2800) / 2637.57 = -0.0616 (portfolio up from HWM)
        assert dd <= 0.0, (
            "With effective_eq > HWM, dd should be <= 0, got {:.4f}".format(dd)
        )
