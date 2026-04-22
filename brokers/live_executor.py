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
# TODO: Refactor — 2190 lines. Split into: PlanLoader, OrderRouter, ExecutionReporter modules.
# TODO: Split into preflight.py, protective_orders.py, execution_journal.py

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

# ── RegimeModel lazy singleton ─────────────────────────────────
_regime_model = None


def _get_regime_model():
    """Return a cached RegimeModel instance (avoids re-init on every call)."""
    global _regime_model
    if _regime_model is None:
        from regime.model import RegimeModel
        _regime_model = RegimeModel()
    return _regime_model
EXECUTION_LOG = PROJECT_ROOT / "logs" / "live_executions.jsonl"


def _health_log(level: str, message: str, detail: dict = None) -> None:
    """Write to system_log table. Non-fatal."""
    try:
        from monitor.health_writer import log_error, log_warning, log_critical, log_info
        fn = {"error": log_error, "warning": log_warning, "critical": log_critical}.get(level, log_info)
        fn("live_executor", message, detail)
    except (RuntimeError, ValueError, OSError, KeyError, AttributeError) as e:
        logger.debug("health_log write failed: %s", e)
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
        # Circuit breaker state (A4)
        self._circuit_breaker_tripped = False
        self._daily_start_equity: float = 0.0

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

    @property
    def max_daily_loss_pct(self) -> float:
        """Maximum allowed daily portfolio drawdown as a fraction (e.g. 0.02 = 2%).

        Read from ``trading.live_safety.max_daily_loss_pct`` (default 0.02).
        """
        raw = self.safety.get("max_daily_loss_pct", 0.02)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.02

    # ── Circuit Breaker (A4) ───────────────────────────────────

    def _reset_circuit_breaker_if_new_day(self, trade_date: str) -> None:
        """Reset circuit breaker state when a new trading day begins."""
        if trade_date != self._daily_date:
            self._circuit_breaker_tripped = False
            self._daily_start_equity = 0.0

    def _capture_start_equity(self) -> None:
        """Capture the portfolio equity at the start of execution.

        Called once per execute_plan() call to establish the daily P&L baseline.
        Does nothing if equity has already been captured today.
        Silently skips if the broker is unavailable (non-blocking).
        """
        if self._daily_start_equity > 0:
            return  # Already captured for today
        if not self._broker:
            return
        try:
            account = self._broker.get_account_info()
            if account and account.equity > 0:
                self._daily_start_equity = account.equity
                logger.info(
                    "Circuit breaker: start equity captured $%.2f",
                    self._daily_start_equity,
                )
        except Exception as e:
            logger.warning(
                "Circuit breaker: could not capture start equity (non-fatal): %s", e
            )

    def _check_circuit_breaker(self, trade_date: str) -> bool:
        """Check if the daily loss circuit breaker should trip.

        Returns True (BLOCKED) if daily drawdown has exceeded the configured
        maximum.  Returns False (ALLOWED) if within limits or if the check
        cannot be completed.

        Side-effects:
        - Sets ``self._circuit_breaker_tripped = True`` on first trip.
        - Sends a Telegram alert on trip.
        - Writes a journal entry on trip.
        - Logs a warning on every blocked call after trip.

        Args:
            trade_date: YYYY-MM-DD string for journal entries.

        Returns:
            True if new entries should be BLOCKED, False if allowed.
        """
        # Already tripped — fast path
        if self._circuit_breaker_tripped:
            logger.warning(
                "CIRCUIT BREAKER: daily loss limit already tripped — "
                "blocking new order placement"
            )
            return True

        # No start equity captured — can't calculate P&L, allow through
        if self._daily_start_equity <= 0:
            return False

        if not self._broker:
            return False

        try:
            account = self._broker.get_account_info()
        except Exception as e:
            logger.warning(
                "Circuit breaker P&L check failed (non-blocking): %s", e
            )
            return False

        if not account or account.equity <= 0:
            return False

        current_equity = account.equity
        loss = self._daily_start_equity - current_equity
        loss_pct = loss / self._daily_start_equity if self._daily_start_equity > 0 else 0.0
        threshold = self.max_daily_loss_pct

        if loss_pct < threshold:
            return False  # Within limits

        # Trip the breaker
        self._circuit_breaker_tripped = True
        _health_log("error", "Circuit breaker tripped", {
            "start_equity": self._daily_start_equity,
            "current_equity": current_equity,
            "loss_pct": round(loss_pct * 100, 4),
        })
        msg = (
            f"CIRCUIT BREAKER TRIPPED: daily loss ${loss:.2f} "
            f"({loss_pct*100:.2f}%) exceeds limit "
            f"({threshold*100:.2f}% of ${self._daily_start_equity:.2f}). "
            "Blocking all new entry orders."
        )
        logger.error(msg)

        _journal_entry("circuit_breaker_tripped", {
            "trade_date": trade_date,
            "start_equity": self._daily_start_equity,
            "current_equity": current_equity,
            "loss": round(loss, 2),
            "loss_pct": round(loss_pct * 100, 4),
            "threshold_pct": round(threshold * 100, 4),
        })

        # Send Telegram alert
        try:
            from utils.telegram import send_message
            alert = (
                "🔴 <b>ATLAS CIRCUIT BREAKER TRIPPED</b>\n\n"
                f"Daily loss: <b>${loss:.2f} ({loss_pct*100:.2f}%)</b>\n"
                f"Limit: {threshold*100:.2f}% of ${self._daily_start_equity:.2f}\n"
                f"Date: {trade_date}\n\n"
                "⛔ All new entry orders are BLOCKED for today.\n"
                "Existing positions and protective stops are unaffected."
            )
            send_message(alert)
        except Exception as tg_exc:
            logger.warning("Circuit breaker: could not send Telegram alert: %s", tg_exc)

        return True

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
            _health_log("error", f"Broker connect failed: {broker_name}")

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
            logger.warning(
                "Tradability check failed (proceeding anyway): %s", e, exc_info=True
            )

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

        # Reset daily counter and circuit breaker if new day
        if trade_date != self._daily_date:
            self._daily_order_count = 0
            self._daily_date = trade_date
            self._reset_circuit_breaker_if_new_day(trade_date)

        # Capture starting equity for circuit breaker P&L calculations
        if not self.is_dry_run:
            self._capture_start_equity()

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
            # ── Circuit breaker check before any new entries ─────────────
            if not self.is_dry_run and self._check_circuit_breaker(trade_date):
                report["circuit_breaker_tripped"] = True
                for entry_rec in plan.get("proposed_entries", []):
                    report["entries"].append({
                        "ticker": entry_rec.get("ticker", ""),
                        "side": "BUY",
                        "qty": entry_rec.get("position_size", 0),
                        "price": entry_rec.get("entry_price", 0),
                        "success": False,
                        "blocked": True,
                        "reason": "circuit_breaker",
                        "message": (
                            f"Daily loss limit exceeded "
                            f"({self.max_daily_loss_pct*100:.1f}% threshold)"
                        ),
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

        _health_log("info", "Execution completed", {
            "trade_date": trade_date,
            "entries": report["successful_entries"],
            "exits": report["successful_exits"],
            "total_entries": report["total_entries"],
            "total_exits": report["total_exits"],
            "dry_run": self.is_dry_run,
        })

        return report

    # ── Order Intent Convention ──────────────────────────────────────────
    # Orders carry intent via the `remark` prefix → client_order_id:
    #   atlas_entry_*   → opening position (BUY for longs, SELL for shorts)
    #   atlas_exit_*    → closing position (manual/forced exit)
    #   atlas_stop_*    → protective stop-loss (GTC, auto-managed)
    #   atlas_tp_*      → protective take-profit (GTC, auto-managed)
    #
    # sync_protective_orders uses this to distinguish orphaned protective
    # orders (safe to cancel) from pending entry orders (must keep).
    # When adding short strategies, tag SELL entries as atlas_entry_*
    # so the orphan guard doesn't cancel them.
    # ─────────────────────────────────────────────────────────────────────
    def _execute_entry(self, entry: dict, trade_date: str) -> dict:
        """Execute a single entry order."""
        # Kill switch check (B6) — blocks ALL new entries when HALT file exists
        from brokers.kill_switch import is_halted, halt_reason
        if is_halted():
            msg = f"KILL SWITCH ACTIVE — entry blocked: {halt_reason()}"
            logger.critical(msg)
            try:
                from utils.telegram import send_message
                send_message(f"🛑 {msg}")
            except Exception:
                pass
            return {"status": "halted", "reason": "kill_switch"}
        ticker = entry.get("ticker", "")
        # Price arbiter halt check (B5)
        from brokers.price_arbiter import is_ticker_halted
        if is_ticker_halted(ticker):
            logger.critical(
                "execute_entry BLOCKED: %s is halted due to price arbiter disagreement",
                ticker,
            )
            return {"status": "halted", "ticker": ticker, "reason": "price_arbiter_halt"}
        price = entry.get("entry_price", 0)
        qty = entry.get("position_size", 0)
        strategy = entry.get("strategy", "")
        confidence = entry.get("confidence", 0)
        stop_price = entry.get("stop_price", 0)

        # Get current regime for trade record enrichment
        try:
            _regime_state = _get_regime_model().classify_current().state.value
        except (RuntimeError, ValueError, OSError, KeyError, AttributeError):
            _regime_state = None

        direction = "long"
        order_side = OrderSide.BUY
        side_label = order_side.value

        # Pre-flight check
        errors = preflight_check_order(
            ticker, order_side, qty, price,
            self.safety, self._daily_order_count,
        )
        if errors:
            result = {
                "ticker": ticker, "side": side_label, "qty": qty, "price": price,
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
                "ticker": ticker, "side": side_label, "qty": qty, "price": _order_price,
                "strategy": strategy, "confidence": confidence,
                "stop_price": stop_price,
                "direction": direction,
                "position_value": round(_order_price * qty, 2),
                "risk_amount": round(abs(_order_price - stop_price) * qty, 2),
                "success": True, "dry_run": True,
                "order_type": "LIMIT",
                "entry_refinement": entry.get("entry_refinement", ""),
                "message": "DRY RUN — order would be placed",
            }
            logger.info("DRY RUN %s: %s %d x $%.2f", side_label, ticker, qty, _order_price)
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
        except Exception as e:
            logger.debug("Entry spread capture failed (non-blocking): %s", e)

        # Live execution — LIMIT order at (refined) entry price
        _submit_time = datetime.now().isoformat()
        order_result = self._broker.place_order(
            ticker=ticker,
            side=order_side,
            qty=qty,
            price=_order_price,
            order_type=OrderType.LIMIT,
            remark=f"atlas_entry_{strategy}_{trade_date}"[:64],
        )

        # Poll for fill — LIMIT orders submitted pre-market return
        # fill_price=0 and status=SUBMITTED.  If the market is open the
        # fill may arrive within seconds; poll briefly to capture it.
        if (order_result.success
                and order_result.fill_price == 0
                and order_result.order_id
                and order_result.status not in (
                    OrderStatus.FILLED, OrderStatus.FAILED, OrderStatus.CANCELLED)):
            import time as _time
            _poll_start = _time.time()
            _max_wait = 15   # seconds — short poll; full reconciliation runs later
            _poll_interval = 3
            logger.info("Polling for entry fill on %s (order %s)...",
                        ticker, order_result.order_id)
            while _time.time() - _poll_start < _max_wait:
                _time.sleep(_poll_interval)
                try:
                    status_result = self._broker.get_order_status(
                        order_result.order_id)
                    if status_result.fill_price > 0:
                        order_result = status_result
                        logger.info(
                            "Entry fill confirmed: %s @ $%.4f (waited %.0fs)",
                            ticker, status_result.fill_price,
                            _time.time() - _poll_start,
                        )
                        break
                    if status_result.status in (OrderStatus.FAILED,
                                                OrderStatus.CANCELLED):
                        order_result = status_result
                        logger.warning("Entry order %s for %s: %s",
                                       order_result.order_id, ticker,
                                       status_result.status.value)
                        break
                except Exception as _poll_exc:
                    logger.warning("Entry fill poll error for %s: %s",
                                   ticker, _poll_exc)
                    break

        result = {
            "ticker": ticker, "side": side_label, "qty": qty, "price": _order_price,
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
            "direction": direction,
        }

        if order_result.success:
            self._daily_order_count += 1
            logger.info(
                "LIVE %s: %s %d x $%.2f → order_id=%s status=%s",
                side_label, ticker, qty, price, order_result.order_id,
                order_result.status.value,
            )
            # Only record to TradeLedger when the order is actually FILLED.
            # A LIMIT order accepted by the exchange returns success=True but
            # status=SUBMITTED — recording it now would create phantom
            # positions for orders that may never fill.
            if order_result.status == OrderStatus.FILLED:
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
                        "direction": direction,
                        "confidence": confidence,
                        "market_id": self.config.get("market_id", "sp500"),
                        "config_version": self.config.get("version"),
                        "regime_state": _regime_state,
                    })
                except Exception as _ledger_exc:
                    logger.warning("TradeLedger entry record failed (non-fatal): %s", _ledger_exc)
            else:
                logger.info(
                    "Order %s for %s accepted but not yet filled (status=%s) — "
                    "TradeLedger entry deferred to fill confirmation.",
                    order_result.order_id, ticker, order_result.status.value,
                )
        else:
            logger.error(
                "LIVE %s FAILED: %s — %s",
                side_label, ticker, order_result.message,
            )

        _journal_entry("live_entry", result)
        return result

    def _execute_exit(self, exit_rec: dict, trade_date: str) -> dict:
        """Execute a single exit order."""
        # Kill switch log (B6) — exits always proceed; this is informational only
        from brokers.kill_switch import is_halted
        if is_halted():
            logger.info("Kill switch active but exit proceeding (exits always allowed)")
        ticker = exit_rec.get("ticker", "")
        reason = exit_rec.get("reason", "signal_exit")

        # Get current regime for exit record
        try:
            _regime_state = _get_regime_model().classify_current().state.value
        except (RuntimeError, ValueError, OSError, KeyError, AttributeError):
            _regime_state = None

        # Direction: long = SELL to close, short = BUY to cover
        direction = exit_rec.get("direction", "long")

        # Cancel ALL open sell-side orders for this ticker (stops, trailing
        # stops, take-profits) before placing the exit.  The plan generator
        # doesn't always include stop_order_id, and there may be multiple
        # GTC orders (SL + TP) holding shares.  If we don't cancel them
        # Alpaca rejects the sell with "insufficient qty available".
        cancelled_count = self._cancel_open_orders_for_ticker(ticker)

        # Legacy path: also cancel the tracked stop if explicitly provided
        stop_order_id = exit_rec.get("stop_order_id", "")
        if stop_order_id:
            self.cancel_protective_stop(stop_order_id, ticker)
            cancelled_count += 1

        # Brief settle delay after cancelling protective orders.
        # Alpaca's cancel API is synchronous, but the internal state
        # change (releasing held shares) can take a moment to propagate.
        if cancelled_count > 0 and not self.is_dry_run:
            import time as _settle_time
            _settle_time.sleep(1.0)
            logger.info(
                "Settled 1s after cancelling %d protective order(s) for %s",
                cancelled_count, ticker,
            )

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
        except Exception as e:
            logger.debug("Exit entry date parsing failed (skipping holding_days): %s", e)

        exit_side = OrderSide.SELL
        exit_side_label = exit_side.value

        # Pre-flight check
        errors = preflight_check_order(
            ticker, exit_side, qty, price,
            self.safety, self._daily_order_count,
        )
        if errors:
            result = {
                "ticker": ticker, "side": exit_side_label, "qty": qty, "price": price,
                "success": False, "errors": errors, "dry_run": self.is_dry_run,
            }
            logger.warning("EXIT BLOCKED %s: %s", ticker, "; ".join(errors))
            _journal_entry("order_blocked", result)
            return result

        if self.is_dry_run:
            result = {
                "ticker": ticker, "side": exit_side_label, "qty": qty, "price": price,
                "direction": direction,
                "success": True, "dry_run": True, "reason": reason,
                "message": "DRY RUN — exit would be placed",
            }
            logger.info("DRY RUN %s: %s %d x $%.2f [%s]", exit_side_label, ticker, qty, price, reason)
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
        except Exception as e:
            logger.debug("Exit spread capture failed (non-blocking): %s", e)

        _submit_time = datetime.now().isoformat()
        order_result = self._broker.place_order(
            ticker=ticker,
            side=exit_side,
            qty=qty,
            price=_exit_price,
            order_type=_exit_order_type,
            remark=f"atlas_exit_{reason}_{trade_date}",
        )

        result = {
            "ticker": ticker, "side": exit_side_label, "qty": qty, "price": _exit_price,
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
                "LIVE %s: %s %d x $%.2f → order_id=%s [%s]",
                exit_side_label, ticker, qty, price, order_result.order_id, reason,
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
                    try:
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
                    except Exception as _poll_exc:
                        logger.warning("Exit fill poll error for %s: %s",
                                       ticker, _poll_exc)
                        break
                else:
                    logger.warning(
                        "Fill not confirmed for %s after %ds — "
                        "fill_price remains 0. Check order %s manually.",
                        ticker, _max_wait, order_result.order_id,
                    )

            # ── GUARD: Do NOT record exit if the order was not filled ──
            # A LIMIT sell that expires unfilled must not be written to the
            # trade ledger.  The reconcile_exit_fills path will pick it up
            # once the broker confirms the fill.  Recording with a fake
            # fill_price creates a phantom exit that desynchronises the
            # ledger from the broker (root cause of the 2026-04-04 MSI
            # discrepancy — order a685c1f4 ACCEPTED but never filled).
            _actual_fill = result.get("fill_price", 0) or 0
            if _actual_fill == 0:
                logger.warning(
                    "EXIT ORDER UNFILLED — %s order %s accepted but fill_price=0 "
                    "after polling. Skipping ledger/portfolio recording. "
                    "The deferred-fill reconciliation will record it once filled.",
                    ticker, order_result.order_id,
                )
                result["deferred_fill"] = True
                _journal_entry("exit_deferred_fill", result)
                return result

            # Track protective stop fill quality — telemetry, never blocks execution
            try:
                _stop_price = pos.stop_price if pos else 0
                if reason in ("stop_loss", "protective_stop") and _stop_price and _actual_fill > 0:
                    result["stop_expected_price"] = _stop_price
                    result["stop_fill_price"] = _actual_fill
                    result["stop_slippage_bps"] = round(
                        (_stop_price - _actual_fill) / _stop_price * 10000, 1
                    )
            except Exception as e:
                logger.debug("Stop slippage telemetry failed (non-blocking): %s", e)

            # Record exit to TradeLedger — only reached when fill is confirmed
            _fill_price = _actual_fill
            _pnl = round((_fill_price - entry_price) * qty, 2) if entry_price else None
            _pnl_pct = round((_fill_price - entry_price) / entry_price * 100, 2) if entry_price and entry_price > 0 else None
            _exit_record = {
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
                "direction": direction,
                "regime_at_exit": _regime_state,
            }
            try:
                from journal.logger import TradeLedger
                _ledger = TradeLedger()
                _ledger.record_exit(_exit_record)
            except Exception as _ledger_exc:
                logger.warning("TradeLedger exit record failed (non-fatal): %s", _ledger_exc)

            # Record to LivePortfolio closed_trades for dashboard display
            try:
                from brokers.live_portfolio import LivePortfolio
                _market_id = self.config.get("market_id", "sp500")
                _portfolio = LivePortfolio(self.config, market_id=_market_id)
                _closed_trade = {
                    "ticker": ticker,
                    "strategy": _exit_record.get("strategy", "unknown"),
                    "entry_price": _exit_record.get("entry_price", 0),
                    "exit_price": _exit_record.get("fill_price", 0),
                    "shares": _exit_record.get("shares", 0),
                    "pnl": _exit_record.get("pnl", 0),
                    "pnl_pct": _exit_record.get("pnl_pct", 0),
                    "holding_days": _exit_record.get("holding_days", 0),
                    "exit_reason": _exit_record.get("exit_reason", "unknown"),
                    "exit_date": trade_date,
                    "order_id": _exit_record.get("order_id", ""),
                }
                _portfolio.record_closed_trade(_closed_trade)
                logger.debug(
                    "Recorded exit to LivePortfolio: %s PnL=$%.2f",
                    ticker, _exit_record.get("pnl", 0),
                )
            except Exception as _portfolio_exc:
                logger.warning("LivePortfolio exit record failed (non-fatal): %s", _portfolio_exc)

            # Record round-trip trade for post-trade analysis
            try:
                from journal.round_trip import RoundTripStore
                _market_id = self.config.get("market_id", "sp500")
                RoundTripStore().build_and_record(
                    exit_data=_exit_record,
                    position=pos,
                    market_id=_market_id,
                )
            except Exception as _rt_exc:
                logger.warning("Round-trip record failed (non-fatal): %s", _rt_exc)
        else:
            logger.error(
                "LIVE %s FAILED: %s — %s", exit_side_label, ticker, order_result.message,
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
        direction: str = "long",
    ) -> Optional[str]:
        """Place a protective STOP or TRAILING_STOP on the exchange.

        For long positions: STOP SELL (triggers below entry).

        Called after an entry LIMIT order fills. Returns the stop order ID,
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
            direction: Trade direction (always "long").

        Returns:
            Order ID string if placed, None if failed or dry-run.
        """
        if not self._connected or not self._broker:
            logger.error("Cannot place protective stop — not connected")
            return None

        stop_side = OrderSide.SELL
        stop_side_label = stop_side.value

        use_trailing = trailing_atr > 0

        if use_trailing:
            order_type = OrderType.TRAILING_STOP
            log_label = f"TRAILING_STOP {stop_side_label} trail=${trailing_atr:.2f}"
        else:
            order_type = OrderType.STOP
            log_label = f"STOP {stop_side_label} trigger=${stop_price:.2f}"

        logger.info(
            "Placing protective stop: %s %s %d shares [%s]",
            ticker, log_label, qty, strategy,
        )

        if self.is_dry_run:
            _journal_entry("dry_run_protective_stop", {
                "ticker": ticker, "qty": qty, "stop_price": stop_price,
                "trailing_atr": trailing_atr, "order_type": order_type.value,
                "strategy": strategy, "direction": direction,
            })
            logger.info("DRY RUN: would place %s for %s", log_label, ticker)
            return None

        # Build order kwargs — all protective orders use GTC
        if use_trailing:
            # TRAILING_STOP: trail_price is the dollar trail distance (Alpaca)
            order_result = self._broker.place_order(
                ticker=ticker,
                side=stop_side,
                qty=qty,
                price=stop_price,  # reference/activation price
                order_type=order_type,
                remark=f"atlas_stop_{strategy}_{trade_date}"[:64],
                trail_price=trailing_atr,
                tif="gtc",
            )
        else:
            # Fixed STOP: aux_price is the trigger
            order_result = self._broker.place_order(
                ticker=ticker,
                side=stop_side,
                qty=qty,
                price=stop_price,  # limit price after trigger (= stop price for market-like fill)
                order_type=order_type,
                stop_price=stop_price,  # trigger price
                remark=f"atlas_stop_{strategy}_{trade_date}"[:64],
                tif="gtc",
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

    def _cancel_open_orders_for_ticker(self, ticker: str) -> int:
        """Cancel all open sell-side orders for *ticker* (stops, TPs, etc.).

        This ensures shares are not "held_for_orders" when we need to
        place an exit order.  Returns the number of orders cancelled.
        """
        if not self._connected or not self._broker:
            return 0

        try:
            open_orders = self._broker.get_open_orders()
        except Exception as e:
            logger.warning(
                "Could not fetch open orders for %s cleanup: %s", ticker, e,
            )
            return 0

        cancelled = 0
        for order in open_orders:
            if getattr(order, "ticker", "") != ticker:
                continue
            # Cancel any sell-side order (STOP, TRAILING_STOP, LIMIT SELL, etc.)
            side_val = getattr(order, "side", None)
            if hasattr(side_val, "value"):
                side_val = side_val.value
            if str(side_val).upper() not in ("SELL",):
                continue
            oid = getattr(order, "order_id", "")
            if not oid:
                continue
            result = self._broker.cancel_order(oid)
            if result.success:
                cancelled += 1
                logger.info(
                    "Pre-exit cancel: cancelled %s order %s for %s",
                    getattr(order, "order_type", "?"), oid[:12], ticker,
                )
            else:
                logger.warning(
                    "Pre-exit cancel failed for %s order %s: %s",
                    ticker, oid[:12], result.message,
                )
        if cancelled:
            _journal_entry("pre_exit_orders_cancelled", {
                "ticker": ticker, "count": cancelled,
            })
        return cancelled

    def place_take_profit(
        self,
        ticker: str,
        qty: int,
        take_profit: float,
        strategy: str = "",
        trade_date: str = "",
        direction: str = "long",
    ) -> Optional[str]:
        """Place a take-profit LIMIT SELL GTC order on the exchange.

        Args:
            ticker: Position ticker.
            qty: Number of shares.
            take_profit: Target limit price.
            strategy: Strategy name (for remark/journal).
            trade_date: For remark/journal.
            direction: Trade direction (always "long").

        Returns:
            Order ID string if placed, None if failed or dry-run.
        """
        if not self._connected or not self._broker:
            logger.error("Cannot place take-profit — not connected")
            return None

        tp_side = OrderSide.SELL

        logger.info(
            "Placing take-profit: %s LIMIT %s %d shares @ $%.2f (GTC) [%s]",
            ticker, tp_side.value, qty, take_profit, strategy,
        )

        if self.is_dry_run:
            _journal_entry("dry_run_take_profit", {
                "ticker": ticker, "qty": qty, "take_profit": take_profit,
                "strategy": strategy, "direction": direction,
            })
            logger.info("DRY RUN: would place TP LIMIT %s %s @ $%.2f",
                        tp_side.value, ticker, take_profit)
            return None

        tp_result = self._broker.place_order(
            ticker=ticker,
            side=tp_side,
            qty=qty,
            price=round(take_profit, 2),
            order_type=OrderType.LIMIT,
            remark=f"atlas_tp_{strategy}_{trade_date}"[:64],
            tif="gtc",
        )

        if not tp_result.success:
            logger.error(
                "Take-profit FAILED for %s: %s", ticker, tp_result.message,
            )
            _journal_entry("take_profit_failed", {
                "ticker": ticker, "error": tp_result.message,
            })
            return None

        order_id = tp_result.order_id
        logger.info(
            "Take-profit placed: %s LIMIT %s @ $%.2f → order_id=%s",
            ticker, tp_side.value, take_profit, order_id,
        )
        _journal_entry("take_profit_placed", {
            "ticker": ticker, "order_id": order_id,
            "take_profit": take_profit, "strategy": strategy,
        })
        return order_id

    def place_stops_for_plan(
        self,
        plan: dict,
        entry_results: list[dict],
        config: dict,
        trade_date: str,
    ) -> dict[str, str]:
        """Place protective orders for all successfully filled entries.

        Called after execute_plan(). For each filled entry:
          - If strategy provides take_profit: fixed SL (GTC) + TP limit (GTC)
          - If no take_profit: trailing stop (GTC) — combined SL + profit capture

        Returns:
            Dict of ticker → order_id for successfully placed protective orders.
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
            take_profit = entry_rec.get("take_profit")
            direction = entry_rec.get("direction", "long")

            if not ticker or not qty or not stop_price:
                continue

            # Skip stop placement if the entry order is still pending (not filled).
            # Placing a STOP SELL while a LIMIT BUY is open causes a "wash trade"
            # rejection on Alpaca.  sync_protective_orders.py handles post-fill stops.
            entry_status = result.get("status", "").upper()
            if entry_status in ("SUBMITTED", "NEW", "ACCEPTED", "PENDING_NEW",
                                "PENDING", "PARTIALLY_FILLED"):
                logger.info(
                    "Skipping immediate protective orders for %s (order status=%s) — "
                    "sync_protective_orders will place after fill.",
                    ticker, entry_status,
                )
                continue

            has_tp = take_profit is not None and float(take_profit) > 0

            if has_tp:
                # ── Strategy has TP: fixed SL (GTC) + TP limit (GTC) ──
                # Place fixed SL
                sl_id = self.place_protective_stop(
                    ticker=ticker,
                    qty=qty,
                    stop_price=stop_price,
                    strategy=strategy,
                    trailing_atr=0.0,  # fixed stop, not trailing
                    trade_date=trade_date,
                    direction=direction,
                )
                if sl_id:
                    stop_orders[ticker] = sl_id

                # Place TP limit order
                tp_id = self.place_take_profit(
                    ticker=ticker,
                    qty=qty,
                    take_profit=float(take_profit),
                    strategy=strategy,
                    trade_date=trade_date,
                    direction=direction,
                )
                if tp_id:
                    stop_orders[f"{ticker}_tp"] = tp_id

            else:
                # ── No TP: trailing stop (GTC) — combined SL + profit capture ──
                # Trail distance = entry - stop (same initial risk).
                # Use strategy trailing_stop_atr_mult × ATR if configured,
                # otherwise fall back to the plan's stop distance.
                strat_cfg = config.get("strategies", {}).get(strategy, {})
                trailing_mult = strat_cfg.get("trailing_stop_atr_mult", 0)
                atr_value = entry_rec.get("features", {}).get("atr", 0)

                if trailing_mult > 0 and atr_value > 0:
                    trailing_atr = round(trailing_mult * atr_value, 4)
                else:
                    # Default: use the plan's stop distance as trail
                    entry_price = entry_rec.get("entry_price", 0)
                    trailing_atr = round(abs(entry_price - stop_price), 2)
                    if trailing_atr <= 0:
                        trailing_atr = round(entry_price * 0.05, 2)

                logger.info(
                    "No TP for %s — placing trailing stop with trail=$%.2f",
                    ticker, trailing_atr,
                )

                order_id = self.place_protective_stop(
                    ticker=ticker,
                    qty=qty,
                    stop_price=stop_price,
                    strategy=strategy,
                    trailing_atr=trailing_atr,
                    trade_date=trade_date,
                    direction=direction,
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
        _health_log("critical", f"EMERGENCY HALT: {reason}")
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

    # WARNING: Do not add to cron without verifying noon-ET cutoff against
    # local timezone (AEST).  See 2026-04-10 incident where timezone
    # mismatch caused premature cancellation of valid entry orders.
    def cancel_unfilled_limits(self, cutoff_hour: int = 12) -> list:
        """Cancel unfilled limit BUY orders after cutoff hour (default noon ET).

        Called by a midday cron job to avoid stale open-limit orders sitting
        on the exchange through the close.  Only BUY limit orders are cancelled;
        protective STOP/TRAILING_STOP SELL orders are left untouched.

        **Safety:** A hard floor of noon ET (hour 12) is enforced at runtime.
        If cutoff_hour < 12, it is clamped to 12 and a warning is logged.

        Args:
            cutoff_hour: Hour in ET (0-23) after which unfilled limits are
                         cancelled.  Default is 12 (noon ET).  Cannot be
                         set below 12 (clamped with warning).

        Returns:
            List of cancellation result dicts, one per attempted cancel.
        """
        if not self._connected or not self._broker:
            logger.warning("cancel_unfilled_limits: not connected")
            return []

        # Hard floor: never cancel before noon ET regardless of caller arg
        if cutoff_hour < 12:
            logger.warning(
                "cancel_unfilled_limits: cutoff_hour=%d is below safety floor 12 "
                "— clamping to 12.  See 2026-04-10 incident.",
                cutoff_hour,
            )
            cutoff_hour = 12

        # Determine current ET time without requiring pytz
        from zoneinfo import ZoneInfo
        _now_et = datetime.now(tz=ZoneInfo("America/New_York"))
        current_et_hour = _now_et.hour

        if current_et_hour < cutoff_hour:
            logger.warning(
                "cancel_unfilled_limits: BLOCKED — ET hour %d < cutoff %d. "
                "No orders will be cancelled.",
                current_et_hour, cutoff_hour,
            )
            return []

        logger.info(
            "cancel_unfilled_limits: PROCEEDING — ET hour %d >= cutoff %d",
            current_et_hour, cutoff_hour,
        )

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

    # ── Fill reconciliation ───────────────────────────────────

    def reconcile_entry_fills(self, plan: dict | None = None) -> list:
        """Reconcile pending entry orders — record fills missed at submission.

        Checks the broker for the status of entry orders that were accepted
        but not yet filled at execution time.  For each order that is now
        FILLED, a TradeLedger entry is recorded.

        Safe to call repeatedly — already-recorded orders are skipped.

        Args:
            plan: Optional trade plan dict (provides stop_price/strategy
                  context).  If None, entries are recorded with available
                  broker data only.

        Returns:
            List of dicts describing each reconciled order.
        """
        if not self._connected or not self._broker:
            logger.warning("reconcile_entry_fills: not connected")
            return []

        # Build lookup from plan entries keyed by ticker (approximate)
        plan_by_ticker: dict = {}
        if plan:
            for entry in plan.get("proposed_entries", []):
                t = entry.get("ticker", "")
                if t:
                    plan_by_ticker[t] = entry

        # Get current regime for enrichment
        try:
            _recon_regime = _get_regime_model().classify_current().state.value
        except (RuntimeError, ValueError, OSError, KeyError, AttributeError):
            _recon_regime = None

        # Load existing ledger order IDs to skip duplicates
        try:
            from journal.logger import TradeLedger
            _ledger = TradeLedger()
        except Exception as e:
            logger.error("reconcile_entry_fills: cannot load TradeLedger: %s", e)
            return []

        recorded_order_ids = {
            t.get("order_id") for t in _ledger.trades if t.get("order_id")
        }

        # Get recent CLOSED orders from broker — use CLOSED status with
        # explicit lookback window.  The previous QueryOrderStatus.ALL
        # without an `after` param only returned today's orders, silently
        # missing fills from prior days.
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from datetime import timezone, timedelta
            client = self._broker._trade_client
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=datetime.now(tz=timezone.utc) - timedelta(days=7),
                limit=200,
            )
            orders = self._broker._broker_call(self._broker._trade_client.get_orders, filter=req)
        except Exception as e:
            logger.error("reconcile_entry_fills: cannot fetch orders: %s", e)
            return []

        reconciled = []
        for order in orders:
            order_id = str(order.id)
            if order_id in recorded_order_ids:
                continue  # already in ledger

            # Only reconcile BUY LIMIT/MARKET orders that are now FILLED
            if order.side.value.lower() != "buy":
                continue
            status_val = order.status.value.lower() if hasattr(order.status, 'value') else str(order.status).lower()
            if status_val != "filled":
                continue

            # Skip protective orders — only reconcile entry fills.
            # Intent convention: atlas_entry_* = entry, atlas_stop_*/atlas_tp_* = protective
            client_order_id = str(getattr(order, "client_order_id", ""))
            if "atlas_stop_" in client_order_id or "atlas_tp_" in client_order_id:
                continue

            ticker = str(order.symbol)
            fill_price = float(order.filled_avg_price or 0)
            qty = int(float(order.filled_qty or order.qty or 0))

            if fill_price <= 0 or qty <= 0:
                continue

            # Get plan context
            plan_entry = plan_by_ticker.get(ticker, {})
            strategy = plan_entry.get("strategy") or "reconciled"
            if strategy == "unknown":
                strategy = "reconciled"
            stop_price = plan_entry.get("stop_price", 0)
            planned_price = plan_entry.get("entry_price", 0)

            # Guard: skip if stop_price=0 — would create a ghost row with no real stop
            if stop_price <= 0:
                logger.warning(
                    "reconcile_entry_fills: skipping %s — stop_price=0 (not in plan). "
                    "Run sync_protective_orders to place stop first.",
                    ticker,
                )
                continue

            # Dedup guard vs SQLite: check if this ticker already has an open row.
            # The JSON ledger only deduplicates by order_id; SQLite dedup by ticker
            # prevents cross-market ghost rows when fills from other-market orders
            # are returned by the broker's 7-day history scan.
            try:
                from db import atlas_db as _adb
                with _adb.get_db() as _chk_db:
                    _sqlite_open = _chk_db.execute(
                        "SELECT id, strategy FROM trades WHERE status='open' AND ticker=? LIMIT 1",
                        (ticker,),
                    ).fetchone()
                if _sqlite_open:
                    _existing_id = _sqlite_open["id"]
                    _existing_strat = _sqlite_open["strategy"]
                    if _existing_strat in ("unknown", "reconciled", "") and strategy not in ("unknown", "reconciled", ""):
                        with _adb.get_db() as _upd_db:
                            _upd_db.execute(
                                "UPDATE trades SET strategy=?, stop_price=? WHERE id=?",
                                (strategy, stop_price, _existing_id),
                            )
                        logger.info(
                            "reconcile_entry_fills: dedup_guard upgraded id=%d %s strategy %s→%s",
                            _existing_id, ticker, _existing_strat, strategy,
                        )
                    else:
                        logger.debug(
                            "reconcile_entry_fills: dedup_guard: %s already open id=%d, skipping INSERT",
                            ticker, _existing_id,
                        )
                    continue
            except Exception as _dedup_exc:
                logger.warning(
                    "reconcile_entry_fills: SQLite dedup check failed for %s (non-fatal): %s",
                    ticker, _dedup_exc,
                )

            # Record to TradeLedger
            ledger_record = {
                "ticker": ticker,
                "strategy": strategy,
                "shares": qty,
                "fill_price": fill_price,
                "planned_price": planned_price,
                "stop_price": stop_price,
                "slippage_bps": round(
                    (fill_price - planned_price) / planned_price * 10000, 1
                ) if planned_price > 0 else None,
                "order_id": order_id,
                "timestamp": str(getattr(order, "filled_at", ""))[:19],
                "direction": "long",
                "reconciled": True,  # flag to distinguish from inline records
                "confidence": plan_entry.get("confidence", 0),
                "market_id": self.config.get("market_id", "sp500"),
                "config_version": self.config.get("version"),
                "regime_state": _recon_regime,
            }
            try:
                _ledger.record_entry(ledger_record)
                logger.info(
                    "Reconciled fill: BUY %s %d @ $%.2f (order %s)",
                    ticker, qty, fill_price, order_id[:12],
                )
                reconciled.append(ledger_record)
            except Exception as e:
                logger.error(
                    "Failed to reconcile fill for %s: %s", ticker, e,
                )

        if reconciled:
            logger.info(
                "reconcile_entry_fills: recorded %d deferred fills",
                len(reconciled),
            )
        else:
            logger.info("reconcile_entry_fills: no deferred fills to reconcile")

        return reconciled

    def reconcile_exit_fills(self) -> list:
        """Reconcile filled SELL orders not recorded in the trade ledger.

        Catches:
          - Trailing stop fills (protective orders that filled during market hours)
          - Plan-based exits submitted pre-market as LIMIT orders
          - Any other sell that filled but wasn't captured at submission time

        Safe to call repeatedly — already-recorded orders are skipped.

        Returns:
            List of dicts describing each reconciled exit.
        """
        if not self._connected or not self._broker:
            logger.warning("reconcile_exit_fills: not connected")
            return []

        try:
            from journal.logger import TradeLedger
            _ledger = TradeLedger()
        except Exception as e:
            logger.error("reconcile_exit_fills: cannot load TradeLedger: %s", e)
            return []

        # Existing exit order IDs
        exit_order_ids = {
            t.get("order_id") for t in _ledger.trades
            if t.get("type") == "exit" and t.get("order_id")
        }

        # Build entry lookup for PnL calculation
        entry_by_ticker: dict = {}
        for t in _ledger.trades:
            if t.get("type") == "entry":
                entry_by_ticker[t["ticker"]] = t

        # Fetch closed orders from last 7 days
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            from datetime import timezone, timedelta
            client = self._broker._trade_client
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=datetime.now(tz=timezone.utc) - timedelta(days=7),
                limit=200,
            )
            orders = self._broker._broker_call(self._broker._trade_client.get_orders, filter=req)
        except Exception as e:
            logger.error("reconcile_exit_fills: cannot fetch orders: %s", e)
            return []

        # Get current regime for exit record enrichment
        try:
            _recon_exit_regime = _get_regime_model().classify_current().state.value
        except (RuntimeError, ValueError, OSError, KeyError, AttributeError):
            _recon_exit_regime = None

        reconciled = []
        for order in orders:
            order_id = str(order.id)
            if order_id in exit_order_ids:
                continue

            # Only SELL orders that are FILLED
            if order.side.value.lower() != "sell":
                continue
            status_val = (
                order.status.value.lower()
                if hasattr(order.status, "value")
                else str(order.status).lower()
            )
            if status_val != "filled":
                continue

            ticker = str(order.symbol)
            fill_price = float(order.filled_avg_price or 0)
            qty = int(float(order.filled_qty or order.qty or 0))
            if fill_price <= 0 or qty <= 0:
                continue

            # Only reconcile Atlas-originated orders
            coid = str(getattr(order, "client_order_id", ""))
            if not coid.startswith("atlas_"):
                continue

            # Determine exit reason from client_order_id
            if "trail" in coid:
                reason = "trailing_stop_fill"
            elif "exit" in coid:
                reason = "signal_exit"
            elif "sl" in coid or "stop" in coid:
                reason = "stop_loss"
            else:
                reason = "broker_fill"

            # Get entry context
            entry = entry_by_ticker.get(ticker, {})
            entry_price = entry.get("fill_price", 0)
            strategy = entry.get("strategy", "unknown")
            pnl = (
                round((fill_price - entry_price) * qty, 2)
                if entry_price
                else None
            )
            pnl_pct = (
                round((fill_price - entry_price) / entry_price * 100, 2)
                if entry_price and entry_price > 0
                else None
            )

            exit_record = {
                "ticker": ticker,
                "strategy": strategy,
                "shares": qty,
                "fill_price": fill_price,
                "entry_price": entry_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "exit_reason": reason,
                "order_id": order_id,
                "timestamp": str(getattr(order, "filled_at", ""))[:26],
                "reconciled": True,
                "regime_at_exit": _recon_exit_regime,
            }
            try:
                _ledger.record_exit(exit_record)
                logger.info(
                    "Reconciled exit: SELL %s %d @ $%.2f PnL=$%s (order %s)",
                    ticker, qty, fill_price,
                    f"{pnl:+.2f}" if pnl is not None else "?",
                    order_id[:12],
                )
                reconciled.append(exit_record)
            except Exception as e:
                logger.error(
                    "Failed to reconcile exit for %s: %s", ticker, e,
                )

            # Also record to LivePortfolio.closed_trades (broker state JSON)
            try:
                from brokers.live_portfolio import LivePortfolio
                _market_id = self.config.get("market_id", "sp500")
                _portfolio = LivePortfolio(self.config, market_id=_market_id)
                _closed_trade = {
                    "ticker": ticker,
                    "strategy": strategy,
                    "entry_price": entry_price,
                    "exit_price": fill_price,
                    "shares": qty,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "holding_days": entry.get("holding_days"),
                    "exit_reason": reason,
                    "exit_date": str(getattr(order, "filled_at", ""))[:10],
                    "order_id": order_id,
                    "reconciled": True,
                }
                _portfolio.record_closed_trade(_closed_trade)
                logger.debug("Recorded reconciled exit to LivePortfolio: %s", ticker)
            except Exception as _port_exc:
                logger.error(
                    "Failed to record reconciled exit to LivePortfolio for %s: %s",
                    ticker, _port_exc, exc_info=True,
                )

        if reconciled:
            logger.info(
                "reconcile_exit_fills: recorded %d deferred exit fills",
                len(reconciled),
            )
        else:
            logger.info("reconcile_exit_fills: no deferred exits to reconcile")

        return reconciled

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
        _health_log("error", f"Execution blocked: {message}")
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
