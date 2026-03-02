"""Hong Kong (SEHK) Market Profile for Atlas.

Provides the ticker universe, fee structure, trading hours, and
configuration for the Stock Exchange of Hong Kong (SEHK).

Trades via IBKR (Interactive Brokers) on the Hang Seng Composite index.
All ticker codes are STRINGS with leading zeros (e.g. '0700', '0005').
yfinance format appends '.HK' suffix (e.g. '0700.HK', '0005.HK').
"""

from __future__ import annotations

from typing import Dict, List

from markets.base import FeeStructure, MarketProfile, TradingHours


class HKMarket(MarketProfile):
    """Stock Exchange of Hong Kong (SEHK) — Hang Seng Composite market profile.

    Benchmark: Tracker Fund of Hong Kong (2800.HK).
    Broker: IBKR via IB Gateway.
    All raw ticker codes are zero-padded strings (e.g. '0700', '0005').
    """

    @property
    def market_id(self) -> str:
        return "hk"

    @property
    def display_name(self) -> str:
        return "Hang Seng Composite"

    @property
    def country(self) -> str:
        return "HK"

    @property
    def currency(self) -> str:
        return "HKD"

    @property
    def yfinance_suffix(self) -> str:
        return ".HK"

    @property
    def benchmark_ticker(self) -> str:
        return "2800.HK"  # Tracker Fund of Hong Kong (largest HK ETF)

    @property
    def risk_free_rate(self) -> float:
        return 0.04  # HIBOR / HKMA base rate proxy

    @property
    def trading_days_per_year(self) -> int:
        return 247  # SEHK trades ~247 days/year

    @property
    def operator_timezone(self) -> str:
        return "Australia/Brisbane"  # Operator is in AEST (no DST)

    @property
    def pre_market_alert_hours_before(self) -> float:
        return 1.5  # Alert at 08:00 AEST (HK opens 09:30 HKT = 07:30 AEST + 1.5h buffer = 08:00 AEST wait, let me recalculate)
        # HKT = UTC+8. AEST = UTC+10 (Brisbane, no DST).
        # HK open 09:30 HKT = 11:30 AEST.
        # Alert 1.5h before = 10:00 AEST. Reasonable.

    @property
    def trading_hours(self) -> TradingHours:
        return TradingHours(
            timezone="Asia/Hong_Kong",
            market_open="09:30",
            market_close="16:00",
            pre_market_open="09:00",
            post_market_close="16:10",
        )

    @property
    def default_fees(self) -> FeeStructure:
        return FeeStructure(
            commission_per_trade=18.0,    # IBKR HK: min HKD 18 per order
            commission_pct=0.0005,        # 0.05% of trade value
            slippage_pct=0.001,           # 0.1% estimated slippage
            flat_fee_threshold=36000.0,   # HKD 36,000 → pct fee applies (~USD 4,600)
            min_position_value=2000.0,    # HKD 2,000 minimum position
        )

    def get_universe_tickers(self) -> List[str]:
        """Return ~130 most liquid Hang Seng Composite / Large-Mid Cap tickers.

        All codes are STRINGS with leading zeros — this is critical for
        correct yfinance lookups (e.g. '0700' not 700, '0005' not 5).
        Covers all major GICS sectors on SEHK.

        Use format_ticker() to get yfinance-ready symbols (adds '.HK' suffix).
        """
        tickers = [
            # Financials — Banks, Insurance, Exchanges
            "0005",  # HSBC Holdings
            "0011",  # Hang Seng Bank
            "0023",  # Bank of East Asia
            "0388",  # Hong Kong Exchanges & Clearing (HKEX)
            "0440",  # Dah Sing Financial Holdings
            "0939",  # China Construction Bank (CCB)
            "1299",  # AIA Group
            "1336",  # PICC Property & Casualty
            "1398",  # Industrial & Commercial Bank of China (ICBC)
            "2318",  # Ping An Insurance Group
            "2388",  # BOC Hong Kong Holdings
            "2601",  # China Pacific Insurance Group (CPIC)
            "2628",  # China Life Insurance
            "3968",  # China Merchants Bank
            "3988",  # Bank of China
            "6030",  # CITIC Securities
            "6881",  # China Galaxy Securities
            "6886",  # Huatai Securities (HTSC)

            # Technology — Internet, Semiconductors, Hardware
            "0020",  # SenseTime Group
            "0268",  # Kingdee International Software
            "0700",  # Tencent Holdings
            "0981",  # Semiconductor Manufacturing International (SMIC)
            "0992",  # Lenovo Group
            "1024",  # Kuaishou Technology
            "1347",  # Hua Hong Semiconductor
            "1810",  # Xiaomi Corporation
            "2015",  # Li Auto
            "2382",  # Sunny Optical Technology
            "3690",  # Meituan
            "6618",  # JD Health International
            "9618",  # JD.com (HK-listed)
            "9626",  # Bilibili
            "9866",  # NIO (HK-listed)
            "9868",  # XPeng (HK-listed)
            "9888",  # Baidu (HK-listed)
            "9988",  # Alibaba Group (HK-listed)
            "9999",  # NetEase (HK-listed)

            # Healthcare — Pharma, Biotech, Medical Devices
            "0241",  # Alibaba Health Information Technology
            "0460",  # Sihuan Pharmaceutical Holdings
            "0853",  # Microport Scientific
            "0867",  # China Medical System Holdings
            "1093",  # CSPC Pharmaceutical Group
            "1177",  # Sino Biopharmaceutical
            "1548",  # Genscript Biotech
            "1833",  # Ping An Healthcare & Technology
            "2196",  # Shanghai Fosun Pharmaceutical
            "2269",  # WuXi Biologics Cayman
            "6160",  # BeiGene

            # Consumer Discretionary — Autos, Leisure, Retail
            "0027",  # Galaxy Entertainment Group
            "0175",  # Geely Automobile Holdings
            "0291",  # China Resources Beer Holdings
            "0551",  # Yue Yuen Industrial Holdings
            "0669",  # Techtronic Industries
            "0880",  # SJM Holdings
            "1068",  # Weichai Power
            "1368",  # Xtep International Holdings
            "1928",  # Sands China
            "1929",  # Chow Tai Fook Jewellery
            "2020",  # ANTA Sports Products
            "2313",  # Shenzhou International Group
            "2333",  # Great Wall Motor Company
            "6110",  # Topsports International Holdings

            # Consumer Staples — Food, Beverage, Household
            "0151",  # Want Want China Holdings
            "0168",  # Tsingtao Brewery
            "0220",  # Uni-President China Holdings
            "0288",  # WH Group (Shuanghui International)
            "0322",  # Tingyi (Cayman Islands) Holdings
            "0345",  # Vitasoy International Holdings
            "1044",  # Hengan International Group
            "2319",  # China Mengniu Dairy
            "3799",  # Dali Foods Group

            # Telecommunications
            "0008",  # PCCW Limited
            "0728",  # China Telecom (HK)
            "0762",  # China Unicom (HK)
            "0941",  # China Mobile
            "6823",  # HKT Trust & HKT Limited

            # Energy — Oil, Gas, Coal
            "0135",  # Kunlun Energy
            "0386",  # China Petroleum & Chemical (Sinopec)
            "0857",  # PetroChina
            "0883",  # CNOOC
            "1088",  # China Shenhua Energy
            "1171",  # Yanzhou Coal Mining
            "2688",  # ENN Energy Holdings

            # Utilities — Power, Gas, Infrastructure
            "0002",  # CLP Holdings
            "0003",  # HK & China Gas (Towngas)
            "0006",  # Power Assets Holdings
            "0836",  # China Resources Power Holdings
            "1038",  # CK Infrastructure Holdings

            # Real Estate — Developers, REITs, Property Services
            "0004",  # Wharf Holdings
            "0012",  # Henderson Land Development
            "0016",  # Sun Hung Kai Properties
            "0017",  # New World Development
            "0083",  # Sino Land
            "0101",  # Hang Lung Properties
            "0688",  # China Overseas Land & Investment
            "0960",  # Longfor Group Holdings
            "1109",  # China Resources Land
            "1113",  # CK Asset Holdings
            "1997",  # Wharf Real Estate Investment Company (REIC)
            "2007",  # Country Garden Holdings
            "2202",  # China Vanke (H-Share)
            "3383",  # Agile Group Holdings
            "6098",  # Country Garden Services Holdings

            # Industrials — Conglomerates, Transport, Construction
            "0001",  # CK Hutchison Holdings
            "0019",  # Swire Pacific (Class A)
            "0066",  # MTR Corporation
            "0144",  # China Merchants Port Holdings
            "0177",  # Jiangsu Expressway
            "0267",  # CITIC Limited
            "0293",  # Cathay Pacific Airways
            "0316",  # Orient Overseas Container Line (OOCL)
            "0390",  # China Railway Group
            "0656",  # Fosun International
            "0670",  # China Eastern Airlines
            "0753",  # Air China
            "1186",  # China Railway Construction (CRCC)
            "1199",  # COSCO Shipping Holdings
            "1800",  # China Communications Construction (CCCC)
            "1919",  # COSCO Shipping Development
            "2866",  # COSCO Shipping Ports

            # Materials — Metals, Mining, Cement, Chemicals
            "0347",  # Angang Steel Company
            "0358",  # Jiangxi Copper Company
            "0914",  # Anhui Conch Cement
            "1053",  # Chongqing Iron & Steel
            "1313",  # China Resources Cement Holdings
            "1818",  # Zhaojin Mining Industry
            "2600",  # Aluminum Corporation of China (Chalco)
            "2899",  # Zijin Mining Group
            "3331",  # Vinda International Holdings
            "3993",  # China Molybdenum
        ]

        # Deduplicate while preserving order and leading zeros
        seen = set()
        result = []
        for t in tickers:
            t_clean = t.strip()  # Preserve leading zeros — do NOT call upper() on HK codes
            if t_clean not in seen:
                seen.add(t_clean)
                result.append(t_clean)
        return result

    def get_sector_map(self) -> Dict[str, str]:
        """Return SEHK ticker -> GICS sector mapping.

        Returns an empty dict — sectors are fetched dynamically via
        yfinance in the universe builder for HK.
        """
        return {}
