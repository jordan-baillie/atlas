"""live/virtual_book.py — per-strategy VIRTUAL SUB-BOOK (prime-brokerage pattern).

Multiple deployed strategies share ONE paper brokerage account. If each rebalance diffed its targets
against the ACCOUNT's positions, strategies would liquidate each other's books daily and account-equity
return attribution would blend them all together. Instead each strategy keeps its own virtual book:

    data/live/<name>/book.json = {"cash": float, "positions": {symbol: qty}, "capital_base": float}

- Rebalance diffs target weights against the VIRTUAL book's positions (not the account's).
- Orders still go to the shared paper account (realistic routing/fills environment).
- Fills are applied to the virtual book at the fill/reference price.
- Realized daily return = day-over-day change of the book's mark-to-market equity — exact per-strategy
  attribution that captures fills, slippage and costs, regardless of how many strategies share the account.

The shared account becomes a pure execution simulator; the books are the accounting truth.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional
from atlas.kernel.paths import LIVE_DATA_DIR, PROJECT_ROOT

logger = logging.getLogger("atlas.live.virtual_book")

LIVE_DATA = PROJECT_ROOT / "data" / "live"


class VirtualBook:
    """Positions + cash for ONE deployed strategy, persisted to data/live/<name>/book.json."""

    def __init__(self, name: str, capital_base: float = 0.0):
        self.name = name
        self.path = LIVE_DATA / name / "book.json"
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                self.cash = float(d.get("cash", 0.0))
                self.positions = {k: int(v) for k, v in (d.get("positions") or {}).items() if int(v) != 0}
                self.capital_base = float(d.get("capital_base", capital_base))
                return
            except Exception as e:
                logger.warning("book.json corrupt for %s (%s) — refusing to guess; manual fix required", name, e)
                raise
        # New book: all cash at the strategy's capital slice.
        self.cash = float(capital_base)
        self.positions = {}
        self.capital_base = float(capital_base)

    # ── accounting ────────────────────────────────────────────────────────────
    def current_qty(self) -> dict:
        return dict(self.positions)

    def apply_fill(self, ticker: str, side: str, qty: int, price: float, multiplier: float = 1.0) -> None:
        """side: 'BUY'|'SELL' (OrderSide.value). Updates positions and cash at the given price."""
        signed = int(qty) if str(side).upper().startswith("B") else -int(qty)
        self.positions[ticker] = self.positions.get(ticker, 0) + signed
        if self.positions[ticker] == 0:
            del self.positions[ticker]
        self.cash -= signed * float(price) * float(multiplier)

    def mtm(self, prices: dict, multipliers: Optional[dict] = None) -> Optional[float]:
        """Mark-to-market equity = cash + sum(qty * px * mult). Returns None if any held name has no
        price (NEVER silently mis-mark a book — a missing price would corrupt the return series)."""
        total = self.cash
        for sym, q in self.positions.items():
            px = prices.get(sym)
            if px is None or float(px) <= 0:
                logger.warning("mtm(%s): missing price for held %s — equity unavailable", self.name, sym)
                return None
            total += q * float(px) * float((multipliers or {}).get(sym, 1.0))
        return float(total)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {"cash": round(self.cash, 2), "positions": self.positions,
             "capital_base": self.capital_base}, indent=2))

    def __repr__(self) -> str:
        return f"VirtualBook({self.name}: {len(self.positions)} positions, cash ${self.cash:,.0f})"
