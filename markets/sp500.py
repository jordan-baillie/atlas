"""S&P 500 Market Profile for Atlas.

Provides the ticker universe, fee structure, trading hours, and
configuration for the US S&P 500 index.
"""

from __future__ import annotations

from typing import Dict, List

from markets.base import FeeStructure, MarketProfile, TradingHours


class SP500Market(MarketProfile):
    """US S&P 500 market profile."""

    @property
    def market_id(self) -> str:
        return "sp500"

    @property
    def display_name(self) -> str:
        return "S&P 500"

    @property
    def country(self) -> str:
        return "US"

    @property
    def currency(self) -> str:
        return "USD"

    @property
    def yfinance_suffix(self) -> str:
        return ""  # US tickers have no suffix in yfinance

    @property
    def benchmark_ticker(self) -> str:
        return "SPY"

    @property
    def risk_free_rate(self) -> float:
        return 0.05  # Fed funds rate proxy

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
            commission_per_trade=0.0,   # Most US brokers are zero-commission
            commission_pct=0.0,
            slippage_pct=0.0005,        # Tighter spreads than ASX
            flat_fee_threshold=0.0,
            min_position_value=100.0,
        )

    def get_universe_tickers(self) -> List[str]:
        """Return top ~200 liquid S&P 500 ticker codes.

        Covers mega-cap and large-cap across all GICS sectors.
        No suffix needed for US tickers in yfinance.
        """
        tickers = [
            # Information Technology
            "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "CSCO",
            "ACN", "ADBE", "IBM", "INTU", "TXN", "QCOM", "NOW", "AMAT",
            "ADI", "LRCX", "KLAC", "SNPS", "CDNS", "MRVL", "MU", "MSI",
            "ROP", "PANW", "CRWD", "FTNT", "APH", "NXPI", "MCHP", "TEL",
            "HPQ", "KEYS", "ON", "ANSS", "CDW", "FSLR", "MPWR", "TYL",

            # Healthcare
            "UNH", "JNJ", "LLY", "ABBV", "MRK", "TMO", "ABT", "DHR",
            "PFE", "AMGN", "BMY", "MDT", "ISRG", "SYK", "GILD", "VRTX",
            "CI", "ELV", "ZTS", "BDX", "BSX", "HCA", "MCK", "REGN",
            "EW", "IDXX", "A", "DXCM", "IQV", "MTD", "RMD", "GEHC",

            # Financials
            "BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "MS",
            "SPGI", "BLK", "C", "AXP", "MMC", "PGR", "CB", "SCHW",
            "ICE", "CME", "AON", "MCO", "USB", "PNC", "TFC", "AIG",
            "MET", "PRU", "TROW", "AFL", "ALL", "TRV", "BK", "FITB",

            # Consumer Discretionary
            "AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "TJX",
            "BKNG", "CMG", "ORLY", "AZO", "ROST", "MAR", "HLT", "GM",
            "F", "DHI", "LEN", "GPC", "EBAY", "ETSY", "POOL", "BBY",
            "ULTA", "DRI", "WYNN", "LVS", "MGM", "CCL", "YUM", "DPZ",

            # Communication Services
            "META", "GOOGL", "GOOG", "NFLX", "DIS", "CMCSA", "VZ", "T",
            "TMUS", "CHTR", "EA", "TTWO", "ATVI", "WBD", "MTCH", "LYV",
            "OMC", "IPG", "FOXA", "PARA",

            # Consumer Staples
            "PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "CL",
            "MDLZ", "GIS", "KMB", "SYY", "STZ", "KHC", "HSY", "KDP",
            "MKC", "CHD", "K", "CAG", "SJM", "CLX", "TSN", "HRL",

            # Industrials
            "GE", "CAT", "HON", "UNP", "UPS", "RTX", "DE", "BA",
            "LMT", "GD", "NOC", "ADP", "WM", "ITW", "EMR", "ETN",
            "PH", "CSX", "NSC", "FDX", "TT", "CARR", "OTIS", "ROK",
            "FAST", "PCAR", "GWW", "CTAS", "VRSK", "IR", "DOV", "AME",

            # Energy
            "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO",
            "PXD", "OXY", "WMB", "KMI", "HES", "DVN", "HAL", "BKR",
            "FANG", "CTRA", "OKE", "TRGP",

            # Materials
            "LIN", "APD", "SHW", "ECL", "NEM", "FCX", "NUE", "DOW",
            "DD", "PPG", "VMC", "MLM", "EMN", "CE", "CF", "MOS",
            "IFF", "ALB", "BALL", "PKG",

            # Real Estate
            "PLD", "AMT", "CCI", "EQIX", "PSA", "SPG", "O", "DLR",
            "WELL", "VICI", "ARE", "AVB", "EQR", "MAA", "UDR", "ESS",
            "SUI", "PEAK", "BXP", "VTR",

            # Utilities
            "NEE", "DUK", "SO", "D", "AEP", "SRE", "EXC", "XEL",
            "ED", "WEC", "ES", "AWK", "DTE", "PPL", "FE", "AEE",
            "CMS", "CNP", "ATO", "EVRG",
        ]

        # Deduplicate
        seen = set()
        result = []
        for t in tickers:
            t_upper = t.upper().strip()
            if t_upper not in seen:
                seen.add(t_upper)
                result.append(t_upper)
        return result

    def get_sector_map(self) -> Dict[str, str]:
        """Return S&P 500 ticker -> GICS sector mapping.

        Returns an empty dict — sectors are fetched dynamically via
        yfinance in the universe builder.
        """
        return {}
