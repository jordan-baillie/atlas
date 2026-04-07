#!/usr/bin/env python3
"""Broker-Local State Reconciliation.

Compares Alpaca broker state (positions, filled orders) with Atlas local state
(trade plans, trade ledger, position tracking) and generates a reconciliation
report with auto-fix capabilities.

Usage:
    python3 scripts/reconcile.py --market sp500 [--dry-run] [--auto-fix]
"""
import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logger = logging.getLogger(__name__)


@dataclass
class Discrepancy:
    """A single state discrepancy between broker and local."""

    category: str  # "missing_local", "missing_broker", "sl_filled", "missing_protective", "stale_plan"
    ticker: str
    description: str
    severity: str  # "high", "medium", "low"
    auto_fixable: bool = False
    fix_action: str = ""
    fixed: bool = False


@dataclass
class ReconciliationReport:
    """Full reconciliation report."""

    timestamp: str
    market_id: str
    broker_positions: int = 0
    local_positions: int = 0
    discrepancies: List[Discrepancy] = field(default_factory=list)
    fixes_applied: List[str] = field(default_factory=list)
    broker_equity: float = 0.0

    @property
    def clean(self) -> bool:
        return len(self.discrepancies) == 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["clean"] = self.clean
        return d


class StateReconciler:
    """Compare broker state with local state and generate report."""

    def __init__(self, config: dict, market_id: str = "sp500"):
        self.config = config
        self.market_id = market_id
        self.report = ReconciliationReport(
            timestamp=datetime.now().isoformat(),
            market_id=market_id,
        )

    def _get_broker_positions(self) -> Dict[str, dict]:
        """Get current positions from Alpaca broker.

        Returns {ticker: {qty, market_value, avg_entry, unrealized_pl, side}}.
        """
        try:
            from brokers.alpaca.broker import AlpacaBroker

            broker = AlpacaBroker(self.config, live=True)
            if not broker.connect():
                logger.error("Failed to connect to broker")
                return {}

            positions = {}
            for pos in broker.get_positions():
                ticker = pos.ticker if hasattr(pos, "ticker") else pos.get("ticker", "")
                positions[ticker] = {
                    "qty": getattr(pos, "shares", None) or pos.get("qty", 0),
                    "market_value": getattr(pos, "market_value", None) or pos.get("market_value", 0),
                    "avg_entry": getattr(pos, "entry_price", None) or pos.get("avg_entry_price", 0),
                    "unrealized_pl": getattr(pos, "unrealized_pnl", None) or pos.get("unrealized_pl", 0),
                    "side": pos.get("side", "long") if isinstance(pos, dict) else "long",
                }

            try:
                account = broker._trade_client.get_account()
                self.report.broker_equity = float(getattr(account, "equity", 0) or 0)
            except Exception:
                pass

            self.report.broker_positions = len(positions)
            return positions
        except Exception as e:
            logger.error(f"Failed to get broker positions: {e}")
            return {}

    def _get_local_positions(self) -> Dict[str, dict]:
        """Get local position tracking state.

        Reads from journal/trade_ledger.json — a flat list of entry/exit events.
        Open positions are derived by net-quantity: sum entries, subtract exits.
        """
        positions = {}

        ledger_path = PROJECT / "journal" / "trade_ledger.json"
        if ledger_path.exists():
            try:
                with open(ledger_path) as f:
                    trades = json.load(f)

                if not isinstance(trades, list):
                    logger.warning("trade_ledger.json is not a list — unexpected format")
                    self.report.local_positions = 0
                    return positions

                # Derive open positions: net qty per ticker (entries - exits)
                net_qty: Dict[str, int] = {}
                last_entry: Dict[str, dict] = {}
                for t in trades:
                    ticker = t.get("ticker", "")
                    if not ticker:
                        continue
                    qty = int(t.get("shares", 0))
                    if t.get("type") == "entry":
                        net_qty[ticker] = net_qty.get(ticker, 0) + qty
                        last_entry[ticker] = t
                    elif t.get("type") == "exit":
                        net_qty[ticker] = net_qty.get(ticker, 0) - qty

                for ticker, qty in net_qty.items():
                    if qty > 0:
                        entry = last_entry.get(ticker, {})
                        positions[ticker] = {
                            "strategy": entry.get("strategy", "unknown"),
                            "entry_date": entry.get("timestamp", ""),
                            "entry_price": entry.get("fill_price", 0),
                            "shares": qty,
                            "direction": entry.get("direction", "long"),
                        }
            except Exception as e:
                logger.warning(f"Failed to read trade ledger: {e}")

        self.report.local_positions = len(positions)
        return positions

    def _get_recent_fills(self, hours: int = 24) -> List[dict]:
        """Get recently filled orders from broker (last N hours)."""
        try:
            from brokers.alpaca.broker import AlpacaBroker

            broker = AlpacaBroker(self.config, live=True)
            if not broker.connect():
                return []

            # Get closed orders from last N hours
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            request = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=datetime.now(tz=__import__('datetime').timezone.utc) - timedelta(hours=hours),
                limit=100,
            )
            orders = broker._trade_client.get_orders(filter=request)

            fills = []
            for order in orders:
                # Use .value for Alpaca enums — str() returns "OrderStatus.FILLED",
                # but .value returns "filled".
                raw_status = getattr(order, "status", "")
                status_str = (raw_status.value if hasattr(raw_status, "value") else str(raw_status)).lower()
                if status_str in ("filled", "partially_filled"):
                    client_oid = str(getattr(order, "client_order_id", ""))
                    raw_side = getattr(order, "side", "")
                    side_str = raw_side.value if hasattr(raw_side, "value") else str(raw_side)
                    fills.append(
                        {
                            "ticker": str(getattr(order, "symbol", "")),
                            "side": side_str,
                            "qty": float(getattr(order, "filled_qty", 0) or 0),
                            "fill_price": float(getattr(order, "filled_avg_price", 0) or 0),
                            "limit_price": float(getattr(order, "limit_price", 0) or 0),
                            "filled_at": str(getattr(order, "filled_at", "")),
                            "order_id": str(getattr(order, "id", "")),
                            "client_order_id": client_oid,
                            "is_atlas": client_oid.startswith("atlas_"),
                            "order_type": str(
                                getattr(order, "order_class", getattr(order, "type", ""))
                            ),
                        }
                    )
            return fills
        except Exception as e:
            logger.warning(f"Failed to get recent fills: {e}")
            return []

    def _find_atlas_fill(self, ticker: str, recent_fills: List[dict]) -> Optional[dict]:
        """Find an Atlas-originated BUY fill for a ticker.

        Returns the fill dict if found, None otherwise.
        Atlas orders have client_order_id starting with 'atlas_'.
        """
        for fill in recent_fills:
            if (fill["ticker"] == ticker
                    and "buy" in fill["side"].lower()
                    and fill.get("is_atlas", False)):
                return fill
        # Also check plan entries — the fill might be older than recent_fills window
        return None

    def _get_plan_entry(self, ticker: str) -> Optional[dict]:
        """Search recent plans for a proposed entry matching this ticker."""
        plans_dir = PROJECT / "plans"
        if not plans_dir.exists():
            return None
        # Check last 5 plans
        plan_files = sorted(plans_dir.glob(f"plan_{self.market_id}_*.json"), reverse=True)
        for plan_file in plan_files[:5]:
            try:
                with open(plan_file) as f:
                    plan = json.load(f)
                for entry in plan.get("proposed_entries", []):
                    if entry.get("ticker") == ticker:
                        return entry
            except Exception:
                continue
        return None

    def reconcile(self) -> "ReconciliationReport":
        """Run full reconciliation and return report."""
        broker_positions = self._get_broker_positions()
        local_positions = self._get_local_positions()
        recent_fills = self._get_recent_fills()

        broker_tickers = set(broker_positions.keys())
        local_tickers = set(local_positions.keys())

        # 1. Positions on broker but NOT in local tracking
        for ticker in sorted(broker_tickers - local_tickers):
            bp = broker_positions[ticker]
            qty = bp.get("qty", 0)
            # Check if this is a known Atlas order by looking at recent fills
            atlas_fill = self._find_atlas_fill(ticker, recent_fills)
            self.report.discrepancies.append(
                Discrepancy(
                    category="missing_local",
                    ticker=ticker,
                    description=(
                        f"Position on broker ({qty} shares) not in trade ledger"
                        + (" — Atlas order found, can auto-backfill" if atlas_fill else
                           " — likely a manual trade; add entry via cli or manually")
                    ),
                    severity="high",
                    auto_fixable=bool(atlas_fill),
                    fix_action=(
                        "Auto-backfill ledger entry from broker fill data"
                        if atlas_fill else
                        "Manual: record entry in journal/trade_ledger.json"
                    ),
                )
            )

        # 2. Positions in local tracking but NOT on broker
        for ticker in sorted(local_tickers - broker_tickers):
            # Check if SL/exit was filled recently
            sl_fill = any(
                f["ticker"] == ticker and "sell" in f["side"].lower()
                for f in recent_fills
            )
            if sl_fill:
                self.report.discrepancies.append(
                    Discrepancy(
                        category="sl_filled",
                        ticker=ticker,
                        description=(
                            "Stop loss filled during outage — local still shows open"
                        ),
                        severity="high",
                        auto_fixable=True,
                        fix_action="Backfill ledger exit, mark position closed",
                    )
                )
            else:
                self.report.discrepancies.append(
                    Discrepancy(
                        category="missing_broker",
                        ticker=ticker,
                        description="Local tracking shows position but not on broker",
                        severity="high",
                        auto_fixable=False,
                        fix_action="Manual investigation needed",
                    )
                )

        # 3. Check for stale open plans (> 5 business days old with no fill)
        self._check_stale_plans()

        return self.report

    def _check_stale_plans(self) -> None:
        """Flag trade plans older than 5 days that haven't been executed."""
        plans_dir = PROJECT / "plans"
        if not plans_dir.exists():
            return
        cutoff = datetime.now() - timedelta(days=7)
        for plan_file in sorted(plans_dir.glob("plan_*.json")):
            try:
                with open(plan_file) as f:
                    plan = json.load(f)
                if plan.get("status") in ("APPROVED", "PENDING"):
                    plan_date_str = plan.get("trade_date", "")
                    if plan_date_str:
                        try:
                            plan_date = datetime.strptime(plan_date_str, "%Y-%m-%d")
                        except ValueError:
                            continue
                        if plan_date < cutoff:
                            self.report.discrepancies.append(
                                Discrepancy(
                                    category="stale_plan",
                                    ticker="(plan)",
                                    description=(
                                        f"Stale {plan.get('status')} plan: "
                                        f"{plan_file.name} (trade_date={plan_date_str})"
                                    ),
                                    severity="medium",
                                    auto_fixable=True,
                                    fix_action=f"expire_plan:{plan_file}",
                                )
                            )
            except Exception as e:
                logger.debug(f"Could not check plan {plan_file}: {e}")

    def auto_fix(self) -> List[str]:
        """Apply automatic fixes for fixable discrepancies.

        Returns list of fix descriptions.
        """
        fixes = []
        recent_fills = self._get_recent_fills(hours=168)  # 7 days for auto-fix

        for disc in self.report.discrepancies:
            if disc.auto_fixable and not disc.fixed:
                if disc.category == "sl_filled":
                    # Find the actual sell fill from broker
                    sell_fill = next(
                        (f for f in recent_fills
                         if f["ticker"] == disc.ticker and "sell" in f["side"].lower()),
                        None,
                    )
                    if sell_fill:
                        # Get entry info from ledger to build complete exit record
                        local_pos = self._get_local_positions().get(disc.ticker, {})
                        entry_price = local_pos.get("entry_price", 0)
                        strategy = local_pos.get("strategy", "unknown")
                        shares = int(sell_fill["qty"])
                        fill_price = sell_fill["fill_price"]
                        pnl = round((fill_price - entry_price) * shares, 2) if entry_price else 0

                        ledger_exit = {
                            "type": "exit",
                            "ticker": disc.ticker,
                            "strategy": strategy,
                            "shares": shares,
                            "fill_price": fill_price,
                            "entry_price": entry_price,
                            "pnl": pnl,
                            "exit_reason": "broker_stop_fill",
                            "order_id": sell_fill.get("order_id", ""),
                            "timestamp": sell_fill.get("filled_at", datetime.now().isoformat()),
                            "recorded_at": datetime.now().isoformat(),
                            "note": "Auto-backfilled by reconciliation — broker protective stop filled",
                        }

                        try:
                            ledger_path = PROJECT / "journal" / "trade_ledger.json"
                            ledger = []
                            if ledger_path.exists():
                                with open(ledger_path) as f:
                                    ledger = json.load(f)

                            # Avoid duplicate exit entries
                            existing_exit_oids = {
                                e.get("order_id") for e in ledger
                                if e.get("type") == "exit" and e.get("order_id")
                            }
                            if ledger_exit["order_id"] and ledger_exit["order_id"] in existing_exit_oids:
                                logger.info(f"Exit for {disc.ticker} already in ledger — skipping")
                                disc.fixed = True
                                fixes.append(f"Skipped {disc.ticker} exit — already in ledger")
                                continue

                            ledger.append(ledger_exit)
                            with open(ledger_path, "w") as f:
                                json.dump(ledger, f, indent=2)

                            disc.fixed = True
                            fixes.append(
                                f"Recorded exit for {disc.ticker}: {shares} shares "
                                f"@ ${fill_price:.2f} (broker stop fill, PnL=${pnl:+.2f})"
                            )
                            logger.info(
                                "Auto-fix: recorded exit for %s — %d shares @ $%.2f, PnL=$%+.2f",
                                disc.ticker, shares, fill_price, pnl,
                            )
                        except Exception as e:
                            logger.error(f"Failed to record exit for {disc.ticker}: {e}")
                    else:
                        logger.warning(f"sl_filled for {disc.ticker} but no sell fill found in recent orders")
                        disc.fixed = False

                elif disc.category == "missing_local":
                    # Backfill trade ledger from broker fill data
                    atlas_fill = self._find_atlas_fill(disc.ticker, recent_fills)
                    plan_entry = self._get_plan_entry(disc.ticker)

                    if atlas_fill:
                        strategy = "unknown"
                        stop_price = 0
                        planned_price = atlas_fill.get("limit_price", 0)
                        if plan_entry:
                            strategy = plan_entry.get("strategy", "unknown")
                            stop_price = plan_entry.get("stop_price", 0)
                            planned_price = plan_entry.get("entry_price", planned_price)
                        else:
                            # Parse strategy from client_order_id: atlas_atlas_{strat}_...
                            coid = atlas_fill.get("client_order_id", "")
                            parts = coid.split("_")
                            if len(parts) >= 3:
                                strategy = parts[2] if not parts[2].startswith("atlas") else parts[2]

                        fill_price = atlas_fill["fill_price"]
                        slippage = round((fill_price - planned_price) / planned_price * 10000, 1) if planned_price > 0 else None

                        ledger_entry = {
                            "type": "entry",
                            "ticker": disc.ticker,
                            "strategy": strategy,
                            "shares": int(atlas_fill["qty"]),
                            "fill_price": fill_price,
                            "planned_price": planned_price,
                            "stop_price": stop_price,
                            "slippage_bps": slippage,
                            "order_id": atlas_fill.get("order_id", ""),
                            "timestamp": atlas_fill.get("filled_at", datetime.now().isoformat()),
                            "recorded_at": datetime.now().isoformat(),
                            "note": "Auto-backfilled by reconciliation from Alpaca fill data",
                        }

                        try:
                            ledger_path = PROJECT / "journal" / "trade_ledger.json"
                            ledger = []
                            if ledger_path.exists():
                                with open(ledger_path) as f:
                                    ledger = json.load(f)

                            # Avoid duplicate entries
                            existing_oids = {e.get("order_id") for e in ledger if e.get("order_id")}
                            if ledger_entry["order_id"] and ledger_entry["order_id"] in existing_oids:
                                logger.info(f"Ledger entry for {disc.ticker} already exists (order_id match) — skipping")
                                disc.fixed = True
                                fixes.append(f"Skipped {disc.ticker} — already in ledger")
                                continue

                            ledger.append(ledger_entry)
                            with open(ledger_path, "w") as f:
                                json.dump(ledger, f, indent=2)

                            disc.fixed = True
                            fixes.append(
                                f"Backfilled {disc.ticker}: {int(atlas_fill['qty'])} shares "
                                f"@ ${fill_price:.2f} ({strategy})"
                            )
                            logger.info(f"Auto-fix: backfilled ledger entry for {disc.ticker}")
                        except Exception as e:
                            logger.error(f"Failed to backfill {disc.ticker}: {e}")

                elif disc.category == "stale_plan" and disc.fix_action.startswith("expire_plan:"):
                    plan_path = Path(disc.fix_action.split(":", 1)[1])
                    try:
                        with open(plan_path) as f:
                            plan = json.load(f)
                        old_status = plan.get("status", "?")
                        plan["status"] = "EXPIRED"
                        plan["expired_reason"] = (
                            f"Auto-expired by reconciliation — stale {old_status} "
                            f"plan older than 7 days"
                        )
                        with open(plan_path, "w") as f:
                            json.dump(plan, f, indent=2, default=str)
                        disc.fixed = True
                        fixes.append(
                            f"Expired stale plan {plan_path.name} "
                            f"(was {old_status}, trade_date={plan.get('trade_date','')})"
                        )
                        logger.info("Auto-fix: expired stale plan %s", plan_path.name)
                    except Exception as e:
                        logger.error("Failed to expire plan %s: %s", plan_path, e)

        self.report.fixes_applied = fixes
        return fixes

    def format_telegram_message(self) -> str:
        """Format report as Telegram HTML message."""
        r = self.report
        if r.clean:
            return (
                f"✅ <b>Reconciliation Clean</b> [{r.market_id.upper()}]\n"
                f"<i>{r.timestamp[:16]}</i>\n\n"
                f"Broker: {r.broker_positions} positions, ${r.broker_equity:,.2f} equity\n"
                f"Local: {r.local_positions} positions\n"
                f"No discrepancies found."
            )

        msg = (
            f"⚠️ <b>Reconciliation Report</b> [{r.market_id.upper()}]\n"
            f"<i>{r.timestamp[:16]}</i>\n\n"
            f"Broker: {r.broker_positions} positions, ${r.broker_equity:,.2f} equity\n"
            f"Local: {r.local_positions} positions\n"
            f"<b>Discrepancies: {len(r.discrepancies)}</b>\n\n"
        )

        for d in r.discrepancies:
            icon = "🔴" if d.severity == "high" else "🟡" if d.severity == "medium" else "⚪"
            fixed = " ✅" if d.fixed else ""
            msg += f"{icon} <b>{d.ticker}</b>: {d.description}{fixed}\n"

        if r.fixes_applied:
            msg += "\n<b>Fixes Applied:</b>\n"
            for fix in r.fixes_applied:
                msg += f"  • {fix}\n"

        return msg


