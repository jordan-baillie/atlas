"""Tests for per-market drawdown HWM wiring — RCA latent #6 Fix 2.

Verifies that:
- _get_per_market_equity() reads from market_equity_history and scales correctly
- check_daily_drawdown() uses per-market equity for independent halt decisions
- Market A at 15% drawdown halts even when global portfolio is only ~7.55% down
- Market B at 0.1% drawdown does NOT halt when market A halts
- Fallback to global broker equity when attribution data unavailable
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(_ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ATLAS_ROOT))

from brokers.live_portfolio import LivePortfolio


# ── Minimal config factory ────────────────────────────────────────────────────

def _make_config(
    market_id: str = "sp500",
    starting_equity: float = 1000.0,
    max_daily_dd: float = 0.10,
    dual_write: bool = False,
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
        "dual_write_market_state": dual_write,
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


# ── Helpers for seeding a tmp SQLite with market_equity_history ───────────────

def _seed_market_equity_history(
    db_path: Path,
    rows: list[dict],
) -> None:
    """Seed market_equity_history rows into a temporary SQLite DB."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_equity_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            market_id   TEXT NOT NULL,
            allocated_equity REAL,
            position_mv REAL,
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
                r["allocated_equity"],
                r.get("position_mv", 0.0),
                r.get("cash_attributed", 0.0),
                r["broker_equity"],
                r.get("broker_cash", 0.0),
                r.get("snapshot_time", "2026-04-29T00:00:00+00:00"),
                r.get("created_at", "2026-04-29 00:00:00"),
            ),
        )
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: _get_per_market_equity()
# ═══════════════════════════════════════════════════════════════════════════════

