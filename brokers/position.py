"""Position — represents a single open trading position.

Position data class used across broker adapters.
"""

from typing import Optional

import pandas as pd


class Position:
    """Represents an open trading position."""

    def __init__(self, ticker: str, strategy: str, entry_date: str,
                 entry_price: float, shares: int, stop_price: float,
                 take_profit: Optional[float], confidence: float,
                 rationale: str, sector: str = "Unknown"):
        self.ticker = ticker
        self.strategy = strategy
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.shares = shares
        self.stop_price = stop_price
        self.take_profit = take_profit
        self.confidence = confidence
        self.rationale = rationale
        self.sector = sector
        self.entry_value = round(entry_price * shares, 2)
        self.mae = 0.0  # max adverse excursion
        self.mfe = 0.0  # max favorable excursion
        self.stop_order_id = ""  # Broker SL order ID (stop-loss placed on exchange)
        self.tp_order_id = ""   # Broker TP order ID (take-profit placed on exchange, empty = none)
        self.entry_commission = 0.0  # Audit M7: track entry commission for accurate PnL

    def current_value(self, price: float) -> float:
        return round(price * self.shares, 2)

    def unrealized_pnl(self, price: float) -> float:
        return round((price - self.entry_price) * self.shares, 2)

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.entry_price == 0:
            return 0.0
        return round((price - self.entry_price) / self.entry_price * 100, 2)

    def holding_days(self, current_date: str) -> int:
        entry = pd.Timestamp(self.entry_date)
        current = pd.Timestamp(current_date)
        return (current - entry).days

    def update_excursions(self, price: float):
        pnl_pct = (price - self.entry_price) / self.entry_price
        self.mae = min(self.mae, pnl_pct)
        self.mfe = max(self.mfe, pnl_pct)

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "strategy": self.strategy,
            "entry_date": self.entry_date,
            "entry_price": self.entry_price,
            "shares": self.shares,
            "stop_price": self.stop_price,
            "take_profit": self.take_profit,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "sector": self.sector,
            "entry_value": self.entry_value,
            "mae": self.mae,
            "mfe": self.mfe,
            "stop_order_id": self.stop_order_id,
            "tp_order_id": self.tp_order_id,
            "entry_commission": self.entry_commission,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Position":
        pos = cls(
            ticker=d["ticker"], strategy=d["strategy"],
            entry_date=d["entry_date"], entry_price=d["entry_price"],
            shares=d["shares"], stop_price=d["stop_price"],
            take_profit=d.get("take_profit"),
            confidence=d.get("confidence", 0.5),
            rationale=d.get("rationale", ""),
            sector=d.get("sector", "Unknown"),
        )
        pos.mae = d.get("mae", 0.0)
        pos.mfe = d.get("mfe", 0.0)
        pos.entry_value = d.get("entry_value", pos.entry_price * pos.shares)
        pos.stop_order_id = d.get("stop_order_id", "")
        pos.tp_order_id = d.get("tp_order_id", "")
        pos.entry_commission = d.get("entry_commission", 0.0)
        return pos
