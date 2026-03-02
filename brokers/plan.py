"""TradePlanGenerator — generates daily trade plans for approval.

Plans are saved to plans/ at the project root.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.allocation import build_allocation_pool

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


class TradePlanGenerator:
    """Generates daily trade plans for approval."""

    PLANS_DIR = "plans"

    def __init__(self, portfolio, config: dict):
        self.portfolio = portfolio
        self.config = config

    def generate_plan(self, signals: list, exit_recommendations: list,
                      prices: dict, trade_date: str) -> dict:
        """Generate a daily trade plan."""
        # Build allocation pool (no-op when allocation.enabled=false)
        allocation_pool = build_allocation_pool(self.config)

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

            # Simulate proposed positions for pool check (portfolio positions + already proposed)
            proposed_pos_dicts = [{"strategy": e["strategy"]} for e in proposed_entries]
            passed, reason = self.portfolio.check_risk_limits(signal, allocation_pool=allocation_pool)
            # Additional pool check against already-proposed entries in this plan
            if passed and allocation_pool.is_enabled():
                live_pos_dicts = [{"strategy": p.strategy} for p in self.portfolio.positions]
                combined_pos = live_pos_dicts + proposed_pos_dicts
                pool_ok, pool_reason = allocation_pool.can_accept(signal.strategy, combined_pos)
                if not pool_ok:
                    passed = False
                    reason = pool_reason

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
            "allocation_summary": allocation_pool.counts_summary(
                [{"strategy": p.strategy} for p in self.portfolio.positions]
            ) if allocation_pool.is_enabled() else {},
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
        # Try per-market file first, then generic
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
