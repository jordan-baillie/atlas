"""Conditional Volatility Scaling for the Atlas backtest engine.

Tracks trailing portfolio volatility using exponentially weighted returns and
scales position sizes down when the portfolio is in an elevated-vol regime.

Config key: ``vol_scaling`` (nested in top-level config dict).

Example config snippet::

    "vol_scaling": {
        "enabled": true,
        "lookback": 60,
        "half_life": 20,
        "target_vol": 0.12,
        "conditional": true,
        "percentile_threshold": 80
    }

When ``conditional`` is True (default) the scaler only reduces sizes when the
current realized volatility is *above* the ``percentile_threshold``-th percentile
of the distribution of historical absolute daily returns.  This avoids
unnecessary de-sizing during normal market conditions.

When ``conditional`` is False the scaler always applies the ratio
``target_vol / realized_vol`` (capped at 1.0).
"""
import logging
import math
from typing import List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_ANNUALIZE = math.sqrt(252)  # √252 — daily → annual vol conversion


class VolatilityScaler:
    """Tracks trailing portfolio vol and returns a position-size scale factor.

    Args:
        config: Full backtest config dict. Reads from the ``vol_scaling`` key.
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("vol_scaling", {})
        self.enabled: bool = cfg.get("enabled", False)
        self.lookback: int = int(cfg.get("lookback", 60))       # trading days
        self.half_life: int = int(cfg.get("half_life", 20))     # for EWMA
        self.target_vol: float = float(cfg.get("target_vol", 0.12))  # annualized
        self.conditional: bool = bool(cfg.get("conditional", True))
        self.percentile_threshold: int = int(cfg.get("percentile_threshold", 80))

        # Internal buffer: stores raw daily portfolio returns (most-recent last)
        self._returns: List[float] = []

        if self.enabled:
            logger.info(
                "VolatilityScaler enabled: lookback=%d, half_life=%d, "
                "target_vol=%.2f, conditional=%s, percentile_threshold=%d",
                self.lookback,
                self.half_life,
                self.target_vol,
                self.conditional,
                self.percentile_threshold,
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update(self, daily_return: float) -> None:
        """Record a new daily portfolio return.

        Args:
            daily_return: Today's return as a fraction (e.g. 0.012 = +1.2%).
        """
        self._returns.append(daily_return)

    def scale_factor(self) -> float:
        """Return a position-size multiplier in [0.0, 1.0].

        Returns:
            1.0  — scaling disabled, insufficient data, or vol below threshold
            <1.0 — current portfolio vol is elevated relative to target / history
        """
        if not self.enabled:
            return 1.0

        if len(self._returns) < self.lookback:
            return 1.0

        # --- Compute current realized vol (EWMA, annualized) ---------------
        recent = self._returns[-self.lookback:]
        realized_vol = self._ewm_std(recent) * _ANNUALIZE

        if realized_vol <= 0:
            return 1.0

        # --- Conditional gate ----------------------------------------------
        if self.conditional:
            # Compare realized vol to the Nth percentile of the annualized
            # absolute daily returns in the *full* history buffer.  This proxy
            # represents the typical "daily vol" observed at the threshold
            # percentile and lets us distinguish low-vol from high-vol regimes
            # without recomputing rolling stds over the whole buffer.
            ann_abs = np.abs(self._returns) * _ANNUALIZE
            threshold_vol = float(np.percentile(ann_abs, self.percentile_threshold))

            if realized_vol <= threshold_vol:
                logger.debug(
                    "VolScaler: realized_vol=%.4f <= threshold=%.4f (pct%d) — no scaling",
                    realized_vol,
                    threshold_vol,
                    self.percentile_threshold,
                )
                return 1.0

        # --- Compute scale -------------------------------------------------
        scale = min(1.0, self.target_vol / realized_vol)
        logger.debug(
            "VolScaler: realized_vol=%.4f, target_vol=%.4f -> scale=%.4f",
            realized_vol,
            self.target_vol,
            scale,
        )
        return scale

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ewm_std(self, returns: List[float]) -> float:
        """Exponentially weighted standard deviation of a return series.

        Uses pandas ``ewm`` with the configured ``half_life``.

        Args:
            returns: List of daily returns (fraction).

        Returns:
            EWM std of the series (daily units).
        """
        s = pd.Series(returns, dtype=float)
        ewm_std = s.ewm(halflife=self.half_life, adjust=True).std()
        val = float(ewm_std.iloc[-1])
        return val if not math.isnan(val) else 0.0
