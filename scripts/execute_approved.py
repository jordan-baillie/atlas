#!/usr/bin/env python3
"""Execute today's APPROVED trade plan via the live broker.

Called by cron at 23:15 AEST (15 min before US market open) so that
LIMIT orders are submitted with fresh pre-market prices rather than
sitting on the exchange for hours.

If no approved plan exists for today, exits cleanly (not an error).

Usage:
    python3 scripts/execute_approved.py --market sp500 [--dry-run]
"""
import sys
import os
import argparse
import logging
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
os.chdir(PROJECT)

from utils.logging_config import setup_logging
log = setup_logging("execute_approved", extra_log_file="execute_approved")


def main():
    parser = argparse.ArgumentParser(description="Execute approved plan")
    parser.add_argument("-m", "--market", default="sp500")
    parser.add_argument("--date", default=None, help="Trade date (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no orders")
    args = parser.parse_args()

    market_id = args.market
    trade_date = args.date or datetime.now().strftime("%Y-%m-%d")

    log.info("Execute approved plan: market=%s date=%s dry_run=%s",
             market_id, trade_date, args.dry_run)

    # ── Load config and plan ─────────────────────────────────
    from utils.config import get_active_config
    config = get_active_config(market_id)

    mode = config.get("trading", {}).get("mode", "")
    if mode != "live":
        log.info("Trading mode is '%s', not 'live' — skipping", mode)
        return

    from brokers.plan import TradePlanGenerator
    plan_gen = TradePlanGenerator(None, config)
    plan = plan_gen.load_plan(trade_date, market_id=market_id)

    if not plan:
        log.info("No plan found for %s — nothing to execute", trade_date)
        return

    status = plan.get("status", "")
    auto_approve = config.get("trading", {}).get("auto_approve", False)
    if status != "APPROVED":
        if auto_approve and status in ("", "PENDING", "PENDING_APPROVAL", "GENERATED", "DRAFT"):
            _n_entries = len(plan.get("proposed_entries", []))
            _n_exits = len(plan.get("proposed_exits", []))
            log.warning(
                "AUTO_APPROVE: trade_date=%s market=%s n_entries=%d n_exits=%d "
                "reason=config.trading.auto_approve=true",
                trade_date, market_id, _n_entries, _n_exits,
            )
            plan = plan_gen.approve_plan(
                trade_date, market_id=market_id, auto=True, approver="auto"
            )
            if not plan:
                log.error("auto_approve: approve_plan() returned None — aborting")
                return
            # Annotate with auto-approval metadata
            plan["auto_approved"] = True
            plan["approval_source"] = "auto_approve_config_flag"
            status = plan.get("status", "")
            # Best-effort Telegram notification
            _notify_auto_approve(market_id, trade_date, _n_entries, _n_exits)
        if status != "APPROVED":
            log.info("Plan status is '%s' (need APPROVED) — skipping", status)
            return

    entries = plan.get("proposed_entries", [])
    exits = plan.get("proposed_exits", [])
    log.info("Plan has %d entries, %d exits", len(entries), len(exits))

    if not entries and not exits:
        log.info("Empty plan — nothing to execute")
        return

    # ── Apply overlay sizing_override + avoid_tickers (#215) ────────────
    overlay_ctx = plan.get("overlay_context") or {}
    # plan.py writes the field as "tickers_to_avoid"; also handle legacy "avoid_tickers"
    _avoid_raw = (
        overlay_ctx.get("tickers_to_avoid")
        or overlay_ctx.get("avoid_tickers")
        or []
    )
    overlay_avoid = set(_avoid_raw)
    overlay_sizing = overlay_ctx.get("sizing_override")
    if overlay_sizing is not None:
        overlay_sizing = float(overlay_sizing)

    if overlay_ctx:
        log.info(
            "overlay_context active: action=%s sizing_override=%s avoid=%s",
            overlay_ctx.get("action", "no_change"),
            overlay_sizing,
            sorted(overlay_avoid),
        )

    filtered_entries: list = []
    for _entry in entries:
        _ticker = _entry.get("ticker", "")

        # Skip tickers the overlay wants to avoid
        if _ticker in overlay_avoid:
            log.info(
                "overlay_applied: ticker=%s action=skip reason=avoid_tickers "
                "avoided=%s",
                _ticker, sorted(overlay_avoid),
            )
            continue

        # Apply sizing_override multiplier to position_size
        if overlay_sizing is not None:
            _orig_qty = _entry.get("position_size", 0)
            _new_qty = int(_orig_qty * overlay_sizing)
            if _new_qty <= 0:
                log.info(
                    "overlay_applied: ticker=%s sizing=%s qty→0 — skipping "
                    "avoided=%s",
                    _ticker, overlay_sizing, sorted(overlay_avoid),
                )
                continue
            _entry = dict(_entry)   # shallow copy — do not mutate original plan
            _entry["position_size"] = _new_qty
            log.info(
                "overlay_applied: ticker=%s sizing=%s qty=%d→%d avoided=%s",
                _ticker, overlay_sizing, _orig_qty, _new_qty, sorted(overlay_avoid),
            )

        filtered_entries.append(_entry)

    if len(filtered_entries) != len(entries):
        log.info(
            "overlay: %d/%d entries proceeding after overlay filter (dropped %d)",
            len(filtered_entries), len(entries),
            len(entries) - len(filtered_entries),
        )
        plan = dict(plan)   # shallow copy — do not mutate stored plan
        plan["proposed_entries"] = filtered_entries
        entries = filtered_entries

    # ── Execute via LiveExecutor ─────────────────────────────
    from brokers.live_executor import LiveExecutor

    executor = LiveExecutor(config)
    if args.dry_run:
        executor.is_dry_run = True

    if not executor.connect():
        log.error("Failed to connect to broker — aborting")
        _notify_error(market_id, trade_date, "Broker connection failed")
        return

    try:
        report = executor.execute_plan(plan, trade_date)

        # ── Update plan status ───────────────────────────────
        if not args.dry_run:
            plan["status"] = "EXECUTED"
            plan["executed_at"] = datetime.now().isoformat()
            plan["execution_report"] = {
                "successful_entries": report.get("successful_entries", 0),
                "successful_exits": report.get("successful_exits", 0),
                "total_entries": report.get("total_entries", 0),
                "total_exits": report.get("total_exits", 0),
            }
            plan_gen._save_plan(plan, trade_date)

        # ── Summary ──────────────────────────────────────────
        ok_entries = report.get("successful_entries", 0)
        ok_exits = report.get("successful_exits", 0)
        total_entries = report.get("total_entries", 0)
        total_exits = report.get("total_exits", 0)

        log.info(
            "Execution complete: entries=%d/%d exits=%d/%d dry_run=%s",
            ok_entries, total_entries, ok_exits, total_exits, args.dry_run,
        )

        # ── Telegram notification ────────────────────────────
        if not args.dry_run:
            _notify_execution(market_id, trade_date, report)

    except Exception as e:
        log.error("Execution failed: %s", e, exc_info=True)
        _notify_error(market_id, trade_date, str(e))
    finally:
        executor.disconnect()


