"""Paper Trading Execution Engine with Approval Gate.

Manages paper portfolio: positions, cash, PnL, open risk.
Generates daily trade plans and simulates fills after approval.
"""

import json
import os
import logging
from datetime import datetime, date
from typing import Optional
from pathlib import Path

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


class Position:
    """Represents an open paper position."""

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
        self.stop_order_id = ""  # Moomoo protective stop order ID (empty = no exchange stop)
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
        pos.entry_commission = d.get("entry_commission", 0.0)
        return pos


class PaperPortfolio:
    """Paper trading portfolio with full state management."""

    STATE_FILE = "paper_engine/portfolio_state.json"

    def __init__(self, config: dict, market_id: str = None):
        self.config = config
        self.market_id = market_id or config.get("market", "asx")
        self.starting_equity = config["risk"]["starting_equity"]
        self.max_risk_per_trade = config["risk"]["max_risk_per_trade_pct"]
        self.max_positions = config["risk"]["max_open_positions"]
        self.max_sector_conc = config["risk"]["max_sector_concentration"]
        self.max_daily_dd = config["risk"]["max_daily_drawdown_pct"]
        self.commission_flat = config["fees"]["commission_per_trade"]
        self.commission_pct = config["fees"]["commission_pct"]
        self.slippage_pct = config["fees"]["slippage_pct"]
        self.flat_fee_threshold = config.get("fees", {}).get("flat_fee_threshold", 2000.0)

        # State
        self.cash = self.starting_equity
        self.positions: list[Position] = []
        self.closed_trades: list[dict] = []
        self.equity_history: list[dict] = []
        self.daily_high_water = self.starting_equity
        self.halted = False
        self.halt_reason = ""
        self.trade_date = ""

        # Try to load saved state
        self._load_state()

    def _state_path(self) -> Path:
        """Per-market state file. Falls back to legacy path for ASX only."""
        per_market = PROJECT_ROOT / "paper_engine" / "state" / f"{self.market_id}.json"
        if per_market.exists():
            return per_market
        # Only fall back to legacy for the default market (asx)
        if self.market_id == "asx":
            legacy = PROJECT_ROOT / self.STATE_FILE
            if legacy.exists():
                return legacy
        return per_market

    def _load_state(self):
        path = self._state_path()
        if path.exists():
            try:
                with open(path) as f:
                    state = json.load(f)
                self.cash = state.get("cash", self.starting_equity)
                self.positions = [Position.from_dict(p) for p in state.get("positions", [])]
                self.closed_trades = state.get("closed_trades", [])
                self.equity_history = state.get("equity_history", [])
                self.daily_high_water = state.get("daily_high_water", self.starting_equity)
                self.halted = state.get("halted", False)
                self.halt_reason = state.get("halt_reason", "")
                logger.info(f"Loaded portfolio state: cash={self.cash:.2f}, {len(self.positions)} positions")
            except Exception as e:
                logger.warning(f"Failed to load state: {e}. Starting fresh.")

    def save_state(self):
        # Always save to per-market path going forward
        save_path = PROJECT_ROOT / "paper_engine" / "state" / f"{self.market_id}.json"
        state = {
            "market_id": self.market_id,
            "cash": self.cash,
            "positions": [p.to_dict() for p in self.positions],
            "closed_trades": self.closed_trades,
            "equity_history": self.equity_history,
            "daily_high_water": self.daily_high_water,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "last_saved": datetime.now().isoformat(),
        }
        save_path.parent.mkdir(parents=True, exist_ok=True)
        # Audit M15: atomic write to prevent corruption on crash
        tmp_path = save_path.with_suffix('.json.tmp')
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(str(tmp_path), str(save_path))

    def equity(self, prices: dict[str, float] = None) -> float:
        """Total equity = cash + market value of positions."""
        pos_value = 0.0
        for p in self.positions:
            if prices and p.ticker in prices:
                pos_value += p.current_value(prices[p.ticker])
            else:
                pos_value += p.entry_value
        return round(self.cash + pos_value, 2)

    def _calc_commission(self, value: float) -> float:
        # Audit H4: match backtest engine's smart commission model
        pct_commission = value * self.commission_pct
        if value < self.flat_fee_threshold:
            return round(pct_commission, 2)
        return round(max(self.commission_flat, pct_commission), 2)

    def _apply_slippage(self, price: float, direction: str) -> float:
        if direction == "buy":
            return round(price * (1 + self.slippage_pct), 4)
        else:
            return round(price * (1 - self.slippage_pct), 4)

    # ── Risk checks ──────────────────────────────────────────────

    def check_risk_limits(self, signal) -> tuple[bool, str]:
        """Validate a proposed trade against all risk limits."""
        reasons = []

        # Max positions
        if len(self.positions) >= self.max_positions:
            reasons.append(f"Max positions ({self.max_positions}) reached")

        # Sector concentration
        sector = getattr(signal, "sector", "Unknown")
        sector_count = sum(1 for p in self.positions if p.sector == sector)
        if sector_count >= self.max_sector_conc:
            reasons.append(f"Max sector concentration ({self.max_sector_conc}) for {sector}")

        # Already holding this ticker
        if any(p.ticker == signal.ticker for p in self.positions):
            reasons.append(f"Already holding {signal.ticker}")

        # Risk per trade
        risk_amount = abs(signal.entry_price - signal.stop_price) * signal.position_size
        max_risk = self.equity() * self.max_risk_per_trade
        if risk_amount > max_risk * 1.1:  # 10% tolerance
            reasons.append(f"Risk ${risk_amount:.2f} exceeds max ${max_risk:.2f}")

        # Sufficient cash
        cost = signal.entry_price * signal.position_size + self._calc_commission(signal.entry_price * signal.position_size)
        if cost > self.cash:
            reasons.append(f"Insufficient cash: need ${cost:.2f}, have ${self.cash:.2f}")

        # Daily drawdown halt
        if self.halted:
            reasons.append(f"Trading halted: {self.halt_reason}")

        if reasons:
            return False, "; ".join(reasons)
        return True, "All checks passed"

    def check_daily_drawdown(self, prices: dict[str, float]):
        """Check if daily drawdown limit breached."""
        current_eq = self.equity(prices)
        self.daily_high_water = max(self.daily_high_water, current_eq)
        dd = (self.daily_high_water - current_eq) / self.daily_high_water
        if dd >= self.max_daily_dd:
            self.halted = True
            self.halt_reason = f"Daily drawdown {dd:.2%} >= {self.max_daily_dd:.2%}"
            logger.warning(f"HALT: {self.halt_reason}")
            return True, dd
        return False, dd

    # ── Execution ────────────────────────────────────────────────

    def execute_entry(self, signal, fill_price: float, trade_date: str) -> dict:
        """Simulate a buy fill."""
        slipped_price = self._apply_slippage(fill_price, "buy")
        position_value = slipped_price * signal.position_size
        commission = self._calc_commission(position_value)
        total_cost = position_value + commission

        pos = Position(
            ticker=signal.ticker,
            strategy=signal.strategy,
            entry_date=trade_date,
            entry_price=slipped_price,
            shares=signal.position_size,
            stop_price=signal.stop_price,
            take_profit=signal.take_profit,
            confidence=signal.confidence,
            rationale=signal.rationale,
            sector=getattr(signal, "sector", "Unknown"),
        )
        pos.entry_commission = commission  # Audit M7: store for accurate PnL on exit

        self.cash -= total_cost
        self.positions.append(pos)
        self.save_state()

        fill_record = {
            "type": "entry",
            "ticker": signal.ticker,
            "strategy": signal.strategy,
            "date": trade_date,
            "signal_price": signal.entry_price,
            "fill_price": slipped_price,
            "shares": signal.position_size,
            "commission": commission,
            "total_cost": total_cost,
            "slippage": round(slipped_price - fill_price, 4),
        }
        logger.info(f"ENTRY: {signal.ticker} {signal.position_size}@{slipped_price:.2f} cost=${total_cost:.2f}")
        return fill_record

    def execute_exit(self, ticker: str, fill_price: float, trade_date: str,
                     reason: str) -> Optional[dict]:
        """Simulate a sell fill."""
        pos = next((p for p in self.positions if p.ticker == ticker), None)
        if not pos:
            logger.warning(f"No position found for {ticker}")
            return None

        slipped_price = self._apply_slippage(fill_price, "sell")
        proceeds = slipped_price * pos.shares
        commission = self._calc_commission(proceeds)
        net_proceeds = proceeds - commission

        pnl = net_proceeds - pos.entry_value - pos.entry_commission  # Audit M7: use stored entry commission
        pnl_pct = pnl / pos.entry_value * 100

        self.cash += net_proceeds
        self.positions.remove(pos)

        trade_record = {
            "ticker": ticker,
            "strategy": pos.strategy,
            "entry_date": pos.entry_date,
            "exit_date": trade_date,
            "entry_price": pos.entry_price,
            "exit_price": slipped_price,
            "shares": pos.shares,
            "entry_value": pos.entry_value,
            "exit_value": round(proceeds, 2),
            "entry_commission": pos.entry_commission,  # Audit M7: use stored entry commission
            "exit_commission": commission,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "mae": round(pos.mae * 100, 2),
            "mfe": round(pos.mfe * 100, 2),
            "holding_days": pos.holding_days(trade_date),
            "exit_reason": reason,
            "confidence": pos.confidence,
        }

        self.closed_trades.append(trade_record)
        self.save_state()

        logger.info(f"EXIT: {ticker} {pos.shares}@{slipped_price:.2f} PnL=${pnl:.2f} ({pnl_pct:+.1f}%) [{reason}]")
        return trade_record

    def update_positions(self, prices: dict[str, float]):
        """Update MAE/MFE for all open positions."""
        for pos in self.positions:
            if pos.ticker in prices:
                pos.update_excursions(prices[pos.ticker])

    def record_equity(self, trade_date: str, prices: dict[str, float]):
        """Record daily equity snapshot."""
        eq = self.equity(prices)
        self.equity_history.append({
            "date": trade_date,
            "equity": eq,
            "cash": self.cash,
            "positions_value": round(eq - self.cash, 2),
            "num_positions": len(self.positions),
        })
        self.save_state()

    def reset_daily_halt(self):
        """Reset halt flag at start of new trading day."""
        if self.halted:
            logger.info(f"Resetting daily halt (was: {self.halt_reason})")
        self.halted = False
        self.halt_reason = ""

    # ── Reporting ────────────────────────────────────────────────

    def portfolio_summary(self, prices: dict[str, float] = None) -> dict:
        """Generate portfolio summary."""
        eq = self.equity(prices)
        total_pnl = eq - self.starting_equity
        total_pnl_pct = total_pnl / self.starting_equity * 100

        open_positions = []
        for p in self.positions:
            price = prices.get(p.ticker, p.entry_price) if prices else p.entry_price
            open_positions.append({
                "ticker": p.ticker,
                "strategy": p.strategy,
                "entry_date": p.entry_date,
                "entry_price": p.entry_price,
                "current_price": price,
                "shares": p.shares,
                "unrealized_pnl": p.unrealized_pnl(price),
                "unrealized_pnl_pct": p.unrealized_pnl_pct(price),
                "stop_price": p.stop_price,
                "take_profit": p.take_profit,
                "sector": p.sector,
                "mae_pct": round(p.mae * 100, 2),
                "mfe_pct": round(p.mfe * 100, 2),
                "holding_days": p.holding_days(datetime.now().strftime("%Y-%m-%d")),
            })

        return {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "equity": eq,
            "cash": self.cash,
            "starting_equity": self.starting_equity,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "open_positions": open_positions,
            "num_open": len(self.positions),
            "num_closed_trades": len(self.closed_trades),
            "halted": self.halted,
        }


