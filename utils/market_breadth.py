"""Market Breadth Indicators for Atlas-ASX.

Phase 7C: Compute market-wide breadth metrics from universe OHLCV data.
These indicators measure overall market health and participation.

Indicators:
    - pct_above_50ma: % of stocks trading above their 50-day moving average
    - pct_above_200ma: % of stocks trading above their 200-day moving average
    - ad_ratio: ratio of advancing to declining stocks (1-day)
    - breadth_thrust: 10-day EMA of pct_above_50ma (momentum of breadth)
    - net_new_highs_pct: % stocks making 52-week highs minus % making lows

Usage:
    from utils.market_breadth import MarketBreadth
    mb = MarketBreadth(data_dict)
    breadth = mb.compute(as_of_date)
    breadth_series = mb.compute_series()
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, Any

logger = logging.getLogger("atlas.breadth")


class MarketBreadth:
    """Compute market breadth indicators from universe OHLCV data."""

    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        short_ma: int = 50,
        long_ma: int = 200,
        thrust_period: int = 10,
        high_low_lookback: int = 252,
    ):
        self.data = data
        self.short_ma = short_ma
        self.long_ma = long_ma
        self.thrust_period = thrust_period
        self.high_low_lookback = high_low_lookback
        logger.info(
            f"MarketBreadth initialized: {len(data)} tickers, "
            f"MA={short_ma}/{long_ma}, thrust={thrust_period}"
        )

    def compute(self, as_of_date: pd.Timestamp) -> Dict[str, float]:
        """Compute breadth indicators for a single date."""
        above_50 = 0
        above_200 = 0
        advancing = 0
        declining = 0
        new_highs = 0
        new_lows = 0
        valid_50 = 0
        valid_200 = 0
        valid_ad = 0
        valid_hl = 0

        for ticker, df in self.data.items():
            mask = df.index <= as_of_date
            if not mask.any():
                continue
            subset = df.loc[mask]
            if len(subset) < 2:
                continue

            close = subset["close"]
            current_close = close.iloc[-1]
            prev_close = close.iloc[-2]

            # % above 50-day MA
            if len(close) >= self.short_ma:
                ma50 = close.rolling(window=self.short_ma).mean().iloc[-1]
                if not pd.isna(ma50):
                    valid_50 += 1
                    if current_close > ma50:
                        above_50 += 1

            # % above 200-day MA
            if len(close) >= self.long_ma:
                ma200 = close.rolling(window=self.long_ma).mean().iloc[-1]
                if not pd.isna(ma200):
                    valid_200 += 1
                    if current_close > ma200:
                        above_200 += 1

            # Advance/Decline
            valid_ad += 1
            if current_close > prev_close:
                advancing += 1
            elif current_close < prev_close:
                declining += 1

            # 52-week highs/lows
            if len(close) >= self.high_low_lookback:
                valid_hl += 1
                lookback_high = close.iloc[-self.high_low_lookback:].max()
                lookback_low = close.iloc[-self.high_low_lookback:].min()
                if current_close >= lookback_high:
                    new_highs += 1
                if current_close <= lookback_low:
                    new_lows += 1

        pct_above_50 = (above_50 / valid_50 * 100) if valid_50 > 0 else 50.0
        pct_above_200 = (above_200 / valid_200 * 100) if valid_200 > 0 else 50.0

        if declining > 0:
            ad_ratio = advancing / declining
        elif advancing > 0:
            ad_ratio = float(advancing)
        else:
            ad_ratio = 1.0

        net_new_highs = 0.0
        if valid_hl > 0:
            net_new_highs = ((new_highs - new_lows) / valid_hl) * 100

        return {
            "pct_above_50ma": round(pct_above_50, 1),
            "pct_above_200ma": round(pct_above_200, 1),
            "ad_ratio": round(ad_ratio, 2),
            "advancing": advancing,
            "declining": declining,
            "unchanged": valid_ad - advancing - declining,
            "new_highs": new_highs,
            "new_lows": new_lows,
            "net_new_highs_pct": round(net_new_highs, 1),
            "valid_stocks_50": valid_50,
            "valid_stocks_200": valid_200,
        }

    def compute_series(
        self,
        start_date: Optional[pd.Timestamp] = None,
        end_date: Optional[pd.Timestamp] = None,
    ) -> pd.DataFrame:
        """Compute daily breadth indicators for a date range (vectorized)."""
        # Pre-compute MAs and metrics for all tickers
        ticker_ma50 = {}
        ticker_ma200 = {}
        ticker_close = {}
        ticker_rolling_high = {}
        ticker_rolling_low = {}

        for ticker, df in self.data.items():
            close = df["close"]
            ticker_close[ticker] = close
            ticker_ma50[ticker] = close.rolling(window=self.short_ma).mean()
            ticker_ma200[ticker] = close.rolling(window=self.long_ma).mean()
            ticker_rolling_high[ticker] = close.rolling(
                window=self.high_low_lookback,
                min_periods=self.high_low_lookback,
            ).max()
            ticker_rolling_low[ticker] = close.rolling(
                window=self.high_low_lookback,
                min_periods=self.high_low_lookback,
            ).min()

        # Find union of all dates
        all_dates = set()
        for df in self.data.values():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        if start_date:
            all_dates = [d for d in all_dates if d >= start_date]
        if end_date:
            all_dates = [d for d in all_dates if d <= end_date]

        if not all_dates:
            return pd.DataFrame()

        # Compute daily breadth
        records = []
        for date in all_dates:
            above_50 = 0
            above_200 = 0
            advancing = 0
            declining = 0
            new_highs = 0
            new_lows = 0
            valid_50 = 0
            valid_200 = 0
            valid_ad = 0
            valid_hl = 0

            for ticker in self.data:
                close_s = ticker_close[ticker]
                if date not in close_s.index:
                    continue

                current_close = close_s.loc[date]

                # 50-day MA
                if date in ticker_ma50[ticker].index:
                    ma50_val = ticker_ma50[ticker].loc[date]
                    if not pd.isna(ma50_val):
                        valid_50 += 1
                        if current_close > ma50_val:
                            above_50 += 1

                # 200-day MA
                if date in ticker_ma200[ticker].index:
                    ma200_val = ticker_ma200[ticker].loc[date]
                    if not pd.isna(ma200_val):
                        valid_200 += 1
                        if current_close > ma200_val:
                            above_200 += 1

                # Advance/Decline
                date_loc = close_s.index.get_loc(date)
                if date_loc > 0:
                    prev_close = close_s.iloc[date_loc - 1]
                    valid_ad += 1
                    if current_close > prev_close:
                        advancing += 1
                    elif current_close < prev_close:
                        declining += 1

                # 52-week highs/lows
                if date in ticker_rolling_high[ticker].index:
                    rh = ticker_rolling_high[ticker].loc[date]
                    rl = ticker_rolling_low[ticker].loc[date]
                    if not pd.isna(rh) and not pd.isna(rl):
                        valid_hl += 1
                        if current_close >= rh:
                            new_highs += 1
                        if current_close <= rl:
                            new_lows += 1

            pct_50 = (above_50 / valid_50 * 100) if valid_50 > 0 else 50.0
            pct_200 = (above_200 / valid_200 * 100) if valid_200 > 0 else 50.0
            ad_r = (
                (advancing / declining)
                if declining > 0
                else (float(advancing) if advancing > 0 else 1.0)
            )
            nnh = ((new_highs - new_lows) / valid_hl * 100) if valid_hl > 0 else 0.0

            records.append(
                {
                    "date": date,
                    "pct_above_50ma": round(pct_50, 1),
                    "pct_above_200ma": round(pct_200, 1),
                    "ad_ratio": round(ad_r, 2),
                    "advancing": advancing,
                    "declining": declining,
                    "new_highs": new_highs,
                    "new_lows": new_lows,
                    "net_new_highs_pct": round(nnh, 1),
                    "valid_stocks_50": valid_50,
                    "valid_stocks_200": valid_200,
                }
            )

        df_breadth = pd.DataFrame(records).set_index("date")

        # Breadth thrust (10-day EMA of pct_above_50ma)
        df_breadth["breadth_thrust"] = (
            df_breadth["pct_above_50ma"]
            .ewm(span=self.thrust_period, adjust=False)
            .mean()
            .round(1)
        )

        # Breadth momentum (5-day change in pct_above_50ma)
        df_breadth["breadth_momentum"] = (
            df_breadth["pct_above_50ma"].diff(5).round(1)
        )

        logger.info(
            f"Breadth series computed: {len(df_breadth)} days, "
            f"{df_breadth.index[0].date()} to {df_breadth.index[-1].date()}"
        )

        return df_breadth

    def classify_regime(self, breadth: Dict[str, float]) -> str:
        """Classify market regime based on breadth indicators.

        Returns one of: strong_bull, bull, neutral, bear, strong_bear

        This is informational only (Phase 7C info-only approach).
        """
        pct50 = breadth.get("pct_above_50ma", 50.0)
        pct200 = breadth.get("pct_above_200ma", 50.0)
        nnh = breadth.get("net_new_highs_pct", 0.0)

        # Scoring system
        score = 0

        # 50-day MA breadth
        if pct50 >= 70:
            score += 2
        elif pct50 >= 55:
            score += 1
        elif pct50 <= 30:
            score -= 2
        elif pct50 <= 45:
            score -= 1

        # 200-day MA breadth
        if pct200 >= 65:
            score += 2
        elif pct200 >= 50:
            score += 1
        elif pct200 <= 35:
            score -= 2
        elif pct200 <= 45:
            score -= 1

        # Net new highs
        if nnh >= 5:
            score += 1
        elif nnh <= -5:
            score -= 1

        # Classify
        if score >= 4:
            return "strong_bull"
        elif score >= 2:
            return "bull"
        elif score <= -4:
            return "strong_bear"
        elif score <= -2:
            return "bear"
        else:
            return "neutral"