def _notify_execution(market_id: str, trade_date: str, report: dict):
    """Send Telegram summary of executed orders."""
    try:
        from utils.telegram import send_message, tg_escape as _tge
        ok_e = report.get("successful_entries", 0)
        tot_e = report.get("total_entries", 0)
        ok_x = report.get("successful_exits", 0)
        tot_x = report.get("total_exits", 0)

        lines = [
            f"🚀 <b>Orders Submitted</b> ({market_id.upper()} {trade_date})",
            f"  Entries: {ok_e}/{tot_e} | Exits: {ok_x}/{tot_x}",
            "",
        ]

        # Entry details
        for e in report.get("entries", []):
            ticker = e.get("ticker", "?")
            status = e.get("status", "?")
            price = e.get("price", 0)
            qty = e.get("qty", 0)
            emoji = "✅" if e.get("success") else "❌"
            lines.append(f"  {emoji} BUY {_tge(ticker)} {qty}x @ ${price:.2f} [{_tge(status)}]")

        vol_gate = report.get("volatility_gate", {})
        if vol_gate.get("action") not in (None, "none"):
            lines.append(f"\n⚠️ Vol gate: {_tge(vol_gate.get('action', ''))} — {_tge(vol_gate.get('message', ''))}")

        lines.append("\n⏰ Market opens in ~15 min. Fills will be reconciled at sync.")
        send_message("\n".join(lines))
    except Exception as e:
        log.warning("Telegram notification failed (non-fatal): %s", e)


def _notify_auto_approve(
    market_id: str,
    trade_date: str,
    n_entries: int,
    n_exits: int,
) -> None:
    """Send Telegram notification when a plan is auto-approved (best-effort)."""
    try:
        from utils.telegram import send_message, tg_escape as _tge
        lines = [
            "\U0001F916 <b>AUTO-APPROVED</b> " + _tge(market_id.upper())
            + " plan for " + _tge(trade_date),
            "Entries: " + str(n_entries) + "  Exits: " + str(n_exits),
            "Execution starting now...",
        ]
        send_message("\n".join(lines))
    except Exception as e:
        log.warning("Auto-approve Telegram notification failed (non-fatal): %s", e)

def _notify_error(market_id: str, trade_date: str, error: str):
    """Send Telegram error alert."""
    try:
        from utils.telegram import send_message, tg_escape as _tge
        send_message(
            f"❌ <b>Execution Failed</b> ({market_id.upper()} {trade_date})\n\n"
            f"<pre>{_tge(error[:500])}</pre>\n\n"
            f"Manual intervention required."
        )
    except Exception as e:
        log.warning(f"Error-notification Telegram send failed: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Top-level crash guard — alert via Telegram so cron failures aren't silent
        try:
            from utils.telegram import send_message, tg_escape as _tge
            send_message(
                f"🚨 <b>execute_approved CRASHED</b>\n\n"
                f"<pre>{_tge(type(exc).__name__)}: {_tge(str(exc)[:500])}</pre>\n\n"
                f"Check logs/execute_approved.log"
            )
        except Exception as e:
            log.warning(f"Crash-alert Telegram notification failed: {e}")
        raise
