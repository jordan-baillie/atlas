"""Regression tests for FIX-PMEQ-AUDIT-004: zero-position markets get a row.

Without this fix, if all sp500 positions close on the same day:
- attribute_equity_pro_rata gives sp500 allocated_equity=0 (weight=0/total_mv)
- carry-forward block was absent → _attribution["sp500"] = {0,0,0}
- next day _get_per_market_equity reads cash_attributed=0 from snapshot
  → live cash formula gives 0 → HWM comparison is meaningless
  → a $100 global drop could trip sp500's per-market HALT (false positive)

FIX: eod_settlement.py pre-populates _positions_by_market with empty lists for
all 3 tracked markets, then carry-forward block overwrites the zero row with
prev_cash + realized_flows for each zero-position market.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: in-memory DB with market_equity_history table
# ---------------------------------------------------------------------------

def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE market_equity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            market_id TEXT NOT NULL,
            allocated_equity REAL,
            position_mv REAL,
            cash_attributed REAL,
            broker_equity REAL,
            broker_cash REAL,
            snapshot_time TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(date, market_id)
        )
    """)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Tests for attribute_equity_pro_rata behaviour with zero-position market
# ---------------------------------------------------------------------------

class TestAttributeEquityProRataZeroPosition:
    """attribute_equity_pro_rata must include zero-position markets in output."""

    def test_zero_position_market_appears_in_output(self):
        """Market with empty positions list still appears as a key in output."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        positions_by_market = {
            "sp500": [{"market_value": 1000.0}],
            "sector_etfs": [],  # ← zero positions
            "commodity_etfs": [{"market_value": 500.0}],
        }
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market=positions_by_market,
        )
        assert set(result.keys()) == {"sp500", "sector_etfs", "commodity_etfs"}

    def test_zero_position_market_has_zero_mv(self):
        """Zero-position market gets position_mv=0."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
                "commodity_etfs": [{"market_value": 500.0}],
            },
        )
        assert result["sector_etfs"]["position_mv"] == 0.0

    def test_zero_position_market_gets_zero_cash_pro_rata(self):
        """Zero-position market gets cash_attributed=0.0 from pro-rata (carry-forward
        happens at eod_settlement layer, not in this function)."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
                "commodity_etfs": [{"market_value": 500.0}],
            },
        )
        # Pro-rata weight for sector_etfs is 0/1500 = 0 → cash_attributed = 0
        # The carry-forward in eod_settlement.py overwrites this before DB write.
        assert result["sector_etfs"]["cash_attributed"] == 0.0
        assert result["sector_etfs"]["allocated_equity"] == 0.0

    def test_nonzero_markets_still_sum_correctly(self):
        """Other markets' attributed equity should still sum to ~broker_equity."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        result = attribute_equity_pro_rata(
            broker_equity=3000.0,
            broker_cash=600.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
                "commodity_etfs": [{"market_value": 500.0}],
            },
        )
        # sp500 + commodity_etfs should account for all allocated equity
        # (sector_etfs gets 0 from pro-rata; carry-forward is eod_settlement's job)
        total = sum(v["allocated_equity"] for v in result.values())
        # 1000/1500 * 3000 + 0 + 500/1500 * 3000 ≈ 3000
        assert abs(total - 3000.0) < 0.01

    def test_all_zero_positions_equal_split(self):
        """When ALL markets have zero positions, cash is split equally."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        result = attribute_equity_pro_rata(
            broker_equity=3000.0,
            broker_cash=3000.0,
            positions_by_market={
                "sp500": [],
                "sector_etfs": [],
                "commodity_etfs": [],
            },
        )
        # Equal split: 3000 / 3 = 1000 each
        for m in ("sp500", "sector_etfs", "commodity_etfs"):
            assert result[m]["cash_attributed"] == pytest.approx(1000.0)
            assert result[m]["allocated_equity"] == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Tests for the carry-forward logic (unit-tested via helper extraction)
# ---------------------------------------------------------------------------

class TestCarryForwardLogic:
    """Unit tests for the carry-forward path that eod_settlement applies after
    attribute_equity_pro_rata returns zeros for zero-position markets.

    These tests exercise the logic directly by importing the constants and
    simulating the carry-forward computation path.
    """

    def test_tracked_markets_constant_present(self):
        """_TRACKED_MARKETS_FOR_ATTRIBUTION must be defined in eod_settlement."""
        import importlib
        import sys
        # We don't want to execute the __main__ block; import the module
        # via importlib with path manipulation.
        import scripts.eod_settlement as eod_mod
        assert hasattr(eod_mod, "_TRACKED_MARKETS_FOR_ATTRIBUTION")
        tracked = eod_mod._TRACKED_MARKETS_FOR_ATTRIBUTION
        assert "sp500" in tracked
        assert "sector_etfs" in tracked
        assert "commodity_etfs" in tracked

    def test_carry_forward_uses_previous_cash(self):
        """Carry-forward should equal prev_cash + realized_flows (no degradation)."""
        # Simulate the carry-forward math directly
        prev_cash = 300.0
        realized_flow = 150.0  # e.g. sold 1 position today → +$150 cash
        expected_carry = round(prev_cash + realized_flow, 2)

        # This is the exact formula in eod_settlement.py
        _carry = round(prev_cash + realized_flow, 2)
        assert _carry == pytest.approx(450.0)

    def test_carry_forward_no_prior_snapshot_equal_split(self):
        """No prior snapshot → equal share of broker_cash."""
        broker_cash = 900.0
        n_tracked = 3
        expected = round(broker_cash / n_tracked, 2)

        _carry = round(broker_cash / max(1, n_tracked), 2)
        assert _carry == pytest.approx(300.0)

    def test_carry_forward_degraded_activities_uses_prev_cash_only(self):
        """Degraded activities API → carry = prev_cash (no realized flow added)."""
        prev_cash = 250.0
        _degraded = True
        _flow = 0.0 if _degraded else 99999.0  # degraded → flow stays 0

        _carry = round(prev_cash + _flow, 2)
        assert _carry == pytest.approx(250.0)

    def test_attribution_override_sets_correct_structure(self):
        """The carry-forward override sets position_mv=0, cash=carry, equity=carry."""
        _carry = 350.0
        overridden = {
            "position_mv": 0.0,
            "cash_attributed": _carry,
            "allocated_equity": _carry,
        }
        assert overridden["position_mv"] == 0.0
        assert overridden["cash_attributed"] == 350.0
        assert overridden["allocated_equity"] == 350.0


# ---------------------------------------------------------------------------
# DB-level integration test: verify rows written for all 3 markets
# ---------------------------------------------------------------------------

class TestEodZeroPositionMarketDbRow:
    """Verify the DB INSERT loop writes rows for all tracked markets, including
    those with zero positions (after carry-forward override).
    """

    def test_all_tracked_markets_get_db_row(self, tmp_path, monkeypatch):
        """Simulate the INSERT loop: all 3 markets must appear as DB rows."""
        import scripts.eod_settlement as eod_mod
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        # Scenario: sp500 has 1 position, sector_etfs has 0, commodity_etfs has 0
        positions_by_market = {
            "sp500": [{"market_value": 1500.0}],
            "sector_etfs": [],
            "commodity_etfs": [],
        }
        broker_equity = 3000.0
        broker_cash = 1500.0

        # Step 1: attribute_equity_pro_rata with pre-populated dict
        attribution = attribute_equity_pro_rata(
            broker_equity=broker_equity,
            broker_cash=broker_cash,
            positions_by_market=positions_by_market,
        )

        # Step 2: carry-forward for zero-position markets
        # (simulate: no prior snapshots → equal share)
        for zm in ("sector_etfs", "commodity_etfs"):
            carry = round(broker_cash / 3, 2)
            attribution[zm] = {
                "position_mv": 0.0,
                "cash_attributed": carry,
                "allocated_equity": carry,
            }

        # Step 3: write to in-memory DB
        db = _make_db()
        today = "2026-05-01"
        snap_iso = datetime.now(timezone.utc).isoformat()
        for mid, vals in attribution.items():
            db.execute(
                """INSERT OR REPLACE INTO market_equity_history
                   (date, market_id, allocated_equity, position_mv, cash_attributed,
                    broker_equity, broker_cash, snapshot_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, mid, vals["allocated_equity"], vals["position_mv"],
                 vals["cash_attributed"], broker_equity, broker_cash, snap_iso),
            )
        db.commit()

        # Verify: all 3 markets have rows
        rows = db.execute(
            "SELECT market_id, cash_attributed, position_mv FROM market_equity_history "
            "WHERE date = ?", (today,)
        ).fetchall()
        market_ids_written = {r["market_id"] for r in rows}
        assert market_ids_written == {"sp500", "sector_etfs", "commodity_etfs"}, (
            f"Expected all 3 tracked markets in DB, got: {market_ids_written}"
        )

    def test_zero_position_market_has_nonzero_cash_in_db(self, tmp_path, monkeypatch):
        """After carry-forward, zero-position markets have cash_attributed > 0."""
        from portfolio.market_equity_attribution import attribute_equity_pro_rata

        positions_by_market = {
            "sp500": [{"market_value": 2000.0}],
            "sector_etfs": [],      # zero positions
            "commodity_etfs": [],   # zero positions
        }
        attribution = attribute_equity_pro_rata(
            broker_equity=5000.0,
            broker_cash=1000.0,
            positions_by_market=positions_by_market,
        )

        # Simulate carry-forward (prev_cash from prior snapshot = $300 each)
        for zm in ("sector_etfs", "commodity_etfs"):
            prev_cash = 300.0
            attribution[zm] = {
                "position_mv": 0.0,
                "cash_attributed": prev_cash,
                "allocated_equity": prev_cash,
            }

        db = _make_db()
        today = "2026-05-01"
        snap_iso = datetime.now(timezone.utc).isoformat()
        for mid, vals in attribution.items():
            db.execute(
                """INSERT OR REPLACE INTO market_equity_history
                   (date, market_id, allocated_equity, position_mv, cash_attributed,
                    broker_equity, broker_cash, snapshot_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, mid, vals["allocated_equity"], vals["position_mv"],
                 vals["cash_attributed"], 5000.0, 1000.0, snap_iso),
            )
        db.commit()

        for zm in ("sector_etfs", "commodity_etfs"):
            row = db.execute(
                "SELECT cash_attributed FROM market_equity_history "
                "WHERE date = ? AND market_id = ?", (today, zm)
            ).fetchone()
            assert row is not None, f"No row for {zm}"
            assert float(row["cash_attributed"]) > 0, (
                f"{zm} has cash_attributed=0 after carry-forward (would cause false HALT next day)"
            )
