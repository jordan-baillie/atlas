"""Phase 8D: Dynamic Position Sizing Module.

Adjusts position size based on:
1. Signal confidence (higher confidence -> larger position)
2. Stock volatility (higher vol -> smaller position)
3. Equity curve health (drawdown -> smaller position)
"""
import logging
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)


class DynamicSizer:
    """Calculates adjusted risk percentage for position sizing."""

    def __init__(self, config: dict):
        ds_cfg = config.get("dynamic_sizing", {})
        self.enabled = ds_cfg.get("enabled", False)
        self.base_risk_pct = ds_cfg.get("base_risk_pct", 0.005)
        self.min_risk_pct = ds_cfg.get("min_risk_pct", 0.003)
        self.max_risk_pct = ds_cfg.get("max_risk_pct", 0.008)

        # Confidence scaling params
        cs = ds_cfg.get("confidence_scaling", {})
        self.conf_enabled = cs.get("enabled", False)
        self.conf_min = cs.get("min_confidence", 0.75)
        self.conf_max = cs.get("max_confidence", 0.95)

        # Volatility scaling params
        vs = ds_cfg.get("volatility_scaling", {})
        self.vol_enabled = vs.get("enabled", False)
        self.low_vol_thresh = vs.get("low_vol_threshold", 0.02)
        self.high_vol_thresh = vs.get("high_vol_threshold", 0.05)
        self.low_vol_mult = vs.get("low_vol_mult", 1.2)
        self.high_vol_mult = vs.get("high_vol_mult", 0.7)

        # Equity curve scaling params
        ec = ds_cfg.get("equity_curve_scaling", {})
        self.ec_enabled = ec.get("enabled", False)
        self.ec_lookback = ec.get("lookback_trades", 10)
        self.ec_dd_threshold = ec.get("drawdown_threshold", 0.03)
        self.ec_dd_multiplier = ec.get("drawdown_multiplier", 0.5)

    def calculate_risk_pct(
        self,
        confidence: float,
        atr: float = 0.0,
        price: float = 0.0,
        equity_history: Optional[List[float]] = None,
    ) -> float:
        """Calculate adjusted risk percentage for a trade.

        Args:
            confidence: Signal confidence score (0-1)
            atr: Current ATR value for the stock
            price: Current stock price
            equity_history: List of equity values (most recent last)

        Returns:
            Adjusted risk percentage (e.g., 0.005 for 0.5%)
        """
        if not self.enabled:
            return self.base_risk_pct

        risk_pct = self.base_risk_pct

        # 1. Confidence scaling: linear interpolation
        if self.conf_enabled and confidence > 0:
            conf_range = self.conf_max - self.conf_min
            if conf_range > 0:
                # Clamp confidence to [min, max] range
                conf_clamped = max(self.conf_min, min(self.conf_max, confidence))
                # Linear interpolation from min_risk to max_risk
                conf_frac = (conf_clamped - self.conf_min) / conf_range
                risk_pct = self.min_risk_pct + conf_frac * (self.max_risk_pct - self.min_risk_pct)
                logger.debug(
                    f"Confidence scaling: conf={confidence:.3f} -> "
                    f"risk={risk_pct*100:.3f}%"
                )

        # 2. Volatility scaling: adjust based on ATR/price ratio
        if self.vol_enabled and atr > 0 and price > 0:
            atr_pct = atr / price
            if atr_pct < self.low_vol_thresh:
                vol_mult = self.low_vol_mult
            elif atr_pct > self.high_vol_thresh:
                vol_mult = self.high_vol_mult
            else:
                # Linear interpolation between low and high vol
                vol_range = self.high_vol_thresh - self.low_vol_thresh
                vol_frac = (atr_pct - self.low_vol_thresh) / vol_range
                vol_mult = self.low_vol_mult + vol_frac * (self.high_vol_mult - self.low_vol_mult)
            risk_pct *= vol_mult
            logger.debug(
                f"Volatility scaling: atr_pct={atr_pct:.4f} -> "
                f"mult={vol_mult:.2f}, risk={risk_pct*100:.3f}%"
            )

        # 3. Equity curve scaling: reduce during drawdowns
        if self.ec_enabled and equity_history and len(equity_history) >= 2:
            recent = equity_history[-self.ec_lookback:] if len(equity_history) >= self.ec_lookback else equity_history
            peak = max(recent)
            current = recent[-1]
            if peak > 0:
                dd = (peak - current) / peak
                if dd > self.ec_dd_threshold:
                    risk_pct *= self.ec_dd_multiplier
                    logger.debug(
                        f"Equity curve scaling: dd={dd:.4f} > "
                        f"threshold={self.ec_dd_threshold:.4f}, "
                        f"risk halved to {risk_pct*100:.3f}%"
                    )

        # Clamp to absolute min/max
        risk_pct = max(self.min_risk_pct, min(self.max_risk_pct, risk_pct))
        return risk_pct

    def calculate_position_size(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        confidence: float,
        atr: float = 0.0,
        equity_history: Optional[List[float]] = None,
    ) -> int:
        """Calculate number of shares for a position.

        Args:
            equity: Current portfolio equity
            entry_price: Planned entry price
            stop_price: Planned stop loss price
            confidence: Signal confidence score
            atr: Current ATR value
            equity_history: List of equity values

        Returns:
            Number of shares (integer)
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0

        risk_pct = self.calculate_risk_pct(
            confidence=confidence,
            atr=atr,
            price=entry_price,
            equity_history=equity_history,
        )

        risk_amount = equity * risk_pct
        risk_per_share = abs(entry_price - stop_price)

        if risk_per_share <= 0:
            return 0

        shares = int(risk_amount / risk_per_share)
        return max(0, shares)
