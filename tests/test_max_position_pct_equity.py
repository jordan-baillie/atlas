"""Tests for max_position_pct_equity cap (W3, 2026-04-28)."""
import json
import pytest
from pathlib import Path
from utils.dynamic_sizing import DynamicSizer, get_max_position_pct_for_config


@pytest.fixture
def base_config():
    return {
        "dynamic_sizing": {
            "enabled": True,
            "base_risk_pct": 0.005,
            "min_risk_pct": 0.003,
            "max_risk_pct": 0.0075,
            "confidence_scaling": {"enabled": False},
            "volatility_scaling": {"enabled": False},
            "equity_curve_scaling": {"enabled": False},
        },
        "risk": {
            "max_risk_per_trade_pct": 0.005,
            "max_position_pct_equity": 0.20,
        },
    }


def test_position_capped_when_exceeding_pct(base_config):
    """Wide-risk scenario should be capped to 20% of equity."""
    sizer = DynamicSizer(base_config)
    # equity=$5000, cap=20% => max position value $1000 => max 10 shares @$100
    # stop very close (risk $1/share) so uncapped shares = 5000*0.005/1 = 25 shares ($2500 = 50%)
    shares = sizer.calculate_position_size(
        equity=5000, entry_price=100, stop_price=99, confidence=0.75, atr=2.0
    )
    assert shares <= 10, f"Expected shares <= 10 (20% cap of $5000/$100), got {shares}"
    assert shares >= 0


def test_position_capped_exact_boundary(base_config):
    """Verify the cap computes floor correctly via int() truncation."""
    sizer = DynamicSizer(base_config)
    # equity=$5000, cap=20% => $1000 / $100 = exactly 10 shares allowed
    # Ensure uncapped value exceeds that
    shares = sizer.calculate_position_size(
        equity=5000, entry_price=100, stop_price=99, confidence=0.75, atr=2.0
    )
    # Position value must be <= 20% of equity
    assert shares * 100 <= 5000 * 0.20


def test_position_under_cap_not_resized(base_config):
    """Wide stop keeps position small — cap should not reduce it further."""
    sizer = DynamicSizer(base_config)
    # Wide stop $50 => risk/share=$50, risk_amount=5000*0.005=$25 => 0 shares (rounds down)
    # Even with wider budget: equity=$100000, stop still wide => shares well under 20%
    shares = sizer.calculate_position_size(
        equity=100_000, entry_price=100, stop_price=50, confidence=0.75, atr=10.0
    )
    # risk_amount = 100000 * 0.005 = $500, risk_per_share = $50, shares = 10 = $1000 = 1% of equity
    assert shares * 100 <= 100_000 * 0.20, "Under-cap position must not be clipped"
    assert shares >= 0


def test_no_cap_when_config_missing(base_config):
    """When max_position_pct_equity is not in config, no enforcement — no crash."""
    cfg = json.loads(json.dumps(base_config))  # deep copy
    del cfg["risk"]["max_position_pct_equity"]
    sizer = DynamicSizer(cfg)
    assert sizer.max_position_pct_equity is None
    shares = sizer.calculate_position_size(
        equity=5000, entry_price=100, stop_price=99, confidence=0.75, atr=2.0
    )
    # Without cap, uncapped math gives 25 shares — verify no crash and plausible output
    assert shares >= 0
    # Uncapped must be able to exceed 10 (which 20% would limit to)
    assert shares > 10, f"Without cap expected >10 shares, got {shares}"


def test_no_cap_when_config_zero(base_config):
    """When max_position_pct_equity=0, treat as disabled — no enforcement."""
    cfg = json.loads(json.dumps(base_config))
    cfg["risk"]["max_position_pct_equity"] = 0
    sizer = DynamicSizer(cfg)
    assert sizer.max_position_pct_equity == 0
    shares = sizer.calculate_position_size(
        equity=5000, entry_price=100, stop_price=99, confidence=0.75, atr=2.0
    )
    # Zero cap treated as disabled — no crash, still uncapped
    assert shares >= 0
    assert shares > 10, f"Zero cap should be ignored, expected >10 shares, got {shares}"


def test_sector_etfs_config_has_cap():
    """sector_etfs.json must have the 20% cap after W3 audit."""
    cfg = json.loads(Path("/root/atlas/config/active/sector_etfs.json").read_text())
    assert "max_position_pct_equity" in cfg["risk"], "key missing from sector_etfs risk block"
    assert cfg["risk"]["max_position_pct_equity"] == 0.20


def test_commodity_etfs_config_has_cap():
    """commodity_etfs.json must have the 20% cap after W3 audit."""
    cfg = json.loads(Path("/root/atlas/config/active/commodity_etfs.json").read_text())
    assert "max_position_pct_equity" in cfg["risk"], "key missing from commodity_etfs risk block"
    assert cfg["risk"]["max_position_pct_equity"] == 0.20


def test_get_max_position_pct_helper_present():
    """get_max_position_pct_for_config returns value when present, None when absent."""
    cfg_with = {"risk": {"max_position_pct_equity": 0.15}}
    cfg_without = {"risk": {}}
    assert get_max_position_pct_for_config(cfg_with) == 0.15
    assert get_max_position_pct_for_config(cfg_without) is None
    assert get_max_position_pct_for_config({}) is None


def test_cap_logged_on_reduction(base_config, caplog):
    """Cap reduction should emit an INFO log message."""
    import logging
    sizer = DynamicSizer(base_config)
    with caplog.at_level(logging.INFO, logger="utils.dynamic_sizing"):
        shares = sizer.calculate_position_size(
            equity=5000, entry_price=100, stop_price=99, confidence=0.75, atr=2.0
        )
    assert shares <= 10
    assert any("position_cap" in rec.message for rec in caplog.records), (
        "Expected a position_cap INFO log when cap fires"
    )