def main():
    parser = argparse.ArgumentParser(description="Broker-Local State Reconciliation")
    parser.add_argument("--market", default="sp500")
    parser.add_argument("--dry-run", action="store_true", help="Report only, no fixes or Telegram")
    parser.add_argument("--auto-fix", action="store_true", help="Apply automatic fixes")
    args = parser.parse_args()

    from utils.config import get_active_config
    from utils.logging_config import setup_logging

    setup_logging("reconcile", telegram_errors=False)

    config = get_active_config(args.market)
    reconciler = StateReconciler(config, args.market)
    report = reconciler.reconcile()

    # Print report
    if report.clean:
        logger.info(
            f"Reconciliation clean: {report.broker_positions} broker positions match local state"
        )
    else:
        logger.warning(f"Found {len(report.discrepancies)} discrepancies:")
        for d in report.discrepancies:
            logger.warning(f"  [{d.severity}] {d.ticker}: {d.description}")

    # Auto-fix if requested
    if args.auto_fix and not args.dry_run:
        fixes = reconciler.auto_fix()
        for fix in fixes:
            logger.info(f"Fixed: {fix}")

    # Save report / notify
    if not args.dry_run:
        report_dir = PROJECT / "logs" / "reconciliation"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = (
            report_dir
            / f"reconcile_{args.market}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        )
        report_path.write_text(json.dumps(report.to_dict(), indent=2, default=str))
        logger.info(f"Report saved to {report_path}")

        # Send Telegram only when there are discrepancies
        if not report.clean:
            try:
                from utils.telegram import send_message

                send_message(reconciler.format_telegram_message())
            except Exception as e:
                logger.warning(f"Failed to send Telegram: {e}")
    else:
        # Print Telegram message to stdout for dry run
        print(reconciler.format_telegram_message())

    return 0 if report.clean else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message
            send_message(
                f"🚨 <b>reconcile CRASHED</b>\n\n"
                f"<pre>{type(exc).__name__}: {str(exc)[:500]}</pre>\n\n"
                f"Check logs/reconciliation/ for details"
            )
        except Exception:
            pass
        raise
