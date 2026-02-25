"""
Atlas Sector Rotation Strategy (Phase 8D)
=============================================
Top-down strategy that selects sectors by momentum, then buys
the strongest stocks within those sectors.

Config Section: strategies.sector_rotation
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from strategies.base import BaseStrategy, Signal
from utils.helpers import calc_atr, calc_position_size

logger = logging.getLogger(__name__)

SECTOR_MAP_PATH = Path(__file__).parent.parent / "data" / "processed" / "sector_map.json"


def load_sector_map(path: Path = SECTOR_MAP_PATH) -> Dict[str, str]:
    if not path.exists():
        logger.warning(f"Sector map not found at {path}")
        return {}
    with open(path) as fh:
        return json.load(fh)


class SectorRotation(BaseStrategy):
    """Sector rotation: buy strongest stocks in top-momentum sectors."""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        strat_cfg = config.get("strategies", {}).get("sector_rotation", {})
        self.sector_momentum_period = strat_cfg.get("sector_momentum_period", 60)
        self.top_sectors = strat_cfg.get("top_sectors", 3)
        self.bottom_sectors = strat_cfg.get("bottom_sectors", 2)
        self.rebalance_days = strat_cfg.get("rebalance_days", 20)
        self.atr_period = strat_cfg.get("atr_period", 14)
        self.atr_stop_mult = strat_cfg.get("atr_stop_mult", 3.0)
        self.trailing_stop_atr_mult = strat_cfg.get("trailing_stop_atr_mult", 3.5)
        self.max_hold_days = strat_cfg.get("max_hold_days", 25)
        self.min_sector_stocks = strat_cfg.get("min_sector_stocks", 3)
        self.stocks_per_sector = strat_cfg.get("stocks_per_sector", 2)
        self.sector_map = load_sector_map()
        self._logger.info(
            f"SectorRotation init: momentum={self.sector_momentum_period}, "
            f"top={self.top_sectors}, bottom={self.bottom_sectors}, "
            f"sectors={len(self.sector_map)}"
        )

    @property
    def name(self) -> str:
        return "sector_rotation"

    def _classify_stocks_by_sector(self, data: Dict[str, pd.DataFrame]) -> Dict[str, List[str]]:
        sectors: Dict[str, List[str]] = {}
        for ticker in data:
            sector = self.sector_map.get(ticker, "Unknown")
            if sector == "Unknown":
                continue
            sectors.setdefault(sector, []).append(ticker)
        return sectors

    def _calc_sector_momentum(self, data: Dict[str, pd.DataFrame], sector_stocks: Dict[str, List[str]]) -> Dict[str, float]:
        sector_momentum = {}
        for sector, tickers in sector_stocks.items():
            if len(tickers) < self.min_sector_stocks:
                continue
            rocs = []
            for ticker in tickers:
                df = data.get(ticker)
                if df is None or len(df) < self.sector_momentum_period + 5:
                    continue
                close = df["close"]
                prev = close.iloc[-self.sector_momentum_period]
                if prev > 0:
                    roc = (close.iloc[-1] / prev - 1.0) * 100
                    if not np.isnan(roc) and abs(roc) < 200:
                        rocs.append(roc)
            if len(rocs) >= self.min_sector_stocks:
                sector_momentum[sector] = float(np.median(rocs))
        return sector_momentum

    def _rank_sectors(self, sector_momentum: Dict[str, float]) -> Tuple[List[str], List[str]]:
        if not sector_momentum:
            return [], []
        ranked = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)
        top = [s[0] for s in ranked[:self.top_sectors]]
        bottom = [s[0] for s in ranked[-self.bottom_sectors:]]
        return top, bottom

    def _select_stocks_in_sector(self, data: Dict[str, pd.DataFrame], sector_tickers: List[str], held_tickers: set) -> List[Tuple[str, float]]:
        candidates = []
        for ticker in sector_tickers:
            if ticker in held_tickers:
                continue
            df = data.get(ticker)
            if df is None or len(df) < 60:
                continue
            close = df["close"]
            scores = []
            for period in [20, 60]:
                if len(close) >= period + 1 and close.iloc[-period] > 0:
                    roc = close.iloc[-1] / close.iloc[-period] - 1.0
                    if not np.isnan(roc):
                        scores.append(roc)
            if scores:
                rs = 0.3 * scores[0] + 0.7 * scores[1] if len(scores) == 2 else scores[0]
                if rs > 0:
                    candidates.append((ticker, rs))
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:self.stocks_per_sector]

    def generate_signals(
        self,
        data: Dict[str, pd.DataFrame],
        equity: float,
        existing_positions: List[Dict[str, Any]],
    ) -> List[Signal]:
        """Generate sector rotation entry signals."""
        signals: List[Signal] = []
        held_tickers = self._get_held_tickers(existing_positions)
        risk_pct = self.risk_config.get("max_risk_per_trade_pct", 0.005)
        min_rows = max(self.sector_momentum_period, self.atr_period) + 20

        if not self.sector_map:
            self._logger.warning("No sector map loaded")
            return signals

        # Step 1: Classify stocks by sector
        sector_stocks = self._classify_stocks_by_sector(data)

        # Step 2: Calculate sector momentum
        sector_momentum = self._calc_sector_momentum(data, sector_stocks)

        if len(sector_momentum) < 3:
            self._logger.debug(f"Only {len(sector_momentum)} sectors, need 3+")
            return signals

        # Step 3: Rank sectors
        top_sectors, bottom_sectors = self._rank_sectors(sector_momentum)
        self._logger.debug(f"Sectors: top={top_sectors}, bottom={bottom_sectors}")

        # Step 4: Generate signals for top sector stocks
        for sector in top_sectors:
            tickers = sector_stocks.get(sector, [])
            if not tickers:
                continue
            if not self._can_open_position(existing_positions):
                break

            selected = self._select_stocks_in_sector(data, tickers, held_tickers)

            for ticker, rs_score in selected:
                if not self._can_open_position(existing_positions):
                    break
                df = data.get(ticker)
                if df is None or not self._has_sufficient_data(df, min_rows):
                    continue

                close = df["close"]
                high = df["high"]
                low = df["low"]
                today_close = close.iloc[-1]
                if today_close <= 0:
                    continue

                atr_series = calc_atr(high, low, close, self.atr_period)
                if atr_series is None or atr_series.empty:
                    continue
                atr = float(atr_series.iloc[-1])
                if np.isnan(atr) or atr <= 0:
                    continue

                entry_price = today_close
                stop_price = entry_price - self.atr_stop_mult * atr
                if stop_price <= 0 or stop_price >= entry_price:
                    continue

                try:
                    pos = calc_position_size(
                        equity=equity, risk_pct=risk_pct,
                        entry_price=entry_price, stop_price=stop_price,
                    )
                except ValueError:
                    continue
                if pos["shares"] <= 0:
                    continue

                position_size = pos["shares"]
                position_value = pos["position_value"]
                risk_amount = pos["total_risk"]
                sect_mom = sector_momentum.get(sector, 0)

                # Confidence scoring
                base_confidence = 0.72
                momentum_bonus = min(0.15, max(0, sect_mom / 30.0 * 0.15))
                rs_bonus = min(0.1, max(0, rs_score / 0.2 * 0.1))
                confidence = min(1.0, base_confidence + momentum_bonus + rs_bonus)

                rationale = (
                    f"Sector rotation: {sector} ranked top "
                    f"(mom={sect_mom:.1f}%). {ticker} RS={rs_score:.3f}. "
                    f"Entry={entry_price:.2f}, Stop={stop_price:.2f} "
                    f"({self.atr_stop_mult}x ATR)."
                )

                try:
                    signal = Signal(
                        ticker=ticker, strategy=self.name,
                        direction="long", entry_price=entry_price,
                        stop_price=stop_price, take_profit=None,
                        position_size=position_size,
                        position_value=position_value,
                        risk_amount=risk_amount,
                        confidence=confidence,
                        rationale=rationale,
                        features={
                            "sector": sector,
                            "sector_momentum": sect_mom,
                            "sector_rank": top_sectors.index(sector) + 1,
                            "rs_score": float(rs_score),
                            "atr": float(atr),
                        },
                    )
                    signals.append(signal)
                    held_tickers.add(ticker)
                    self._logger.debug(
                        f"SECTOR_ROT: {ticker} in {sector} "
                        f"(mom={sect_mom:.1f}%, rs={rs_score:.3f}, conf={confidence:.2f})"
                    )
                except (ValueError, Exception) as e:
                    self._logger.debug(f"{ticker}: signal failed: {e}")
                    continue

        return signals

    def check_exits(
        self,
        data: Dict[str, pd.DataFrame],
        positions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Check sector rotation positions for exit conditions."""
        exit_recs: List[Dict[str, Any]] = []

        for pos in positions:
            if pos.get("strategy") != self.name:
                continue

            ticker = pos["ticker"]
            df = data.get(ticker)
            if df is None or df.empty:
                continue

            close = df["close"]
            high = df["high"]
            low = df["low"]
            today_close = close.iloc[-1]
            today_low = low.iloc[-1]
            entry_price = pos["entry_price"]
            entry_date = pos.get("entry_date")
            stop_price = pos.get("stop_price", 0)

            # Calculate days held
            days_held = 0
            if entry_date:
                try:
                    entry_dt = pd.Timestamp(entry_date)
                    days_held = len(df.loc[entry_dt:])
                except Exception:
                    pass

            # Exit 1: Stop loss
            if today_low <= stop_price:
                exit_recs.append({
                    "ticker": ticker, "reason": "stop_hit",
                    "exit_price": stop_price,
                    "details": f"Stop hit at {stop_price:.2f} (low={today_low:.2f})"
                })
                continue

            # Exit 2: Trailing stop
            if entry_date:
                try:
                    entry_dt = pd.Timestamp(entry_date)
                    post_entry = df.loc[entry_dt:]
                    if len(post_entry) > 1:
                        highest = post_entry["high"].max()
                        atr_series = calc_atr(high, low, close, self.atr_period)
                        if atr_series is not None and not atr_series.empty:
                            atr = float(atr_series.iloc[-1])
                            if not np.isnan(atr) and atr > 0:
                                trail_stop = highest - self.trailing_stop_atr_mult * atr
                                if today_low <= trail_stop and trail_stop > stop_price:
                                    exit_recs.append({
                                        "ticker": ticker, "reason": "trailing_stop",
                                        "exit_price": trail_stop,
                                        "details": f"Trailing stop at {trail_stop:.2f} (high={highest:.2f})"
                                    })
                                    continue
                except Exception:
                    pass

            # Exit 3: Max hold period
            if days_held >= self.max_hold_days:
                exit_recs.append({
                    "ticker": ticker, "reason": "time_exit",
                    "exit_price": today_close,
                    "details": f"Max hold {self.max_hold_days}d reached ({days_held}d held)"
                })
                continue

        return exit_recs
