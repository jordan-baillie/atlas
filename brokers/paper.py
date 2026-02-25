"""Paper broker — wraps the existing PaperPortfolio as a BrokerAdapter.

No behaviour changes to the paper engine. This is a thin adapter
so the rest of Atlas can use the unified broker interface.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from brokers.base import (
    BrokerAdapter, OrderResult, PositionInfo, AccountInfo,
    OrderStatus, OrderSide, OrderType, DealInfo,
)
from paper_engine.engine import PaperPortfolio

logger = logging.getLogger("atlas.broker.paper")


class _SignalProxy:
    """Minimal signal object to satisfy PaperPortfolio.execute_entry()."""
    def __init__(self, ticker, strategy, entry_price, stop_price,
                 take_profit, position_size, confidence, rationale, sector):
        self.ticker = ticker
        self.strategy = strategy
        self.entry_price = entry_price
        self.stop_price = stop_price
        self.take_profit = take_profit
        self.position_size = position_size
        self.confidence = confidence
        self.rationale = rationale
        self.sector = sector


class PaperBroker(BrokerAdapter):
    """Adapter that wraps PaperPortfolio for the unified broker interface."""

    def __init__(self, config: dict):
        super().__init__(config)
        self._portfolio = PaperPortfolio(config)
        # Track paper "orders" for get_open_orders / get_order_status
        self._order_log: dict[str, OrderResult] = {}

    @property
    def name(self) -> str:
        return "PaperBroker"

    @property
    def is_live(self) -> bool:
        return False

    @property
    def portfolio(self) -> PaperPortfolio:
        """Direct access to underlying portfolio for legacy code paths."""
        return self._portfolio

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        self._connected = True
        logger.info("PaperBroker connected (no-op)")
        return True

    def disconnect(self):
        self._portfolio.save_state()
        self._connected = False
        logger.info("PaperBroker disconnected, state saved")

    # ── Account ────────────────────────────────────────────────

    def get_account_info(self, prices: dict[str, float] | None = None) -> AccountInfo:
        summary = self._portfolio.portfolio_summary(prices)
        return AccountInfo(
            equity=summary["equity"],
            cash=summary["cash"],
            market_value=round(summary["equity"] - summary["cash"], 2),
            buying_power=summary["cash"],
            total_pnl=summary["total_pnl"],
            total_pnl_pct=summary["total_pnl_pct"],
            num_positions=summary["num_open"],
            currency="AUD",
            halted=summary["halted"],
        )

    def get_positions(self, prices: dict[str, float] | None = None) -> list[PositionInfo]:
        positions = []
        for p in self._portfolio.positions:
            cp = prices.get(p.ticker, p.entry_price) if prices else p.entry_price
            positions.append(PositionInfo(
                ticker=p.ticker,
                strategy=p.strategy,
                entry_date=p.entry_date,
                entry_price=p.entry_price,
                shares=p.shares,
                current_price=cp,
                market_value=round(cp * p.shares, 2),
                unrealized_pnl=p.unrealized_pnl(cp),
                unrealized_pnl_pct=p.unrealized_pnl_pct(cp),
                stop_price=p.stop_price,
                take_profit=p.take_profit,
                cost_basis=p.entry_value,
                sector=p.sector,
            ))
        return positions

    # ── Orders ─────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: OrderSide,
        qty: int,
        price: float,
        order_type: OrderType = OrderType.LIMIT,
        stop_price: Optional[float] = None,
        remark: str = "",
        # Paper-specific extras
        strategy: str = "manual",
        take_profit: Optional[float] = None,
        confidence: float = 0.5,
        rationale: str = "",
        sector: str = "Unknown",
        trade_date: str = "",
    ) -> OrderResult:
        order_id = f"paper_{uuid.uuid4().hex[:12]}"

        if side == OrderSide.BUY:
            return self._execute_buy(
                order_id, ticker, qty, price, stop_price or 0.0,
                take_profit, strategy, confidence, rationale, sector, trade_date,
            )
        else:
            return self._execute_sell(
                order_id, ticker, price, remark or "sell", trade_date,
            )

    def _execute_buy(self, order_id, ticker, qty, price, stop_price,
                     take_profit, strategy, confidence, rationale, sector,
                     trade_date) -> OrderResult:
        sig = _SignalProxy(
            ticker=ticker, strategy=strategy, entry_price=price,
            stop_price=stop_price, take_profit=take_profit,
            position_size=qty, confidence=confidence,
            rationale=rationale, sector=sector,
        )

        # Risk check
        passed, reason = self._portfolio.check_risk_limits(sig)
        if not passed:
            result = OrderResult(
                success=False, order_id=order_id, ticker=ticker,
                side=OrderSide.BUY, status=OrderStatus.FAILED,
                requested_qty=qty, requested_price=price,
                message=f"Risk check failed: {reason}",
            )
            self._order_log[order_id] = result
            return result

        fill = self._portfolio.execute_entry(sig, price, trade_date)
        result = OrderResult(
            success=True, order_id=order_id, ticker=ticker,
            side=OrderSide.BUY, status=OrderStatus.FILLED,
            requested_qty=qty, filled_qty=fill["shares"],
            requested_price=price, fill_price=fill["fill_price"],
            commission=fill["commission"],
            message="Paper fill",
            raw=fill,
        )
        self._order_log[order_id] = result
        return result

    def _execute_sell(self, order_id, ticker, price, reason, trade_date) -> OrderResult:
        pos = next((p for p in self._portfolio.positions if p.ticker == ticker), None)
        if not pos:
            result = OrderResult(
                success=False, order_id=order_id, ticker=ticker,
                side=OrderSide.SELL, status=OrderStatus.FAILED,
                message=f"No position in {ticker}",
            )
            self._order_log[order_id] = result
            return result

        fill = self._portfolio.execute_exit(ticker, price, trade_date, reason)
        if not fill:
            result = OrderResult(
                success=False, order_id=order_id, ticker=ticker,
                side=OrderSide.SELL, status=OrderStatus.FAILED,
                message=f"Exit failed for {ticker}",
            )
            self._order_log[order_id] = result
            return result

        result = OrderResult(
            success=True, order_id=order_id, ticker=ticker,
            side=OrderSide.SELL, status=OrderStatus.FILLED,
            requested_qty=fill["shares"], filled_qty=fill["shares"],
            requested_price=price, fill_price=fill["exit_price"],
            commission=fill["exit_commission"],
            message=f"Paper exit: {reason}",
            raw=fill,
        )
        self._order_log[order_id] = result
        return result

    def cancel_order(self, order_id: str) -> OrderResult:
        # Paper orders fill instantly, nothing to cancel
        return OrderResult(
            success=False, order_id=order_id,
            status=OrderStatus.FAILED,
            message="Paper orders fill instantly — nothing to cancel",
        )

    def cancel_all_orders(self) -> list[OrderResult]:
        return []  # No pending orders in paper mode

    def get_open_orders(self) -> list[OrderResult]:
        return []  # Paper orders fill instantly

    def get_order_status(self, order_id: str) -> OrderResult:
        if order_id in self._order_log:
            return self._order_log[order_id]
        return OrderResult(
            success=False, order_id=order_id,
            status=OrderStatus.UNKNOWN,
            message="Order not found",
        )

    # ── Market Data ────────────────────────────────────────────

    def get_prices(self, tickers: list[str]) -> dict[str, float]:
        # Paper broker doesn't provide live prices — caller uses yfinance cache
        return {}

    # ── Paper-specific helpers ─────────────────────────────────

    def check_risk_limits(self, signal) -> tuple[bool, str]:
        """Expose PaperPortfolio risk checks for plan generation."""
        return self._portfolio.check_risk_limits(signal)

    def check_daily_drawdown(self, prices: dict[str, float]) -> tuple[bool, float]:
        """Check daily drawdown circuit breaker."""
        return self._portfolio.check_daily_drawdown(prices)

    def update_positions(self, prices: dict[str, float]):
        """Update MAE/MFE excursions."""
        self._portfolio.update_positions(prices)

    def record_equity(self, trade_date: str, prices: dict[str, float]):
        """Record daily equity snapshot."""
        self._portfolio.record_equity(trade_date, prices)

    def save_state(self):
        """Persist portfolio state."""
        self._portfolio.save_state()
