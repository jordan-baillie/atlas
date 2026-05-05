"""Tests for FIX-PMEQ-001 — per-market equity live cash attribution.

Verifies that:
1. The Apr 30 sector_etfs phantom 13.69% drawdown is fixed (reproducer test).
2. CSD/CSW/JNLC deposits are NOT attributed to any market.
3. Multi-market fills are independently routed by derive_universe.
4. Activities API failure → degraded mode → kill switch suppressed.
5. Catastrophic 20% override still fires in degraded mode.
6. Legacy snapshot fallback (pos_mv=0, cash=0) unchanged.
7. BUY fills subtract from market cash.
8. Dividend attribution adds to market cash.
9. Cache TTL prevents redundant API calls.
10. HWM reset path unchanged (2-day simulation).
"""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from brokers.live_portfolio import LivePortfolio
from portfolio.per_market_cash_flow import _clear_cache, compute_realized_cash_flow_since


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_config(
    market_id: str = "sector_etfs",
    starting_equity: float = 1749.77,
    max_daily_dd: float = 0.02,
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
    market_id: str = "sector_etfs",
    starting_equity: float = 1749.77,
    max_daily_dd: float = 0.02,
) -> LivePortfolio:
    cfg = _make_config(market_id, starting_equity, max_daily_dd)
    with patch.object(LivePortfolio, "_load_local_state", return_value=None):
        lp = LivePortfolio(cfg, market_id=market_id)
    lp._broker_equity = 0.0
    lp.broker_data_valid = True
    return lp


def _seed_market_equity_history(db_path: Path, rows: list[dict]) -> None:
    """Seed market_equity_history rows into a temp SQLite DB."""
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
                r.get("allocated_equity", 0.0),
                r.get("position_mv", 0.0),
                r.get("cash_attributed", 0.0),
                r.get("broker_equity", 5134.19),
                r.get("broker_cash", 800.0),
                r.get("snapshot_time", "2026-04-29T22:01:06+00:00"),
                r.get("created_at", "2026-04-29 22:01:06"),
            ),
        )
    conn.commit()
    conn.close()


def _make_fill_activity(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    tx_time: str = "2026-04-30T08:00:34Z",
) -> dict:
    """Return a FILL activity as a plain dict (raw Alpaca HTTP response shape)."""
    return {
        "activity_type": "FILL",
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": tx_time,
    }


def _make_div_activity(
    symbol: str,
    net_amount: float,
    tx_time: str = "2026-04-30T12:00:00Z",
) -> dict:
    return {
        "activity_type": "DIV",
        "symbol": symbol,
        "net_amount": str(net_amount),
        "transaction_time": tx_time,
    }


def _make_cash_activity(activity_type: str, net_amount: float) -> dict:
    """Return a cash deposit/withdrawal activity."""
    return {
        "activity_type": activity_type,  # CSD, CSW, JNLC
        "net_amount": str(net_amount),
        "date": "2026-04-30",
    }


def _mock_broker(activities: list) -> MagicMock:
    """Build a mock broker whose activities API returns `activities`."""
    broker = MagicMock()
    # _broker_call(fn, req) → fn(req)
    broker._broker_call.side_effect = lambda fn, req: fn(req)
    broker._trade_client.get.return_value = activities
    return broker


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Phantom drawdown reproducer (Apr 30 sector_etfs incident)
# ═══════════════════════════════════════════════════════════════════════════════

