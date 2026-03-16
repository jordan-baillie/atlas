"""Tests for volume-aware slippage model (Task 3).

Verifies the ``_apply_slippage`` method on BacktestEngine supports both
``"fixed"`` (original behaviour) and ``"volume_aware"`` modes.

Key properties being tested:
  - Fixed mode: effective slippage == slippage_pct regardless of order_shares/bar_volume
  - Volume-aware: slippage scales up with participation (order_shares / bar_volume)
  - Volume-aware + zero volume: falls back to fixed slippage
  - Volume-aware + zero shares: falls back to fixed slippage
  - Slippage floored at 0.0001 and capped at 0.02 in volume-aware mode
  - Direction handling: buy raises price, sell lowers price (both modes)

No network calls; tests instantiate BacktestEngine directly with minimal configs.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from backtest.engine import BacktestEngine

# ---------------------------------------------------------------------------
# Helper: build a minimal engine with specific fee settings
# ---------------------------------------------------------------------------

def _make_engine(slippage_pct: float = 0.001,
                 slippage_model: str = "fixed",
                 slippage_impact_exponent: float = 0.5) -> BacktestEngine:
    config = {
        "market": "sp500",
        "risk": {
            "starting_equity": 10_000.0,
            "leverage": 1.0,
            "max_risk_per_trade_pct": 0.01,
            "max_open_positions": 5,
            "max_sector_concentration": 5,
            "max_daily_drawdown_pct": 0.10,
            "require_stop_loss": True,
            "require_planned_exit": True,
            "min_confidence": 0.0,
        },
        "fees": {
            "commission_per_trade": 0,
            "commission_pct": 0.0,
            "slippage_pct": slippage_pct,
            "slippage_model": slippage_model,
            "slippage_impact_exponent": slippage_impact_exponent,
            "min_position_value": 0.0,
            "flat_fee_threshold": 0,
        },
        "trading": {
            "mode": "paper",
            "broker": "alpaca",
            "live_enabled": False,
            "live_safety": {"max_order_value": 0, "max_daily_orders": 100},
        },
        "backtest": {
            "train_window_days": 60,
            "test_window_days": 30,
            "step_days": 10,
            "min_history_days": 60,
        },
        "data": {
            "source": "yfinance",
            "history_years": 1,
            "cache_dir": "data/cache",
            "raw_dir": "data/raw",
            "processed_dir": "data/processed",
        },
        "allocation": {"enabled": False, "mode": "soft_pool", "overflow_enabled": True, "pools": {}},
        "universe": {
            "method": "top_liquid",
            "top_n": 10,
            "min_median_daily_value": 0,
            "min_price": 0.0,
            "min_market_cap": 0,
            "exclusions": [],
            "benchmark_ticker": "SPY",
        },
    }
    # Patch download_ticker so __init__ can load the benchmark without network
    import pandas as pd
    dummy = pd.DataFrame(
        {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1_000_000]},
        index=pd.date_range("2023-01-02", periods=1, freq="B"),
    )
    with patch("backtest.engine.download_ticker", return_value=dummy):
        engine = BacktestEngine(config, market_id="sp500")
    return engine


# ---------------------------------------------------------------------------
# Fixed mode tests
# ---------------------------------------------------------------------------

class TestFixedSlippage:
    """_apply_slippage with slippage_model='fixed'."""

    def test_buy_increases_price(self):
        engine = _make_engine(slippage_pct=0.001, slippage_model="fixed")
        price = 100.0
        result = engine._apply_slippage(price, "buy")
        assert result == pytest.approx(100.1)

    def test_sell_decreases_price(self):
        engine = _make_engine(slippage_pct=0.001, slippage_model="fixed")
        price = 100.0
        result = engine._apply_slippage(price, "sell")
        assert result == pytest.approx(99.9)

    def test_fixed_ignores_order_shares(self):
        """In fixed mode, passing order_shares should not change the result."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="fixed")
        price = 100.0
        r_no_shares = engine._apply_slippage(price, "buy")
        r_with_shares = engine._apply_slippage(price, "buy", order_shares=500, bar_volume=1000)
        assert r_no_shares == pytest.approx(r_with_shares)

    def test_fixed_ignores_bar_volume(self):
        """In fixed mode, bar_volume has no effect."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="fixed")
        price = 100.0
        r_low_vol = engine._apply_slippage(price, "buy", order_shares=100, bar_volume=200)
        r_high_vol = engine._apply_slippage(price, "buy", order_shares=100, bar_volume=2_000_000)
        assert r_low_vol == pytest.approx(r_high_vol)

    def test_fixed_zero_slippage(self):
        """slippage_pct=0 → fill price equals input price."""
        engine = _make_engine(slippage_pct=0.0, slippage_model="fixed")
        price = 150.0
        assert engine._apply_slippage(price, "buy") == pytest.approx(price)
        assert engine._apply_slippage(price, "sell") == pytest.approx(price)


# ---------------------------------------------------------------------------
# Volume-aware mode tests
# ---------------------------------------------------------------------------

class TestVolumeAwareSlippage:
    """_apply_slippage with slippage_model='volume_aware'."""

    def test_buy_raises_price(self):
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware")
        result = engine._apply_slippage(100.0, "buy", order_shares=10, bar_volume=1_000)
        assert result > 100.0, "Volume-aware buy should raise price above 100"

    def test_sell_lowers_price(self):
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware")
        result = engine._apply_slippage(100.0, "sell", order_shares=10, bar_volume=1_000)
        assert result < 100.0, "Volume-aware sell should lower price below 100"

    def test_slippage_increases_with_participation(self):
        """Higher order_shares/bar_volume ratio → higher slippage."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware",
                              slippage_impact_exponent=0.5)
        price = 100.0
        # Low participation (1 %): order=10, volume=1000
        fill_low = engine._apply_slippage(price, "buy", order_shares=10, bar_volume=1_000)
        # High participation (50 %): order=500, volume=1000
        fill_high = engine._apply_slippage(price, "buy", order_shares=500, bar_volume=1_000)
        # Higher participation → worse fill (higher buy price)
        assert fill_high > fill_low, (
            f"High participation ({fill_high:.6f}) should give worse fill than "
            f"low participation ({fill_low:.6f})"
        )

    def test_fallback_to_fixed_when_bar_volume_zero(self):
        """Volume-aware with bar_volume=0 must fall back to fixed slippage."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware")
        price = 100.0
        result_zero_vol = engine._apply_slippage(price, "buy", order_shares=100, bar_volume=0)
        result_fixed = engine._apply_slippage(price, "buy")  # fixed mode baseline
        assert result_zero_vol == pytest.approx(result_fixed), (
            "Zero bar_volume should fall back to fixed slippage"
        )

    def test_fallback_to_fixed_when_order_shares_zero(self):
        """Volume-aware with order_shares=0 must fall back to fixed slippage."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware")
        price = 100.0
        result_zero_shares = engine._apply_slippage(price, "sell", order_shares=0, bar_volume=10_000)
        result_fixed = engine._apply_slippage(price, "sell")  # fixed mode baseline
        assert result_zero_shares == pytest.approx(result_fixed), (
            "Zero order_shares should fall back to fixed slippage"
        )

    def test_slippage_floor_applied(self):
        """Effective slippage is floored at 0.0001 even for tiny participation."""
        engine = _make_engine(slippage_pct=0.001, slippage_model="volume_aware",
                              slippage_impact_exponent=0.5)
        price = 100.0
        # Tiny participation: 1 share out of 1 000 000 → raw slippage would be very small
        result = engine._apply_slippage(price, "buy", order_shares=1, bar_volume=1_000_000)
        # Effective slippage ≥ 0.0001 → fill price ≥ 100 * (1 + 0.0001) = 100.01
        floor_fill = price * (1 + 0.0001)
        assert result >= floor_fill - 1e-9, (
            f"Slippage floor of 0.0001 not applied; fill={result:.6f}, floor={floor_fill:.6f}"
        )

    def test_slippage_cap_applied(self):
        """Effective slippage is capped at 0.02 (2 %) even for huge participation."""
        engine = _make_engine(slippage_pct=0.1, slippage_model="volume_aware",
                              slippage_impact_exponent=0.5)
        price = 100.0
        # Full-bar order: participation=1, raw=0.1*1.0=0.1 → capped at 0.02
        result = engine._apply_slippage(price, "buy", order_shares=1_000, bar_volume=1_000)
        cap_fill = price * 1.02
        assert result <= cap_fill + 1e-9, (
            f"Slippage cap of 0.02 not applied; fill={result:.4f}, cap_fill={cap_fill:.4f}"
        )
        # Also verify it's not using the uncapped value (which would be 110.0)
        assert result < 105.0

    def test_volume_aware_participates_correctly(self):
        """Check the actual formula: eff_slip = slip_pct * (shares/vol)^exp."""
        slippage_pct = 0.001
        exponent = 0.5
        order_shares = 100
        bar_volume = 10_000
        engine = _make_engine(slippage_pct=slippage_pct, slippage_model="volume_aware",
                              slippage_impact_exponent=exponent)
        price = 200.0
        participation = order_shares / bar_volume  # 0.01
        expected_slip = slippage_pct * (participation ** exponent)  # 0.001 * 0.1 = 0.0001
        expected_slip = max(0.0001, min(0.02, expected_slip))
        expected_fill = price * (1 + expected_slip)  # buy

        result = engine._apply_slippage(
            price, "buy", order_shares=order_shares, bar_volume=bar_volume
        )
        assert result == pytest.approx(expected_fill, rel=1e-9)

    def test_slippage_model_attribute_loaded(self):
        """BacktestEngine reads slippage_model and slippage_impact_exponent from config."""
        engine = _make_engine(slippage_model="volume_aware", slippage_impact_exponent=0.75)
        assert engine.slippage_model == "volume_aware"
        assert engine.slippage_impact_exponent == pytest.approx(0.75)

    def test_default_slippage_model_is_fixed(self):
        """Default slippage_model (key absent from config) must be 'fixed'."""
        config = {
            "market": "sp500",
            "risk": {
                "starting_equity": 10_000.0,
                "leverage": 1.0,
                "max_risk_per_trade_pct": 0.01,
                "max_open_positions": 5,
                "max_sector_concentration": 5,
                "max_daily_drawdown_pct": 0.10,
                "require_stop_loss": True,
                "require_planned_exit": True,
                "min_confidence": 0.0,
            },
            "fees": {
                "commission_per_trade": 0,
                "commission_pct": 0.0,
                "slippage_pct": 0.001,
                # slippage_model intentionally absent
                "min_position_value": 0.0,
                "flat_fee_threshold": 0,
            },
            "trading": {
                "mode": "paper",
                "broker": "alpaca",
                "live_enabled": False,
                "live_safety": {"max_order_value": 0, "max_daily_orders": 100},
            },
            "backtest": {
                "train_window_days": 60,
                "test_window_days": 30,
                "step_days": 10,
                "min_history_days": 60,
            },
            "data": {
                "source": "yfinance",
                "history_years": 1,
                "cache_dir": "data/cache",
                "raw_dir": "data/raw",
                "processed_dir": "data/processed",
            },
            "allocation": {"enabled": False, "mode": "soft_pool", "overflow_enabled": True, "pools": {}},
            "universe": {
                "method": "top_liquid",
                "top_n": 10,
                "min_median_daily_value": 0,
                "min_price": 0.0,
                "min_market_cap": 0,
                "exclusions": [],
                "benchmark_ticker": "SPY",
            },
        }
        import pandas as pd
        dummy = pd.DataFrame(
            {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.0], "volume": [1_000_000]},
            index=pd.date_range("2023-01-02", periods=1, freq="B"),
        )
        with patch("backtest.engine.download_ticker", return_value=dummy):
            engine = BacktestEngine(config, market_id="sp500")

        assert engine.slippage_model == "fixed"
        assert engine.slippage_impact_exponent == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Backward-compatibility: original call sites (no extra kwargs) still work
# ---------------------------------------------------------------------------

class TestBackwardCompatibility:
    """Calling _apply_slippage without new kwargs must give original result."""

    def test_fixed_mode_no_kwargs(self):
        engine = _make_engine(slippage_pct=0.002, slippage_model="fixed")
        buy_price = engine._apply_slippage(100.0, "buy")
        sell_price = engine._apply_slippage(100.0, "sell")
        assert buy_price == pytest.approx(100.2)
        assert sell_price == pytest.approx(99.8)

    def test_volume_aware_without_kwargs_falls_back_to_fixed(self):
        """volume_aware with no order_shares/bar_volume kwarg → falls back to fixed."""
        engine = _make_engine(slippage_pct=0.002, slippage_model="volume_aware")
        buy_price = engine._apply_slippage(100.0, "buy")
        assert buy_price == pytest.approx(100.2), (
            "volume_aware without shares/volume should behave like fixed"
        )
