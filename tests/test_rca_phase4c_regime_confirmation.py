"""
tests/test_rca_phase4c_regime_confirmation.py

Phase 4C — N-day regime confirmation gate

Tests that:
- confirmation_days=1 (default) preserves instant-flip behaviour
- confirmation_days=2 requires 2 consecutive same-state raw classifications
- confirmation_days=3 requires 3 consecutive same-state raw classifications
- pending_state is populated while confirmation is building
- pending_state is NULL once confirmed
- pending_state is NULL when the pending change reverts before confirmation

All tests use an isolated in-memory / tmp SQLite DB (conftest _isolate_prod_db
fixture is autouse; tests also call init_db() explicitly to bootstrap the schema).

Run:
    cd /root/atlas
    python3 -m pytest tests/test_rca_phase4c_regime_confirmation.py -v --timeout=30
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import db.atlas_db as _adb
from db.atlas_db import get_regime_history, init_db, record_regime, upsert_macro_indicators
from regime.model import RegimeClassification, RegimeModel
from regime.states import RegimeState

# ──────────────────────────────────────────────────────────────────────────────
# Synthetic indicator sets (minimal — just enough to drive each state)
# ──────────────────────────────────────────────────────────────────────────────

BULL = {
    "spy_close": 500, "spy_200dma": 450,
    "spy_above_200dma": 1, "spy_200dma_slope": 0.05,
    "vix": 15, "vix3m": 17, "vix_term_ratio": 0.88,
    "credit_oas": 0.8,
    "yield_curve_10y2y": 1.5, "yield_curve_10y3m": 2.0,
    "dxy": 100, "gold_copper_ratio": 16,
}

BEAR = {
    "spy_close": 250, "spy_200dma": 350,
    "spy_above_200dma": 0, "spy_200dma_slope": -0.08,
    "vix": 55, "vix3m": 35, "vix_term_ratio": 1.57,
    "credit_oas": 3.5,
    "yield_curve_10y2y": -0.5, "yield_curve_10y3m": -1.0,
    "dxy": 108, "gold_copper_ratio": 28,
}


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _model_with_confirmation(tmp_db: str, confirmation_days: int) -> RegimeModel:
    """
    Return a RegimeModel with `regime_confirmation_days` overridden to
    *confirmation_days* and the DB pointed at *tmp_db*.
    """
    model = RegimeModel()
    model._config = dict(model._config)          # shallow copy
    model._config["regime_confirmation_days"] = confirmation_days
    return model


def _seed_history(date: str, state: str, pending: Optional[str] = None) -> None:
    """Insert a regime_history row directly, bypassing the confirmation gate."""
    record_regime(
        date=date,
        state=state,
        trend_score=0.5 if "bull" in state else -0.5,
        risk_score=0.5 if "bull" in state else -0.5,
        active_universes=["sp500"],
        sizing_multiplier=1.0,
        reasoning="seeded for test",
        enabled_strategies=["momentum_breakout"],
        model_version="v1",
        pending_state=pending,
    )


def _classify_date(model: RegimeModel, date: str, indicators: dict) -> RegimeClassification:
    """Insert indicators then classify_and_record for *date*."""
    upsert_macro_indicators(date, **indicators)
    return model.classify_and_record(date=date)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestInstantFlipDefault:
    """confirmation_days=1 → current behaviour preserved (instant flip)."""

    def test_default_no_confirmation_instant_flip(self, tmp_path):
        """
        With confirmation_days=1 (default OFF), a single BEAR classification
        immediately changes the confirmed regime from BULL to BEAR.
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=1)

            # Seed a BULL baseline in history.
            _seed_history("2026-01-01", "bull_risk_on")

            # Day 2: BEAR indicators — should flip immediately.
            result = _classify_date(model, "2026-01-02", BEAR)

            assert result.state == RegimeState.BEAR_CAPITULATION, (
                f"Expected instant flip to bear_capitulation, got {result.state}"
            )
            assert result.pending_state is None, "No pending_state on instant flip"

            rows = get_regime_history()
            # Most recent row (date DESC) is 2026-01-02.
            latest = next(r for r in rows if r["date"] == "2026-01-02")
            assert latest["regime_state"] == "bear_capitulation"
            assert latest["pending_state"] is None
        finally:
            _adb._db_path_override = None