class TradePlanGenerator:
    """Generates daily trade plans for approval."""

    PLANS_DIR = "paper_engine/plans"

    def __init__(self, portfolio: PaperPortfolio, config: dict):
        self.portfolio = portfolio
        self.config = config

    def generate_plan(self, signals: list, exit_recommendations: list,
                      prices: dict[str, float], trade_date: str) -> dict:
        """Generate a daily trade plan."""
        # Risk check each signal
        proposed_entries = []
        rejected_entries = []
        min_confidence = self.config.get("risk", {}).get("min_confidence", 0.0)
        max_positions = self.config.get("risk", {}).get("max_open_positions", 5)
        available_slots = max_positions - len(self.portfolio.positions)
        for signal in signals:
            # Build a rich entry dict with all signal data for future analysis
            base_entry = {
                "ticker": signal.ticker,
                "strategy": signal.strategy,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_price,
                "take_profit": signal.take_profit,
                "position_size": signal.position_size,
                "position_value": round(signal.entry_price * signal.position_size, 2),
                "risk_amount": round(abs(signal.entry_price - signal.stop_price) * signal.position_size, 2),
                "confidence": signal.confidence,
                "rationale": signal.rationale,
                "features": getattr(signal, "features", {}),
                "sector": getattr(signal, "sector", "Unknown"),
                "market_id": getattr(signal, "market_id", self.config.get("market", "")),
            }

            # Cap entries at available position slots
            if len(proposed_entries) >= available_slots:
                base_entry["rejection_reason"] = f"Max positions ({max_positions}) would be exceeded"
                rejected_entries.append(base_entry)
                continue

            # Filter by minimum confidence threshold
            if signal.confidence < min_confidence:
                base_entry["rejection_reason"] = f"Confidence {signal.confidence:.3f} below threshold {min_confidence}"
                rejected_entries.append(base_entry)
                continue

            passed, reason = self.portfolio.check_risk_limits(signal)
            if passed:
                proposed_entries.append(base_entry)
            else:
                base_entry["rejection_reason"] = reason
                rejected_entries.append(base_entry)

        # Portfolio state after proposed trades
        proposed_cost = sum(e["entry_price"] * e["position_size"] for e in proposed_entries)
        proposed_risk = sum(e["risk_amount"] for e in proposed_entries)

        current_eq = self.portfolio.equity(prices)
        summary = self.portfolio.portfolio_summary(prices)

        market_id = self.config.get("market", "")
        plan = {
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(),
            "market_id": market_id,
            "config_version": self.config.get("version", ""),
            "status": "PENDING_APPROVAL",
            "portfolio_snapshot": {
                "equity": current_eq,
                "cash": self.portfolio.cash,
                "open_positions": len(self.portfolio.positions),
                "total_pnl": summary["total_pnl"],
                "total_pnl_pct": summary["total_pnl_pct"],
            },
            "proposed_entries": proposed_entries,
            "rejected_entries": rejected_entries,
            "proposed_exits": exit_recommendations,
            "total_signals_generated": len(signals),
            "risk_summary": {
                "total_proposed_cost": round(proposed_cost, 2),
                "total_proposed_risk": round(proposed_risk, 2),
                "risk_pct_of_equity": round(proposed_risk / current_eq * 100, 2) if current_eq > 0 else 0,
                "positions_after": len(self.portfolio.positions) + len(proposed_entries) - len(exit_recommendations),
                "cash_after_entries": round(self.portfolio.cash - proposed_cost, 2),
                "portfolio_exposure_pct": round((current_eq - self.portfolio.cash + proposed_cost) / current_eq * 100, 2) if current_eq > 0 else 0,
            },
            "open_positions": summary["open_positions"],
        }

        # Save plan
        self._save_plan(plan, trade_date)
        return plan

    def _save_plan(self, plan: dict, trade_date: str):
        market_id = plan.get("market_id", "") or self.config.get("market", "")
        plans_dir = PROJECT_ROOT / self.PLANS_DIR
        plans_dir.mkdir(parents=True, exist_ok=True)
        # Per-market plan file (e.g. plan_asx_2026-03-02.json)
        if market_id:
            path = plans_dir / f"plan_{market_id}_{trade_date}.json"
        else:
            path = plans_dir / f"plan_{trade_date}.json"
        with open(path, "w") as f:
            json.dump(plan, f, indent=2, default=str)
        logger.info(f"Trade plan saved: {path}")

    def load_plan(self, trade_date: str, market_id: str = "") -> Optional[dict]:
        plans_dir = PROJECT_ROOT / self.PLANS_DIR
        market_id = market_id or self.config.get("market", "")
        # Try per-market file first, fall back to legacy shared file
        candidates = []
        if market_id:
            candidates.append(plans_dir / f"plan_{market_id}_{trade_date}.json")
        candidates.append(plans_dir / f"plan_{trade_date}.json")
        for path in candidates:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        return None

    def approve_plan(self, trade_date: str, market_id: str = "") -> Optional[dict]:
        """Mark a plan as approved."""
        plan = self.load_plan(trade_date, market_id=market_id)
        if plan:
            plan["status"] = "APPROVED"
            plan["approved_at"] = datetime.now().isoformat()
            self._save_plan(plan, trade_date)
            return plan
        return None

    def format_plan_text(self, plan: dict) -> str:
        """Format trade plan as readable text."""
        # Audit M2: use single quotes inside f-string expressions (Python < 3.12 compat)
        lines = []
        lines.append(f"═══════════════════════════════════════════════")
        lines.append(f"  DAILY TRADE PLAN — {plan['trade_date']}")
        lines.append(f"  Status: {plan['status']}")
        lines.append(f"═══════════════════════════════════════════════")
        lines.append("")

        snap = plan["portfolio_snapshot"]
        lines.append(f"📊 PORTFOLIO: Equity ${snap['equity']:,.2f} | "
                     f"Cash ${snap['cash']:,.2f} | "
                     f"PnL ${snap['total_pnl']:+,.2f} ({snap['total_pnl_pct']:+.1f}%) | "
                     f"Positions {snap['open_positions']}")
        lines.append("")

        # Proposed entries
        if plan["proposed_entries"]:
            lines.append(f"🟢 PROPOSED ENTRIES ({len(plan['proposed_entries'])})")
            lines.append(f"{'Ticker':<8} {'Strategy':<20} {'Entry':>8} {'Stop':>8} {'Size':>5} {'Risk$':>7} {'Conf':>5}")
            lines.append(f"{'─'*8} {'─'*20} {'─'*8} {'─'*8} {'─'*5} {'─'*7} {'─'*5}")
            for e in plan["proposed_entries"]:
                lines.append(f"{e['ticker']:<8} {e['strategy']:<20} "
                             f"${e['entry_price']:>7.2f} ${e['stop_price']:>7.2f} "
                             f"{e['position_size']:>5} ${e['risk_amount']:>6.2f} "
                             f"{e['confidence']:>5.2f}")
                lines.append(f"  → {e['rationale']}")
            lines.append("")

        # Rejected
        if plan["rejected_entries"]:
            lines.append(f"🔴 REJECTED ({len(plan['rejected_entries'])})")
            for e in plan["rejected_entries"]:
                lines.append(f"  {e['ticker']} ({e['strategy']}): {e['rejection_reason']}")
            lines.append("")

        # Exits
        if plan["proposed_exits"]:
            lines.append(f"🟡 PROPOSED EXITS ({len(plan['proposed_exits'])})")
            for ex in plan["proposed_exits"]:
                lines.append(f"  {ex.get('ticker', '?')} — {ex.get('reason', '?')}")
            lines.append("")

        # Risk summary
        risk = plan["risk_summary"]
        lines.append(f"⚠️  RISK: Cost ${risk['total_proposed_cost']:,.2f} | "
                     f"Risk ${risk['total_proposed_risk']:,.2f} | "
                     f"Positions after: {risk['positions_after']} | "
                     f"Exposure: {risk['portfolio_exposure_pct']:,.1f}%")
        lines.append("")

        # Open positions
        if plan["open_positions"]:
            lines.append(f"📋 OPEN POSITIONS ({len(plan['open_positions'])})")
            lines.append(f"{'Ticker':<8} {'Entry':>8} {'Current':>8} {'PnL$':>8} {'PnL%':>7} {'Stop':>8}")
            lines.append(f"{'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*7} {'─'*8}")
            for p in plan["open_positions"]:
                lines.append(f"{p['ticker']:<8} ${p['entry_price']:>7.2f} "
                             f"${p['current_price']:>7.2f} "
                             f"${p['unrealized_pnl']:>+7.2f} "
                             f"{p['unrealized_pnl_pct']:>+6.1f}% "
                             f"${p['stop_price']:>7.2f}")
            lines.append("")

        lines.append("⏳ Reply APPROVED to execute, or REJECT to skip.")
        return "\n".join(lines)
