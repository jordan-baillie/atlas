"""Rejected Signal Analysis — post-hoc analysis of signals filtered out during plan generation.

Analyzes plan JSON files to understand why signals were rejected, and optionally
computes hypothetical P&L to quantify the cost/benefit of each filter gate.

Plan files live in the ``plans/`` directory at project root and follow the naming
convention ``plan_{market_id}_{date}.json`` or ``plan_{date}.json``.

Usage::

    from research.rejected_signal_analysis import RejectedSignalAnalyzer

    analyzer = RejectedSignalAnalyzer()
    signals  = analyzer.extract_rejected()               # scan all plans
    report   = analyzer.analyze(signals, price_data=px)  # optional price dict
    print(analyzer.format_telegram(report))
    analyzer.save_report(report)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PLANS_DIR = PROJECT_ROOT / "plans"
DEFAULT_REPORTS_DIR = PROJECT_ROOT / "research" / "reports"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _categorize_reason(reason: str) -> str:
    """Map a raw rejection reason string to a broad category label."""
    r = reason.lower()
    if "max position" in r or "position limit" in r or "would be exceeded" in r:
        return "position_limit"
    if "confidence" in r and "below" in r:
        return "low_confidence"
    if "vix" in r:
        return "vix_gate"
    if "fred" in r or "macro" in r or "yield curve" in r or "claims" in r:
        return "macro_filter"
    if "allocation" in r or "pool" in r:
        return "allocation_pool"
    if "sector" in r:
        return "sector_limit"
    if "risk" in r:
        return "risk_limit"
    return "other"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RejectedSignal:
    """A single signal that was rejected during plan generation.

    Attributes mirror the ``rejected_entries`` fields written by
    :class:`brokers.plan.TradePlanGenerator`.
    """

    ticker: str
    strategy: str
    rejection_reason: str
    trade_date: str
    entry_price: float
    stop_price: float
    take_profit: Optional[float]
    position_size: int
    position_value: float
    risk_amount: float
    confidence: float
    rationale: str
    features: Dict[str, Any] = field(default_factory=dict)
    sector: str = "Unknown"
    market_id: str = ""

    @property
    def rejection_category(self) -> str:
        """Broad category derived from *rejection_reason*."""
        return _categorize_reason(self.rejection_reason)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["rejection_category"] = self.rejection_category
        return d


@dataclass
class HypotheticalTrade:
    """Simulated outcome for a rejected signal if it had been executed.

    The trade is simulated by scanning OHLCV bars after entry date and checking
    whether take-profit or stop-loss is hit first.  If neither fires within
    *max_hold_days*, the position is exited at the final close price.
    """

    ticker: str
    strategy: str
    trade_date: str
    entry_price: float
    exit_price: float
    exit_reason: str   # "take_profit" | "stop_loss" | "expired"
    hold_days: int
    pnl: float         # absolute P&L in currency units
    pnl_pct: float     # percentage return relative to entry price


@dataclass
class RejectionReport:
    """Aggregate analysis of rejected signals across one or more plan files.

    Attributes:
        generated_at:            ISO-8601 timestamp of when this report was created.
        plan_dates:              Sorted list of trade dates represented.
        total_rejected:          Total number of rejected signals.
        reason_distribution:     Raw rejection reason → count.
        category_distribution:   Broad category → count.
        strategy_breakdown:      strategy → {total, reasons dict}.
        hypothetical_trades:     Simulated trade outcomes (requires price data).
        total_hypothetical_pnl:  Sum of all simulated P&L values.
        hypothetical_pnl_by_strategy: strategy → cumulative P&L.
        hypothetical_pnl_by_category: category → cumulative P&L.
        win_rate:                Percentage of simulated trades with positive P&L.
        signals:                 Original list of :class:`RejectedSignal` objects.
    """

    generated_at: str
    plan_dates: List[str]
    total_rejected: int
    reason_distribution: Dict[str, int]
    category_distribution: Dict[str, int]
    strategy_breakdown: Dict[str, Dict]
    hypothetical_trades: List[HypotheticalTrade]
    total_hypothetical_pnl: float
    hypothetical_pnl_by_strategy: Dict[str, float]
    hypothetical_pnl_by_category: Dict[str, float]
    win_rate: float
    signals: List[RejectedSignal]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # asdict() recursively converts nested dataclasses but ignores @property.
        # Re-serialise each signal via its own to_dict() so rejection_category is included.
        d["signals"] = [s.to_dict() for s in self.signals]
        return d


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------

class RejectedSignalAnalyzer:
    """Analyze rejected signals from plan JSON files.

    Args:
        plans_dir:   Directory containing ``plan_*.json`` files.
                     Defaults to ``<project_root>/plans``.
        reports_dir: Directory where JSON reports are written.
                     Defaults to ``<project_root>/research/reports``.
    """

    DEFAULT_MAX_HOLD_DAYS: int = 20

    def __init__(
        self,
        plans_dir: Optional[Path] = None,
        reports_dir: Optional[Path] = None,
    ) -> None:
        self.plans_dir = Path(plans_dir) if plans_dir is not None else DEFAULT_PLANS_DIR
        self.reports_dir = Path(reports_dir) if reports_dir is not None else DEFAULT_REPORTS_DIR

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_plan(self, path: Path) -> Optional[dict]:
        try:
            with open(path) as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Failed to load plan %s: %s", path, exc)
            return None

    def _plan_paths(
        self, date_range: Optional[Tuple[str, str]] = None
    ) -> List[Path]:
        """Return sorted plan file paths, optionally filtered by *date_range*.

        *date_range* is an optional ``(start_date, end_date)`` tuple of
        ``"YYYY-MM-DD"`` strings (inclusive).  The date component is taken
        from the last ``_``-delimited part of the file stem.
        """
        if not self.plans_dir.exists():
            return []

        paths = sorted(self.plans_dir.glob("plan_*.json"))
        if not date_range:
            return paths

        start, end = date_range
        filtered: List[Path] = []
        for p in paths:
            # stem examples: "plan_sp500_2026-01-15", "plan_2026-01-15"
            date_part = p.stem.split("_")[-1]
            if start <= date_part <= end:
                filtered.append(p)
        return filtered

    # ------------------------------------------------------------------
    # Extraction
    # ------------------------------------------------------------------

    def extract_rejected(
        self,
        plan_paths: Optional[List[Path]] = None,
        date_range: Optional[Tuple[str, str]] = None,
    ) -> List[RejectedSignal]:
        """Extract rejected signals from plan JSON files.

        Args:
            plan_paths: Explicit list of plan paths.  If *None*, auto-discovers
                        files in :attr:`plans_dir`.
            date_range: Optional ``(start_date, end_date)`` strings ``"YYYY-MM-DD"``
                        to restrict which plan files are loaded.

        Returns:
            List of :class:`RejectedSignal` objects — one per rejected entry.
        """
        if plan_paths is None:
            plan_paths = self._plan_paths(date_range=date_range)

        signals: List[RejectedSignal] = []
        for path in plan_paths:
            plan = self._load_plan(path)
            if not plan:
                continue

            trade_date = plan.get("trade_date", "")
            market_id = plan.get("market_id", "")

            for entry in plan.get("rejected_entries", []):
                try:
                    sig = RejectedSignal(
                        ticker=str(entry.get("ticker", "")),
                        strategy=str(entry.get("strategy", "")),
                        rejection_reason=str(entry.get("rejection_reason", "unknown")),
                        trade_date=trade_date,
                        entry_price=float(entry.get("entry_price") or 0.0),
                        stop_price=float(entry.get("stop_price") or 0.0),
                        take_profit=(
                            float(entry["take_profit"])
                            if entry.get("take_profit") is not None
                            else None
                        ),
                        position_size=int(entry.get("position_size") or 0),
                        position_value=float(entry.get("position_value") or 0.0),
                        risk_amount=float(entry.get("risk_amount") or 0.0),
                        confidence=float(entry.get("confidence") or 0.0),
                        rationale=str(entry.get("rationale") or ""),
                        features=entry.get("features") or {},
                        sector=str(entry.get("sector") or "Unknown"),
                        market_id=str(entry.get("market_id") or market_id),
                    )
                    signals.append(sig)
                except Exception as exc:
                    logger.warning(
                        "Skipping malformed rejected entry in %s: %s", path, exc
                    )

        return signals

    # ------------------------------------------------------------------
    # Hypothetical P&L simulation
    # ------------------------------------------------------------------

    def _simulate_trade(
        self,
        signal: RejectedSignal,
        price_data: Dict[str, pd.DataFrame],
        max_hold_days: int,
    ) -> Optional[HypotheticalTrade]:
        """Simulate what would have happened if *signal* had been executed.

        Scans up to *max_hold_days* OHLCV bars after the signal's trade date
        and returns the first outcome:

        1. **take_profit** — high of any bar >= take-profit price.
        2. **stop_loss**   — low of any bar  <= stop-loss  price.
        3. **expired**     — neither fired; exits at the final close.

        Returns *None* if price data for the ticker is unavailable or the
        signal's trade date is not representable as a timestamp.
        """
        df = price_data.get(signal.ticker)
        if df is None or df.empty:
            return None

        try:
            entry_date = pd.Timestamp(signal.trade_date)
        except Exception:
            return None

        future = df[df.index > entry_date].head(max_hold_days)
        if future.empty:
            return None

        entry_price = signal.entry_price
        stop_price = signal.stop_price
        take_profit = signal.take_profit
        position_size = signal.position_size if signal.position_size > 0 else 1

        for hold_idx, (_, row) in enumerate(future.iterrows()):
            try:
                high = float(row["high"])
            except (KeyError, TypeError):
                high = float(row.get("close", entry_price))  # type: ignore[arg-type]
            try:
                low = float(row["low"])
            except (KeyError, TypeError):
                low = float(row.get("close", entry_price))  # type: ignore[arg-type]

            # Take-profit checked first (optimistic — if same bar, TP wins)
            if take_profit and take_profit > 0 and high >= take_profit:
                pnl = (take_profit - entry_price) * position_size
                return HypotheticalTrade(
                    ticker=signal.ticker,
                    strategy=signal.strategy,
                    trade_date=signal.trade_date,
                    entry_price=entry_price,
                    exit_price=take_profit,
                    exit_reason="take_profit",
                    hold_days=hold_idx + 1,
                    pnl=round(pnl, 2),
                    pnl_pct=round((take_profit / entry_price - 1) * 100, 3),
                )

            # Stop-loss
            if stop_price and stop_price > 0 and low <= stop_price:
                pnl = (stop_price - entry_price) * position_size
                return HypotheticalTrade(
                    ticker=signal.ticker,
                    strategy=signal.strategy,
                    trade_date=signal.trade_date,
                    entry_price=entry_price,
                    exit_price=stop_price,
                    exit_reason="stop_loss",
                    hold_days=hold_idx + 1,
                    pnl=round(pnl, 2),
                    pnl_pct=round((stop_price / entry_price - 1) * 100, 3),
                )

        # Neither fired: exit at last close
        try:
            last_close = float(future["close"].iloc[-1])
        except (KeyError, IndexError):
            last_close = entry_price

        hold_days = len(future)
        pnl = (last_close - entry_price) * position_size
        return HypotheticalTrade(
            ticker=signal.ticker,
            strategy=signal.strategy,
            trade_date=signal.trade_date,
            entry_price=entry_price,
            exit_price=last_close,
            exit_reason="expired",
            hold_days=hold_days,
            pnl=round(pnl, 2),
            pnl_pct=round((last_close / entry_price - 1) * 100, 3),
        )

    def compute_hypothetical_pnl(
        self,
        signals: List[RejectedSignal],
        price_data: Dict[str, pd.DataFrame],
        max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    ) -> Tuple[List[HypotheticalTrade], float, Dict[str, float], Dict[str, float]]:
        """Simulate trades for all *signals* and return aggregated P&L.

        Args:
            signals:      Rejected signals to simulate.
            price_data:   Dict ``ticker → OHLCV DataFrame`` with a DatetimeIndex.
            max_hold_days: Maximum bars to scan before forcing an exit.

        Returns:
            ``(trades, total_pnl, pnl_by_strategy, pnl_by_category)``
        """
        # Build a quick lookup: (ticker, trade_date) -> RejectedSignal for category mapping
        sig_map: Dict[Tuple[str, str], RejectedSignal] = {}
        for sig in signals:
            sig_map[(sig.ticker, sig.trade_date)] = sig

        trades: List[HypotheticalTrade] = []
        for sig in signals:
            trade = self._simulate_trade(sig, price_data, max_hold_days)
            if trade is not None:
                trades.append(trade)

        total_pnl = round(sum(t.pnl for t in trades), 2)

        pnl_by_strategy: Dict[str, float] = {}
        pnl_by_category: Dict[str, float] = {}

        for trade in trades:
            strat = trade.strategy
            pnl_by_strategy[strat] = round(
                pnl_by_strategy.get(strat, 0.0) + trade.pnl, 2
            )
            orig = sig_map.get((trade.ticker, trade.trade_date))
            cat = orig.rejection_category if orig else "other"
            pnl_by_category[cat] = round(
                pnl_by_category.get(cat, 0.0) + trade.pnl, 2
            )

        return trades, total_pnl, pnl_by_strategy, pnl_by_category

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def analyze(
        self,
        signals: List[RejectedSignal],
        price_data: Optional[Dict[str, pd.DataFrame]] = None,
        max_hold_days: int = DEFAULT_MAX_HOLD_DAYS,
    ) -> RejectionReport:
        """Analyze a list of rejected signals and produce a :class:`RejectionReport`.

        Args:
            signals:      Output of :meth:`extract_rejected`.
            price_data:   Optional ticker → OHLCV mapping for hypothetical P&L.
            max_hold_days: Simulation horizon when *price_data* is provided.

        Returns:
            A fully populated :class:`RejectionReport`.
        """
        reason_distribution: Dict[str, int] = {}
        category_distribution: Dict[str, int] = {}
        strategy_breakdown: Dict[str, Dict] = {}
        plan_dates_set: set[str] = set()

        for sig in signals:
            plan_dates_set.add(sig.trade_date)

            # Reason distribution
            r = sig.rejection_reason
            reason_distribution[r] = reason_distribution.get(r, 0) + 1

            # Category distribution
            cat = sig.rejection_category
            category_distribution[cat] = category_distribution.get(cat, 0) + 1

            # Per-strategy breakdown
            strat = sig.strategy
            if strat not in strategy_breakdown:
                strategy_breakdown[strat] = {"total": 0, "reasons": {}}
            strategy_breakdown[strat]["total"] += 1
            reasons_map = strategy_breakdown[strat]["reasons"]
            reasons_map[r] = reasons_map.get(r, 0) + 1

        # Hypothetical P&L (optional)
        trades: List[HypotheticalTrade] = []
        total_pnl = 0.0
        pnl_by_strategy: Dict[str, float] = {}
        pnl_by_category: Dict[str, float] = {}

        if price_data is not None and signals:
            trades, total_pnl, pnl_by_strategy, pnl_by_category = (
                self.compute_hypothetical_pnl(signals, price_data, max_hold_days)
            )

        win_rate = 0.0
        if trades:
            win_rate = round(
                sum(1 for t in trades if t.pnl > 0) / len(trades) * 100, 1
            )

        return RejectionReport(
            generated_at=datetime.now().isoformat(),
            plan_dates=sorted(plan_dates_set),
            total_rejected=len(signals),
            reason_distribution=reason_distribution,
            category_distribution=category_distribution,
            strategy_breakdown=strategy_breakdown,
            hypothetical_trades=trades,
            total_hypothetical_pnl=total_pnl,
            hypothetical_pnl_by_strategy=pnl_by_strategy,
            hypothetical_pnl_by_category=pnl_by_category,
            win_rate=win_rate,
            signals=signals,
        )

    # ------------------------------------------------------------------
    # Formatting & persistence
    # ------------------------------------------------------------------

    def format_telegram(self, report: RejectionReport) -> str:
        """Format *report* as a Telegram HTML message.

        Returns a multi-line HTML string suitable for sending via the
        Telegram Bot API with ``parse_mode=HTML``.
        """
        lines: List[str] = []
        lines.append("📊 <b>Rejected Signal Analysis</b>")

        dates_str = (
            ", ".join(report.plan_dates) if report.plan_dates else "N/A"
        )
        lines.append(f"🗓 Dates: {dates_str}")
        lines.append(f"🔢 Total rejected: <b>{report.total_rejected}</b>")

        if report.category_distribution:
            lines.append("")
            lines.append("📋 <b>Rejection Categories</b>")
            for cat, count in sorted(
                report.category_distribution.items(), key=lambda x: -x[1]
            ):
                pct = (
                    round(count / report.total_rejected * 100, 1)
                    if report.total_rejected
                    else 0.0
                )
                lines.append(f"  • {cat}: {count} ({pct}%)")

        if report.strategy_breakdown:
            lines.append("")
            lines.append("📈 <b>By Strategy</b>")
            for strat, data in sorted(
                report.strategy_breakdown.items(), key=lambda x: -x[1]["total"]
            ):
                lines.append(f"  • {strat}: {data['total']} rejected")

        if report.hypothetical_trades:
            lines.append("")
            sign = "+" if report.total_hypothetical_pnl >= 0 else ""
            lines.append("💰 <b>Hypothetical P&amp;L (if signals taken)</b>")
            lines.append(
                f"  Total: <b>{sign}${report.total_hypothetical_pnl:,.2f}</b>"
            )
            lines.append(f"  Win rate: {report.win_rate:.1f}%")
            lines.append(
                f"  Trades simulated: {len(report.hypothetical_trades)}"
            )
            if report.hypothetical_pnl_by_strategy:
                lines.append("")
                lines.append("  <i>By strategy:</i>")
                for strat, pnl in sorted(
                    report.hypothetical_pnl_by_strategy.items(),
                    key=lambda x: -x[1],
                ):
                    s = "+" if pnl >= 0 else ""
                    lines.append(f"    {strat}: {s}${pnl:,.2f}")
        else:
            lines.append("")
            lines.append(
                "ℹ️ No price data provided — hypothetical P&amp;L not computed"
            )

        return "\n".join(lines)

    def save_report(
        self,
        report: RejectionReport,
        output_path: Optional[Path] = None,
    ) -> Path:
        """Serialise *report* to JSON and write to disk.

        Args:
            report:      :class:`RejectionReport` to save.
            output_path: Explicit destination path.  If *None*, a timestamped
                         file is auto-generated under :attr:`reports_dir`.

        Returns:
            The :class:`~pathlib.Path` where the report was written.
        """
        if output_path is None:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = self.reports_dir / f"rejected_signal_analysis_{ts}.json"
        else:
            output_path = Path(output_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, "w") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)

        logger.info("Rejected signal report saved: %s", output_path)
        return output_path
