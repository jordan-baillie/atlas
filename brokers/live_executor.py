"""Live Trade Executor — bridges trade plan → broker order execution.

Reads an approved trade plan and executes it through the configured
broker (Alpaca or any future BrokerAdapter implementation).
Broker selection is driven by config and handled by the registry.

Safety architecture:
    1. live_enabled must be True in config (default: False)
    2. Plans must be APPROVED status before execution
    3. Every order goes through pre-flight checks (value cap, daily limit, etc.)
    4. Dry-run mode logs what WOULD happen without touching the broker
    5. All executions are journaled to logs/live_executions.jsonl
    6. Kill switch: set config trading.live_enabled=False or call emergency_halt()

This module is the ONLY code path that sends real orders. Nothing else
in Atlas can place live trades — broker instantiation is gated behind
live_enabled checks in the registry.

Usage:
    executor = LiveExecutor(config)
    executor.connect()
    result = executor.execute_plan(plan, trade_date)
    executor.disconnect()
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from brokers.base import (
    AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo,
)

logger = logging.getLogger("atlas.live_executor")

PROJECT_ROOT = Path(__file__).parent.parent
EXECUTION_LOG = PROJECT_ROOT / "logs" / "live_executions.jsonl"
HALT_FILE = PROJECT_ROOT / ".live_halt"


# ═══════════════════════════════════════════════════════════════
# Pre-flight safety checks
# ═══════════════════════════════════════════════════════════════

class PreflightError(Exception):
    """Raised when a pre-flight safety check fails."""
    pass


def preflight_check_config(config: dict) -> list[str]:
    """Validate config has all required safety fields. Returns list of errors."""
    errors = []
    trading = config.get("trading", {})

    if not trading.get("live_enabled", False):
        errors.append("trading.live_enabled is False")

    safety = trading.get("live_safety", {})
    if not safety:
        errors.append("trading.live_safety section missing")
    else:
        if safety.get("max_order_value", 0) <= 0:
            errors.append("live_safety.max_order_value must be > 0")
        if safety.get("max_daily_orders", 0) <= 0:
            errors.append("live_safety.max_daily_orders must be > 0")

    return errors


def preflight_check_order(
    ticker: str,
    side: OrderSide,
    qty: int,
    price: float,
    safety: dict,
    daily_order_count: int,
) -> list[str]:
    """Validate a single order against safety limits. Returns list of errors."""
    errors = []
    order_value = price * qty

    max_value = safety.get("max_order_value", 2000)
    if order_value > max_value:
        errors.append(
            f"Order value ${order_value:.2f} exceeds max ${max_value:.2f}"
        )

    max_daily = safety.get("max_daily_orders", 10)
    if daily_order_count >= max_daily:
        errors.append(
            f"Daily order limit ({max_daily}) reached"
        )

    if qty <= 0:
        errors.append(f"Invalid quantity: {qty}")

    if price <= 0:
        errors.append(f"Invalid price: {price}")

    return errors


# ═══════════════════════════════════════════════════════════════
# Execution journal
# ═══════════════════════════════════════════════════════════════

def _journal_entry(event: str, data: dict):
    """Append a line to the execution journal (JSONL).

    Resilient: any write failure is caught and logged — it must never
    interrupt or crash real trade execution.

    Atomic write pattern: the JSON line is staged to a .tmp file first.
    Only when that succeeds is the line appended to the live log.  This
    prevents a partial JSON line from corrupting the JSONL file if the
    process is killed or a disk-full error occurs mid-write.
    """
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": event,
            **data,
        }
        EXECUTION_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, default=str) + "\n"

        # Stage to temp; copy to log only when fully written and serialised.
        tmp = EXECUTION_LOG.with_suffix(".tmp")
        tmp.write_text(line, encoding="utf-8")
        with open(EXECUTION_LOG, "ab") as log_f:
            log_f.write(tmp.read_bytes())
        tmp.unlink(missing_ok=True)
    except Exception as exc:
        # Journal failure must NEVER crash execution — just warn.
        logger.warning("Journal write failed (execution continues): %s", exc)


# ═══════════════════════════════════════════════════════════════
# Live Executor
# ═══════════════════════════════════════════════════════════════

class LiveExecutor:
    """Executes approved trade plans through the configured broker.

    Instantiation alone does NOT connect to the broker or enable trading.
    You must call connect() explicitly, and config must have
    trading.live_enabled=True.
    """

    def __init__(self, config: dict):
        self.config = config
        self._broker = None
        self._connected = False
        self._daily_order_count = 0
        self._daily_date = ""
        self._halted = False
        self._halt_reason = ""

    @property
    def is_live_enabled(self) -> bool:
        """Whether live trading is enabled in config."""
        return self.config.get("trading", {}).get("live_enabled", False)

    @property
    def is_dry_run(self) -> bool:
        """Whether dry-run mode is active (logs but doesn't execute)."""
        return self.config.get("trading", {}).get("live_safety", {}).get(
            "dry_run_first", True
        )

    @property
    def safety(self) -> dict:
        return self.config.get("trading", {}).get("live_safety", {})

    # ── Lifecycle ──────────────────────────────────────────────

    def connect(self) -> bool:
        """Connect to the configured broker. Fails fast if config isn't ready."""
        # Check halt file
        if HALT_FILE.exists():
            reason = HALT_FILE.read_text().strip() or "Manual halt"
            self._halted = True
            self._halt_reason = reason
            logger.error("HALTED: %s", reason)
            return False

        # Pre-flight config checks
        errors = preflight_check_config(self.config)
        if errors:
            for e in errors:
                logger.error("Pre-flight FAIL: %s", e)
            _journal_entry("connect_blocked", {"errors": errors})
            return False

        # Use registry to get the right broker — no hardcoded imports
        from brokers.registry import get_live_broker
        self._broker = get_live_broker(self.config)
        if not self._broker:
            broker_name = self.config.get("trading", {}).get("broker", "alpaca")
            _journal_entry("connect_failed", {"reason": f"No live broker for {broker_name}"})
            logger.error("LiveExecutor: no live broker available for '%s'", broker_name)
            return False

        success = self._broker.connect()

        if success:
            self._connected = True
            _journal_entry("connected", {
                "broker": self._broker.name,
                "dry_run": self.is_dry_run,
            })
            logger.info(
                "LiveExecutor connected via %s (dry_run=%s)",
                self._broker.name, self.is_dry_run,
            )
        else:
            broker_name = self._broker.name
            _journal_entry("connect_failed", {"broker": broker_name})
            logger.error("LiveExecutor failed to connect via %s", broker_name)

        return success

    def disconnect(self):
        """Disconnect from broker."""
        if self._broker:
            self._broker.disconnect()
            self._broker = None
        self._connected = False
        _journal_entry("disconnected", {})
        logger.info("LiveExecutor disconnected")

    # ── Execution ──────────────────────────────────────────────

    def execute_plan(self, plan: dict, trade_date: str) -> dict:
        """Execute an approved trade plan.

        Args:
            plan: Trade plan dict (from TradePlanGenerator).
            trade_date: YYYY-MM-DD string.

        Returns:
            Execution report dict with results for each order.
        """
        if not self._connected:
            return self._error_report("Not connected", trade_date)

        if self._halted:
            return self._error_report(
                f"HALTED: {self._halt_reason}", trade_date
            )

        # Verify plan is approved
        status = plan.get("status", "")
        if status != "APPROVED":
            return self._error_report(
                f"Plan status is '{status}', must be APPROVED", trade_date
            )

        # Pre-trade: filter untradable tickers from entries
        try:
            from brokers.alpaca.tradable_assets import filter_tradable
            entry_tickers = [e.get("ticker", "") for e in plan.get("proposed_entries", [])]
            _, untradable = filter_tradable(entry_tickers)
            if untradable:
                logger.warning(
                    "Filtering %d untradable tickers from plan: %s",
                    len(untradable), untradable,
                )
                untradable_set = set(untradable)
                original_entries = plan.get("proposed_entries", [])
                plan = dict(plan)  # shallow copy — don't mutate original
                plan["proposed_entries"] = [
                    e for e in original_entries
                    if e.get("ticker", "") not in untradable_set
                ]
                # Record filtered entries in report
                for ticker in untradable:
                    _journal_entry("entry_filtered_untradable", {
                        "ticker": ticker, "trade_date": trade_date,
                        "reason": "not tradable on Alpaca",
                    })
        except Exception as e:
            logger.warning("Tradability check failed (proceeding anyway): %s", e)

        # Pre-trade: check market state
        all_plan_tickers = (
            [e.get("ticker") for e in plan.get("proposed_entries", [])]
            + [e.get("ticker") for e in plan.get("proposed_exits", [])]
        )
        all_plan_tickers = [t for t in all_plan_tickers if t]
        if all_plan_tickers:
            mkt_check = self.check_market_state(all_plan_tickers[:5])
            if not mkt_check["is_tradeable"]:
                logger.warning("Market state check: %s", mkt_check["message"])
                # Don't block — just warn (AU state unavailable)

        # Reset daily counter if new day
        if trade_date != self._daily_date:
            self._daily_order_count = 0
            self._daily_date = trade_date

        report = {
            "trade_date": trade_date,
            "executed_at": datetime.now().isoformat(),
            "dry_run": self.is_dry_run,
            "entries": [],
            "exits": [],
            "errors": [],
        }

        # Execute exits first (frees cash) — protective orders always proceed
        for exit_rec in plan.get("proposed_exits", []):
            result = self._execute_exit(exit_rec, trade_date)
            report["exits"].append(result)

        # ── Volatility gate: check macro conditions before new entries ──────
        vol_gate = self._run_volatility_gate()
        report["volatility_gate"] = vol_gate

        if vol_gate["action"] == "block":
            # Block all new entries — protective orders above already processed
            logger.warning(
                "Volatility gate BLOCKED all new entries: %s", vol_gate["message"],
            )
            _journal_entry("volatility_gate_block", {
                "trade_date": trade_date,
                "flags": vol_gate.get("flags", []),
                "message": vol_gate["message"],
            })
            # Send Telegram alert
            try:
                from scripts.volatility_gate import send_volatility_alert
                send_volatility_alert(vol_gate)
            except Exception as _e:
                logger.warning("Could not send volatility alert: %s", _e)
            # Record skipped entries
            for entry_rec in plan.get("proposed_entries", []):
                report["entries"].append({
                    "ticker": entry_rec.get("ticker", ""),
                    "side": "BUY",
                    "qty": entry_rec.get("position_size", 0),
                    "price": entry_rec.get("entry_price", 0),
                    "success": False,
                    "blocked": True,
                    "reason": "volatility_gate",
                    "message": vol_gate["message"],
                    "dry_run": self.is_dry_run,
                })

        else:
            # Proceed with entries — apply size reduction if gate is in "reduce" mode
            for entry_rec in plan.get("proposed_entries", []):
                if vol_gate["action"] == "reduce":
                    # Apply 50% size reduction: halve position_size
                    original_qty = entry_rec.get("position_size", 0)
                    reduced_qty = max(1, int(original_qty * vol_gate["size_multiplier"]))
                    entry_rec = dict(entry_rec)   # shallow copy — don't mutate plan
                    entry_rec["position_size"] = reduced_qty
                    entry_rec["vol_gate_reduced"] = True
                    entry_rec["vol_gate_original_qty"] = original_qty
                    logger.warning(
                        "Volatility gate REDUCING %s size: %d → %d (50%%): %s",
                        entry_rec.get("ticker", ""), original_qty, reduced_qty,
                        vol_gate["message"],
                    )
                result = self._execute_entry(entry_rec, trade_date)
                report["entries"].append(result)

        # Place protective stop orders for filled entries
        stop_orders = self.place_stops_for_plan(
            plan, report["entries"], self.config, trade_date,
        )
        report["stop_orders"] = stop_orders

        # Summary
        report["total_entries"] = len(report["entries"])
        report["total_exits"] = len(report["exits"])
        report["successful_entries"] = sum(
            1 for e in report["entries"] if e.get("success")
        )
        report["successful_exits"] = sum(
            1 for e in report["exits"] if e.get("success")
        )

        _journal_entry("plan_executed", {
            "trade_date": trade_date,
            "entries": report["total_entries"],
            "exits": report["total_exits"],
            "stops_placed": len(stop_orders),
            "dry_run": self.is_dry_run,
        })

        return report

    def _execute_entry(self, entry: dict, trade_date: str) -> dict:
        """Execute a single entry order."""
        ticker = entry.get("ticker", "")
        price = entry.get("entry_price", 0)
        qty = entry.get("position_size", 0)
        strategy = entry.get("strategy", "")
        confidence = entry.get("confidence", 0)
        stop_price = entry.get("stop_price", 0)

        # Pre-flight check
        errors = preflight_check_order(
            ticker, OrderSide.BUY, qty, price,
            self.safety, self._daily_order_count,
        )
        if errors:
            result = {
                "ticker": ticker, "side": "BUY", "qty": qty, "price": price,
                "success": False, "errors": errors, "dry_run": self.is_dry_run,
            }
            report_msg = f"ENTRY BLOCKED {ticker}: {'; '.join(errors)}"
            logger.warning(report_msg)
            _journal_entry("order_blocked", result)
            return result

        # Determine effective order price from entry refinement (if present)
        _refined_order_type = entry.get("order_type", "")        # "limit" | "" | "market"
        _refined_limit_price = entry.get("limit_price")           # set by entry_optimizer
        if _refined_order_type == "limit" and _refined_limit_price:
            _order_price = round(float(_refined_limit_price), 2)
            logger.info(
                "Limit order (refined): %s @ %.2f (DAY)", ticker, _order_price,
            )
        else:
            _order_price = round(price, 2)   # original entry_price

        if self.is_dry_run:
            result = {
                "ticker": ticker, "side": "BUY", "qty": qty, "price": _order_price,
                "strategy": strategy, "confidence": confidence,
                "stop_price": stop_price,
                "position_value": round(_order_price * qty, 2),
                "risk_amount": round(abs(_order_price - stop_price) * qty, 2),
                "success": True, "dry_run": True,
                "order_type": "LIMIT",
                "entry_refinement": entry.get("entry_refinement", ""),
                "message": "DRY RUN — order would be placed",
            }
            logger.info("DRY RUN BUY: %s %d x $%.2f", ticker, qty, _order_price)
            _journal_entry("dry_run_entry", result)
            self._daily_order_count += 1
            return result

        # Capture bid-ask spread at order time — never blocks execution
        spread_info = {}
        try:
            if self._broker and hasattr(self._broker, 'get_market_snapshot'):
                snap = self._broker.get_market_snapshot(ticker)
                if snap:
                    ask = snap.get("ask", 0)
                    bid = snap.get("bid", 0)
                    spread_info = {
                        "bid": bid,
                        "ask": ask,
                        "spread_bps": round((ask - bid) / ask * 10000, 1) if ask else None,
                        "last_trade": snap.get("last_trade", 0),
                    }
        except Exception:
            pass  # Never let spread capture block execution

        # Live execution — LIMIT order at (refined) entry price
        _submit_time = datetime.now().isoformat()
        order_result = self._broker.place_order(
            ticker=ticker,
            side=OrderSide.BUY,
            qty=qty,
            price=_order_price,
            order_type=OrderType.LIMIT,
            remark=f"atlas_{strategy}_{trade_date}"[:64],
        )

        result = {
            "ticker": ticker, "side": "BUY", "qty": qty, "price": _order_price,
            "strategy": strategy, "confidence": confidence,
            "stop_price": stop_price,
            "position_value": round(_order_price * qty, 2),
            "risk_amount": round(abs(_order_price - stop_price) * qty, 2),
            "success": order_result.success,
            "order_id": order_result.order_id,
            "status": order_result.status.value,
            "fill_price": order_result.fill_price,
            "message": order_result.message,
            "dry_run": False,
            # Enriched execution telemetry
            "submit_time": _submit_time,
            "fill_time": order_result.raw.get("filled_at", ""),
            "submitted_at": order_result.raw.get("submitted_at", ""),
            "planned_price": price,         # original signal entry price
            "refined_price": _order_price,  # actual price used (may differ from planned)
            "entry_refinement": entry.get("entry_refinement", ""),
            "slippage_bps": round((order_result.fill_price - _order_price) / _order_price * 10000, 1) if order_result.fill_price > 0 and _order_price > 0 else None,
            "order_type": "LIMIT",
            "spread": spread_info,
        }

        if order_result.success:
            self._daily_order_count += 1
            logger.info(
                "LIVE BUY: %s %d x $%.2f → order_id=%s",
                ticker, qty, price, order_result.order_id,
            )
            # Record entry to TradeLedger — telemetry must never crash execution
            try:
                from journal.logger import TradeLedger
                _ledger = TradeLedger()
                _ledger.record_entry({
                    "ticker": ticker, "strategy": strategy, "shares": qty,
                    "fill_price": order_result.fill_price or price,
                    "planned_price": price, "stop_price": stop_price,
                    "slippage_bps": result.get("slippage_bps"),
                    "order_id": order_result.order_id,
                    "timestamp": datetime.now().isoformat(),
                })
            except Exception as _ledger_exc:
                logger.warning("TradeLedger entry record failed (non-fatal): %s", _ledger_exc)
        else:
            logger.error(
                "LIVE BUY FAILED: %s — %s",
                ticker, order_result.message,
            )

        _journal_entry("live_entry", result)
        return result

    def _execute_exit(self, exit_rec: dict, trade_date: str) -> dict:
        """Execute a single exit order."""
        ticker = exit_rec.get("ticker", "")
        reason = exit_rec.get("reason", "signal_exit")

        # Cancel any protective stop order first (prevent double-sell)
        stop_order_id = exit_rec.get("stop_order_id", "")
        if stop_order_id:
            self.cancel_protective_stop(stop_order_id, ticker)

        # Get current position to determine qty and price
        if self._broker:
            positions = self._broker.get_positions()
            pos = next((p for p in positions if p.ticker == ticker), None)
        else:
            pos = None

        if not pos:
            result = {
                "ticker": ticker, "side": "SELL", "success": False,
                "message": f"No live position in {ticker}",
                "dry_run": self.is_dry_run,
            }
            _journal_entry("exit_no_position", result)
            return result

        qty = pos.shares
        price = pos.current_price

        # Capture entry telemetry for PnL and holding-period calculations
        entry_price = pos.entry_price if pos else None
        holding_days = None
        try:
            if pos and hasattr(pos, 'entry_date') and pos.entry_date:
                from datetime import date as _date
                _entry_str = str(pos.entry_date)[:10]
                _entry_date = _date.fromisoformat(_entry_str)
                holding_days = (_date.today() - _entry_date).days
        except Exception:
            pass

        # Pre-flight check
        errors = preflight_check_order(
            ticker, OrderSide.SELL, qty, price,
            self.safety, self._daily_order_count,
        )
        if errors:
            result = {
                "ticker": ticker, "side": "SELL", "qty": qty, "price": price,
                "success": False, "errors": errors, "dry_run": self.is_dry_run,
            }
            logger.warning("EXIT BLOCKED %s: %s", ticker, "; ".join(errors))
            _journal_entry("order_blocked", result)
            return result

        if self.is_dry_run:
            result = {
                "ticker": ticker, "side": "SELL", "qty": qty, "price": price,
                "success": True, "dry_run": True, "reason": reason,
                "message": "DRY RUN — exit would be placed",
            }
            logger.info("DRY RUN SELL: %s %d x $%.2f [%s]", ticker, qty, price, reason)
            _journal_entry("dry_run_exit", result)
            self._daily_order_count += 1
            return result

        # Audit H9: stop-triggered exits use MARKET to guarantee fill;
        # other exits use LIMIT with 1% buffer below market price.
        _stop_reasons = {"stop_loss", "broker_stop_fill"}
        if reason in _stop_reasons:
            _exit_order_type = OrderType.MARKET
            _exit_price = round(price, 2)  # price ignored for MARKET but required by API
        else:
            _exit_order_type = OrderType.LIMIT
            _exit_price = round(price * 0.99, 2)  # 1% buffer to improve fill odds

        # Capture bid-ask spread at exit order time — never blocks execution
        spread_info = {}
        try:
            if self._broker and hasattr(self._broker, 'get_market_snapshot'):
                snap = self._broker.get_market_snapshot(ticker)
                if snap:
                    ask = snap.get("ask", 0)
                    bid = snap.get("bid", 0)
                    spread_info = {
                        "bid": bid,
                        "ask": ask,
                        "spread_bps": round((ask - bid) / ask * 10000, 1) if ask else None,
                        "last_trade": snap.get("last_trade", 0),
                    }
        except Exception:
            pass  # Never let spread capture block execution

        _submit_time = datetime.now().isoformat()
        order_result = self._broker.place_order(
            ticker=ticker,
            side=OrderSide.SELL,
            qty=qty,
            price=_exit_price,
            order_type=_exit_order_type,
            remark=f"atlas_exit_{reason}_{trade_date}",
        )

        result = {
            "ticker": ticker, "side": "SELL", "qty": qty, "price": _exit_price,
            "order_type": _exit_order_type.value,
            "success": order_result.success,
            "order_id": order_result.order_id,
            "status": order_result.status.value,
            "fill_price": order_result.fill_price,
            "reason": reason,
            "message": order_result.message,
            "dry_run": False,
            # Enriched exit telemetry
            "submit_time": _submit_time,
            "fill_time": order_result.raw.get("filled_at", ""),
            "submitted_at": order_result.raw.get("submitted_at", ""),
            "planned_price": _exit_price,
            "slippage_bps": round((order_result.fill_price - _exit_price) / _exit_price * 10000, 1) if order_result.fill_price > 0 and _exit_price > 0 else None,
            "entry_price": entry_price,
            "holding_days": holding_days,
            "spread": spread_info,
        }

        if order_result.success:
            self._daily_order_count += 1
            logger.info(
                "LIVE SELL: %s %d x $%.2f → order_id=%s [%s]",
                ticker, qty, price, order_result.order_id, reason,
            )

            # Poll for fill confirmation — LIMIT orders return fill_price=0
            # at submission time. Wait up to 60s for the fill to come through.
            if order_result.fill_price == 0 and order_result.order_id:
                import time as _time
                _poll_start = _time.time()
                _max_wait = 60  # seconds
                _poll_interval = 3  # seconds
                logger.info("Waiting for fill on %s (order %s)...",
                            ticker, order_result.order_id)
                while _time.time() - _poll_start < _max_wait:
                    _time.sleep(_poll_interval)
                    status_result = self._broker.get_order_status(
                        order_result.order_id)
                    if status_result.fill_price > 0:
                        result["fill_price"] = status_result.fill_price
                        result["status"] = status_result.status.value
                        logger.info(
                            "Fill confirmed: %s @ $%.4f (waited %.0fs)",
                            ticker, status_result.fill_price,
                            _time.time() - _poll_start,
                        )
                        break
                    if status_result.status.value in ("FAILED", "CANCELLED",
                                                       "CANCELLED_ALL"):
                        result["status"] = status_result.status.value
                        result["message"] = f"Order {status_result.status.value}"
                        logger.warning("Order %s for %s: %s",
                                       order_result.order_id, ticker,
                                       status_result.status.value)
                        break
                else:
                    logger.warning(
                        "Fill not confirmed for %s after %ds — "
                        "fill_price remains 0. Check order %s manually.",
                        ticker, _max_wait, order_result.order_id,
                    )

            # Track protective stop fill quality — telemetry, never blocks execution
            try:
                _stop_price = pos.stop_price if pos else 0
                if reason in ("stop_loss", "protective_stop") and _stop_price and result.get("fill_price", 0) > 0:
                    result["stop_expected_price"] = _stop_price
                    result["stop_fill_price"] = result["fill_price"]
                    result["stop_slippage_bps"] = round(
                        (_stop_price - result["fill_price"]) / _stop_price * 10000, 1
                    )
            except Exception:
                pass

            # Record exit to TradeLedger — telemetry must never crash execution
            try:
                from journal.logger import TradeLedger
                _ledger = TradeLedger()
                _fill_price = result.get("fill_price") or _exit_price
                _pnl = round((_fill_price - entry_price) * qty, 2) if entry_price else None
                _pnl_pct = round((_fill_price - entry_price) / entry_price * 100, 2) if entry_price and entry_price > 0 else None
                _ledger.record_exit({
                    "ticker": ticker,
                    "strategy": pos.strategy if pos and hasattr(pos, 'strategy') else "",
                    "shares": qty,
                    "fill_price": _fill_price,
                    "entry_price": entry_price,
                    "pnl": _pnl,
                    "pnl_pct": _pnl_pct,
                    "holding_days": holding_days,
                    "exit_reason": reason,
                    "slippage_bps": result.get("slippage_bps"),
                    "order_id": order_result.order_id,
                })
            except Exception as _ledger_exc:
                logger.warning("TradeLedger exit record failed (non-fatal): %s", _ledger_exc)
        else:
            logger.error(
                "LIVE SELL FAILED: %s — %s", ticker, order_result.message,
            )

        _journal_entry("live_exit", result)
        return result

    # ── Protective stop orders ─────────────────────────────────

    def place_protective_stop(
        self,
        ticker: str,
        qty: int,
        stop_price: float,
        strategy: str = "",
        trailing_atr: float = 0.0,
        trade_date: str = "",
    ) -> Optional[str]:
        """Place a protective STOP SELL or TRAILING_STOP SELL on the exchange.

        Called after an entry LIMIT BUY fills. Returns the stop order ID,
        or None on failure.

        Args:
            ticker: Position ticker (.AX format).
            qty: Number of shares to protect (must match position).
            stop_price: Hard stop price (used for STOP orders).
            strategy: Strategy name — determines stop type.
            trailing_atr: If > 0, place TRAILING_STOP with this dollar amount
                         instead of a fixed STOP. Calculated as
                         trailing_stop_atr_mult × ATR at entry time.
            trade_date: For remark/journal.

        Returns:
            Order ID string if placed, None if failed or dry-run.
        """
        if not self._connected or not self._broker:
            logger.error("Cannot place protective stop — not connected")
            return None

        use_trailing = trailing_atr > 0

        if use_trailing:
            order_type = OrderType.TRAILING_STOP
            log_label = f"TRAILING_STOP SELL trail=${trailing_atr:.2f}"
        else:
            order_type = OrderType.STOP
            log_label = f"STOP SELL trigger=${stop_price:.2f}"

        logger.info(
            "Placing protective stop: %s %s %d shares [%s]",
            ticker, log_label, qty, strategy,
        )

        if self.is_dry_run:
            _journal_entry("dry_run_protective_stop", {
                "ticker": ticker, "qty": qty, "stop_price": stop_price,
                "trailing_atr": trailing_atr, "order_type": order_type.value,
                "strategy": strategy,
            })
            logger.info("DRY RUN: would place %s for %s", log_label, ticker)
            return None

        # Build order kwargs
        if use_trailing:
            # TRAILING_STOP SELL: trail_price is the dollar trail distance (Alpaca)
            order_result = self._broker.place_order(
                ticker=ticker,
                side=OrderSide.SELL,
                qty=qty,
                price=stop_price,  # reference/activation price
                order_type=order_type,
                remark=f"atlas_stop_{strategy}_{trade_date}"[:64],
                trail_price=trailing_atr,
            )
        else:
            # Fixed STOP SELL: aux_price is the trigger
            order_result = self._broker.place_order(
                ticker=ticker,
                side=OrderSide.SELL,
                qty=qty,
                price=stop_price,  # limit price after trigger (= stop price for market-like fill)
                order_type=order_type,
                stop_price=stop_price,  # trigger price
                remark=f"atlas_stop_{strategy}_{trade_date}"[:64],
            )

        if not order_result.success:
            logger.error(
                "Protective stop FAILED for %s: %s",
                ticker, order_result.message,
            )
            _journal_entry("protective_stop_failed", {
                "ticker": ticker, "error": order_result.message,
                "order_type": order_type.value,
            })
            return None

        order_id = order_result.order_id
        logger.info(
            "Protective stop placed: %s %s → order_id=%s",
            ticker, log_label, order_id,
        )
        _journal_entry("protective_stop_placed", {
            "ticker": ticker, "order_id": order_id,
            "stop_price": stop_price, "trailing_atr": trailing_atr,
            "order_type": order_type.value, "strategy": strategy,
        })
        return order_id

    def cancel_protective_stop(self, order_id: str, ticker: str = "") -> bool:
        """Cancel a protective stop order (e.g. before placing a new one or on exit)."""
        if not self._connected or not self._broker or not order_id:
            return False

        result = self._broker.cancel_order(order_id)
        if result.success:
            logger.info("Cancelled protective stop %s for %s", order_id, ticker)
            _journal_entry("protective_stop_cancelled", {
                "order_id": order_id, "ticker": ticker,
            })
        else:
            logger.warning(
                "Failed to cancel protective stop %s for %s: %s",
                order_id, ticker, result.message,
            )
        return result.success

    def place_stops_for_plan(
        self,
        plan: dict,
        entry_results: list[dict],
        config: dict,
        trade_date: str,
    ) -> dict[str, str]:
        """Place protective stops for all successfully filled entries.

        Called after execute_plan(). Reads strategy config to determine
        stop type (fixed vs trailing) and calculates trail amount.

        Returns:
            Dict of ticker → stop_order_id for successfully placed stops.
        """
        stop_orders = {}
        entries = plan.get("proposed_entries", [])

        for entry_rec, result in zip(entries, entry_results):
            if not result.get("success"):
                continue

            ticker = entry_rec.get("ticker", "")
            qty = entry_rec.get("position_size", 0)
            stop_price = entry_rec.get("stop_price", 0)
            strategy = entry_rec.get("strategy", "")

            if not ticker or not qty or not stop_price:
                continue

            # Skip stop placement if the entry order is still pending (not filled).
            # Placing a STOP SELL while a LIMIT BUY is open causes a "wash trade"
            # rejection on Alpaca.  sync_protective_orders.py handles post-fill stops.
            entry_status = result.get("status", "").upper()
            if entry_status in ("SUBMITTED", "NEW", "ACCEPTED", "PENDING_NEW",
                                "PENDING", "PARTIALLY_FILLED"):
                logger.info(
                    "Skipping immediate stop for %s (order status=%s) — "
                    "sync_protective_orders will place stop after fill.",
                    ticker, entry_status,
                )
                continue

            # Determine if this strategy uses trailing stops
            strat_cfg = config.get("strategies", {}).get(strategy, {})
            trailing_mult = strat_cfg.get("trailing_stop_atr_mult", 0)

            trailing_atr = 0.0
            if trailing_mult > 0:
                # Calculate trail amount from entry signal features
                atr_value = entry_rec.get("features", {}).get("atr", 0)
                if atr_value > 0:
                    trailing_atr = round(trailing_mult * atr_value, 4)

            order_id = self.place_protective_stop(
                ticker=ticker,
                qty=qty,
                stop_price=stop_price,
                strategy=strategy,
                trailing_atr=trailing_atr,
                trade_date=trade_date,
            )

            if order_id:
                stop_orders[ticker] = order_id

        return stop_orders

    # ── Account queries (delegated to broker) ──────────────────

    def get_account_info(self) -> Optional[AccountInfo]:
        if not self._connected or not self._broker:
            return None
        return self._broker.get_account_info()

    def get_positions(self) -> list[PositionInfo]:
        if not self._connected or not self._broker:
            return []
        return self._broker.get_positions()

    def get_open_orders(self) -> list[OrderResult]:
        if not self._connected or not self._broker:
            return []
        return self._broker.get_open_orders()

    # ── Emergency controls ─────────────────────────────────────

    def emergency_halt(self, reason: str = "Manual emergency halt"):
        """Immediately halt all live trading and cancel open orders."""
        self._halted = True
        self._halt_reason = reason
        logger.critical("EMERGENCY HALT: %s", reason)
        _journal_entry("emergency_halt", {"reason": reason})

        # Write halt file (persists across restarts)
        HALT_FILE.write_text(f"{reason}\n{datetime.now().isoformat()}")

        # Cancel all open orders
        if self._connected and self._broker:
            try:
                results = self._broker.cancel_all_orders()
                logger.warning(
                    "Cancelled %d orders during emergency halt",
                    len(results),
                )
            except Exception as e:
                logger.error("Failed to cancel orders during halt: %s", e)

    def clear_halt(self):
        """Clear the halt state. Requires manual intervention."""
        if HALT_FILE.exists():
            HALT_FILE.unlink()
        self._halted = False
        self._halt_reason = ""
        _journal_entry("halt_cleared", {})
        logger.info("Halt cleared")

    # ── Market State Check ──────────────────────────────────────

    def check_market_state(self, tickers: list[str] = None) -> dict:
        """Check if markets are open before trading.

        Returns dict with:
            - is_tradeable: bool — whether we should proceed
            - states: list of MarketStateInfo
            - message: human-readable summary
        """
        if not self._connected or not self._broker:
            return {"is_tradeable": False, "message": "Not connected", "states": []}

        if not tickers:
            tickers = ["US.SPY"]  # Default probe ticker

        states = self._broker.get_market_states(tickers)
        if not states:
            # If market state query fails, don't block — just warn
            logger.warning("Could not check market state, proceeding anyway")
            return {"is_tradeable": True, "message": "Market state unknown", "states": []}

        closed_states = {"REST", "OVERNIGHT", "AFTER_HOURS_END"}
        tradeable = True
        messages = []

        for s in states:
            if s.market_state == "AU_UNSUPPORTED":
                messages.append(f"{s.ticker}: AU market state unavailable")
            elif s.market_state in closed_states:
                tradeable = False
                messages.append(f"{s.ticker}: market {s.market_state}")
            else:
                messages.append(f"{s.ticker}: {s.market_state}")

        result = {
            "is_tradeable": tradeable,
            "states": states,
            "message": "; ".join(messages),
        }
        _journal_entry("market_state_check", {
            "is_tradeable": tradeable,
            "message": result["message"],
        })
        return result

    # ── Post-Trade Fee & Slippage Analysis ─────────────────────

    def get_fee_analysis(self, days: int = 90) -> dict:
        """Analyse actual fees vs assumed fees in config.

        Returns comparison report for backtest fee calibration.
        """
        if not self._connected or not self._broker:
            return {"error": "Not connected"}

        # Get filled orders to find order IDs
        orders = self._broker.get_history_orders(days=days)
        filled_ids = [
            o.order_id for o in orders
            if o.status.value in ("FILLED", "PARTIAL_FILLED") and o.order_id
        ]

        if not filled_ids:
            return {"total_orders": 0, "message": "No filled orders in period"}

        # Query actual fees
        fees = self._broker.get_order_fees(filled_ids[:50])  # API limit safety

        if not fees:
            return {"total_orders": len(filled_ids), "message": "Fee query returned no data"}

        # Compute actuals
        total_actual = sum(f.total_fee for f in fees)
        avg_actual = total_actual / len(fees) if fees else 0

        # Compute fee breakdown
        fee_breakdown = {}
        for f in fees:
            for name, amount in f.fee_details:
                fee_breakdown.setdefault(name, {"count": 0, "total": 0.0})
                fee_breakdown[name]["count"] += 1
                fee_breakdown[name]["total"] += amount

        # Compare with config assumptions
        config_flat = self.config.get("fees", {}).get("commission_per_trade", 3.0)
        config_pct = self.config.get("fees", {}).get("commission_pct", 0.0003)

        # Get average order value from filled orders
        order_map = {o.order_id: o for o in orders}
        order_values = []
        for f in fees:
            o = order_map.get(f.order_id)
            if o and o.fill_price > 0 and o.filled_qty > 0:
                order_values.append(o.fill_price * o.filled_qty)
        avg_order_value = sum(order_values) / len(order_values) if order_values else 0

        # Expected fee per config
        expected_per_trade = max(config_flat, avg_order_value * config_pct)

        report = {
            "period_days": days,
            "total_orders_filled": len(filled_ids),
            "orders_with_fees": len(fees),
            "total_actual_fees": round(total_actual, 2),
            "avg_actual_fee": round(avg_actual, 2),
            "fee_breakdown": {
                name: {"count": v["count"], "avg": round(v["total"] / v["count"], 2)}
                for name, v in fee_breakdown.items()
            },
            "config_commission_flat": config_flat,
            "config_commission_pct": config_pct,
            "avg_order_value": round(avg_order_value, 2),
            "expected_fee_per_config": round(expected_per_trade, 2),
            "fee_delta": round(avg_actual - expected_per_trade, 2),
            "fee_delta_pct": round(
                ((avg_actual - expected_per_trade) / expected_per_trade * 100)
                if expected_per_trade > 0 else 0, 1
            ),
        }

        _journal_entry("fee_analysis", report)
        return report

    def get_slippage_analysis(self, days: int = 90) -> dict:
        """Analyse actual slippage vs assumed slippage in config.

        Returns comparison report for backtest slippage calibration.
        """
        if not self._connected or not self._broker:
            return {"error": "Not connected"}

        slippage_data = self._broker.get_slippage_report(days=days)
        if not slippage_data:
            return {"total_orders": 0, "message": "No filled orders for slippage analysis"}

        buy_slips = [s for s in slippage_data if s.side == "BUY"]
        sell_slips = [s for s in slippage_data if s.side == "SELL"]

        config_slip = self.config.get("fees", {}).get("slippage_pct", 0.001)

        def _slip_stats(slips):
            if not slips:
                return {"count": 0}
            pcts = [s.slippage_pct for s in slips]
            costs = [s.slippage_cost for s in slips]
            return {
                "count": len(slips),
                "avg_slippage_pct": round(sum(pcts) / len(pcts), 4),
                "max_slippage_pct": round(max(pcts), 4),
                "min_slippage_pct": round(min(pcts), 4),
                "total_slippage_cost": round(sum(costs), 2),
                "avg_slippage_cost": round(sum(costs) / len(costs), 2),
            }

        report = {
            "period_days": days,
            "total_orders": len(slippage_data),
            "config_slippage_pct": config_slip * 100,
            "buy_slippage": _slip_stats(buy_slips),
            "sell_slippage": _slip_stats(sell_slips),
            "all_slippage": _slip_stats(slippage_data),
            "details": [
                {
                    "ticker": s.ticker, "side": s.side,
                    "requested": s.requested_price, "filled": s.fill_price,
                    "slip_pct": s.slippage_pct, "cost": s.slippage_cost,
                }
                for s in slippage_data
            ],
        }

        # Calibration recommendation
        actual_avg = report["all_slippage"].get("avg_slippage_pct", 0)
        if actual_avg != 0:
            report["recommendation"] = (
                f"Config slippage: {config_slip*100:.2f}% | "
                f"Actual avg: {actual_avg:.4f}% | "
                f"{'Config is conservative' if config_slip*100 > actual_avg else 'Config may underestimate slippage'}"
            )

        _journal_entry("slippage_analysis", report)
        return report

    def get_execution_history(self, days: int = 30) -> dict:
        """Full execution history with fees, slippage, and P&L per trade."""
        if not self._connected or not self._broker:
            return {"error": "Not connected"}

        orders = self._broker.get_history_orders(days=days)
        deals = self._broker.get_history_deals(days=days)

        # Get fees for filled orders
        filled_ids = [
            o.order_id for o in orders
            if o.status.value in ("FILLED", "PARTIAL_FILLED") and o.order_id
        ]
        fees = {}
        if filled_ids:
            fee_list = self._broker.get_order_fees(filled_ids[:50])
            fees = {f.order_id: f for f in fee_list}

        # Build per-order summary
        history = []
        for order in orders:
            order_deals = [d for d in deals if d.order_id == order.order_id]
            total_qty = sum(d.qty for d in order_deals)
            vwap = (
                sum(d.price * d.qty for d in order_deals) / total_qty
                if total_qty > 0 else 0
            )
            fee_info = fees.get(order.order_id)

            history.append({
                "order_id": order.order_id,
                "ticker": order.ticker,
                "side": order.side.value,
                "status": order.status.value,
                "requested_qty": order.requested_qty,
                "filled_qty": total_qty,
                "requested_price": order.requested_price,
                "fill_vwap": round(vwap, 4),
                "fee": fee_info.total_fee if fee_info else 0,
                "fee_details": fee_info.fee_details if fee_info else [],
                "deal_count": len(order_deals),
                "create_time": order.raw.get("create_time", ""),
                "error_msg": order.message if order.status == OrderStatus.FAILED else "",
            })

        return {
            "period_days": days,
            "total_orders": len(orders),
            "filled": sum(1 for h in history if h["status"] in ("FILLED", "PARTIAL_FILLED")),
            "cancelled": sum(1 for h in history if h["status"] == "CANCELLED"),
            "failed": sum(1 for h in history if h["status"] == "FAILED"),
            "total_fees": round(sum(h["fee"] for h in history), 2),
            "orders": history,
        }



    # ── Limit order lifecycle ──────────────────────────────────

    def cancel_unfilled_limits(self, cutoff_hour: int = 12) -> list:
        """Cancel unfilled limit BUY orders after cutoff hour (default noon ET).

        Called by a midday cron job to avoid stale open-limit orders sitting
        on the exchange through the close.  Only BUY limit orders are cancelled;
        protective STOP/TRAILING_STOP SELL orders are left untouched.

        Args:
            cutoff_hour: Hour in ET (0-23) after which unfilled limits are
                         cancelled.  Default is 12 (noon ET).

        Returns:
            List of cancellation result dicts, one per attempted cancel.
        """
        if not self._connected or not self._broker:
            logger.warning("cancel_unfilled_limits: not connected")
            return []

        # Determine current ET time without requiring pytz
        from datetime import timezone, timedelta
        _et_offset = timedelta(hours=-4)   # approximate EDT (UTC-4); -5 in EST
        _now_et = datetime.now(tz=timezone(_et_offset))
        current_et_hour = _now_et.hour

        if current_et_hour < cutoff_hour:
            logger.info(
                "cancel_unfilled_limits: ET hour %d < cutoff %d — skipping",
                current_et_hour, cutoff_hour,
            )
            return []

        open_orders = self._broker.get_open_orders()
        cancelled = []

        for order in open_orders:
            # Only cancel BUY-side orders (not protective STOP SELL orders)
            if order.side != OrderSide.BUY:
                continue
            # Only cancel non-terminal orders
            if order.status.value in ("FILLED", "CANCELLED", "FAILED", "CANCELLED_ALL"):
                continue

            cancel_result = self._broker.cancel_order(order.order_id)
            entry = {
                "ticker":       order.ticker,
                "order_id":     order.order_id,
                "success":      cancel_result.success,
                "message":      cancel_result.message,
                "reason":       "unfilled_limit_cutoff",
                "cutoff_hour":  cutoff_hour,
                "et_hour":      current_et_hour,
                "cancelled_at": _now_et.isoformat(),
            }
            cancelled.append(entry)

            if cancel_result.success:
                logger.info(
                    "Cancelled unfilled limit BUY: %s order_id=%s",
                    order.ticker, order.order_id,
                )
            else:
                logger.warning(
                    "Failed to cancel limit order %s for %s: %s",
                    order.order_id, order.ticker, cancel_result.message,
                )
            _journal_entry("limit_cancelled_cutoff", entry)

        logger.info(
            "cancel_unfilled_limits: cancelled %d/%d orders (ET hour %d, cutoff %d)",
            sum(1 for c in cancelled if c["success"]),
            len(cancelled),
            current_et_hour,
            cutoff_hour,
        )
        return cancelled

    # ── Volatility gate ────────────────────────────────────────

    def _run_volatility_gate(self) -> dict:
        """Run the pre-market volatility gate check.

        Loads the gate module lazily to avoid import overhead when not needed.
        Returns a safe no-action result if the module fails to import or errors.
        """
        try:
            from scripts.volatility_gate import check_volatility_gate
            return check_volatility_gate(self.config)
        except ImportError as e:
            logger.warning("Volatility gate module unavailable: %s — skipping", e)
        except Exception as e:
            logger.error("Volatility gate check failed: %s — skipping", e)
        # Safe fallback: no gate action (proceed normally)
        return {
            "gate_enabled": False,
            "triggered_count": 0,
            "flags": [],
            "action": "none",
            "size_multiplier": 1.0,
            "message": "Volatility gate skipped (error or unavailable)",
        }

    # ── Helpers ────────────────────────────────────────────────

    def _error_report(self, message: str, trade_date: str) -> dict:
        logger.error("Execution blocked: %s", message)
        _journal_entry("execution_blocked", {
            "trade_date": trade_date, "reason": message,
        })
        return {
            "trade_date": trade_date,
            "executed_at": datetime.now().isoformat(),
            "error": message,
            "entries": [],
            "exits": [],
        }
