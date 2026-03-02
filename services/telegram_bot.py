#!/usr/bin/env python3
"""Atlas Telegram Approval Bot.

Long-running bot that:
  - Sends trade plans with Approve / Reject inline buttons
  - Executes approved plans through the live (or paper) executor
  - Reports execution results back to chat

Credentials from ~/.atlas-secrets.json:
    telegram_bot_token, telegram_chat_id

Run:
    python3 services/telegram_bot.py          # foreground
    systemctl start atlas-telegram-bot        # systemd
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import traceback
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Optional

# ── Project path setup ─────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from utils.config import get_active_config
from brokers.plan import TradePlanGenerator
from utils.telegram import _load_credentials, _esc, _build_portfolio_snapshot

logger = logging.getLogger("atlas.telegram_bot")

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

DEFAULT_MARKET = os.environ.get("ATLAS_MARKET", "sp500")
SECRETS_PATH = Path.home() / ".atlas-secrets.json"


def _authorized(chat_id: int | str) -> bool:
    """Only the configured chat_id may interact with the bot."""
    try:
        _, allowed = _load_credentials()
        return str(chat_id) == str(allowed)
    except ValueError:
        return False


# ═══════════════════════════════════════════════════════════════
# Plan formatting (with inline buttons)
# ═══════════════════════════════════════════════════════════════

def format_plan_message(plan: dict, market_id: str = "asx") -> str:
    """Build an HTML summary of the trade plan for Telegram."""
    trade_date = plan.get("trade_date", "?")
    entries = plan.get("proposed_entries", [])
    exits = plan.get("proposed_exits", [])
    snap = plan.get("portfolio_snapshot", {})
    risk = plan.get("risk_summary", {})

    lines = [
        f"📊 <b>Atlas Trade Plan — {_esc(trade_date)}</b>",
        "",
    ]

    # Portfolio snapshot
    if snap:
        equity = snap.get("equity", 0)
        cash = snap.get("cash", 0)
        pnl = snap.get("total_pnl", 0)
        pnl_pct = snap.get("total_pnl_pct", 0)
        n_pos = snap.get("open_positions", 0)
        lines.append(
            f"<b>Portfolio:</b> ${equity:,.0f} equity | "
            f"${cash:,.0f} cash | "
            f"PnL {'+' if pnl >= 0 else ''}{pnl:,.0f} ({pnl_pct:+.1f}%) | "
            f"{n_pos} pos"
        )
        lines.append("")

    # Entries
    if entries:
        lines.append(f"<b>🟢 Entries ({len(entries)}):</b>")
        for e in entries[:8]:
            ticker = e.get("ticker", "?")
            strategy = e.get("strategy", "?")
            price = e.get("entry_price", 0)
            size = e.get("position_size", 0)
            conf = e.get("confidence", 0)
            value = price * size
            lines.append(
                f"  <b>{_esc(ticker)}</b> {size}× @ ${price:.2f} "
                f"= ${value:.0f}  [{strategy}] {conf:.0%}"
            )
        if len(entries) > 8:
            lines.append(f"  … +{len(entries) - 8} more")
        lines.append("")
    else:
        lines.append("<b>🟢 Entries:</b> None")
        lines.append("")

    # Exits
    if exits:
        lines.append(f"<b>🟡 Exits ({len(exits)}):</b>")
        for ex in exits[:6]:
            ticker = ex.get("ticker", "?")
            reason = ex.get("reason", ex.get("exit_reason", "?"))
            lines.append(f"  <b>{_esc(ticker)}</b> — {_esc(reason)}")
        if len(exits) > 6:
            lines.append(f"  … +{len(exits) - 6} more")
        lines.append("")
    else:
        lines.append("<b>🟡 Exits:</b> None")
        lines.append("")

    # Risk summary
    if risk:
        cost = risk.get("total_proposed_cost", 0)
        risk_amt = risk.get("total_proposed_risk", 0)
        exposure = risk.get("portfolio_exposure_pct", 0)
        lines.append(
            f"<b>⚠️ Risk:</b> Cost ${cost:,.0f} | "
            f"Risk ${risk_amt:,.0f} | "
            f"Exposure {exposure:.0f}%"
        )
        lines.append("")

    # Mode indicator
    config = get_active_config(market_id)
    mode = config.get("trading", {}).get("mode", "paper")
    dry_run = config.get("trading", {}).get("live_safety", {}).get("dry_run_first", True)
    broker = config.get("trading", {}).get("broker", "paper")

    if broker == "paper":
        mode_str = "📝 PAPER"
    elif dry_run:
        mode_str = "🔶 LIVE (DRY RUN)"
    else:
        mode_str = "🔴 LIVE"

    lines.append(f"<b>Mode:</b> {mode_str}")

    if not entries and not exits:
        lines.append("\n→ <b>Hold all positions</b> — no action needed today.")

    return "\n".join(lines)


def approval_keyboard(trade_date: str, market_id: str = "asx") -> InlineKeyboardMarkup:
    """Build Approve / Reject inline buttons for a plan.

    Callback data format: plan:{date}:{action}:{market}
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Approve & Execute",
                callback_data=f"plan:{trade_date}:approve:{market_id}",
            ),
            InlineKeyboardButton(
                "❌ Reject",
                callback_data=f"plan:{trade_date}:reject:{market_id}",
            ),
        ]
    ])


