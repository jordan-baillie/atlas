"""Atlas ETF Market Profiles.

Provides market profiles for the 5 ETF universes:
  - sector_etfs    (SPDR sector ETFs)
  - commodity_etfs (commodity/resource ETFs)
  - treasury_etfs  (US Treasury bond ETFs)
  - gold_etfs      (Gold-related ETFs)
  - defensive_etfs (Defensive / low-volatility ETFs)

All are US-listed ETFs traded on NYSE/NASDAQ — same trading hours and
conventions as SP500. No yfinance suffix needed.
"""

from __future__ import annotations

from typing import Dict, List

from markets.base import FeeStructure, MarketProfile, TradingHours


class _BaseUSETFMarket(MarketProfile):
    """Shared base for US-listed ETF market profiles."""

    @property
    def country(self) -> str:
        return "US"

    @property
    def currency(self) -> str:
        return "USD"

    @property
    def yfinance_suffix(self) -> str:
        return ""  # US ETFs — no suffix needed in yfinance

    @property
    def risk_free_rate(self) -> float:
        return 0.05  # Fed funds rate proxy

    @property
    def trading_days_per_year(self) -> int:
        return 252  # NYSE/NASDAQ ~252 days/year

    @property
    def operator_timezone(self) -> str:
        return "Australia/Brisbane"  # Operator in AEST

    @property
    def pre_market_alert_hours_before(self) -> float:
        return 5.5

    @property
    def trading_hours(self) -> TradingHours:
        return TradingHours(
            timezone="America/New_York",
            market_open="09:30",
            market_close="16:00",
            pre_market_open="04:00",
            post_market_close="20:00",
        )

    @property
    def default_fees(self) -> FeeStructure:
        return FeeStructure(
            commission_per_trade=0.0,
            commission_pct=0.0,
            slippage_pct=0.0005,
            flat_fee_threshold=0.0,
            min_position_value=100.0,
        )

    def get_sector_map(self) -> Dict[str, str]:
        return {}


class SectorETFsMarket(_BaseUSETFMarket):
    """SPDR Sector ETFs — all 11 GICS sector funds."""

    @property
    def market_id(self) -> str:
        return "sector_etfs"

    @property
    def display_name(self) -> str:
        return "Sector ETFs"

    @property
    def benchmark_ticker(self) -> str:
        return "SPY"

    def get_universe_tickers(self) -> List[str]:
        return ["XLF", "XLE", "XLK", "XLV", "XLI", "XLC", "XLY", "XLP", "XLU", "XLB", "XLRE"]


class CommodityETFsMarket(_BaseUSETFMarket):
    """Commodity ETFs — energy, metals, agriculture."""

    @property
    def market_id(self) -> str:
        return "commodity_etfs"

    @property
    def display_name(self) -> str:
        return "Commodity ETFs"

    @property
    def benchmark_ticker(self) -> str:
        return "DBC"

    def get_universe_tickers(self) -> List[str]:
        return ["GLD", "SLV", "USO", "XOP", "CORN", "DBA", "DBB", "UNG", "CCJ", "FCX"]


class TreasuryETFsMarket(_BaseUSETFMarket):
    """US Treasury Bond ETFs — short, medium, long duration."""

    @property
    def market_id(self) -> str:
        return "treasury_etfs"

    @property
    def display_name(self) -> str:
        return "Treasury ETFs"

    @property
    def benchmark_ticker(self) -> str:
        return "AGG"

    def get_universe_tickers(self) -> List[str]:
        return ["TLT", "IEF", "SHY", "TIP", "BND"]


class GoldETFsMarket(_BaseUSETFMarket):
    """Gold and gold-mining ETFs."""

    @property
    def market_id(self) -> str:
        return "gold_etfs"

    @property
    def display_name(self) -> str:
        return "Gold ETFs"

    @property
    def benchmark_ticker(self) -> str:
        return "GLD"

    def get_universe_tickers(self) -> List[str]:
        return ["GLD", "IAU", "GDX", "GDXJ"]


class DefensiveETFsMarket(_BaseUSETFMarket):
    """Defensive / low-volatility ETFs including inverse funds."""

    @property
    def market_id(self) -> str:
        return "defensive_etfs"

    @property
    def display_name(self) -> str:
        return "Defensive ETFs"

    @property
    def benchmark_ticker(self) -> str:
        return "SPY"

    def get_universe_tickers(self) -> List[str]:
        return ["SH", "PSQ", "XLU", "XLP", "VIG", "USMV"]