class TestTwoDayConfirmation:
    """confirmation_days=2 — flip requires 2 consecutive same-state raw results."""

    def test_confirmation_two_days_two_consecutive_then_flip(self, tmp_path):
        """
        Sequence: BULL(day1) BEAR(day2) BEAR(day3)
        - Day 2: first BEAR — no flip, pending=bear
        - Day 3: second BEAR — confirmed, regime flips
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)

            # Seed BULL baseline.
            _seed_history("2026-01-01", "bull_risk_on")

            # Day 2: first BEAR raw — should NOT flip.
            r2 = _classify_date(model, "2026-01-02", BEAR)
            assert r2.state == RegimeState.BULL_RISK_ON, (
                f"Day 2: expected to stay bull_risk_on (pending), got {r2.state}"
            )
            assert r2.pending_state == "bear_capitulation", (
                f"Day 2: expected pending_state=bear_capitulation, got {r2.pending_state}"
            )

            # Day 3: second BEAR raw — confirmed!
            r3 = _classify_date(model, "2026-01-03", BEAR)
            assert r3.state == RegimeState.BEAR_CAPITULATION, (
                f"Day 3: expected flip to bear_capitulation, got {r3.state}"
            )
            assert r3.pending_state is None, (
                f"Day 3: pending_state should be NULL after confirmation"
            )

            # Verify regime_history row for day 3.
            rows = get_regime_history()
            d3 = next(r for r in rows if r["date"] == "2026-01-03")
            assert d3["regime_state"] == "bear_capitulation"
            assert d3["pending_state"] is None
        finally:
            _adb._db_path_override = None

    def test_confirmation_two_days_alternating_no_flip(self, tmp_path):
        """
        Sequence: BULL BEAR BULL BEAR — alternating, never 2 consecutive BEAR.
        Confirmed regime should remain BULL throughout.
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)

            # Seed BULL baseline.
            _seed_history("2026-01-01", "bull_risk_on")

            # Day 2: BEAR (first — pending only).
            r2 = _classify_date(model, "2026-01-02", BEAR)
            assert r2.state == RegimeState.BULL_RISK_ON, (
                f"Day 2: expected bull (pending), got {r2.state}"
            )

            # Day 3: BULL (revert — streak broken).
            r3 = _classify_date(model, "2026-01-03", BULL)
            assert r3.state == RegimeState.BULL_RISK_ON, (
                f"Day 3: expected bull (reverted), got {r3.state}"
            )

            # Day 4: BEAR again (first of new streak).
            r4 = _classify_date(model, "2026-01-04", BEAR)
            assert r4.state == RegimeState.BULL_RISK_ON, (
                f"Day 4: expected bull (pending again), got {r4.state}"
            )

            # Across all 4 days, confirmed regime never flipped.
            rows = get_regime_history()
            for row in rows:
                if row["date"] != "2026-01-01":
                    # Day 2 and 4 should still be bull_risk_on in regime_state.
                    if row["date"] in ("2026-01-02", "2026-01-04"):
                        assert row["regime_state"] == "bull_risk_on", (
                            f"{row['date']}: expected bull_risk_on, got {row['regime_state']}"
                        )
        finally:
            _adb._db_path_override = None

    def test_flip_happens_on_second_not_first_bear(self, tmp_path):
        """
        Explicitly verify: day 2 (first BEAR) = no flip; day 3 (second BEAR) = flip.
        This is the core RCA4C requirement.
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)
            _seed_history("2026-01-01", "bull_risk_on")

            r2 = _classify_date(model, "2026-01-02", BEAR)
            # Must NOT have flipped yet.
            assert r2.state != RegimeState.BEAR_CAPITULATION, (
                "Premature flip on first BEAR day"
            )

            r3 = _classify_date(model, "2026-01-03", BEAR)
            # Must flip on second consecutive BEAR.
            assert r3.state == RegimeState.BEAR_CAPITULATION, (
                f"Expected flip on second consecutive BEAR day, got {r3.state}"
            )
        finally:
            _adb._db_path_override = None


class TestThreeDayConfirmation:
    """confirmation_days=3 — requires 3 consecutive same-state raw results."""

    def test_confirmation_three_days_two_consecutive_no_flip(self, tmp_path):
        """
        Sequence: BULL BEAR BEAR with confirmation_days=3.
        After 2 BEARs, regime has NOT flipped yet (need 3).
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=3)
            _seed_history("2026-01-01", "bull_risk_on")

            # Day 2: first BEAR.
            r2 = _classify_date(model, "2026-01-02", BEAR)
            assert r2.state == RegimeState.BULL_RISK_ON, f"Day 2: got {r2.state}"

            # Day 3: second BEAR — still not confirmed (need 3).
            r3 = _classify_date(model, "2026-01-03", BEAR)
            assert r3.state == RegimeState.BULL_RISK_ON, (
                f"Day 3: expected still bull (only 2/3), got {r3.state}"
            )
            assert r3.pending_state == "bear_capitulation", (
                f"Day 3: pending_state should still be bear_capitulation, got {r3.pending_state}"
            )
        finally:
            _adb._db_path_override = None

    def test_confirmation_three_days_three_consecutive_then_flip(self, tmp_path):
        """
        Sequence: BULL BEAR BEAR BEAR with confirmation_days=3.
        Flip must occur on day 4 (third consecutive BEAR).
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=3)
            _seed_history("2026-01-01", "bull_risk_on")

            _classify_date(model, "2026-01-02", BEAR)   # 1/3
            _classify_date(model, "2026-01-03", BEAR)   # 2/3

            r4 = _classify_date(model, "2026-01-04", BEAR)   # 3/3 → flip
            assert r4.state == RegimeState.BEAR_CAPITULATION, (
                f"Day 4: expected flip after 3 consecutive BEAR days, got {r4.state}"
            )
            assert r4.pending_state is None
        finally:
            _adb._db_path_override = None


class TestPendingStateField:
    """Verify pending_state is correctly set in return value and DB."""

    def test_pending_regime_field_populated_during_confirmation(self, tmp_path):
        """
        When raw differs from confirmed but not yet confirmed,
        result.pending_state = raw.state.value and DB pending_state = raw.state.
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)
            _seed_history("2026-01-01", "bull_risk_on")

            result = _classify_date(model, "2026-01-02", BEAR)

            # Return value.
            assert result.pending_state == "bear_capitulation", (
                f"Expected pending_state='bear_capitulation', got {result.pending_state!r}"
            )

            # DB row.
            rows = get_regime_history()
            d2 = next(r for r in rows if r["date"] == "2026-01-02")
            assert d2["pending_state"] == "bear_capitulation"
            assert d2["regime_state"] == "bull_risk_on"   # confirmed unchanged
        finally:
            _adb._db_path_override = None

    def test_pending_regime_cleared_on_confirmation(self, tmp_path):
        """
        Once confirmed (2nd consecutive BEAR), pending_state becomes NULL
        in both the return value and the DB row.
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)
            _seed_history("2026-01-01", "bull_risk_on")

            _classify_date(model, "2026-01-02", BEAR)           # pending
            result = _classify_date(model, "2026-01-03", BEAR)  # confirmed

            # Return value: no pending state.
            assert result.pending_state is None, (
                f"Confirmed: expected pending_state=None, got {result.pending_state!r}"
            )

            # DB row for confirmation day.
            rows = get_regime_history()
            d3 = next(r for r in rows if r["date"] == "2026-01-03")
            assert d3["pending_state"] is None
            assert d3["regime_state"] == "bear_capitulation"
        finally:
            _adb._db_path_override = None

    def test_pending_regime_cleared_on_revert(self, tmp_path):
        """
        Sequence: BULL → BEAR (pending) → BULL (revert).
        After the BULL revert, pending_state = NULL (streak broken,
        no longer tracking a BEAR change).
        """
        db = str(tmp_path / "atlas.db")
        init_db(db)
        _adb._db_path_override = db
        try:
            model = _model_with_confirmation(db, confirmation_days=2)
            _seed_history("2026-01-01", "bull_risk_on")

            _classify_date(model, "2026-01-02", BEAR)   # pending bear

            # Day 3: reverts to BULL.
            result = _classify_date(model, "2026-01-03", BULL)

            # BULL raw == confirmed (bull_risk_on) — no pending change.
            assert result.pending_state is None, (
                f"Revert: expected pending_state=None, got {result.pending_state!r}"
            )
            assert result.state == RegimeState.BULL_RISK_ON

            rows = get_regime_history()
            d3 = next(r for r in rows if r["date"] == "2026-01-03")
            assert d3["pending_state"] is None
        finally:
            _adb._db_path_override = None


class TestDefaultConfig:
    """Verify the production config ships with regime_confirmation_days=1 (OFF)."""

    def test_default_config_has_confirmation_days_1(self):
        """
        config/active/regime.json must have regime_confirmation_days=1.
        This ensures default behaviour is instant flip (no confirmation).
        """
        cfg_path = PROJECT / "config" / "active" / "regime.json"
        assert cfg_path.exists(), "regime.json not found"
        cfg = json.loads(cfg_path.read_text())
        assert "regime_confirmation_days" in cfg, (
            "regime_confirmation_days key missing from regime.json"
        )
        assert cfg["regime_confirmation_days"] == 1, (
            f"Expected regime_confirmation_days=1 (OFF), "
            f"got {cfg['regime_confirmation_days']}"
        )

    def test_model_reads_confirmation_days_from_config(self):
        """RegimeModel correctly reads regime_confirmation_days from its config."""
        model = RegimeModel()
        assert model._config.get("regime_confirmation_days") == 1