class TestGetPerMarketEquity:
    """Unit tests for LivePortfolio._get_per_market_equity()."""

    def test_returns_none_when_db_has_no_rows(self, tmp_path, monkeypatch):
        """No market_equity_history rows → returns None (fallback to global)."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")
        result = lp._get_per_market_equity(5000.0)
        assert result is None

    def test_returns_scaled_value_when_row_present(self, tmp_path, monkeypatch):
        """Snapshot: alloc=$1000, broker=$5000. Current broker=$5500 → scaled=$1100."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2026-04-29",
                "market_id": "sp500",
                "allocated_equity": 1000.0,
                "broker_equity": 5000.0,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")
        result = lp._get_per_market_equity(5500.0)
        # 1000 * (5500/5000) = 1100
        assert result == pytest.approx(1100.0, rel=1e-4)

    def test_same_broker_equity_returns_snapshot_value(self, tmp_path, monkeypatch):
        """No change in total equity → per-market eq equals snapshot value."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2026-04-29",
                "market_id": "commodity_etfs",
                "allocated_equity": 1001.81,
                "broker_equity": 5213.4,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("commodity_etfs")
        result = lp._get_per_market_equity(5213.4)
        assert result == pytest.approx(1001.81, rel=1e-3)

    def test_returns_none_on_db_failure(self, monkeypatch):
        """DB read failure → None (non-fatal), falls back to global equity."""
        def _bad_get_db(*a, **kw):
            raise RuntimeError("DB connection refused")

        monkeypatch.setattr("db.atlas_db.get_db", _bad_get_db, raising=False)

        lp = _make_portfolio("sp500")
        result = lp._get_per_market_equity(5000.0)
        assert result is None

    def test_returns_none_for_wrong_market_id(self, tmp_path, monkeypatch):
        """Row exists for sp500 but portfolio is sector_etfs → None."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2026-04-29",
                "market_id": "sp500",
                "allocated_equity": 971.0,
                "broker_equity": 5213.4,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sector_etfs")
        result = lp._get_per_market_equity(5213.4)
        assert result is None

    def test_returns_none_when_snapshot_stale(self, tmp_path, monkeypatch):
        """Snapshot older than 3 days → None (too stale, avoid phantom signals)."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2020-01-01",  # very old snapshot
                "market_id": "sp500",
                "allocated_equity": 1000.0,
                "broker_equity": 5000.0,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500")
        result = lp._get_per_market_equity(5000.0)
        assert result is None

    def test_scale_down_when_broker_equity_drops(self, tmp_path, monkeypatch):
        """Broker equity drops from $5213 to $4500 → per-market eq scales down."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2026-04-29",
                "market_id": "sector_etfs",
                "allocated_equity": 3216.0,
                "broker_equity": 5213.0,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sector_etfs")
        result = lp._get_per_market_equity(4500.0)
        expected = 3216.0 * (4500.0 / 5213.0)
        assert result == pytest.approx(expected, rel=1e-4)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: check_daily_drawdown() per-market isolation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPerMarketDrawdownIsolation:
    """Verify per-market drawdown halts independently of global equity.

    Scenario (mirrors the spec exactly):
        Market A: HWM $1000, current $850 → 15% drawdown (exceeds 10% limit)
        Market B: HWM $1000, current $999 → 0.1% drawdown (within 10% limit)
        Global:   HWM $2000, current $1849 → 7.55% drawdown (does NOT trip global 10%)

    Expected:
        - Market A HALTS
        - Market B does NOT halt
        - Without per-market equity, global 7.55% wouldn't trigger either — this
          shows the value of per-market attribution
    """

    def _setup(self, tmp_path: Path, monkeypatch) -> tuple:
        """Seed DB, create two portfolios with different per-market equity."""
        import datetime as _dt
        _today = _dt.date.today().isoformat()

        db = tmp_path / "atlas.db"

        # Snapshot: market_a=$1000 + market_b=$1000 = global broker $2000
        _seed_market_equity_history(db, [
            {
                "date": _today,
                "market_id": "market_a",
                "allocated_equity": 1000.0,
                "broker_equity": 2000.0,
            },
            {
                "date": _today,
                "market_id": "market_b",
                "allocated_equity": 1000.0,
                "broker_equity": 2000.0,
            },
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # max_daily_dd = 10% for both markets
        lp_a = _make_portfolio("market_a", starting_equity=1000.0, max_daily_dd=0.10)
        lp_b = _make_portfolio("market_b", starting_equity=1000.0, max_daily_dd=0.10)

        # Pre-set HWM to $1000 for both (simulating start of session).
        # Use today's date so no session HWM reset fires during the test.
        lp_a.daily_high_water = 1000.0
        lp_a.daily_high_water_date = _today
        lp_b.daily_high_water = 1000.0
        lp_b.daily_high_water_date = _today

        return lp_a, lp_b

    def test_market_a_halts_at_15pct_drawdown(self, tmp_path, monkeypatch):
        """Market A at $850 (15% dd) trips 10% limit → halted=True."""
        lp_a, _ = self._setup(tmp_path, monkeypatch)
        # Current global broker equity: $849 + $1000 = $1849
        # market_a per-market = 1000 * (1849/2000) = $924.5 (scaled)
        # But we want HWM=$1000, current=$850 for a clean 15% test.
        # We control this by mocking _get_per_market_equity directly.
        with patch.object(lp_a, "_get_per_market_equity", return_value=850.0):
            lp_a._broker_equity = 1849.0  # global equity
            halted, dd = lp_a.check_daily_drawdown()

        assert halted is True, f"Market A should be halted (dd={dd:.2%})"
        assert dd == pytest.approx(0.15, rel=1e-3), f"Expected 15% drawdown, got {dd:.2%}"

    def test_market_b_does_not_halt_at_0pt1pct_drawdown(self, tmp_path, monkeypatch):
        """Market B at $999 (0.1% dd) stays below 10% limit → halted=False."""
        _, lp_b = self._setup(tmp_path, monkeypatch)
        with patch.object(lp_b, "_get_per_market_equity", return_value=999.0):
            lp_b._broker_equity = 1849.0  # same global equity
            halted, dd = lp_b.check_daily_drawdown()

        assert halted is False, f"Market B should NOT be halted (dd={dd:.2%})"
        assert dd == pytest.approx(0.001, rel=0.1), f"Expected ~0.1% drawdown, got {dd:.2%}"

    def test_global_7pt55_would_not_trip_but_market_a_does(self, tmp_path, monkeypatch):
        """Key scenario: global 7.55% dd below limit but market_a 15% should still halt.

        Without per-market attribution, check_daily_drawdown uses global broker
        equity ($1849 vs HWM $2000 = 7.55%) → neither market halts.
        With per-market attribution, market_a uses $850 vs $1000 HWM = 15% → halts.
        """
        lp_a, _ = self._setup(tmp_path, monkeypatch)
        global_equity = 1849.0  # global broker equity
        lp_a._broker_equity = global_equity

        # Confirm global would NOT halt (7.55% < 10%)
        global_dd = (lp_a.daily_high_water - global_equity) / lp_a.daily_high_water
        # Note: HWM for lp_a is 1000, not 2000 — because it's a per-MARKET HWM
        # So even global fallback for market_a would show (1000-1849)/1000 = 0 (HWM ratchets up)
        # The real test is: per-market path at $850 triggers halt that global wouldn't.

        # Simulate: if we used global broker equity, no halt
        with patch.object(lp_a, "_get_per_market_equity", return_value=None):
            # No per-market data → falls back to broker_eq = $1849
            # HWM was $1000 → ratchets UP to $1849 (new high) → dd=0
            halted_global, dd_global = lp_a.check_daily_drawdown()
        assert halted_global is False, (
            f"Global fallback should not halt (dd_global={dd_global:.2%})"
        )

        # Reset HWM for the per-market test
        import datetime as _dt
        lp_a.daily_high_water = 1000.0
        lp_a.daily_high_water_date = _dt.date.today().isoformat()  # today — no session reset

        # Now: per-market path at $850 should halt
        with patch.object(lp_a, "_get_per_market_equity", return_value=850.0):
            halted_per_market, dd_per_market = lp_a.check_daily_drawdown()
        assert halted_per_market is True, (
            f"Per-market path should halt market_a (dd={dd_per_market:.2%})"
        )
        assert dd_per_market == pytest.approx(0.15, rel=1e-3)

    def test_fallback_to_broker_eq_when_no_attribution(self, tmp_path, monkeypatch):
        """No per-market data → falls back to global broker equity (original behavior)."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [])  # empty — no data
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        lp.daily_high_water = 5000.0
        import datetime as _dt
        lp.daily_high_water_date = _dt.date.today().isoformat()  # today — no session reset
        lp._broker_equity = 4400.0  # 12% global drawdown

        halted, dd = lp.check_daily_drawdown()

        assert halted is True, f"Global 12% dd should halt without per-market data"
        assert dd == pytest.approx(0.12, rel=1e-3)

    def test_hwm_resets_to_per_market_eq_on_new_day(self, tmp_path, monkeypatch):
        """On new calendar day, HWM resets to per-market equity, not global."""
        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": "2026-04-29",
                "market_id": "sp500",
                "allocated_equity": 971.0,
                "broker_equity": 5213.4,
            }
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        lp = _make_portfolio("sp500", max_daily_dd=0.10)
        # Simulate old day
        lp.daily_high_water = 5000.0  # stale global HWM from prior session
        lp.daily_high_water_date = "2026-01-01"  # yesterday/old
        lp._broker_equity = 5213.4

        # First call → session reset for new day
        with patch("brokers.live_portfolio.datetime") as mock_dt:
            mock_dt.now.return_value.strftime.return_value = "2026-04-29"
            with patch.object(lp, "_get_per_market_equity", return_value=971.0):
                with patch("brokers.live_portfolio.send_message", side_effect=Exception, create=True):
                    halted, dd = lp.check_daily_drawdown()

        # HWM should reset to 971 (per-market), not 5213.4 (global)
        assert lp.daily_high_water == pytest.approx(971.0, rel=1e-3), (
            f"HWM should reset to per-market $971, got ${lp.daily_high_water:.2f}"
        )

    def test_markets_independent_halts_full_scenario(self, tmp_path, monkeypatch):
        """Full integration: market_a halts, market_b continues — independent."""
        import datetime as _dt
        today = _dt.date.today().isoformat()  # use today's date — no session HWM reset

        db = tmp_path / "atlas.db"
        _seed_market_equity_history(db, [
            {
                "date": today,
                "market_id": "market_a",
                "allocated_equity": 1000.0,
                "broker_equity": 2000.0,
            },
            {
                "date": today,
                "market_id": "market_b",
                "allocated_equity": 1000.0,
                "broker_equity": 2000.0,
            },
        ])
        monkeypatch.setattr("db.atlas_db._db_path_override", str(db))

        # Market A: HWM $1000, current per-market $850 → 15% dd → halted
        lp_a = _make_portfolio("market_a", starting_equity=1000.0, max_daily_dd=0.10)
        lp_a.daily_high_water = 1000.0
        lp_a.daily_high_water_date = today
        lp_a._broker_equity = 1849.0

        # Market B: HWM $1000, current per-market $999 → 0.1% dd → NOT halted
        lp_b = _make_portfolio("market_b", starting_equity=1000.0, max_daily_dd=0.10)
        lp_b.daily_high_water = 1000.0
        lp_b.daily_high_water_date = today
        lp_b._broker_equity = 1849.0

        with (
            patch.object(lp_a, "_get_per_market_equity", return_value=850.0),
            patch.object(lp_b, "_get_per_market_equity", return_value=999.0),
            patch("brokers.kill_switch.halt") as mock_halt,
        ):
            halted_a, dd_a = lp_a.check_daily_drawdown()
            halted_b, dd_b = lp_b.check_daily_drawdown()

        assert halted_a is True, "Market A should halt"
        assert halted_b is False, "Market B should NOT halt"
        assert dd_a > 0.10, f"Market A drawdown ({dd_a:.2%}) should exceed 10% limit"
        assert dd_b < 0.10, f"Market B drawdown ({dd_b:.2%}) should be below 10% limit"


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: check_equity_config_sum() guard
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckEquityConfigSum:
    """Tests for the Σ(starting_equity) ≤ broker.equity × 1.05 guard."""

    def _write_config(self, config_dir: Path, market: str, starting_equity: float,
                      live_enabled: bool = True) -> None:
        """Write a minimal config JSON for testing."""
        import json
        cfg = {
            "market_id": market,
            "trading": {"mode": "live", "live_enabled": live_enabled},
            "risk": {"starting_equity": starting_equity},
        }
        (config_dir / f"{market}.json").write_text(json.dumps(cfg))

    def _seed_db(self, db_path: Path, broker_equity: float) -> None:
        """Seed market_equity_history with a single broker equity value."""
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_equity_history (
                id INTEGER PRIMARY KEY, date TEXT, market_id TEXT,
                allocated_equity REAL, position_mv REAL, cash_attributed REAL,
                broker_equity REAL, broker_cash REAL, snapshot_time TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            "INSERT INTO market_equity_history "
            "(date, market_id, allocated_equity, broker_equity, created_at) "
            "VALUES ('2026-04-29', 'sp500', 971, ?, '2026-04-29 00:00:00')",
            (broker_equity,)
        )
        conn.commit()
        conn.close()

    def test_ok_when_sum_within_tolerance(self, tmp_path):
        """Sum ≤ broker * 1.05 → ok=True, status=OK."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        self._write_config(cfg_dir, "sp500", 971.0)
        self._write_config(cfg_dir, "commodity_etfs", 1001.0)
        self._write_config(cfg_dir, "sector_etfs", 3216.0)
        self._seed_db(db, broker_equity=5213.4)

        ok, info = check_equity_config_sum(config_dir=cfg_dir, db_path=db)
        assert ok is True
        assert info["status"] == "OK"
        assert info["equity_sum"] == pytest.approx(5188.0, rel=1e-4)

    def test_violation_when_sum_exceeds_limit(self, tmp_path):
        """Sum > broker * 1.05 → ok=False, status=VIOLATION."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        # Claim $15,000 against broker equity of $5,213 → clear violation
        self._write_config(cfg_dir, "sp500", 5011.0)
        self._write_config(cfg_dir, "commodity_etfs", 5000.0)
        self._write_config(cfg_dir, "sector_etfs", 5000.0)
        self._seed_db(db, broker_equity=5213.4)

        # dry_run=True → Telegram NOT called regardless of violation
        ok, info = check_equity_config_sum(
            config_dir=cfg_dir, db_path=db, dry_run=True
        )
        assert ok is False
        assert info["status"] == "VIOLATION"
        assert info["equity_sum"] == pytest.approx(15011.0, rel=1e-4)

    def test_unknown_when_db_is_empty(self, tmp_path):
        """No broker equity snapshot → status=UNKNOWN, ok=True (non-fatal)."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        # Create empty DB (no market_equity_history rows)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE market_equity_history "
            "(id INTEGER PRIMARY KEY, date TEXT, market_id TEXT, "
            "allocated_equity REAL, broker_equity REAL, created_at TEXT)"
        )
        conn.commit()
        conn.close()

        self._write_config(cfg_dir, "sp500", 971.0)

        ok, info = check_equity_config_sum(config_dir=cfg_dir, db_path=db)
        assert ok is True  # cannot assess → non-fatal
        assert info["status"] == "UNKNOWN"

    def test_inactive_markets_not_counted_in_sum(self, tmp_path):
        """live_enabled=False configs are excluded from the equity sum."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        self._write_config(cfg_dir, "sp500", 971.0, live_enabled=True)
        self._write_config(cfg_dir, "crypto", 5000.0, live_enabled=False)  # inactive
        self._write_config(cfg_dir, "defensive_etfs", 5000.0, live_enabled=False)  # inactive
        self._seed_db(db, broker_equity=5213.4)

        ok, info = check_equity_config_sum(config_dir=cfg_dir, db_path=db)
        # Only sp500=$971 counted (crypto + defensive excluded)
        assert ok is True
        assert info["equity_sum"] == pytest.approx(971.0, rel=1e-4)
        assert "sp500" in info["active_markets"]
        assert "crypto" not in info["active_markets"]
        assert "defensive_etfs" not in info["active_markets"]

    def test_active_markets_with_zero_starting_equity_ok(self, tmp_path):
        """Active market with starting_equity=0 → excluded from sum (0 doesn't inflate)."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        self._write_config(cfg_dir, "sp500", 971.0, live_enabled=True)
        self._write_config(cfg_dir, "sector_etfs", 0.0, live_enabled=True)  # 0 equity
        self._seed_db(db, broker_equity=5213.4)

        ok, info = check_equity_config_sum(config_dir=cfg_dir, db_path=db)
        assert ok is True
        assert info["equity_sum"] == pytest.approx(971.0, rel=1e-4)

    def test_dry_run_does_not_call_send_message(self, tmp_path):
        """dry_run=True on violation prints text but doesn't call Telegram send_message."""
        from scripts.health_check import check_equity_config_sum

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        db = tmp_path / "atlas.db"

        self._write_config(cfg_dir, "sp500", 10000.0)  # clearly too high
        self._seed_db(db, broker_equity=5213.4)

        # dry_run=True: patch the send_message at its source module
        with patch("utils.telegram.send_message") as mock_tg:
            ok, info = check_equity_config_sum(
                config_dir=cfg_dir, db_path=db, dry_run=True
            )
        assert ok is False
        mock_tg.assert_not_called(), "send_message should NOT be called in dry_run=True mode"
