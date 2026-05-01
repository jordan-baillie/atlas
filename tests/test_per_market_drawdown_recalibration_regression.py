"""Regression test: per-market drawdown must not false-positive after recalibration.

Reproduces the 2026-04-29 19:29 incident:
- sp500 starting_equity recalibrated $5,011 → $971
- HWM reset to $971 (today's first check_daily_drawdown call)
- effective_eq drops to $854 due to attribution math artefact (snapshot MV vs live
  prices), NOT a real loss — actual position (CAT) only moved 2%
- Without the cooldown guard this triggers a false 12% halt

Verifies:
1. Recalibration drift (12% on snapshot) is suppressed within 1h of HWM reset
2. After cooldown expires (65 min), the halt fires normally
3. Catastrophic loss (>20%) within cooldown window still fires immediately
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from brokers.live_portfolio import LivePortfolio


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config(
    market_id: str = "sp500",
    starting_equity: float = 971.0,
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
    }


def _make_portfolio(
    market_id: str = "sp500",
    starting_equity: float = 971.0,
    max_daily_dd: float = 0.10,
) -> LivePortfolio:
    cfg = _make_config(market_id, starting_equity, max_daily_dd)
    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio(cfg, market_id=market_id)
    lp._broker_equity = 0.0
    lp.broker_data_valid = True
    return lp


def _seed_db(db_path: Path, rows: list[dict]) -> None:
    """Seed a market_equity_history table with the given rows."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            market_id TEXT NOT NULL,
            allocated_equity REAL,
            position_mv REAL,
            cash_attributed REAL,
            broker_equity REAL,
            broker_cash REAL,
            snapshot_time TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    for r in rows:
        conn.execute(
            """
            INSERT INTO market_equity_history
              (date, market_id, allocated_equity, position_mv, cash_attributed,
               broker_equity, broker_cash, snapshot_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                r["date"],
                r["market_id"],
                r.get("allocated_equity", 971.01),
                r.get("position_mv", 817.87),   # CAT MV at snapshot
                r.get("cash_attributed", 153.14), # cash share
                r.get("broker_equity", 5213.4),
                r.get("broker_cash", 818.34),
                r.get("snapshot_time", "2026-04-29T00:19:48+00:00"),
                r.get("created_at", "2026-04-29 00:19:48"),
            ),
        )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestRecalibrationFalsePositive:
    """Regression for 2026-04-29 19:29 false-positive halt."""

    def test_recalibration_drift_does_not_halt(self, tmp_path, monkeypatch, caplog):
        """Scenario: HWM just reset to $971 (5 min ago), per-market eq drifts to $854.
        The 12% gap is attribution artefact (snapshot vs live prices), not a real loss.
        Must be suppressed within the 1h cooldown window.
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        lp.daily_high_water = 971.0
        lp.daily_high_water_date = "2026-05-01"  # today — no session HWM reset
        lp._broker_equity = 5184.0
        # Simulate HWM reset 5 minutes ago (within cooldown window)
        lp._hwm_reset_at = datetime.now() - timedelta(minutes=5)

        import logging
        with (
            patch.object(lp, "_get_per_market_equity", return_value=854.0),
            patch("brokers.kill_switch.halt") as mock_halt,
            caplog.at_level(logging.WARNING, logger="atlas.live_portfolio"),
        ):
            halted, dd = lp.check_daily_drawdown()

        assert halted is False, (
            f"False-positive: CAT only -2% but dd={dd:.2%} triggered halt during cooldown"
        )
        assert dd == pytest.approx(0.12, rel=1e-2), f"Expected ~12% dd, got {dd:.2%}"
        assert lp.halted is False, "lp.halted should remain False after suppressed halt"
        mock_halt.assert_not_called(), "kill_switch.halt must NOT be called on suppressed halt"
        # Check the cooldown suppression log message
        assert any(
            "HALT suppressed" in r.message and "1h of HWM reset" in r.message
            for r in caplog.records
        ), f"Expected 'HALT suppressed — within 1h of HWM reset' log; got: {[r.message for r in caplog.records]}"

    def test_legitimate_halt_after_cooldown_expires(self, tmp_path, monkeypatch):
        """65 minutes after HWM reset, cooldown expires. Same 12% drawdown now halts."""
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        lp.daily_high_water = 971.0
        lp.daily_high_water_date = "2026-05-01"  # use today — no session HWM reset
        lp._broker_equity = 5184.0
        # Simulate HWM reset 65 minutes ago (cooldown expired)
        lp._hwm_reset_at = datetime.now() - timedelta(minutes=65)

        with (
            patch.object(lp, "_get_per_market_equity", return_value=854.0),
            patch("brokers.kill_switch.halt") as mock_halt,
        ):
            halted, dd = lp.check_daily_drawdown()

        assert halted is True, (
            "After 65 min (cooldown expired), 12% drawdown should halt"
        )
        assert lp.halted is True
        mock_halt.assert_called_once()

    def test_no_hwm_reset_at_does_not_suppress(self, tmp_path, monkeypatch):
        """If _hwm_reset_at is None (no reset this session), cooldown is inactive
        and a real drawdown halts normally."""
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        lp.daily_high_water = 971.0
        lp.daily_high_water_date = "2026-05-01"  # use today — no session HWM reset
        lp._broker_equity = 5184.0
        lp._hwm_reset_at = None  # no reset this session

        with (
            patch.object(lp, "_get_per_market_equity", return_value=854.0),
            patch("brokers.kill_switch.halt") as mock_halt,
        ):
            halted, dd = lp.check_daily_drawdown()

        assert halted is True, "Without cooldown, 12% drawdown should halt"
        mock_halt.assert_called_once()

    def test_catastrophic_loss_within_cooldown_still_halts(self, tmp_path, monkeypatch):
        """Within the cooldown window, a >20% drawdown (catastrophic) still halts.
        The cooldown only protects against marginal attribution drift, not real disasters.
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        lp.daily_high_water = 971.0
        lp.daily_high_water_date = "2026-05-01"  # use today — no session HWM reset
        lp._broker_equity = 5184.0
        # Well within cooldown: only 10 minutes since reset
        lp._hwm_reset_at = datetime.now() - timedelta(minutes=10)

        # 25% drawdown: well past the 20% override threshold
        effective_eq_25pct_down = 971.0 * (1.0 - 0.25)  # $728.25

        with (
            patch.object(lp, "_get_per_market_equity", return_value=effective_eq_25pct_down),
            patch("brokers.kill_switch.halt") as mock_halt,
        ):
            halted, dd = lp.check_daily_drawdown()

        assert halted is True, (
            f"Catastrophic 25% drawdown should always halt even within cooldown (dd={dd:.2%})"
        )
        assert dd == pytest.approx(0.25, rel=1e-2)
        mock_halt.assert_called_once()

    def test_hwm_reset_at_set_after_new_day_reset(self, tmp_path, monkeypatch):
        """When check_daily_drawdown fires the HWM date-change reset,
        _hwm_reset_at must be set to datetime.now().
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        # HWM date is yesterday → will trigger session reset
        lp.daily_high_water = 5000.0
        lp.daily_high_water_date = "2026-01-01"
        lp._broker_equity = 5184.0
        assert lp._hwm_reset_at is None, "Should start as None"

        with (
            patch.object(lp, "_get_per_market_equity", return_value=971.0),
            patch("brokers.kill_switch.halt"),
            # Silence Telegram during test
            patch("utils.telegram.send_message", side_effect=Exception("no tg"), create=True),
        ):
            before_call = datetime.now()
            lp.check_daily_drawdown()
            after_call = datetime.now()

        assert lp._hwm_reset_at is not None, "_hwm_reset_at should be set after HWM reset"
        assert before_call <= lp._hwm_reset_at <= after_call, (
            f"_hwm_reset_at {lp._hwm_reset_at} should be between {before_call} and {after_call}"
        )


class TestNewPerMarketEquityFormula:
    """Fix E: verify the position+cash formula produces accurate results."""

    def test_position_mv_plus_live_cash(self, tmp_path, monkeypatch):
        """Snapshot: sp500=$971 = $817.87 positions + $153.14 cash.
        Broker drops from $5213.4 → $5184 (−0.56%).
        With 1 CAT share now at $818 (unchanged from snapshot), no fills since snap:
          effective_eq = $818 + $153.14 (snap_cash, no scaling) = $971.14
        NEW formula: cash is NOT scaled by broker equity ratio.  Instead it reflects
        actual realized cash flows since the snapshot.  With no fills, live_cash = snap_cash.
        No broker connected in test → degraded mode → live_cash = snap_cash (frozen).
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 971.01,
            "position_mv": 817.87,
            "cash_attributed": 153.14,
            "broker_equity": 5213.4,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")

        # Mock one CAT position, current price $818 (unchanged from snapshot)
        mock_pos = MagicMock()
        mock_pos.ticker = "CAT"
        mock_pos.shares = 1
        mock_pos.entry_price = 835.24
        lp.positions = [mock_pos]

        result = lp._get_per_market_equity(5184.0, prices={"CAT": 818.0})

        # NEW formula: no broker → degraded mode → live_cash = snap_cash (no scale).
        # Result = pos_mv + snap_cash = 818.0 + 153.14 = 971.14
        expected = 818.0 + 153.14
        assert result == pytest.approx(expected, rel=1e-4), (
            f"Expected ${expected:.2f} (pos_mv + snap_cash, no scaling), got ${result:.2f}"
        )

    def test_broker_growth_does_not_scale_cash(self, tmp_path, monkeypatch):
        """Snapshot: $1000 = $700 positions + $300 cash. Broker +10% ($5000→$5500).
        Positions held at $700 (no price change). No fills since snapshot.
        NEW formula: broker growth does NOT scale per-market cash.  Cash attribution
        is based on actual realized flows (FILL/DIV), not broker-wide equity ratio.
        No broker connected in test → degraded mode → live_cash = snap_cash = $300.
        Result: $700 + $300 = $1000 (NOT $1030 from the old scaled formula).
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 1000.0,
            "position_mv": 700.0,
            "cash_attributed": 300.0,
            "broker_equity": 5000.0,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")

        mock_pos = MagicMock()
        mock_pos.ticker = "XYZ"
        mock_pos.shares = 7
        mock_pos.entry_price = 100.0
        lp.positions = [mock_pos]

        # Pass prices matching snapshot MV (no change)
        result = lp._get_per_market_equity(5500.0, prices={"XYZ": 100.0})

        # NEW formula: no broker → degraded → live_cash = snap_cash = $300 (no scaling).
        # Broker equity growth does NOT automatically grow per-market cash.
        # Only actual FILL/DIV flows change the per-market cash estimate.
        expected = 700.0 + 300.0  # pos_mv + snap_cash = $1000
        assert result == pytest.approx(expected, rel=1e-4), (
            f"With no fills, per-market eq = pos_mv + snap_cash = $1000, got ${result:.2f}"
        )

    def test_position_price_drop_without_broker_change(self, tmp_path, monkeypatch):
        """Positions lose 10% (snapshot $700 → $630). Broker unchanged.
        Expected: $630 + $300×1.0 = $930 (not $971×0.875 = $849 from old formula).
        """
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 1000.0,
            "position_mv": 700.0,
            "cash_attributed": 300.0,
            "broker_equity": 5000.0,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")

        mock_pos = MagicMock()
        mock_pos.ticker = "XYZ"
        mock_pos.shares = 7
        mock_pos.entry_price = 100.0
        lp.positions = [mock_pos]

        # XYZ drops from $100 to $90 (10% loss on 7 shares = −$70)
        result = lp._get_per_market_equity(5000.0, prices={"XYZ": 90.0})

        pos_mv_now = 7 * 90.0  # $630
        expected = pos_mv_now + 300.0 * 1.0  # $930
        assert result == pytest.approx(expected, rel=1e-4), (
            f"10% position drop should give ${expected:.2f}, got ${result:.2f}"
        )

        # Verify OLD formula would have given a different (wrong) result
        old_formula = 1000.0 * (5000.0 / 5000.0)  # $1000 — no change since broker unchanged
        # Old formula misses the position loss entirely → over-estimates by $70
        assert old_formula != pytest.approx(expected, abs=10.0), (
            "Old formula should have given a different result when position price drops"
        )

    def test_no_positions_uses_zero_pos_mv(self, tmp_path, monkeypatch):
        """No open positions → position MV = 0. Result = scaled cash only."""
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 500.0,
            "position_mv": 0.0,
            "cash_attributed": 500.0,
            "broker_equity": 5000.0,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")
        lp.positions = []  # no positions

        result = lp._get_per_market_equity(5500.0)

        # NEW formula: no broker → degraded → live_cash = snap_cash = $500 (no scaling).
        expected = 0.0 + 500.0  # pos_mv=0 + snap_cash=$500
        assert result == pytest.approx(expected, rel=1e-4), (
            f"No-position case: result should be snap_cash=$500, got ${result:.2f}"
        )

    def test_legacy_fallback_when_no_position_or_cash_data(self, tmp_path, monkeypatch):
        """Snapshot row with position_mv=0 and cash_attributed=0 → legacy proportional scaling."""
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 1000.0,
            "position_mv": 0.0,  # no breakdown in this snapshot
            "cash_attributed": 0.0,
            "broker_equity": 5000.0,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")
        lp.positions = []

        result = lp._get_per_market_equity(5500.0)

        # Legacy formula: 1000 × (5500/5000) = $1100
        assert result == pytest.approx(1100.0, rel=1e-4), (
            "Legacy fallback should scale full allocated_equity proportionally"
        )

    def test_entry_price_fallback_when_no_prices_dict(self, tmp_path, monkeypatch):
        """prices dict is None → falls back to entry_price for position MV."""
        db = tmp_path / "atlas.db"
        _seed_db(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 971.01,
            "position_mv": 817.87,
            "cash_attributed": 153.14,
            "broker_equity": 5213.4,
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")

        mock_pos = MagicMock()
        mock_pos.ticker = "CAT"
        mock_pos.shares = 1
        mock_pos.entry_price = 835.24  # entry price used as fallback
        lp.positions = [mock_pos]

        # No prices dict passed
        result = lp._get_per_market_equity(5184.0, prices=None)

        # Should use entry_price=835.24 for position MV.
        # NEW formula: no broker → degraded → live_cash = snap_cash = 153.14 (no scaling).
        expected = 835.24 + 153.14  # entry_price + snap_cash
        assert result == pytest.approx(expected, rel=1e-4), (
            f"Expected ${expected:.2f} (entry price + snap_cash, no scaling), got ${result:.2f}"
        )
