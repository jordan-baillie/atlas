"""Relative Strength Ranking for Atlas-ASX.

Phase 7B: Compute relative strength metrics ranking each stock's
performance against the full universe. Helps identify which trends
are strongest (for TF) and which stocks are genuinely oversold vs
structurally weak (for MR).

Metrics:
    - rs_percentile: Stock's ROC percentile rank within universe (0-100)
    - rs_score: Composite RS score using multiple lookback periods
    - rs_momentum: Change in RS percentile over recent period
    - roc_20/60/120: Raw rate of change over each period

Usage:
    from utils.relative_strength import RelativeStrength
    rs = RelativeStrength(data_dict)
    ranks = rs.compute(as_of_date)  # {ticker: {rs_percentile, ...}}
    series = rs.compute_series()    # DataFrame with daily RS for all tickers
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, Optional, Any, List

logger = logging.getLogger("atlas.relative_strength")


class RelativeStrength:
    """Compute relative strength rankings across the stock universe."""

    def __init__(
        self,
        data: Dict[str, pd.DataFrame],
        periods: List[int] = None,
        weights: List[float] = None,
        momentum_lookback: int = 10,
    ):
        """
        Args:
            data: Dict mapping ticker -> OHLCV DataFrame with DatetimeIndex.
            periods: ROC lookback periods in trading days. Default [20, 60, 120].
            weights: Weights for each period in composite score. Default [0.4, 0.35, 0.25].
            momentum_lookback: Days to measure RS rank momentum. Default 10.
        """
        self.data = data
        self.periods = periods or [20, 60, 120]
        self.weights = weights or [0.4, 0.35, 0.25]
        self.momentum_lookback = momentum_lookback

        if len(self.periods) != len(self.weights):
            raise ValueError("periods and weights must have same length")
        if abs(sum(self.weights) - 1.0) > 0.01:
            raise ValueError(f"weights must sum to 1.0, got {sum(self.weights)}")

        logger.info(
            f"RelativeStrength initialized: {len(data)} tickers, "
            f"periods={self.periods}, weights={self.weights}, "
            f"momentum_lookback={self.momentum_lookback}"
        )

    def _calc_roc(self, close: pd.Series, period: int) -> pd.Series:
        """Calculate rate of change (percentage) over given period."""
        return close.pct_change(periods=period) * 100

    def compute(self, as_of_date: pd.Timestamp) -> Dict[str, Dict[str, float]]:
        """Compute RS metrics for all tickers as of a specific date.

        Returns:
            Dict mapping ticker -> {
                rs_percentile: float (0-100),
                rs_score: float (composite weighted ROC),
                roc_20: float, roc_60: float, roc_120: float,
            }
        """
        ticker_rocs = {}  # ticker -> {period: roc_value}

        for ticker, df in self.data.items():
            mask = df.index <= as_of_date
            if not mask.any():
                continue
            subset = df.loc[mask]
            close = subset["close"]

            max_period = max(self.periods)
            if len(close) < max_period + 5:  # need buffer
                continue

            rocs = {}
            valid = True
            for period in self.periods:
                roc_series = self._calc_roc(close, period)
                roc_val = roc_series.iloc[-1]
                if pd.isna(roc_val):
                    valid = False
                    break
                rocs[period] = roc_val

            if valid:
                ticker_rocs[ticker] = rocs

        if not ticker_rocs:
            return {}

        # Compute composite score for each ticker
        scores = {}
        for ticker, rocs in ticker_rocs.items():
            composite = sum(
                rocs[p] * w for p, w in zip(self.periods, self.weights)
            )
            scores[ticker] = composite

        # Rank stocks by composite score -> percentile
        sorted_tickers = sorted(scores.keys(), key=lambda t: scores[t])
        n = len(sorted_tickers)

        result = {}
        for rank_idx, ticker in enumerate(sorted_tickers):
            percentile = (rank_idx / (n - 1)) * 100 if n > 1 else 50.0
            rocs = ticker_rocs[ticker]
            result[ticker] = {
                "rs_percentile": round(percentile, 1),
                "rs_score": round(scores[ticker], 2),
            }
            # Add individual ROCs keyed by period
            for period in self.periods:
                result[ticker][f"roc_{period}"] = round(rocs[period], 2)

        return result

    def compute_series(
        self,
        start_date: Optional[pd.Timestamp] = None,
        end_date: Optional[pd.Timestamp] = None,
    ) -> Dict[str, pd.DataFrame]:
        """Compute daily RS metrics for all tickers over a date range.

        This is the efficient vectorized version used by the backtest engine.

        Returns:
            Dict mapping ticker -> DataFrame with columns:
                rs_percentile, rs_score, roc_20, roc_60, roc_120
        """
        # Step 1: Pre-compute ROC series for all tickers and periods
        max_period = max(self.periods)
        ticker_roc_series = {}  # ticker -> {period: Series}
        ticker_composite = {}   # ticker -> Series

        valid_tickers = []
        for ticker, df in self.data.items():
            close = df["close"]
            if len(close) < max_period + 10:
                continue

            rocs = {}
            for period in self.periods:
                rocs[period] = self._calc_roc(close, period)

            # Composite score series
            composite = sum(
                rocs[p] * w for p, w in zip(self.periods, self.weights)
            )

            ticker_roc_series[ticker] = rocs
            ticker_composite[ticker] = composite
            valid_tickers.append(ticker)

        if not valid_tickers:
            return {}

        # Step 2: Build a DataFrame of composite scores (tickers as columns)
        composite_df = pd.DataFrame(
            {t: ticker_composite[t] for t in valid_tickers}
        )

        # Apply date filters
        if start_date:
            composite_df = composite_df.loc[composite_df.index >= start_date]
        if end_date:
            composite_df = composite_df.loc[composite_df.index <= end_date]

        # Step 3: Rank across tickers for each date -> percentile
        # rank(pct=True) gives percentile directly (0-1)
        rank_df = composite_df.rank(axis=1, pct=True, na_option="keep") * 100

        # Step 4: RS momentum (change in percentile over momentum_lookback)
        momentum_df = rank_df.diff(self.momentum_lookback)

        # Step 5: Package results per ticker
        result = {}
        for ticker in valid_tickers:
            if ticker not in rank_df.columns:
                continue

            ticker_df = pd.DataFrame(index=composite_df.index)
            ticker_df["rs_percentile"] = rank_df[ticker].round(1)
            ticker_df["rs_score"] = composite_df[ticker].round(2)
            ticker_df["rs_momentum"] = momentum_df[ticker].round(1)

            # Add individual ROCs
            rocs = ticker_roc_series[ticker]
            for period in self.periods:
                # Align to the same index
                aligned = rocs[period].reindex(composite_df.index)
                ticker_df[f"roc_{period}"] = aligned.round(2)

            result[ticker] = ticker_df

        logger.info(
            f"RS series computed: {len(result)} tickers, "
            f"{len(composite_df)} days"
        )

        return result

    def get_ticker_rs(
        self,
        ticker: str,
        rs_data: Dict[str, pd.DataFrame],
        as_of_date: pd.Timestamp,
    ) -> Dict[str, float]:
        """Get RS metrics for a specific ticker on a specific date.

        Convenience method for use by strategies during signal generation.

        Args:
            ticker: Stock ticker symbol.
            rs_data: Output from compute_series().
            as_of_date: Date to look up.

        Returns:
            Dict with rs_percentile, rs_score, rs_momentum, roc_* values.
            Returns empty dict with defaults if data unavailable.
        """
        defaults = {
            "rs_percentile": 50.0,
            "rs_score": 0.0,
            "rs_momentum": 0.0,
        }
        for p in self.periods:
            defaults[f"roc_{p}"] = 0.0

        if ticker not in rs_data:
            return defaults

        ticker_df = rs_data[ticker]
        if as_of_date not in ticker_df.index:
            # Try nearest prior date
            prior = ticker_df.index[ticker_df.index <= as_of_date]
            if len(prior) == 0:
                return defaults
            as_of_date = prior[-1]

        row = ticker_df.loc[as_of_date]
        result = {}
        for col in ticker_df.columns:
            val = row[col]
            result[col] = round(float(val), 2) if not pd.isna(val) else defaults.get(col, 0.0)

        return result