class TestPhantomDrawdownReproducer:
    """Reproduce the Apr 30 sector_etfs phantom 13.69% drawdown scenario."""

    def test_phantom_drawdown_reproducer(self, tmp_path, monkeypatch):
        """Apr 30 scenario: XLY exits, XLI remains. Old formula gave 13.6% phantom
        drawdown; new formula correctly returns ~growth via live cash attribution.

        Snapshot (Apr 29 22:01 UTC): sector_etfs pos_mv=$1529.37, cash=$220.40,
        broker=$5134.19. HWM after first check_daily_drawdown = $2028.87.
        At 19:03 UTC Apr 30: old formula → per_market≈$1752 → 13.6% dd → HALT.

        With FIX-PMEQ-001:
          XLY sell fill: 21 × $55.74 = +$1170.54 cash flow to sector_etfs
          XLI still held: 9 × $173.97 = $1565.73 position MV
          live_cash = $220.40 + $1170.54 = $1390.94
          per_market_eq = $1565.73 + $1390.94 = $2956.67 (well above HWM $2028.87)
        → no HALT fires.
        """
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sector_etfs",
            "allocated_equity": 1749.77,
            "position_mv": 1529.37,
            "cash_attributed": 220.40,
            "broker_equity": 5134.19,
            "snapshot_time": "2026-04-29T22:01:06+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # Build portfolio: XLI only (XLY has exited)
        lp = _make_portfolio("sector_etfs", starting_equity=1749.77, max_daily_dd=0.02)
        mock_xli = MagicMock()
        mock_xli.ticker = "XLI"
        mock_xli.shares = 9
        mock_xli.entry_price = 173.97
        lp.positions = [mock_xli]

        # Mock broker: XLY SELL fill → +$1170.54
        xly_fill = _make_fill_activity("XLY", "sell", 21, 55.74, "2026-04-30T08:00:34Z")
        lp._broker = _mock_broker([xly_fill])
        lp._broker_equity = 5164.50

        # HWM set to the value from the first check_daily_drawdown on Apr 30
        lp.daily_high_water = 2028.87
        lp.daily_high_water_date = "2026-05-01"  # today — prevents session HWM reset

        result = lp._get_per_market_equity(5164.50, prices={"XLI": 173.97})

        # Verify the arithmetic
        expected_pos_mv = 9 * 173.97  # 1565.73
        expected_live_cash = 220.40 + 21 * 55.74  # 220.40 + 1170.54 = 1390.94
        expected_per_market = expected_pos_mv + expected_live_cash  # 2956.67
        assert result == pytest.approx(expected_per_market, rel=1e-3), (
            f"Expected ${expected_per_market:.2f}, got ${result:.2f}"
        )
        assert result > 2028.87, (
            f"per_market_eq=${result:.2f} should be above HWM=$2028.87 → no drawdown"
        )

        # Verify check_daily_drawdown does NOT halt
        with patch("brokers.kill_switch.halt") as mock_halt:
            halted, dd = lp.check_daily_drawdown(prices={"XLI": 173.97})

        assert halted is False, (
            f"Phantom drawdown should NOT halt; dd={dd:.2%}, per_market_eq=${result:.2f}"
        )
        assert dd <= 0.0, f"Expected dd≤0 (equity grew), got dd={dd:.2%}"
        mock_halt.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Cash deposit NOT attributed to any market
# ═══════════════════════════════════════════════════════════════════════════════

