#!/usr/bin/env python3
"""Execute today's APPROVED trade plan via the live broker.

Called by cron at 23:15 AEST (15 min before US market open) so that
LIMIT orders are submitted with fresh pre-market prices rather than
sitting on the exchange for hours.

If no approved plan exists for today, exits cleanly (not an error).

Usage:
    python3 scripts/execute_approved.py --market sp500 [--dry-run]

Per-strategy routing (Phase B):
    When universe mode is "live", strategies in PAPER lifecycle state are
    routed to the Alpaca paper broker automatically — no universe-level config
    change needed.  Universe mode "passive" skips entirely.  Universe mode
    "paper" sends ALL strategies to the paper broker regardless of lifecycle.
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

from brokers.routing_policy import BrokerRoutingPolicy


def _is_market_halted(market_id: str) -> tuple[bool, str, str]:
    """Query market_state.halted for *market_id*.

    Returns (halted, reason, halted_at).  If the DB query fails, returns
    (False, "", "") — fail-open so a DB hiccup doesn't permanently block
    order execution (the kill_switch HALT file is the hard gate for that).

    Uses db.atlas_db.get_db() so the test-isolation fixture (_db_path_override)
    is respected during automated tests.
    """
    try:
        from db.atlas_db import get_db
        with get_db() as _db:
            _row = _db.execute(
                "SELECT halted, halt_reason, halted_at FROM market_state "
                "WHERE market_id = ?",
                (market_id,),
            ).fetchone()
        if _row and int(_row[0]) == 1:
            return True, _row[1] or "unknown", _row[2] or "unknown"
        return False, "", ""
    except Exception as _e:
        log.warning("Halt state DB query failed (fail-open): %s", _e)
        return False, "", ""


def _run_executor(
    config: dict,
    plan: dict,
    entries: list,
    exits: list,
    market_id: str,
    trade_date: str,
    dry_run: bool,
    label: str,
) -> dict | None:
    """Create a LiveExecutor with *config* and execute *entries* / *exits*.

    A shallow-copy of *plan* is used so the caller's plan dict is not mutated.
    Returns the execution report dict, or None on connection failure / crash.

    Args:
        config:     Active config dict (with ``trading.mode`` already set to
                    "live" or "paper" as appropriate).
        plan:       Full plan dict (status must already be "APPROVED").
        entries:    Subset of proposed_entries to execute.
        exits:      Subset of proposed_exits to execute.
        market_id:  Universe / market id string.
        trade_date: YYYY-MM-DD string.
        dry_run:    If True, no real orders are submitted.
        label:      Log prefix: "[live]" or "[paper]".

    Returns:
        Execution report dict or None.
    """
    if not entries and not exits:
        log.info("%s No entries or exits — skipping executor", label)
        return None

    from brokers.live_executor import LiveExecutor

    # Build a plan copy restricted to this subset of entries/exits
    sub_plan = dict(plan)
    sub_plan["proposed_entries"] = entries
    sub_plan["proposed_exits"] = exits

    mode_val = config.get("trading", {}).get("mode", "?")
    executor = LiveExecutor(config)
    if dry_run:
        executor.is_dry_run = True

    if not executor.connect():
        log.error("%s Failed to connect to broker (mode=%s) — skipping", label, mode_val)
        _notify_error(market_id, trade_date, f"{label} Broker connection failed")
        return None

    try:
        log.info(
            "%s Executing: mode=%s entries=%d exits=%d dry_run=%s",
            label, mode_val, len(entries), len(exits), dry_run,
        )
        report = executor.execute_plan(sub_plan, trade_date)

        ok_entries = report.get("successful_entries", 0)
        ok_exits = report.get("successful_exits", 0)
        total_entries = report.get("total_entries", 0)
        total_exits = report.get("total_exits", 0)
        log.info(
            "%s Execution complete: entries=%d/%d exits=%d/%d dry_run=%s",
            label, ok_entries, total_entries, ok_exits, total_exits, dry_run,
        )
        return report

    except Exception as e:
        log.error("%s Execution failed: %s", label, e, exc_info=True)
        _notify_error(market_id, trade_date, f"{label} {e}")
        return None
    finally:
        executor.disconnect()


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
    policy = BrokerRoutingPolicy(config, market_id=market_id)
    if policy.should_skip():
        log.info("policy.should_skip() True (mode=%s, live_enabled=%s) — skipping",
                 policy.mode, policy.live_enabled)
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

    # ── Halt gate: check market_state.halted before submitting any orders ──
    _halted, _halt_reason, _halted_at = _is_market_halted(market_id)
    if _halted:
        log.error(
            "EXECUTE_APPROVED ABORTED: market %s is halted "
            "(reason=%s, halted_at=%s) — no orders submitted",
            market_id, _halt_reason, _halted_at,
        )
        try:
            from utils.telegram import send_message, tg_escape as _tge
            send_message(
                "⛔ <b>EXECUTE_APPROVED ABORTED</b>: market "
                + "<b>" + _tge(market_id) + "</b> is halted\n"
                + "Reason: " + _tge(_halt_reason) + "\n"
                + "Halted at: " + _tge(_halted_at) + "\n\n"
                + "No orders submitted. Resume trading when safe."
            )
        except Exception as _tg_exc:
            log.warning("Halt-abort Telegram notification failed: %s", _tg_exc)
        sys.exit(2)

    # ── Route by lifecycle state (per-strategy paper vs live) ────────────
    #
    # mode == "passive" → bailed out earlier
    # mode == "paper"   → whole universe in paper mode; all strategies → paper
    # mode == "live"    → split by lifecycle state:
    #                        PAPER-state strategies → paper executor
    #                        all others             → live executor
    #
    live_report: dict | None = None
    paper_report: dict | None = None

    if policy.is_paper:
        # Entire universe is in paper mode — route everything to paper executor
        log.info("[paper] Universe mode=paper — routing all %d entries, %d exits to paper",
                 len(entries), len(exits))
        paper_report = _run_executor(
            config, plan, entries, exits,
            market_id, trade_date, args.dry_run, "[paper]",
        )

    else:
        # mode == "live" — split by per-strategy lifecycle state
        live_entries, paper_entries = policy.split_entries_by_lifecycle(entries)
        live_exits, paper_exits = policy.split_entries_by_lifecycle(exits)

        if paper_entries or paper_exits:
            log.info(
                "lifecycle_split: live_entries=%d paper_entries=%d "
                "live_exits=%d paper_exits=%d",
                len(live_entries), len(paper_entries),
                len(live_exits), len(paper_exits),
            )

        if live_entries or live_exits:
            live_report = _run_executor(
                config, plan, live_entries, live_exits,
                market_id, trade_date, args.dry_run, "[live]",
            )

        if paper_entries or paper_exits:
            paper_config = policy.paper_config
            paper_report = _run_executor(
                paper_config, plan, paper_entries, paper_exits,
                market_id, trade_date, args.dry_run, "[paper]",
            )

    # ── Combine reports and update plan status ───────────────────────────
    # Use the live report as primary (higher-fidelity); fall back to paper.
    report = live_report or paper_report or {}

    if not args.dry_run and report:
        plan["status"] = "EXECUTED"
        plan["executed_at"] = datetime.now().isoformat()
        plan["execution_report"] = {
            "successful_entries": report.get("successful_entries", 0),
            "successful_exits": report.get("successful_exits", 0),
            "total_entries": report.get("total_entries", 0),
            "total_exits": report.get("total_exits", 0),
        }
        plan_gen._save_plan(plan, trade_date)

    # ── Summary ──────────────────────────────────────────────────────────
    if report:
        ok_entries = report.get("successful_entries", 0)
        ok_exits = report.get("successful_exits", 0)
        total_entries = report.get("total_entries", 0)
        total_exits = report.get("total_exits", 0)

        log.info(
            "Execution complete: entries=%d/%d exits=%d/%d dry_run=%s",
            ok_entries, total_entries, ok_exits, total_exits, args.dry_run,
        )

        if not args.dry_run:
            _notify_execution(market_id, trade_date, report)
    else:
        log.info("No executor produced a report (empty plan or connection failures)")


def _notify_execution(market_id: str, trade_date: str, report: dict):
    """Send Telegram summary of executed orders."""
    ok_entries = report.get("successful_entries", 0)
    ok_exits = report.get("successful_exits", 0)
    total_entries = report.get("total_entries", 0)
    total_exits = report.get("total_exits", 0)
    n_errors = max(0, total_entries - ok_entries) + max(0, total_exits - ok_exits)

    # Suppress when nothing executed AND no errors
    if ok_entries == 0 and ok_exits == 0 and n_errors == 0:
        log.info(
            "_notify_execution: %s %s nothing executed (0/0) and no errors — skipping Telegram",
            market_id, trade_date,
        )
        return

    try:
        from utils.telegram import send_message, tg_escape as _tge

        lines = [
            f"🚀 <b>Orders Submitted</b> ({market_id.upper()} {trade_date})",
            f"  Entries: {ok_entries}/{total_entries} | Exits: {ok_exits}/{total_exits}",
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
    # Suppress when there's nothing to auto-approve about
    if n_entries == 0 and n_exits == 0:
        log.info(
            "_notify_auto_approve: %s %s empty plan (0 entries, 0 exits) — skipping Telegram",
            market_id, trade_date,
        )
        return
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
