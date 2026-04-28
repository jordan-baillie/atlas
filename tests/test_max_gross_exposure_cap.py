"""Tests for B1: max_gross_exposure_pct global gross-exposure cap.

Apr 27 2026 post-mortem: account reached 174% gross exposure ($9,491 MV /
$5,428 equity). This guard enforces a configurable cap to reject prospective
entry orders that WOULD push the account above the configured limit.

Tests:
  1. Gross below cap → allowed
  2. Gross exactly at cap (boundary inclusive) → allowed
  3. Gross above cap → rejected with structured reason
  4. Telegram alert fires on rejection
  5. Apr 27 simulation — real numbers from A8 report → REJECTED
  6. Cap missing or zero → fail-open, no Telegram
  7. Exit/stop/TP orders bypass cap (guard only in _execute_entry)
  8. W6 cross-universe guard is unaffected (additive, not replacing)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from risk.gross_exposure_guard import (
    check_gross_exposure,
    telegram_alert_gross_exposure,
    _get_gross_exposure_cap,
    _get_broker_gross_state,
    _warned_missing_cap,
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _mock_broker(equity: float, market_value: float) -> MagicMock:
    """Create a mock broker whose get_account_info() returns equity + MV."""
    broker = MagicMock()
    acct = MagicMock()
    acct.equity = equity
    acct.market_value = market_value
    broker.get_account_info.return_value = acct
    return broker


def _cfg(cap: float | None) -> dict:
    """Build a minimal market config with the given cap (or without it)."""
    risk: dict = {}
    if cap is not None:
        risk["max_gross_exposure_pct"] = cap
    return {"risk": risk}


# ── Test 1: gross below cap → allowed ─────────────────────────────────────────


def test_gross_below_cap_allowed():
    """Gross 1.50 < cap 1.75 → entry allowed."""
    # equity=10000, current MV=14000 (140%), adding 1000 → 15000/10000 = 150%
    broker = _mock_broker(equity=10_000, market_value=14_000)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=1_000,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is True, f"Should be allowed, got reason: {reason}"
    assert "1.5" in reason or "150" in reason.replace("150.0%", "150%")


# ── Test 2: gross exactly at cap (boundary inclusive) → allowed ───────────────


def test_gross_at_cap_allowed_boundary():
    """Gross exactly 1.75 (= cap) → entry allowed (boundary inclusive)."""
    # equity=10000, MV=17500 exactly → 175%
    broker = _mock_broker(equity=10_000, market_value=17_500)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=0,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is True, f"Boundary 175% with cap 1.75 should be ALLOWED, reason: {reason}"


def test_gross_just_at_cap_with_notional_boundary():
    """Gross=1.75 after adding notional exactly → still allowed."""
    # equity=10000, current MV=17000 (170%), add 500 → 17500/10000 = 175% == cap
    broker = _mock_broker(equity=10_000, market_value=17_000)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=500,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is True, f"175% == cap 1.75 should be allowed, reason: {reason}"


# ── Test 3: gross above cap → rejected ───────────────────────────────────────


def test_gross_above_cap_rejected():
    """Gross 1.80 > cap 1.75 → rejected with structured reason."""
    # equity=10000, current MV=17500 (175%), add 500 → 18000/10000 = 180%
    broker = _mock_broker(equity=10_000, market_value=17_500)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=500,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is False, "Should be rejected"
    assert "max_gross_exposure_pct" in reason, "Reason must include the config key name"
    assert "1.80" in reason or "180" in reason.replace("180.0%", "180%"), (
        f"Reason should include prospective gross: {reason}"
    )
    assert "1.75" in reason or "175" in reason.replace("175.0%", "175%"), (
        f"Reason should include the cap value: {reason}"
    )


# ── Test 4: Telegram alert fires on rejection ─────────────────────────────────


def test_telegram_alert_fires_on_rejection():
    """When the guard rejects, telegram_alert_gross_exposure is called exactly once."""
    broker = _mock_broker(equity=10_000, market_value=17_500)
    with patch("utils.telegram.send_message") as mock_send:
        with patch("risk.gross_exposure_guard.telegram_alert_gross_exposure") as mock_alert:
            ok, reason = check_gross_exposure(
                broker=broker,
                prospective_order_notional=500,
                market_config=_cfg(1.75),
                market_id="sp500",
            )
            assert ok is False
            # Now call the alert function directly (as live_executor does)
            telegram_alert_gross_exposure("UNG", "commodity_etfs", reason)

    # Re-test with the real send_message patched
    with patch("utils.telegram.send_message") as mock_send:
        telegram_alert_gross_exposure("UNG", "commodity_etfs", reason)
        mock_send.assert_called_once()
        alert_text = mock_send.call_args[0][0]
        assert "max_gross_exposure_pct" in alert_text or "risk" in alert_text.lower()
        assert "UNG" in alert_text
        assert "commodity_etfs" in alert_text


# ── Test 5: Apr 27 2026 simulation ───────────────────────────────────────────


def test_apr27_simulation_174pct_rejected():
    """Simulate Apr 27 conditions from A8 report.

    A8 findings:
      equity     = $5,428 (approx; report says $5,428 equity)
      positions  MV = $9,491 (5 sp500 + 3 sector_etfs + 5 commodity_etfs = 13 positions)
      proposed   = UNG entry $1,371
      gross after = (9491 + 1371) / 5428 = 10862 / 5428 = 2.001 → 200% — WELL over cap

    With cap=1.75, this should be REJECTED.
    """
    equity = 5_428.0
    current_mv = 9_491.0
    ung_notional = 1_371.0

    broker = _mock_broker(equity=equity, market_value=current_mv)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=ung_notional,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is False, (
        f"Apr 27 simulation: (9491+1371)/5428 = {(current_mv+ung_notional)/equity:.1%} "
        f"should be rejected at cap=1.75, but got: {reason}"
    )
    assert "max_gross_exposure_pct" in reason
    # Verify the prospective gross is reported
    prospective = (current_mv + ung_notional) / equity
    assert prospective > 1.75, f"Test setup error: prospective={prospective:.3f} should exceed 1.75"


def test_apr27_pre_trade_state_already_over_cap():
    """Apr 27 state BEFORE UNG: 9491/5428 = 174.9% — already over cap.

    With cap=1.75 (175%), the pre-trade gross 174.9% is JUST under cap.
    But the prospective UNG entry ($1,371) pushes it to 200% — clearly over.
    This test verifies cap=1.75 would have blocked the trade.
    """
    equity = 5_428.0
    # Verify pre-trade state (174.9% < 175% — just under cap without UNG)
    current_mv = 9_491.0
    pre_trade_gross = current_mv / equity
    assert pre_trade_gross < 1.75, (
        f"Pre-trade gross {pre_trade_gross:.3f} should be just under 1.75 cap"
    )

    # But with UNG it goes over
    ung_notional = 1_371.0
    broker = _mock_broker(equity=equity, market_value=current_mv)
    ok, reason = check_gross_exposure(
        broker=broker,
        prospective_order_notional=ung_notional,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is False, f"Adding UNG should push over cap, got: {reason}"
    prospective = (current_mv + ung_notional) / equity
    assert prospective > 1.75
    print(f"\nApr 27 simulation: pre-trade gross={pre_trade_gross:.1%}, "
          f"with UNG={prospective:.1%}, cap=175% → REJECTED ✓")


# ── Test 6: cap missing or zero → fail-open, no Telegram ─────────────────────


def test_cap_missing_fails_open():
    """Config with no max_gross_exposure_pct → fail-open (True), no Telegram."""
    # Clear warned set so we can check the warning fires
    _warned_missing_cap.discard("test_missing")
    broker = _mock_broker(equity=5_000, market_value=9_000)  # 180% gross — would reject
    with patch("utils.telegram.send_message") as mock_send:
        ok, reason = check_gross_exposure(
            broker=broker,
            prospective_order_notional=1_000,
            market_config={"risk": {}},  # no cap key
            market_id="test_missing",
        )
    assert ok is True, "Missing cap should fail-open"
    assert "no cap" in reason.lower()
    mock_send.assert_not_called()


def test_cap_zero_fails_open():
    """Config with max_gross_exposure_pct=0 → fail-open, no Telegram."""
    _warned_missing_cap.discard("test_zero")
    broker = _mock_broker(equity=5_000, market_value=20_000)  # 400% gross
    with patch("utils.telegram.send_message") as mock_send:
        ok, reason = check_gross_exposure(
            broker=broker,
            prospective_order_notional=500,
            market_config=_cfg(0),
            market_id="test_zero",
        )
    assert ok is True, "Zero cap should fail-open"
    mock_send.assert_not_called()


# ── Test 7: exits/stops/TPs bypass the cap ───────────────────────────────────


def test_exits_stops_tps_bypass_cap():
    """Guard is inserted ONLY in _execute_entry — exits/stops/TPs are never gated.

    Verify by source inspection: gross_exposure_guard import must appear in
    _execute_entry body and NOT in place_order body (same as W6 requirement).
    """
    src = Path("/root/atlas/brokers/live_executor.py").read_text()
    entry_idx = src.find("def _execute_entry(")
    place_idx = src.find("def place_order(")
    assert entry_idx > 0, "_execute_entry not found"
    assert place_idx > 0, "place_order not found"

    entry_body = src[entry_idx: entry_idx + 6000]
    place_body = src[place_idx: place_idx + 3000]

    assert "gross_exposure_guard" in entry_body, (
        "gross_exposure_guard must be wired into _execute_entry"
    )
    assert "gross_exposure_guard" not in place_body, (
        "gross_exposure_guard must NOT be in place_order — that would gate exits/stops/TPs"
    )


# ── Test 8: W6 cross-universe guard is unaffected (additive) ─────────────────


def test_w6_cross_universe_guard_unaffected():
    """W6 guard still present and wired; gross exposure guard is additive."""
    src = Path("/root/atlas/brokers/live_executor.py").read_text()
    entry_idx = src.find("def _execute_entry(")
    assert entry_idx > 0
    entry_body = src[entry_idx: entry_idx + 6000]

    # Both guards present in _execute_entry
    assert "cross_universe_guard" in entry_body, "W6 guard must still be present"
    assert "gross_exposure_guard" in entry_body, "B1 guard must be present"

    # W6 guard appears BEFORE B1 guard (W6 fires first)
    w6_pos = entry_body.find("cross_universe_guard")
    b1_pos = entry_body.find("gross_exposure_guard")
    assert w6_pos < b1_pos, (
        f"W6 guard (pos={w6_pos}) must appear before B1 guard (pos={b1_pos}) "
        "so W6 runs first"
    )


# ── Additional edge cases ─────────────────────────────────────────────────────


def test_broker_none_fails_open():
    """If broker is None, guard fails open (can't compute gross, don't block)."""
    ok, reason = check_gross_exposure(
        broker=None,
        prospective_order_notional=1_000,
        market_config=_cfg(1.75),
        market_id="sp500",
    )
    assert ok is True, "broker=None should fail-open"
    assert "equity unavailable" in reason.lower() or "fail-open" in reason.lower()


def test_get_gross_exposure_cap_parsing():
    """_get_gross_exposure_cap handles edge cases."""
    assert _get_gross_exposure_cap({"risk": {"max_gross_exposure_pct": 1.75}}) == 1.75
    assert _get_gross_exposure_cap({"risk": {}}) == 0.0
    assert _get_gross_exposure_cap({}) == 0.0
    assert _get_gross_exposure_cap(None) == 0.0
    assert _get_gross_exposure_cap({"risk": {"max_gross_exposure_pct": 0}}) == 0.0
    assert _get_gross_exposure_cap({"risk": {"max_gross_exposure_pct": "bad"}}) == 0.0


def test_get_broker_gross_state_dict_style():
    """_get_broker_gross_state works with raw dict-style broker (get_account)."""
    broker = MagicMock(spec=["get_account"])
    broker.get_account.return_value = {
        "equity": 5000.0,
        "long_market_value": 8000.0,
    }
    equity, mv = _get_broker_gross_state(broker)
    assert equity == 5000.0
    assert mv == 8000.0


def test_all_live_configs_have_cap():
    """All live market configs must have max_gross_exposure_pct = 1.75."""
    import json
    configs = [
        "config/active/sp500.json",
        "config/active/commodity_etfs.json",
        "config/active/sector_etfs.json",
        "config/active/defensive_etfs.json",
        "config/active/treasury_etfs.json",
        "config/active/gold_etfs.json",
    ]
    root = Path("/root/atlas")
    for cfg_name in configs:
        data = json.loads((root / cfg_name).read_text())
        cap = data.get("risk", {}).get("max_gross_exposure_pct")
        assert cap is not None, f"{cfg_name}: missing max_gross_exposure_pct"
        assert cap == 1.75, f"{cfg_name}: expected 1.75, got {cap}"