class TestCashDepositNotAttributed:
    def test_cash_only_deposit_not_attributed(self):
        """CSD/CSW/JNLC activities must NOT appear in any market's cash_flow."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sp500": set(), "sector_etfs": set(), "commodity_etfs": set()}

        activities = [
            _make_cash_activity("CSD", 5000.0),
            _make_cash_activity("CSW", -200.0),
            _make_cash_activity("JNLC", 100.0),
        ]
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        for m, v in flows.items():
            assert v == pytest.approx(0.0), (
                f"Market {m!r} got ${v:.2f} from cash deposit — should be $0"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Multi-market fills independently routed
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiMarketFills:
    def test_multi_market_simultaneous_fills(self):
        """XLF→sector_etfs, GLD→commodity_etfs, AAPL→sp500: each attributed independently.

        Note: GLD is in BOTH commodity_etfs and gold_etfs.  derive_universe('GLD')
        returns 'commodity_etfs' (alphabetically first).  Since only sp500/sector_etfs/
        commodity_etfs are in market_symbols, GLD is correctly attributed to commodity_etfs.
        """
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        # Only track 3 markets — gold_etfs NOT included
        market_symbols = {"sp500": set(), "sector_etfs": set(), "commodity_etfs": set()}

        activities = [
            _make_fill_activity("XLF", "sell", 10, 40.0),   # sector_etfs: +$400
            _make_fill_activity("GLD", "sell", 5, 200.0),   # commodity_etfs: +$1000
            _make_fill_activity("AAPL", "buy", 2, 190.0),   # sp500: -$380
        ]
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        assert flows["sector_etfs"] == pytest.approx(400.0, rel=1e-4), (
            f"XLF sell $400 → sector_etfs, got {flows['sector_etfs']:.2f}"
        )
        assert flows["commodity_etfs"] == pytest.approx(1000.0, rel=1e-4), (
            f"GLD sell $1000 → commodity_etfs, got {flows['commodity_etfs']:.2f}"
        )
        assert flows["sp500"] == pytest.approx(-380.0, rel=1e-4), (
            f"AAPL buy -$380 → sp500, got {flows['sp500']:.2f}"
        )

    def test_gold_etfs_universe_not_tracked_is_skipped(self):
        """GLD fill when gold_etfs IS the derive_universe result but is not in market_symbols."""
        _clear_cache()
        # Temporarily break commodity_etfs entry for GLD by only tracking sector_etfs
        from universe.membership import _build_membership, _ALL_MEMBERSHIP_CACHE
        # If derive_universe('GLD') returns 'gold_etfs' (e.g. cache rebuild scenario),
        # and gold_etfs is not in market_symbols, GLD fill is skipped.
        # We test this by restricting market_symbols to sector_etfs only.
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}  # only sector_etfs tracked

        activities = [_make_fill_activity("GLD", "sell", 5, 200.0)]
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        # GLD maps to commodity_etfs or gold_etfs, neither is sector_etfs → skipped
        assert flows["sector_etfs"] == pytest.approx(0.0), (
            f"GLD should be skipped (market not in tracked set), got {flows['sector_etfs']:.2f}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Activities API failure → degraded mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestDegradedMode:
    def test_activities_api_failure_degraded_mode(self, tmp_path, monkeypatch):
        """Broker activities API raises RuntimeError → degraded=True, zeros flows.
        _get_per_market_equity falls back to snap_cash, sets _per_market_equity_degraded.
        check_daily_drawdown does NOT halt at dd=0.13 when degraded and dd < 20%.
        """
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sector_etfs",
            "allocated_equity": 1749.77,
            "position_mv": 1529.37,
            "cash_attributed": 220.40,
            "broker_equity": 5134.19,
            "snapshot_time": "2026-04-29T22:01:06+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # Broker with broken activities API
        broken_broker = MagicMock()
        broken_broker._broker_call.side_effect = RuntimeError("API down")

        # Verify compute_realized_cash_flow_since degrades
        since = datetime(2026, 4, 29, 22, 1, 6, tzinfo=timezone.utc)
        market_symbols = {"sp500": set(), "sector_etfs": set(), "commodity_etfs": set()}
        flows, degraded = compute_realized_cash_flow_since(
            broken_broker, since, market_symbols, cache_ttl_seconds=0.0
        )
        assert degraded is True
        for v in flows.values():
            assert v == pytest.approx(0.0)

        # Now test full _get_per_market_equity path
        lp = _make_portfolio("sector_etfs", starting_equity=1749.77, max_daily_dd=0.02)
        mock_xli = MagicMock()
        mock_xli.ticker = "XLI"
        mock_xli.shares = 9
        mock_xli.entry_price = 173.97
        lp.positions = [mock_xli]
        lp._broker = broken_broker
        lp._broker_equity = 5164.50

        result = lp._get_per_market_equity(5164.50, prices={"XLI": 173.97})
        # Degraded: live_cash = snap_cash = 220.40
        expected = 9 * 173.97 + 220.40  # 1565.73 + 220.40 = 1786.13
        assert result == pytest.approx(expected, rel=1e-3), (
            f"Degraded mode: expected pos_mv + snap_cash = ${expected:.2f}, got ${result:.2f}"
        )
        assert lp._per_market_equity_degraded is True

        # check_daily_drawdown: HWM=2028.87, per_market≈1786 → dd≈11.9% < 20% → SUPPRESSED
        lp.daily_high_water = 2028.87
        lp.daily_high_water_date = "2026-05-01"  # today — prevents session HWM reset

        with patch("brokers.kill_switch.halt") as mock_halt:
            halted, dd = lp.check_daily_drawdown(prices={"XLI": 173.97})

        assert halted is False, (
            f"Degraded mode should suppress halt at dd={dd:.2%} (< 20% override)"
        )
        assert dd == pytest.approx(0.13, abs=0.02), (
            f"Drawdown should be ~13% (the phantom value), got {dd:.2%}"
        )
        mock_halt.assert_not_called()

# ═══════════════════════════════════════════════════════════════════════════════
# 5. Catastrophic override still fires in degraded mode
# ═══════════════════════════════════════════════════════════════════════════════

class TestCatastrophicOverrideDegradedMode:
    def test_catastrophic_override_still_fires_in_degraded_mode(self, tmp_path, monkeypatch):
        """Even when activities API is degraded, dd ≥ 20% still fires the kill switch."""
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sector_etfs",
            "allocated_equity": 1749.77,
            "position_mv": 1529.37,
            "cash_attributed": 220.40,
            "broker_equity": 5134.19,
            "snapshot_time": "2026-04-29T22:01:06+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # Broker with broken activities API
        broken_broker = MagicMock()
        broken_broker._broker_call.side_effect = RuntimeError("API down")

        lp = _make_portfolio("sector_etfs", starting_equity=1749.77, max_daily_dd=0.02)
        # 25% drawdown from HWM $2028.87 → effective_eq = $1521.65
        # Set positions to produce that value with snap_cash as fallback
        # pos_mv = 1521.65 - 220.40 = 1301.25
        # Use 7.48 shares at $173.97 ≈ 1301.01 → close enough
        mock_pos = MagicMock()
        mock_pos.ticker = "XLI"
        mock_pos.shares = 7
        mock_pos.entry_price = 173.97
        lp.positions = [mock_pos]
        lp._broker = broken_broker
        lp._broker_equity = 5000.0

        lp.daily_high_water = 2028.87
        lp.daily_high_water_date = "2026-05-01"  # today — prevents session HWM reset

        # result = 7*173.97 + 220.40 = 1217.79 + 220.40 = 1438.19
        # dd = (2028.87 - 1438.19) / 2028.87 = 29.1% > 20% → should HALT
        with patch("brokers.kill_switch.halt") as mock_halt:
            halted, dd = lp.check_daily_drawdown(prices={"XLI": 173.97})

        assert halted is True, (
            f"Catastrophic dd={dd:.2%} should HALT even in degraded mode"
        )
        assert dd > 0.20, f"Expected dd > 20%, got {dd:.2%}"
        mock_halt.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Legacy snapshot fallback unchanged
# ═══════════════════════════════════════════════════════════════════════════════

class TestLegacySnapshotFallback:
    def test_legacy_snapshot_fallback_unchanged(self, tmp_path, monkeypatch):
        """Snapshot row with pos_mv=0 AND cash_attributed=0 → legacy proportional scaling."""
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sp500",
            "allocated_equity": 971.0,
            "position_mv": 0.0,      # legacy row — no breakdown
            "cash_attributed": 0.0,
            "broker_equity": 5000.0,
            "snapshot_time": "2026-04-29T00:00:00+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", starting_equity=971.0)
        lp.positions = []
        # No broker needed — legacy path doesn't call activities API

        result = lp._get_per_market_equity(5500.0)

        # Legacy formula: snap_alloc * (current_broker / snap_broker)
        expected = 971.0 * (5500.0 / 5000.0)  # $1068.10
        assert result == pytest.approx(expected, rel=1e-4), (
            f"Legacy path: expected ${expected:.2f}, got ${result:.2f}"
        )
        # Legacy path does not set degraded flag
        assert lp._per_market_equity_degraded is False


# ═══════════════════════════════════════════════════════════════════════════════
# 7. BUY fill subtracts from market cash
# ═══════════════════════════════════════════════════════════════════════════════

class TestBuyFillSubtractsCash:
    def test_buy_fill_subtracts_cash(self):
        """BUY fill for XLF: -qty * price subtracts from sector_etfs cash."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}

        activities = [_make_fill_activity("XLF", "buy", 10, 50.0)]  # -$500
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        assert flows["sector_etfs"] == pytest.approx(-500.0, rel=1e-4), (
            f"BUY 10 × $50 should give -$500 to sector_etfs, got {flows['sector_etfs']:.2f}"
        )

    def test_buy_fill_live_cash_goes_negative(self, tmp_path, monkeypatch):
        """sector_etfs starting snap_cash=$500, BUY fills $500 → live_cash=$0."""
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sector_etfs",
            "allocated_equity": 1000.0,
            "position_mv": 500.0,
            "cash_attributed": 500.0,
            "broker_equity": 5000.0,
            "snapshot_time": "2026-04-29T22:00:00+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sector_etfs")
        mock_pos = MagicMock()
        mock_pos.ticker = "XLF"
        mock_pos.shares = 10
        mock_pos.entry_price = 50.0
        lp.positions = [mock_pos]

        # BUY 10 × $50 = -$500 → live_cash = 500 - 500 = 0
        buy_fill = _make_fill_activity("XLF", "buy", 10, 50.0, "2026-04-30T10:00:00Z")
        lp._broker = _mock_broker([buy_fill])
        lp._broker_equity = 5000.0

        result = lp._get_per_market_equity(5000.0, prices={"XLF": 50.0})

        expected = 10 * 50.0 + 0.0  # pos_mv=500 + live_cash=0
        assert result == pytest.approx(expected, rel=1e-4), (
            f"Expected pos_mv + (snap_cash - buy) = $500, got ${result:.2f}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Dividend attribution
# ═══════════════════════════════════════════════════════════════════════════════

class TestDividendAttribution:
    def test_dividend_attribution(self):
        """DIV activity for XLF → +net_amount to sector_etfs cash."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}

        activities = [_make_div_activity("XLF", 12.34)]
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        assert flows["sector_etfs"] == pytest.approx(12.34, rel=1e-4), (
            f"DIV $12.34 for XLF → sector_etfs, got {flows['sector_etfs']:.2f}"
        )

    def test_dividend_and_fill_cumulate(self):
        """Dividend + fill on same market both add up correctly."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}

        activities = [
            _make_fill_activity("XLF", "sell", 5, 40.0),  # +$200
            _make_div_activity("XLF", 12.34),             # +$12.34
        ]
        broker = _mock_broker(activities)

        flows, degraded = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=0.0
        )

        assert degraded is False
        assert flows["sector_etfs"] == pytest.approx(212.34, rel=1e-4), (
            f"Expected $200 + $12.34 = $212.34, got {flows['sector_etfs']:.2f}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Cache TTL — only ONE actual API call within TTL
# ═══════════════════════════════════════════════════════════════════════════════

class TestCacheTTL:
    def test_cache_ttl(self):
        """Two calls within TTL window → broker._broker_call called only ONCE."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}
        activities = [_make_fill_activity("XLF", "sell", 5, 40.0)]
        broker = _mock_broker(activities)

        # First call — should hit API
        flows1, deg1 = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=60.0
        )
        # Second call within TTL — should use cache
        flows2, deg2 = compute_realized_cash_flow_since(
            broker, since, market_symbols, cache_ttl_seconds=60.0
        )

        # broker._broker_call called only once despite two compute calls
        assert broker._broker_call.call_count == 1, (
            f"Expected 1 API call (cache hit on 2nd), got {broker._broker_call.call_count}"
        )
        assert flows1 == flows2
        assert deg1 == deg2 == False

    def test_cache_expires_after_ttl(self):
        """After TTL expires, next call hits API again."""
        _clear_cache()
        since = datetime(2026, 4, 29, 22, 0, 0, tzinfo=timezone.utc)
        market_symbols = {"sector_etfs": set()}
        activities = [_make_fill_activity("XLF", "sell", 5, 40.0)]
        broker = _mock_broker(activities)

        # First call with 0.001s TTL
        compute_realized_cash_flow_since(broker, since, market_symbols, cache_ttl_seconds=0.001)
        time.sleep(0.01)  # let TTL expire
        # Second call — TTL expired, should hit API again
        compute_realized_cash_flow_since(broker, since, market_symbols, cache_ttl_seconds=0.001)

        assert broker._broker_call.call_count == 2, (
            f"Expected 2 API calls after TTL expiry, got {broker._broker_call.call_count}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 10. HWM reset path unchanged (2-day simulation)
# ═══════════════════════════════════════════════════════════════════════════════

class TestHWMResetPath:
    def test_hwm_reset_path_unchanged(self, tmp_path, monkeypatch):
        """Two-day simulation: Day 1 → set HWM. Day 2 → HWM resets to current equity.
        Verify session HWM reset still works correctly with the new formula.
        """
        _clear_cache()
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [{
            "date": "2026-04-29",
            "market_id": "sector_etfs",
            "allocated_equity": 1749.77,
            "position_mv": 1529.37,
            "cash_attributed": 220.40,
            "broker_equity": 5134.19,
            "snapshot_time": "2026-04-29T22:01:06+00:00",
        }])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # Day 1: mock broker returns a sell fill (XLY → +$1170.54)
        xly_fill = _make_fill_activity("XLY", "sell", 21, 55.74, "2026-04-30T08:00:34Z")
        mock_broker_day1 = _mock_broker([xly_fill])

        lp = _make_portfolio("sector_etfs", starting_equity=1749.77, max_daily_dd=0.02)
        mock_xli = MagicMock()
        mock_xli.ticker = "XLI"
        mock_xli.shares = 9
        mock_xli.entry_price = 173.97
        lp.positions = [mock_xli]
        lp._broker = mock_broker_day1
        lp._broker_equity = 5164.50

        # Day 1: HWM from yesterday
        lp.daily_high_water = 1749.77
        lp.daily_high_water_date = "2026-04-29"

        with (
            patch("brokers.live_portfolio.datetime") as mock_dt,
            patch("brokers.kill_switch.halt"),
            patch("utils.telegram.send_message", side_effect=Exception("no tg"), create=True),
        ):
            mock_dt.now.return_value.strftime.return_value = "2026-04-30"
            mock_dt.now.return_value.__sub__ = lambda self, other: timedelta(hours=2)
            mock_dt.now.return_value.__gt__ = lambda self, other: True
            # First check: triggers HWM date reset
            with patch.object(lp, "_get_per_market_equity", return_value=2956.67):
                halted, dd = lp.check_daily_drawdown()

        # HWM should reset to SNAPSHOT anchor ($1749.77), NOT effective_eq ($2956.67)
        # Updated 2026-05-06: old assertion tested the phantom-HWM bug.
        # Fix A anchors session reset to _latest_snapshot_allocated_equity() = $1749.77.
        assert halted is False, "Day 1 reset should not halt"
        assert lp.daily_high_water == pytest.approx(1749.77, rel=1e-3), (
            "HWM should reset to snapshot anchor $1749.77, got ${:.2f}".format(lp.daily_high_water)
        )

        # Day 2: equity drops slightly → no halt (within limit)
        day2_eq = 1749.77 * 0.99  # -1% → well within 2% limit
        lp.daily_high_water_date = "2026-04-30"  # already on this date
        lp.daily_high_water = 1749.77

        with (
            patch.object(lp, "_get_per_market_equity", return_value=day2_eq),
            patch("brokers.kill_switch.halt") as mock_halt,
        ):
            halted_d2, dd_d2 = lp.check_daily_drawdown()

        assert halted_d2 is False, f"Day 2 at -1% should not halt, dd={dd_d2:.2%}"
        mock_halt.assert_not_called()
