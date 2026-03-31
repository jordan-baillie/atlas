"""Journaling and Observability System.

Maintains:
- Decision Journal: every signal, features, rationale, parameters
- Trade Ledger: fills, stops, exits, PnL, MAE/MFE
- Mistake Log: categorized failures with impact analysis
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
JOURNAL_DIR = PROJECT_ROOT / "journal"


class DecisionJournal:
    """Records every signal generated, whether acted upon or not."""

    FILE = JOURNAL_DIR / "decision_journal.json"

    def __init__(self):
        self.entries = self._load()

    def _load(self) -> list:
        if self.FILE.exists():
            try:
                with open(self.FILE) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.FILE, "w") as f:
            json.dump(self.entries, f, indent=2, default=str)

    def record_signal(self, signal, action: str, reason: str = "",
                      config_version: str = "", market_id: str = ""):
        """Record a signal decision.

        Args:
            signal: Signal object from strategy
            action: 'accepted', 'rejected', 'filtered'
            reason: Why this action was taken
            config_version: Current config version
            market_id: Market identifier (e.g., 'sp500', 'asx')
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "ticker": signal.ticker,
            "strategy": signal.strategy,
            "direction": getattr(signal, "direction", "long"),
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
            "market_id": market_id or getattr(signal, "market_id", ""),
            "action": action,
            "action_reason": reason,
            "config_version": config_version,
        }
        self.entries.append(entry)
        self._save()
        # SQLite dual-write — JSON file is source of truth; SQLite failure is non-fatal
        try:
            from db import atlas_db
            atlas_db.record_signal(
                timestamp=entry['timestamp'],
                ticker=entry['ticker'],
                strategy=entry['strategy'],
                universe=entry.get('market_id') or 'sp500',
                direction=entry.get('direction', 'long'),
                entry_price=entry['entry_price'],
                stop_price=entry['stop_price'],
                take_profit=entry.get('take_profit'),
                position_size=entry.get('position_size', 0),
                position_value=entry.get('position_value', 0),
                risk_amount=entry.get('risk_amount', 0),
                confidence=entry.get('confidence', 0),
                rationale=entry.get('rationale'),
                features=entry.get('features'),
                sector=entry.get('sector'),
                action=entry.get('action', 'proposed'),
                action_reason=entry.get('action_reason'),
                config_version=entry.get('config_version'),
                market_id=entry.get('market_id', 'sp500'),
            )
        except Exception as _db_exc:
            logger.warning(f"SQLite signal dual-write failed for {signal.ticker}: {_db_exc}")
        logger.info(f"Decision recorded: {signal.ticker} ({signal.strategy}) -> {action}")

    def get_entries(self, ticker: str = None, strategy: str = None,
                    action: str = None, days: int = None) -> list:
        """Filter journal entries."""
        results = self.entries
        if ticker:
            results = [e for e in results if e["ticker"] == ticker]
        if strategy:
            results = [e for e in results if e["strategy"] == strategy]
        if action:
            results = [e for e in results if e["action"] == action]
        if days:
            cutoff = (datetime.now() - pd.Timedelta(days=days)).isoformat()
            results = [e for e in results if e["timestamp"] >= cutoff]
        return results

    def summary(self, days: int = 7) -> dict:
        """Summary statistics for recent decisions."""
        recent = self.get_entries(days=days)
        if not recent:
            return {"total": 0, "period_days": days}

        by_action = {}
        by_strategy = {}
        for e in recent:
            by_action[e["action"]] = by_action.get(e["action"], 0) + 1
            by_strategy[e["strategy"]] = by_strategy.get(e["strategy"], 0) + 1

        return {
            "total": len(recent),
            "period_days": days,
            "by_action": by_action,
            "by_strategy": by_strategy,
            "avg_confidence": round(
                sum(e["confidence"] for e in recent) / len(recent), 3
            ),
        }