# ═══════════════════════════════════════════════════════════════
# Plan execution (runs in thread pool — blocking I/O)
# ═══════════════════════════════════════════════════════════════

def _do_approve_and_execute(trade_date: str, market_id: str) -> str:
    """Approve plan and execute via live broker. Returns result text.

    This runs in a thread (blocking broker I/O).
    """
    config = get_active_config(market_id)

    # Approve the plan using LivePortfolio
    from brokers.live_portfolio import LivePortfolio
    portfolio = LivePortfolio(config, market_id=market_id)
    portfolio.connect()
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.approve_plan(trade_date)
    if isinstance(portfolio, LivePortfolio):
        portfolio.disconnect()
    if not plan:
        return "❌ No plan found for %s" % trade_date

    # Always execute live — broker is the sole source of truth
    return _execute_live(plan, trade_date, config, market_id)


def _execute_live(plan: dict, trade_date: str, config: dict, market_id: str) -> str:
    """Execute plan through LiveExecutor against the real broker.

    After live execution, also updates the paper portfolio state so the
    $X allocation tracking stays in sync with actual broker fills.
    """
    from brokers.live_executor import LiveExecutor
    from brokers.live_portfolio import LivePortfolio

    dry_run = config.get("trading", {}).get("live_safety", {}).get("dry_run_first", True)
    executor = LiveExecutor(config)

    if not executor.connect():
        broker_name = config.get("trading", {}).get("broker", "unknown")
        return f"❌ Failed to connect to {broker_name} broker"

    live_pf = LivePortfolio(config, market_id=market_id)
    live_pf._broker = executor._broker
    live_pf._connected = True
    live_pf._refresh_from_broker()

    if not live_pf.broker_data_valid:
        executor.disconnect()
        return "❌ Broker returned zeroed data (likely offline). Execution aborted to protect state."

    try:
        report = executor.execute_plan(plan, trade_date)

        # Record closed trades from successful exits with full metrics
        for exit_result in report.get("exits", []):
            if exit_result.get("success"):
                ticker = exit_result.get("ticker", "")
                pre_pos = next((p for p in live_pf.positions if p.ticker == ticker), None)
                exit_price = exit_result.get("fill_price", exit_result.get("price", 0))
                entry_price = pre_pos.entry_price if pre_pos else 0
                shares = exit_result.get("qty", pre_pos.shares if pre_pos else 0)
                entry_value = round(entry_price * shares, 2)
                exit_value = round(exit_price * shares, 2)
                # Approximate commissions from config
                comm_flat = config.get("fees", {}).get("commission_per_trade", 1.10)
                comm_pct = config.get("fees", {}).get("commission_pct", 0.0)
                entry_comm = round(max(comm_flat, entry_value * comm_pct), 2)
                exit_comm = round(max(comm_flat, exit_value * comm_pct), 2)
                pnl = round(exit_value - entry_value - entry_comm - exit_comm, 2)
                pnl_pct = round(pnl / entry_value * 100, 2) if entry_value > 0 else 0
                holding = pre_pos.holding_days(trade_date) if pre_pos else 0
                trade_record = {
                    "ticker": ticker,
                    "strategy": pre_pos.strategy if pre_pos else "unknown",
                    "entry_date": pre_pos.entry_date if pre_pos else "unknown",
                    "exit_date": trade_date,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "shares": shares,
                    "entry_value": entry_value,
                    "exit_value": exit_value,
                    "entry_commission": entry_comm,
                    "exit_commission": exit_comm,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "mae": round(pre_pos.mae * 100, 2) if pre_pos else 0,
                    "mfe": round(pre_pos.mfe * 100, 2) if pre_pos else 0,
                    "holding_days": holding,
                    "exit_reason": exit_result.get("reason", "signal_exit"),
                    "confidence": pre_pos.confidence if pre_pos else 0,
                    "sector": pre_pos.sector if pre_pos else "Unknown",
                    "stop_price": pre_pos.stop_price if pre_pos else 0,
                    "take_profit": pre_pos.take_profit if pre_pos else None,
                    "market_id": market_id,
                    "dry_run": exit_result.get("dry_run", False),
                    "order_id": exit_result.get("order_id", ""),
                }
                live_pf.record_closed_trade(trade_record)

        live_pf.record_equity(trade_date)
    finally:
        executor.disconnect()

    # Format result
    n_entries = report.get("successful_entries", 0)
    n_exits = report.get("successful_exits", 0)
    total_entries = report.get("total_entries", 0)
    total_exits = report.get("total_exits", 0)
    errors = report.get("errors", [])

    prefix = "🔶 DRY RUN" if dry_run else "🔴 LIVE"
    lines = [
        f"{prefix} <b>Execution Complete — {trade_date}</b>",
        "",
        f"  Entries: {n_entries}/{total_entries} successful",
        f"  Exits:   {n_exits}/{total_exits} successful",
    ]

    for entry in report.get("entries", []):
        ticker = entry.get("ticker", "?")
        qty = entry.get("qty", 0)
        price = entry.get("price", 0)
        ok = "✅" if entry.get("success") else "❌"
        msg = entry.get("message", "")
        oid = entry.get("order_id", "")
        detail = f"  {ok} BUY {_esc(ticker)} {qty}× @ ${price:.2f}"
        if oid:
            detail += f" (#{oid})"
        if not entry.get("success"):
            detail += f" — {_esc(msg)}"
        lines.append(detail)

    for exit_ in report.get("exits", []):
        ticker = exit_.get("ticker", "?")
        qty = exit_.get("qty", 0)
        price = exit_.get("price", 0)
        ok = "✅" if exit_.get("success") else "❌"
        reason = exit_.get("reason", "")
        msg = exit_.get("message", "")
        detail = f"  {ok} SELL {_esc(ticker)} {qty}× @ ${price:.2f}"
        if reason:
            detail += f" [{_esc(reason)}]"
        if not exit_.get("success"):
            detail += f" — {_esc(msg)}"
        lines.append(detail)

    if errors:
        lines.append("")
        lines.append("<b>Errors:</b>")
        for e in errors:
            lines.append(f"  ⚠️ {_esc(str(e))}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# Bot handlers
# ═══════════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /status command — show portfolio snapshot."""
    if not _authorized(update.effective_chat.id):
        return

    snapshot = _build_portfolio_snapshot(DEFAULT_MARKET)
    if snapshot:
        await update.message.reply_text(
            f"📊 <b>Atlas Status</b>\n\n{snapshot}",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⚠️ Could not load portfolio.")


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /plan command — show today's plan with approval buttons."""
    if not _authorized(update.effective_chat.id):
        return

    trade_date = datetime.now().strftime("%Y-%m-%d")
    config = get_active_config(DEFAULT_MARKET)
    # Use LivePortfolio for plan loading (just needs TradePlanGenerator for file access)
    from brokers.live_portfolio import LivePortfolio
    portfolio = LivePortfolio(config, market_id=DEFAULT_MARKET)
    portfolio.connect()
    plan_gen = TradePlanGenerator(portfolio, config)
    plan = plan_gen.load_plan(trade_date)

    if not plan:
        await update.message.reply_text(
            f"📊 No plan found for {trade_date}.\nRun pre-market first.",
            parse_mode="HTML",
        )
        return

    msg = format_plan_message(plan, DEFAULT_MARKET)

    if plan.get("status") == "APPROVED":
        await update.message.reply_text(
            msg + "\n\n✅ <b>Already approved</b>", parse_mode="HTML",
        )
    elif plan.get("status") == "EXECUTED":
        await update.message.reply_text(
            msg + "\n\n✅ <b>Already executed</b>", parse_mode="HTML",
        )
    else:
        entries = plan.get("proposed_entries", [])
        exits = plan.get("proposed_exits", [])
        if not entries and not exits:
            await update.message.reply_text(
                msg + "\n\n💤 No trades today — nothing to approve.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                msg, parse_mode="HTML",
                reply_markup=approval_keyboard(trade_date),
            )


async def cmd_halt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /halt command — emergency halt."""
    if not _authorized(update.effective_chat.id):
        return

    halt_file = PROJECT_ROOT / ".live_halt"
    halt_file.write_text(f"Telegram halt\n{datetime.now().isoformat()}")
    await update.message.reply_text(
        "🛑 <b>EMERGENCY HALT ACTIVATED</b>\n\n"
        "All live trading is suspended.\n"
        "Use /unhalt to resume.",
        parse_mode="HTML",
    )


async def cmd_unhalt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /unhalt command — clear halt."""
    if not _authorized(update.effective_chat.id):
        return

    halt_file = PROJECT_ROOT / ".live_halt"
    if halt_file.exists():
        halt_file.unlink()
        await update.message.reply_text("✅ Halt cleared. Trading can resume.")
    else:
        await update.message.reply_text("ℹ️ No halt is active.")


async def handle_approval_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle Approve / Reject button presses.

    Callback data format: plan:{date}:{action}:{market}
    Legacy format (no market): plan:{date}:{action} — defaults to DEFAULT_MARKET.
    """
    query = update.callback_query
    await query.answer()  # Acknowledge the button press immediately

    if not _authorized(query.message.chat_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    data = query.data  # e.g. "plan:2026-02-27:approve:sp500"
    parts = data.split(":")
    if len(parts) < 3 or parts[0] != "plan":
        return

    trade_date = parts[1]
    action = parts[2]
    market_id = parts[3] if len(parts) >= 4 else DEFAULT_MARKET

    if action == "reject":
        # Mark rejected — update the message
        config = get_active_config(market_id)
        # TradePlanGenerator just needs portfolio for plan file access
        from brokers.live_portfolio import LivePortfolio
        portfolio = LivePortfolio(config, market_id=market_id)
        plan_gen = TradePlanGenerator(portfolio, config)
        plan = plan_gen.load_plan(trade_date)
        if plan:
            plan["status"] = "REJECTED"
            plan["rejected_at"] = datetime.now().isoformat()
            plan_gen._save_plan(plan, trade_date)

        await query.edit_message_text(
            query.message.text_html + "\n\n❌ <b>REJECTED</b>",
            parse_mode="HTML",
        )
        return

    if action == "approve":
        # Show "executing..." feedback
        await query.edit_message_text(
            query.message.text_html + f"\n\n⏳ <b>Approving and executing [{market_id.upper()}]…</b>",
            parse_mode="HTML",
        )

        # Run execution in thread pool (blocking broker I/O)
        try:
            loop = asyncio.get_event_loop()
            result_text = await loop.run_in_executor(
                None,
                partial(_do_approve_and_execute, trade_date, market_id),
            )
        except Exception as e:
            logger.error("Execution failed: %s", e, exc_info=True)
            result_text = f"❌ <b>Execution failed</b>\n\n<pre>{_esc(traceback.format_exc()[-500:])}</pre>"

        # Send result as a new message (original is already long)
        await query.message.reply_text(result_text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# Research promotion callback handler
# ═══════════════════════════════════════════════════════════════

def _do_promote(experiment_id: str, market_id: str) -> str:
    """Execute promotion in a thread (blocking I/O)."""
    from scripts.research_promote import promote_candidate, validate_candidate

    # Quick validation sanity check (skip OOS — already done before request was sent)
    candidate_path = PROJECT_ROOT / 'config' / 'candidates' / f'{market_id}_{experiment_id}.json'
    if not candidate_path.exists():
        return f"❌ <b>Candidate config not found</b>\n<code>{candidate_path.name}</code>"

    result = promote_candidate(experiment_id, market_id, candidate_path)
    if result.get('success'):
        return (
            f"✅ <b>Promoted!</b>\n\n"
            f"Experiment: <code>{_esc(experiment_id)}</code>\n"
            f"Market: {_esc(market_id.upper())}\n"
            f"Version: <code>{_esc(str(result.get('version_path', '?')))}</code>\n\n"
            f"Active config updated. Watchdog will monitor for 5 days."
        )
    else:
        return f"❌ <b>Promotion failed</b>\n\n{_esc(result.get('error', 'Unknown error'))}"


def _do_reject_research(experiment_id: str, market_id: str) -> str:
    """Execute rejection in a thread."""
    from scripts.research_promote import reject_candidate
    result = reject_candidate(experiment_id, market_id, reason="Rejected via Telegram button")
    if result.get('success'):
        return f"❌ <b>Rejected</b>\n\nExperiment <code>{_esc(experiment_id)}</code> archived."
    else:
        return f"⚠️ Rejection processing error: {_esc(str(result))}"


async def handle_research_promotion_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle Approve / Reject button presses for research promotion requests.

    Callback data format: research:{experiment_id}:{action}:{market}
    """
    query = update.callback_query
    await query.answer()

    if not _authorized(query.message.chat_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    data = query.data  # e.g. "research:wave1_asx_reopt:approve:asx"
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "research":
        return

    experiment_id = parts[1]
    action = parts[2]
    market_id = parts[3]

    if action == "reject":
        await query.edit_message_text(
            query.message.text_html + "\n\n⏳ <b>Rejecting…</b>",
            parse_mode="HTML",
        )
        try:
            loop = asyncio.get_event_loop()
            result_text = await loop.run_in_executor(
                None,
                partial(_do_reject_research, experiment_id, market_id),
            )
        except Exception as e:
            logger.error("Research rejection failed: %s", e, exc_info=True)
            result_text = f"❌ <b>Rejection failed</b>\n\n<pre>{_esc(traceback.format_exc()[-500:])}</pre>"

        await query.edit_message_text(
            query.message.text_html.replace("⏳ <b>Rejecting…</b>", result_text),
            parse_mode="HTML",
        )
        return

    if action == "approve":
        await query.edit_message_text(
            query.message.text_html + "\n\n⏳ <b>Promoting…</b>",
            parse_mode="HTML",
        )
        try:
            loop = asyncio.get_event_loop()
            result_text = await loop.run_in_executor(
                None,
                partial(_do_promote, experiment_id, market_id),
            )
        except Exception as e:
            logger.error("Research promotion failed: %s", e, exc_info=True)
            result_text = f"❌ <b>Promotion failed</b>\n\n<pre>{_esc(traceback.format_exc()[-500:])}</pre>"

        await query.message.reply_text(result_text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# External API — called by cron/scripts to send plan for approval
# ═══════════════════════════════════════════════════════════════

def send_plan_for_approval(
    plan_path: Optional[str] = None,
    market_id: str = "asx",
) -> bool:
    """Send a plan summary with Approve/Reject buttons.

    Called from pi-cron.sh or scripts. Uses raw HTTP API (no bot instance
    needed — just sends a one-shot message with inline keyboard).
    """
    import urllib.request
    import urllib.error

    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        logger.error("Cannot send approval: %s", e)
        return False

    # Load plan
    if plan_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_path = str(PROJECT_ROOT / f"plans/plan_{today}.json")

    plan_file = Path(plan_path)
    if not plan_file.exists():
        logger.error("Plan file not found: %s", plan_path)
        return False

    with open(plan_file) as f:
        plan = json.load(f)

    trade_date = plan.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
    msg_text = format_plan_message(plan, market_id)

    entries = plan.get("proposed_entries", [])
    exits = plan.get("proposed_exits", [])

    # If no trades, send without buttons
    if not entries and not exits:
        msg_text += "\n\n💤 No trades today — holding all positions."
        keyboard = None
    else:
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Approve & Execute", "callback_data": f"plan:{trade_date}:approve:{market_id}"},
                {"text": "❌ Reject", "callback_data": f"plan:{trade_date}:reject:{market_id}"},
            ]]
        }

    payload = {
        "chat_id": chat_id,
        "text": msg_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if keyboard:
        payload["reply_markup"] = json.dumps(keyboard)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                logger.info("Plan sent for approval (trade_date=%s)", trade_date)
                return True
            logger.warning("Telegram API returned ok=false: %s", body)
            return False
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        logger.error("Telegram HTTP %d: %s", e.code, body)
        return False
    except Exception as e:
        logger.error("Failed to send plan: %s", e)
        return False


# ═══════════════════════════════════════════════════════════════
# Main — run the bot
# ═══════════════════════════════════════════════════════════════

def main():
    from utils.logging_config import setup_logging
    setup_logging("telegram_bot", extra_log_file="telegram_bot", telegram_errors=False)

    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    logger.info("Starting Atlas Telegram Bot (chat_id=%s)", chat_id)

    app = Application.builder().token(token).build()

    # Command handlers
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.add_handler(CommandHandler("unhalt", cmd_unhalt))

    # Callback handler for plan inline buttons (with optional :market suffix)
    app.add_handler(CallbackQueryHandler(
        handle_approval_callback,
        pattern=r"^plan:\d{4}-\d{2}-\d{2}:(approve|reject)(:\w+)?$",
    ))

    # Callback handler for research promotion inline buttons
    app.add_handler(CallbackQueryHandler(
        handle_research_promotion_callback,
        pattern=r"^research:.+:(approve|reject):\w+$",
    ))

    logger.info("Bot polling started. Commands: /status /plan /halt /unhalt")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
