"""ASX 200 Market Profile for Atlas.

Provides the ticker universe, fee structure, trading hours, and
configuration for the Australian Securities Exchange.
"""

from __future__ import annotations

from typing import Dict, List

from markets.base import FeeStructure, MarketProfile, TradingHours


class ASXMarket(MarketProfile):
    """Australian Securities Exchange (ASX 200) market profile."""

    @property
    def market_id(self) -> str:
        return "asx"

    @property
    def display_name(self) -> str:
        return "ASX 200"

    @property
    def country(self) -> str:
        return "AU"

    @property
    def currency(self) -> str:
        return "AUD"

    @property
    def yfinance_suffix(self) -> str:
        return ".AX"

    @property
    def benchmark_ticker(self) -> str:
        return "IOZ.AX"

    @property
    def risk_free_rate(self) -> float:
        return 0.04  # RBA cash rate proxy

    @property
    def trading_hours(self) -> TradingHours:
        return TradingHours(
            timezone="Australia/Sydney",
            market_open="10:00",
            market_close="16:00",
            pre_market_open="07:00",
            post_market_close="16:10",
        )

    @property
    def default_fees(self) -> FeeStructure:
        return FeeStructure(
            commission_per_trade=3.0,
            commission_pct=0.0003,
            slippage_pct=0.001,
            flat_fee_threshold=10000.0,
            min_position_value=500.0,
        )

    def get_universe_tickers(self) -> List[str]:
        """Return 180+ liquid ASX ticker codes (without .AX suffix).

        Covers all major GICS sectors. Use format_ticker() or
        get_formatted_tickers() to get yfinance-ready symbols.
        """
        tickers = [
            # Financials
            "CBA", "NAB", "WBC", "ANZ", "MQG", "SUN", "IAG", "QBE", "BEN",
            "BOQ", "AMP", "PPT", "HUB", "NWL", "CGF", "IFL", "PNI", "JHG",
            "ASX", "MPL", "TYR", "PDN", "GQG", "INR", "NHF",

            # Materials / Mining
            "BHP", "RIO", "FMG", "MIN", "S32", "NCM", "NST", "EVN", "GOR",
            "SFR", "OZL", "IGO", "LYC", "ILU", "AWC", "BSL", "JHX", "AMC",
            "ORA", "BLD", "ABC", "SGM", "WHC", "NHC", "CRN", "PLS", "LTR",
            "PIQ", "DEG", "CMM", "RRL", "STO", "WDS", "RED", "SLR", "WAF",
            "NIC", "TIE", "AGI", "BGL", "NMT", "AIS", "MGX",

            # Healthcare
            "CSL", "COH", "RMD", "SHL", "FPH", "PME", "PRU", "ANN", "EBO",
            "NAN", "IMU", "PNV", "TLX", "NXS", "MSB", "NEU", "SDR", "MVP",

            # Consumer Discretionary
            "WES", "HVN", "JBH", "SUL", "PMV", "LOV", "BRG", "ADH", "NCK",
            "KGN", "TPW", "WEB", "FLT", "ALL", "TAH", "SGR", "SLC", "ARB",
            "PWR", "CAR", "REA", "DHG", "SEK", "IEL", "DSK", "AX1", "BBN",
            "CTT", "EVT", "HMC", "GWA",

            # Consumer Staples
            "WOW", "COL", "TWE", "A2M", "ING", "GNC", "CGC", "BGA", "ELD",
            "CCL", "TGR", "HUO", "BAL",

            # Industrials
            "TCL", "SYD", "BXB", "QAN", "AZJ", "DOW", "SVW", "NWH", "CIM",
            "IPL", "DRR", "QUB", "ALQ", "WOR", "SSM", "MND", "VNT", "AIA",
            "REH", "IFM", "AUB", "NWS", "BKW", "GNG", "ACF", "CVL",

            # Information Technology
            "XRO", "WTC", "CPU", "TNE", "ALU", "MP1", "NXT", "APX", "TYR",
            "DTC", "FCL", "LNK", "IRE", "AD8", "SQ2", "PME", "TLG", "DGL",
            "EML", "UBN", "OFX", "PPH", "DDR",

            # Communication Services
            "TLS", "TPG", "REA", "CAR", "NWS", "SWM", "OML", "NEC", "UNI",

            # Energy
            "WDS", "STO", "ORG", "APA", "VEA", "KAR", "BPT", "WHC", "NHC",
            "STX", "COE", "CVN", "NRG", "WGR",

            # Real Estate (REITs)
            "GMG", "SCG", "VCX", "MGR", "GPT", "SGP", "DXS", "CHC", "CLW",
            "BWP", "CIP", "ABP", "CQR", "NSR", "HMC", "LLC", "CNI", "ARF",
            "HDN", "GOZ", "GDG",

            # Utilities
            "AGL", "ORG", "APA", "SKI", "AST", "MCY",

            # Additional large/mid caps
            "EDV", "RHC", "ORI", "CTD", "IVC", "GUD", "BAP", "APE",
            "SDF", "NUF", "DMP", "CWY", "SKC", "PDN", "BOE", "ERA",
            "TLC", "PXA", "AVZ", "LKE", "VUL", "SYA", "AGY",
            "29M", "LPD", "AKE", "CXO", "GL1",
            "SHL", "AMI", "PBH", "CAJ", "THL",
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
        """Return ASX ticker -> GICS sector mapping.

        Returns an empty dict — sectors are fetched dynamically via
        yfinance in the universe builder for ASX.
        """
        return {}