class TradeLedger:
    """Records all executed trades with full details."""

    FILE = JOURNAL_DIR / "trade_ledger.json"

    def __init__(self):
        self.trades = self._load()

    def _load(self) -> list:
        if self.FILE.exists():
            try:
                with open(self.FILE) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.FILE, "w") as f:
            json.dump(self.trades, f, indent=2, default=str)

    def record_entry(self, fill_record: dict):
        """Record an entry fill."""
        fill_record["recorded_at"] = datetime.now().isoformat()
        self.trades.append({"type": "entry", **fill_record})
        self._save()
        # SQLite dual-write — JSON file is source of truth; SQLite failure is non-fatal
        try:
            from db import atlas_db
            atlas_db.record_trade_entry(
                ticker=fill_record.get('ticker', ''),
                strategy=fill_record.get('strategy', ''),
                universe=fill_record.get('market_id', 'sp500') or 'sp500',
                entry_price=float(fill_record.get('fill_price', 0) or 0),
                shares=int(fill_record.get('shares', 0) or 0),
                stop_price=float(fill_record.get('stop_price', 0) or 0),
                take_profit=None,
                confidence=float(fill_record.get('confidence', 0) or 0),
                regime_state=fill_record.get('regime_state'),
            )
        except Exception as _db_exc:
            logger.warning(f"SQLite trade entry dual-write failed for {fill_record.get('ticker')}: {_db_exc}")
        logger.info(f"Ledger entry: BUY {fill_record.get('ticker')} "
                    f"{fill_record.get('shares')}@{fill_record.get('fill_price')}")

    def record_exit(self, trade_record: dict):
        """Record a completed trade (exit)."""
        trade_record["recorded_at"] = datetime.now().isoformat()
        self.trades.append({"type": "exit", **trade_record})
        self._save()
        # SQLite dual-write — JSON file is source of truth; SQLite failure is non-fatal
        try:
            from db import atlas_db
            atlas_db.record_trade_exit(
                ticker=trade_record.get('ticker', ''),
                strategy=trade_record.get('strategy', ''),
                exit_price=float(trade_record.get('fill_price', trade_record.get('exit_price', 0)) or 0),
                exit_reason=trade_record.get('exit_reason', ''),
            )
        except Exception as _db_exc:
            logger.warning(f"SQLite trade exit dual-write failed for {trade_record.get('ticker')}: {_db_exc}")
        logger.info(f"Ledger exit: SELL {trade_record.get('ticker')} "
                    f"PnL=${trade_record.get('pnl', 0):.2f}")

    def get_closed_trades(self, days: int = None, strategy: str = None) -> list:
        """Get completed trades."""
        results = [t for t in self.trades if t.get("type") == "exit"]
        if strategy:
            results = [t for t in results if t.get("strategy") == strategy]
        if days:
            cutoff = (datetime.now() - pd.Timedelta(days=days)).isoformat()
            results = [t for t in results if t.get("recorded_at", "") >= cutoff]
        return results

    @staticmethod
    def _n(v):
        """Coerce to number, treating None/missing as 0."""
        return v if isinstance(v, (int, float)) else 0

    def performance_summary(self, days: int = None) -> dict:
        """Calculate performance metrics from trade ledger."""
        trades = self.get_closed_trades(days=days)
        if not trades:
            return {"total_trades": 0}

        pnls = [self._n(t.get("pnl")) for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]

        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 0

        return {
            "total_trades": len(trades),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(len(winners) / len(trades) * 100, 1) if trades else 0,
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(trades), 2),
            "avg_winner": round(sum(winners) / len(winners), 2) if winners else 0,
            "avg_loser": round(sum(losers) / len(losers), 2) if losers else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf"),
            "largest_win": round(max(pnls), 2),
            "largest_loss": round(min(pnls), 2),
            "avg_mae": round(
                sum(self._n(t.get("mae")) for t in trades) / len(trades), 2
            ),
            "avg_mfe": round(
                sum(self._n(t.get("mfe")) for t in trades) / len(trades), 2
            ),
            "avg_holding_days": round(
                sum(self._n(t.get("holding_days")) for t in trades) / len(trades), 1
            ),
            "by_strategy": self._by_strategy(trades),
        }

    def _by_strategy(self, trades: list) -> dict:
        """Break down performance by strategy."""
        strategies = {}
        for t in trades:
            s = t.get("strategy", "unknown")
            if s not in strategies:
                strategies[s] = {"trades": 0, "pnl": 0, "winners": 0}
            strategies[s]["trades"] += 1
            strategies[s]["pnl"] += self._n(t.get("pnl"))
            if self._n(t.get("pnl")) > 0:
                strategies[s]["winners"] += 1

        for s in strategies:
            n = strategies[s]["trades"]
            strategies[s]["win_rate"] = round(strategies[s]["winners"] / n * 100, 1) if n else 0
            strategies[s]["avg_pnl"] = round(strategies[s]["pnl"] / n, 2) if n else 0
            strategies[s]["pnl"] = round(strategies[s]["pnl"], 2)
        return strategies


