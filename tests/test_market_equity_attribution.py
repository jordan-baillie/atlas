"""Tests for portfolio.market_equity_attribution.attribute_equity_pro_rata.

Covers:
- Normal pro-rata distribution
- Zero-position market handling (FIX-PMEQ-AUDIT-004)
- All-zero-position equal-split fallback
- Rounding / sum invariants
"""
from __future__ import annotations

import pytest
from portfolio.market_equity_attribution import attribute_equity_pro_rata


class TestProRataDistribution:
    """Standard operation: multiple markets, all with positions."""

    def test_single_market(self):
        result = attribute_equity_pro_rata(
            broker_equity=1000.0,
            broker_cash=200.0,
            positions_by_market={"sp500": [{"market_value": 800.0}]},
        )
        assert set(result.keys()) == {"sp500"}
        assert result["sp500"]["position_mv"] == pytest.approx(800.0)
        assert result["sp500"]["allocated_equity"] == pytest.approx(1000.0)
        assert result["sp500"]["cash_attributed"] == pytest.approx(200.0)

    def test_two_markets_sum_to_broker_equity(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=400.0,
            positions_by_market={
                "sp500": [{"market_value": 600.0}],
                "sector_etfs": [{"market_value": 400.0}],
            },
        )
        total_alloc = sum(v["allocated_equity"] for v in result.values())
        assert abs(total_alloc - 2000.0) < 0.02  # rounding tolerance

    def test_two_markets_cash_sums_to_broker_cash(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=400.0,
            positions_by_market={
                "sp500": [{"market_value": 600.0}],
                "sector_etfs": [{"market_value": 400.0}],
            },
        )
        total_cash = sum(v["cash_attributed"] for v in result.values())
        assert abs(total_cash - 400.0) < 0.02

    def test_pro_rata_weights(self):
        result = attribute_equity_pro_rata(
            broker_equity=3000.0,
            broker_cash=600.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [{"market_value": 500.0}],
            },
        )
        # sp500 weight = 1000/1500 = 2/3
        assert result["sp500"]["allocated_equity"] == pytest.approx(2000.0, abs=0.02)
        # sector_etfs weight = 500/1500 = 1/3
        assert result["sector_etfs"]["allocated_equity"] == pytest.approx(1000.0, abs=0.02)

    def test_multiple_positions_in_one_market(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=300.0,
            positions_by_market={
                "sp500": [
                    {"market_value": 500.0},
                    {"market_value": 700.0},
                ],
                "sector_etfs": [{"market_value": 300.0}],
            },
        )
        # sp500 total_mv = 1200, sector_etfs = 300, total = 1500
        assert result["sp500"]["position_mv"] == pytest.approx(1200.0)
        assert result["sp500"]["allocated_equity"] == pytest.approx(2000.0 * 1200 / 1500, abs=0.02)

    def test_missing_market_value_key_treated_as_zero(self):
        """Positions missing market_value key should contribute 0 to MV."""
        result = attribute_equity_pro_rata(
            broker_equity=1000.0,
            broker_cash=100.0,
            positions_by_market={
                "sp500": [{"market_value": 500.0}, {}],  # second pos has no mv
            },
        )
        assert result["sp500"]["position_mv"] == pytest.approx(500.0)

    def test_none_market_value_treated_as_zero(self):
        """Positions with market_value=None should not crash."""
        result = attribute_equity_pro_rata(
            broker_equity=1000.0,
            broker_cash=100.0,
            positions_by_market={
                "sp500": [{"market_value": None}, {"market_value": 500.0}],
            },
        )
        assert result["sp500"]["position_mv"] == pytest.approx(500.0)


class TestZeroPositionMarketHandling:
    """Zero-position markets (FIX-PMEQ-AUDIT-004).

    When a market is a key in positions_by_market with an empty list, it MUST
    appear in the output — so the eod_settlement carry-forward block can write
    a row for it.  Without a row, next-day HWM comparison falls back to global
    broker equity → false HALT risk.
    """

    def test_zero_position_market_is_in_output(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
                "commodity_etfs": [{"market_value": 500.0}],
            },
        )
        assert "sector_etfs" in result

    def test_zero_position_market_has_zero_mv(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
            },
        )
        assert result["sector_etfs"]["position_mv"] == 0.0

    def test_zero_position_market_gets_zero_pro_rata_cash(self):
        """Pro-rata gives 0 cash to zero-position market; carry-forward is
        the eod_settlement layer's responsibility (not this function's)."""
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=500.0,
            positions_by_market={
                "sp500": [{"market_value": 1000.0}],
                "sector_etfs": [],
                "commodity_etfs": [{"market_value": 500.0}],
            },
        )
        assert result["sector_etfs"]["cash_attributed"] == 0.0
        assert result["sector_etfs"]["allocated_equity"] == 0.0

    def test_two_zero_position_markets(self):
        """Two zero-position markets both appear in output with zeros."""
        result = attribute_equity_pro_rata(
            broker_equity=3000.0,
            broker_cash=1000.0,
            positions_by_market={
                "sp500": [{"market_value": 2000.0}],
                "sector_etfs": [],
                "commodity_etfs": [],
            },
        )
        assert result["sector_etfs"]["position_mv"] == 0.0
        assert result["commodity_etfs"]["position_mv"] == 0.0
        # sp500 gets all allocation (it's the only market with positions)
        assert result["sp500"]["allocated_equity"] == pytest.approx(3000.0)

    def test_nonzero_markets_unaffected_by_zero_market_presence(self):
        """Adding a zero-position market doesn't change other markets' allocation."""
        result_without = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=400.0,
            positions_by_market={
                "sp500": [{"market_value": 600.0}],
                "commodity_etfs": [{"market_value": 400.0}],
            },
        )
        result_with = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=400.0,
            positions_by_market={
                "sp500": [{"market_value": 600.0}],
                "sector_etfs": [],  # zero-position market added
                "commodity_etfs": [{"market_value": 400.0}],
            },
        )
        # sp500 and commodity_etfs allocations should be identical
        assert result_without["sp500"]["allocated_equity"] == pytest.approx(
            result_with["sp500"]["allocated_equity"], abs=0.02
        )
        assert result_without["commodity_etfs"]["allocated_equity"] == pytest.approx(
            result_with["commodity_etfs"]["allocated_equity"], abs=0.02
        )


class TestAllZeroPositionFallback:
    """When ALL markets have zero positions, cash is split equally."""

    def test_equal_split_two_markets(self):
        result = attribute_equity_pro_rata(
            broker_equity=2000.0,
            broker_cash=2000.0,
            positions_by_market={
                "sp500": [],
                "sector_etfs": [],
            },
        )
        assert result["sp500"]["cash_attributed"] == pytest.approx(1000.0)
        assert result["sector_etfs"]["cash_attributed"] == pytest.approx(1000.0)

    def test_equal_split_three_markets(self):
        result = attribute_equity_pro_rata(
            broker_equity=900.0,
            broker_cash=900.0,
            positions_by_market={
                "sp500": [],
                "sector_etfs": [],
                "commodity_etfs": [],
            },
        )
        for m in ("sp500", "sector_etfs", "commodity_etfs"):
            assert result[m]["cash_attributed"] == pytest.approx(300.0)
            assert result[m]["position_mv"] == 0.0

    def test_empty_dict_does_not_crash(self):
        """Empty positions_by_market returns empty dict (no markets to distribute to)."""
        result = attribute_equity_pro_rata(
            broker_equity=1000.0,
            broker_cash=1000.0,
            positions_by_market={},
        )
        assert result == {}