MISTAKE_CATEGORIES = [
    "regime_mismatch",
    "volatility_spike",
    "false_breakout",
    "slippage_assumption",
    "liquidity_issue",
    "overfitting",
    "stop_too_tight",
    "stop_too_wide",
    "position_too_large",
    "held_too_long",
    "exited_too_early",
    "sector_concentration",
    "correlation_risk",
    "data_quality",
    "other",
]


class MistakeLog:
    """Categorized failure analysis."""

    FILE = JOURNAL_DIR / "mistake_log.json"

    def __init__(self):
        self.mistakes = self._load()

    def _load(self) -> list:
        if self.FILE.exists():
            try:
                with open(self.FILE) as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save(self):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.FILE, "w") as f:
            json.dump(self.mistakes, f, indent=2, default=str)

    def record_mistake(self, trade: dict, category: str,
                       description: str, impact: float,
                       lesson: str = ""):
        """Record a categorized mistake.

        Args:
            trade: The trade record that resulted in a mistake
            category: One of MISTAKE_CATEGORIES
            description: What went wrong
            impact: PnL impact in AUD
            lesson: What to do differently
        """
        if category not in MISTAKE_CATEGORIES:
            logger.warning(f"Unknown category '{category}', using 'other'")
            category = "other"

        entry = {
            "timestamp": datetime.now().isoformat(),
            "ticker": trade.get("ticker", ""),
            "strategy": trade.get("strategy", ""),
            "entry_date": trade.get("entry_date", ""),
            "exit_date": trade.get("exit_date", ""),
            "pnl": trade.get("pnl", 0),
            "category": category,
            "description": description,
            "impact": round(impact, 2),
            "lesson": lesson,
        }
        self.mistakes.append(entry)
        self._save()
        logger.info(f"Mistake logged: {category} on {trade.get('ticker')} "
                    f"impact=${impact:.2f}")

    def auto_categorize(self, trade: dict, data: dict = None) -> list:
        """Auto-detect potential mistake categories from a losing trade."""
        categories = []
        pnl = trade.get("pnl", 0)

        if pnl >= 0:
            return categories  # Not a losing trade

        mae = trade.get("mae", 0)
        mfe = trade.get("mfe", 0)
        holding = trade.get("holding_days", 0)

        # If MFE was significantly positive but ended in loss -> exited too late
        if mfe > 2 and pnl < 0:
            categories.append({
                "category": "held_too_long",
                "description": f"MFE was +{mfe:.1f}% but trade ended at {trade.get('pnl_pct', 0):+.1f}%",
                "impact": pnl,
            })

        # If MAE was very deep -> stop too wide
        if mae < -5:
            categories.append({
                "category": "stop_too_wide",
                "description": f"MAE reached {mae:.1f}% before exit",
                "impact": pnl,
            })

        # If stopped out quickly with small MAE -> stop too tight or false breakout
        if holding <= 1 and abs(mae) < 3:
            if trade.get("strategy") == "momentum_breakout":
                categories.append({
                    "category": "false_breakout",
                    "description": f"Stopped out in {holding} days, likely false breakout",
                    "impact": pnl,
                })
            else:
                categories.append({
                    "category": "stop_too_tight",
                    "description": f"Stopped out in {holding} days with MAE {mae:.1f}%",
                    "impact": pnl,
                })

        return categories

    def top_mistakes(self, n: int = 3, days: int = 30) -> list:
        """Get top N mistake categories by frequency and impact."""
        cutoff = (datetime.now() - pd.Timedelta(days=days)).isoformat()
        recent = [m for m in self.mistakes if m.get("timestamp", "") >= cutoff]

        if not recent:
            return []

        # Aggregate by category
        cats = {}
        for m in recent:
            c = m["category"]
            if c not in cats:
                cats[c] = {"count": 0, "total_impact": 0, "examples": []}
            cats[c]["count"] += 1
            cats[c]["total_impact"] += m.get("impact", 0)
            cats[c]["examples"].append(m.get("ticker", ""))

        # Sort by combined score (frequency * impact)
        ranked = sorted(
            cats.items(),
            key=lambda x: x[1]["count"] * abs(x[1]["total_impact"]),
            reverse=True,
        )

        return [
            {
                "category": cat,
                "count": info["count"],
                "total_impact": round(info["total_impact"], 2),
                "examples": info["examples"][:5],
            }
            for cat, info in ranked[:n]
        ]

    def summary(self, days: int = 30) -> dict:
        """Mistake log summary."""
        cutoff = (datetime.now() - pd.Timedelta(days=days)).isoformat()
        recent = [m for m in self.mistakes if m.get("timestamp", "") >= cutoff]

        return {
            "total_mistakes": len(recent),
            "period_days": days,
            "total_impact": round(sum(m.get("impact", 0) for m in recent), 2),
            "top_categories": self.top_mistakes(3, days),
        }


class WeeklySummary:
    """Generate weekly performance + mistake summary."""

    FILE = JOURNAL_DIR / "weekly_summaries.json"

    def __init__(self, ledger: TradeLedger, mistake_log: MistakeLog,
                 decision_journal: DecisionJournal):
        self.ledger = ledger
        self.mistake_log = mistake_log
        self.decisions = decision_journal

    def generate(self, equity: float, starting_equity: float) -> dict:
        """Generate weekly summary report."""
        perf = self.ledger.performance_summary(days=7)
        mistakes = self.mistake_log.summary(days=7)
        decisions = self.decisions.summary(days=7)

        total_return = (equity - starting_equity) / starting_equity * 100

        summary = {
            "generated_at": datetime.now().isoformat(),
            "week_ending": datetime.now().strftime("%Y-%m-%d"),
            "portfolio": {
                "equity": round(equity, 2),
                "starting_equity": starting_equity,
                "total_return_pct": round(total_return, 2),
            },
            "performance": perf,
            "mistakes": mistakes,
            "decisions": decisions,
            "next_experiments": [],  # Filled by annealing loop
        }

        # Save
        self._save(summary)
        return summary

    def _save(self, summary: dict):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        summaries = []
        if self.FILE.exists():
            try:
                with open(self.FILE) as f:
                    summaries = json.load(f)
            except Exception:
                summaries = []
        summaries.append(summary)
        with open(self.FILE, "w") as f:
            json.dump(summaries, f, indent=2, default=str)

    def format_text(self, summary: dict) -> str:
        """Format weekly summary as readable text."""
        lines = []
        lines.append("═" * 50)
        lines.append(f"  WEEKLY SUMMARY — w/e {summary['week_ending']}")
        lines.append("═" * 50)
        lines.append("")

        port = summary["portfolio"]
        lines.append(f"💰 PORTFOLIO: ${port['equity']:,.2f} "
                     f"({port['total_return_pct']:+.2f}% total return)")
        lines.append("")

        perf = summary["performance"]
        if perf.get("total_trades", 0) > 0:
            lines.append(f"📊 TRADES THIS WEEK:")
            lines.append(f"   Total: {perf['total_trades']} | "
                        f"Win rate: {perf['win_rate']}% | "
                        f"PnL: ${perf['total_pnl']:+,.2f}")
            lines.append(f"   Avg winner: ${perf['avg_winner']:+,.2f} | "
                        f"Avg loser: ${perf['avg_loser']:+,.2f} | "
                        f"Profit factor: {perf['profit_factor']}")

            if perf.get("by_strategy"):
                lines.append(f"   By strategy:")
                for s, info in perf["by_strategy"].items():
                    lines.append(f"     {s}: {info['trades']} trades, "
                               f"${info['pnl']:+,.2f}, "
                               f"{info['win_rate']}% win rate")
        else:
            lines.append("📊 No closed trades this week.")
        lines.append("")

        mistakes = summary["mistakes"]
        if mistakes.get("total_mistakes", 0) > 0:
            lines.append(f"⚠️  MISTAKES ({mistakes['total_mistakes']}):")
            lines.append(f"   Total impact: ${mistakes['total_impact']:+,.2f}")
            for cat in mistakes.get("top_categories", []):
                lines.append(f"   • {cat['category']}: {cat['count']}x, "
                           f"${cat['total_impact']:+,.2f}")
        else:
            lines.append("✅ No mistakes logged this week.")
        lines.append("")

        if summary.get("next_experiments"):
            lines.append("🔬 NEXT EXPERIMENTS:")
            for exp in summary["next_experiments"]:
                lines.append(f"   • {exp}")

        return "\n".join(lines)
