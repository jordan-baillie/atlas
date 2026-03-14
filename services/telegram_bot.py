#!/usr/bin/env python3
"""Atlas Telegram Approval Bot.

Long-running bot that:
  - Sends trade plans with Approve / Reject inline buttons
  - Executes approved plans through the live executor
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
from datetime import datetime, timezone
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


def _md_to_telegram_html(text: str) -> str:
    """Convert markdown-ish text to Telegram HTML.

    Handles: **bold**, *italic*, `code`, ```code blocks```,
    - bullet lists, numbered lists. Escapes HTML entities first.
    """
    import re

    # Escape HTML entities first
    text = _esc(text)

    # Code blocks (``` ... ```) → <pre>
    text = re.sub(r'```(?:\w+)?\n?(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)

    # Inline code (`...`) → <code>
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)

    # Bold (**...**) → <b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)

    # Italic (*...*) — but not inside <b> tags from bold conversion
    text = re.sub(r'(?<!\w)\*([^*]+?)\*(?!\w)', r'<i>\1</i>', text)

    # Heading lines (### ... or ## ... or # ...) → bold
    text = re.sub(r'^#{1,3}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Bullet lists (- item or * item) → • item
    text = re.sub(r'^[\s]*[-*]\s+', '• ', text, flags=re.MULTILINE)

    # Horizontal rules (--- or ===) → empty line
    text = re.sub(r'^[-=]{3,}\s*$', '', text, flags=re.MULTILINE)

    # Clean up multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()

# ═══════════════════════════════════════════════════════════════
# Configuration
# ═══════════════════════════════════════════════════════════════

DEFAULT_MARKET = os.environ.get("ATLAS_MARKET", "sp500")
ALL_MARKETS = ["asx", "sp500"]  # Scanned by /plan and /status when no market specified
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

def format_plan_message(plan: dict, market_id: str = "sp500") -> str:
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
    mode = config.get("trading", {}).get("mode", "live")
    dry_run = config.get("trading", {}).get("live_safety", {}).get("dry_run_first", True)
    broker = config.get("trading", {}).get("broker", "alpaca")

    if not broker or broker not in ("alpaca",):
        mode_str = "📝 PAPER"
    elif dry_run:
        mode_str = "🔶 LIVE (DRY RUN)"
    else:
        mode_str = "🔴 LIVE"

    lines.append(f"<b>Mode:</b> {mode_str}")

    if not entries and not exits:
        lines.append("\n→ <b>Hold all positions</b> — no action needed today.")

    return "\n".join(lines)


def approval_keyboard(trade_date: str, market_id: str = "sp500") -> InlineKeyboardMarkup:
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

    # Approve plan (file-based — no broker connection needed)
    plan_gen = TradePlanGenerator(None, config)
    plan = plan_gen.approve_plan(trade_date, market_id=market_id)
    if not plan:
        return "❌ No plan found for %s (%s)" % (trade_date, market_id)

    # Always execute live — broker is the sole source of truth
    return _execute_live(plan, trade_date, config, market_id)


def _execute_live(plan: dict, trade_date: str, config: dict, market_id: str) -> str:
    """Execute plan through LiveExecutor against the real broker.

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
                # Safety: if fill_price is 0 (order not yet filled when checked),
                # fall back to the limit price. A fill at limit-1% is better than
                # recording exit_price=0 which makes P&L show -100%.
                if exit_price == 0:
                    exit_price = exit_result.get("price", 0)
                    logger.warning("Exit fill_price=0 for %s, using limit price $%.2f",
                                   ticker, exit_price)
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
    """Handle /status command — show portfolio snapshot.

    Usage: /status [market]   e.g. /status asx
    Without argument, shows all markets with active configs.
    """
    if not _authorized(update.effective_chat.id):
        return

    # Parse optional market argument
    args = (update.message.text or "").split()
    markets = [args[1].lower()] if len(args) > 1 else ALL_MARKETS

    parts = []
    for mkt in markets:
        config_path = PROJECT_ROOT / "config" / "active" / f"{mkt}.json"
        if not config_path.exists():
            continue
        snapshot = _build_portfolio_snapshot(mkt)
        if snapshot:
            parts.append(f"<b>📊 {mkt.upper()}</b>\n{snapshot}")

    if parts:
        await update.message.reply_text(
            "\n\n".join(parts),
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text("⚠️ Could not load portfolio.")


async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /plan command — show today's plan with approval buttons.

    Usage: /plan [market]   e.g. /plan asx
    Without argument, shows plans for all markets.
    """
    if not _authorized(update.effective_chat.id):
        return

    trade_date = datetime.now().strftime("%Y-%m-%d")
    args = (update.message.text or "").split()
    markets = [args[1].lower()] if len(args) > 1 else ALL_MARKETS

    found_any = False
    for market_id in markets:
        config_path = PROJECT_ROOT / "config" / "active" / f"{market_id}.json"
        if not config_path.exists():
            continue

        config = get_active_config(market_id)
        plan_gen = TradePlanGenerator(None, config)
        plan = plan_gen.load_plan(trade_date, market_id=market_id)
        if not plan:
            continue

        found_any = True
        msg = format_plan_message(plan, market_id)

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
                    reply_markup=approval_keyboard(trade_date, market_id),
                )

    if not found_any:
        await update.message.reply_text(
            f"📊 No plan found for {trade_date}.\nRun pre-market first.",
            parse_mode="HTML",
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
        # TradePlanGenerator for file-based plan access (no broker needed)
        plan_gen = TradePlanGenerator(None, config)
        plan = plan_gen.load_plan(trade_date, market_id=market_id)
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
# Auto-promotion rollback callback handler
# ═══════════════════════════════════════════════════════════════

def _do_rollback(version: str, market_id: str) -> str:
    """Execute rollback in a thread (blocking I/O)."""
    from research.promoter import rollback
    result = rollback(market_id)
    if result.get("success"):
        restored = result.get("version_restored", "?")
        return (
            f"↩️ <b>Rolled back!</b>\n\n"
            f"Market: {_esc(market_id.upper())}\n"
            f"Restored version: <code>{_esc(str(restored))}</code>\n"
            f"Was on: <code>{_esc(version)}</code>\n\n"
            f"Active config updated."
        )
    else:
        return (
            f"❌ <b>Rollback failed</b>\n\n"
            f"{_esc(result.get('message', 'Unknown error'))}"
        )


async def handle_rollback_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle rollback button presses sent by auto-promotion notifications.

    Callback data format: promote:{version}:rollback:{market}
    """
    query = update.callback_query
    await query.answer()

    if not _authorized(query.message.chat_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    data = query.data  # e.g. "promote:v2.3:rollback:sp500"
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "promote" or parts[2] != "rollback":
        return

    version = parts[1]
    market_id = parts[3]

    await query.edit_message_text(
        query.message.text_html + "\n\n⏳ <b>Rolling back…</b>",
        parse_mode="HTML",
    )
    try:
        loop = asyncio.get_event_loop()
        result_text = await loop.run_in_executor(
            None,
            partial(_do_rollback, version, market_id),
        )
    except Exception as e:
        logger.error("Rollback failed: %s", e, exc_info=True)
        result_text = (
            f"❌ <b>Rollback failed</b>\n\n"
            f"<pre>{_esc(traceback.format_exc()[-500:])}</pre>"
        )

    await query.edit_message_text(
        query.message.text_html.replace("⏳ <b>Rolling back…</b>", result_text),
        parse_mode="HTML",
    )


# ═══════════════════════════════════════════════════════════════
# External API — called by cron/scripts to send plan for approval
# ═══════════════════════════════════════════════════════════════

def send_plan_for_approval(
    plan_path: Optional[str] = None,
    market_id: str = "sp500",
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
        plan_path = str(PROJECT_ROOT / f"plans/plan_{market_id}_{today}.json")

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
# Job dispatch handlers (/task, /jobs, /job, /kill, /logs, /specs)
# ═══════════════════════════════════════════════════════════════

async def cmd_task(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /task command — dispatch a Pi agent job.

    Usage:
        /task <prompt>                    Run with any prompt
        /task @healthz                    Run a named skill
        /task @healthz check disk space   Skill + extra instructions
        /task #weekly-reopt               Run a named spec file
    """
    if not _authorized(update.effective_chat.id):
        return

    raw = (update.message.text or "").strip()
    # Strip /task prefix
    body = raw.split(None, 1)[1] if len(raw.split(None, 1)) > 1 else ""

    if not body:
        await update.message.reply_text(
            "📋 <b>Usage:</b>\n"
            "  <code>/task &lt;prompt&gt;</code> — run any task\n"
            "  <code>/task @healthz</code> — run a named skill\n"
            "  <code>/task @healthz fix disk</code> — skill + instructions\n"
            "  <code>/task #weekly-reopt</code> — run a named spec\n\n"
            f"<b>Available skills:</b> {', '.join(sorted(_get_skill_aliases()))}\n"
            f"<b>Available specs:</b> {', '.join(_get_spec_names()) or 'none yet'}",
            parse_mode="HTML",
        )
        return

    # Parse skill reference (@name) and spec reference (#name)
    skill = None
    spec = None
    prompt = body

    if body.startswith("@"):
        parts = body.split(None, 1)
        skill = parts[0][1:]  # strip @
        prompt = parts[1] if len(parts) > 1 else ""
        if not prompt:
            prompt = f"Run the {skill} skill and report results."
    elif body.startswith("#"):
        parts = body.split(None, 1)
        spec = parts[0][1:]  # strip #
        prompt = parts[1] if len(parts) > 1 else ""

    # Create and start job
    from services.job_server import get_manager
    mgr = get_manager()

    try:
        job = mgr.create_job(prompt=prompt, skill=skill, spec=spec)
    except ValueError as e:
        await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")
        return

    try:
        job = mgr.start_job(job["id"])
    except ValueError as e:
        await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")
        return

    skill_tag = f" [{_esc(skill)}]" if skill else ""
    spec_tag = f" [spec: {_esc(spec)}]" if spec else ""

    await update.message.reply_text(
        f"🚀 <b>Job dispatched</b>{skill_tag}{spec_tag}\n"
        f"<b>ID:</b> <code>{_esc(job['id'])}</code>\n"
        f"<b>Prompt:</b> {_esc(prompt[:200])}\n\n"
        f"Track: <code>/job {_esc(job['id'])}</code>\n"
        f"Logs: <code>/logs {_esc(job['id'])}</code>\n"
        f"Kill: <code>/kill {_esc(job['id'])}</code>",
        parse_mode="HTML",
    )

    # Schedule completion check (polls every 30s until job finishes)
    try:
        if ctx.job_queue:
            ctx.job_queue.run_repeating(
                _check_job_completion,
                interval=30,
                first=30,
                data={"job_id": job["id"], "chat_id": update.effective_chat.id},
                name=f"job_monitor_{job['id']}",
            )
    except Exception as e:
        logger.warning("Failed to schedule job monitor (will still run, no auto-notify): %s", e)


async def cmd_jobs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /jobs — list active and recent jobs."""
    if not _authorized(update.effective_chat.id):
        return

    from services.job_server import get_manager
    mgr = get_manager()
    jobs = mgr.list_jobs(limit=10)

    if not jobs:
        await update.message.reply_text("📋 No jobs found.")
        return

    lines = ["📋 <b>Recent Jobs</b>", ""]
    status_icons = {
        "running": "🔄", "queued": "⏳", "done": "✅",
        "failed": "❌", "killed": "🛑", "timeout": "⏰",
    }

    for j in jobs:
        icon = status_icons.get(j["status"], "❓")
        prompt_brief = (j.get("prompt") or "")[:60].replace("\n", " ")
        skill_tag = f" @{j['skill_name']}" if j.get("skill_name") else ""
        elapsed = _job_elapsed(j)
        lines.append(
            f"{icon} <code>{_esc(j['id'])}</code>{skill_tag}\n"
            f"    {_esc(prompt_brief)}…  ({elapsed})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_job(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /job <id> — detailed job status."""
    if not _authorized(update.effective_chat.id):
        return

    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.message.reply_text("Usage: <code>/job &lt;id&gt;</code>", parse_mode="HTML")
        return

    job_id = args[1]
    from services.job_server import get_manager
    job = get_manager().get_job(job_id)

    if not job:
        await update.message.reply_text(f"❌ Job <code>{_esc(job_id)}</code> not found.", parse_mode="HTML")
        return

    status_icons = {
        "running": "🔄", "queued": "⏳", "done": "✅",
        "failed": "❌", "killed": "🛑", "timeout": "⏰",
    }
    icon = status_icons.get(job["status"], "❓")

    skill_tag = f" @{_esc(job['skill_name'])}" if job.get("skill_name") else ""

    lines = [
        f"{icon} <b>Job {_esc(job['id'])}</b>{skill_tag}",
        "",
        f"<b>Status:</b> {job['status']}",
        f"<b>Prompt:</b> {_esc((job.get('prompt') or '')[:300])}",
    ]

    if job.get("spec"):
        lines.append(f"<b>Spec:</b> {_esc(job['spec'])}")

    # Timing info on one line
    timing = []
    if job.get("started_at"):
        timing.append(f"start {_esc(_fmt_time(job['started_at']))}")
    if job.get("completed_at"):
        timing.append(f"end {_esc(_fmt_time(job['completed_at']))}")
    timing.append(f"elapsed {_job_elapsed(job)}")
    lines.append(f"<b>Time:</b> {' → '.join(timing)}")

    if job.get("exit_code") is not None and job["exit_code"] != 0:
        lines.append(f"⚠️ <b>Exit code:</b> {job['exit_code']}")

    # Result summary — markdown → HTML
    summary = (job.get("result_summary") or "").strip()
    if summary and summary not in (
        "No output captured.",
        "Job produced no readable output.",
        "Job completed but produced no readable output.",
        "Could not read log.",
    ):
        formatted = _md_to_telegram_html(summary[:2000])
        lines.extend(["", "<b>Result:</b>", formatted])
    elif job["status"] in ("done", "failed"):
        lines.append(f"\n📄 <code>/logs {_esc(job['id'])}</code> for output")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_kill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /kill <id> — kill a running job."""
    if not _authorized(update.effective_chat.id):
        return

    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.message.reply_text("Usage: <code>/kill &lt;id&gt;</code>", parse_mode="HTML")
        return

    job_id = args[1]
    from services.job_server import get_manager

    try:
        job = get_manager().kill_job(job_id)
        await update.message.reply_text(
            f"🛑 Job <code>{_esc(job_id)}</code> killed.",
            parse_mode="HTML",
        )
    except ValueError as e:
        await update.message.reply_text(f"❌ {_esc(str(e))}", parse_mode="HTML")


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /logs <id> [lines] — show job output."""
    if not _authorized(update.effective_chat.id):
        return

    args = (update.message.text or "").split()
    if len(args) < 2:
        await update.message.reply_text("Usage: <code>/logs &lt;id&gt; [lines]</code>", parse_mode="HTML")
        return

    job_id = args[1]
    lines = int(args[2]) if len(args) > 2 and args[2].isdigit() else 30

    from services.job_server import get_manager
    logs = get_manager().get_logs(job_id, lines=lines)

    if len(logs) > 3800:
        logs = logs[-3800:]

    await update.message.reply_text(
        f"📄 <b>Logs: {_esc(job_id)}</b>\n\n<pre>{_esc(logs)}</pre>",
        parse_mode="HTML",
    )


async def cmd_charts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /charts — generate and send current status charts.

    Usage: /charts              All standard charts
           /charts leaderboard  Strategy leaderboard only
           /charts research     Research progress only
           /charts equity       Equity curve only
    """
    if not _authorized(update.effective_chat.id):
        return

    args = (update.message.text or "").split()
    chart_type = args[1].lower() if len(args) > 1 else "all"

    await update.message.reply_text("📊 Generating charts…")

    loop = asyncio.get_event_loop()

    def _gen():
        from utils.charts import (
            equity_chart, strategy_leaderboard_chart,
            research_progress_chart, generate_all_charts,
        )
        if chart_type == "all":
            return generate_all_charts()
        elif chart_type in ("leaderboard", "lb", "strategies"):
            c = strategy_leaderboard_chart()
            return [c] if c else []
        elif chart_type in ("research", "progress"):
            c = research_progress_chart()
            return [c] if c else []
        elif chart_type in ("equity", "eq", "portfolio"):
            c = equity_chart()
            return [c] if c else []
        else:
            return generate_all_charts()

    charts = await loop.run_in_executor(None, _gen)

    if not charts:
        await update.message.reply_text("⚠️ No chart data available.")
        return

    from utils.telegram import send_photo as _send_photo
    for i, chart_path in enumerate(charts):
        cap = f"📊 <b>Atlas Charts</b> ({len(charts)} total)" if i == 0 else ""
        await loop.run_in_executor(
            None, _send_photo, str(chart_path), cap, False,
        )


async def cmd_specs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /specs — list available task specs."""
    if not _authorized(update.effective_chat.id):
        return

    from services.job_server import get_manager
    specs = get_manager().list_specs()

    if not specs:
        await update.message.reply_text(
            "📋 No specs found.\n"
            f"Add <code>.md</code> files to <code>{_esc(str(SPECS_DIR))}</code>",
            parse_mode="HTML",
        )
        return

    lines = ["📋 <b>Available Specs</b>", ""]
    for s in specs:
        lines.append(f"  <code>#{_esc(s['name'])}</code> — {_esc(s['title'])}")

    lines.extend(["", "Run with: <code>/task #spec-name</code>"])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ── Job monitoring (background) ──────────────────────────────

async def _check_job_completion(ctx: ContextTypes.DEFAULT_TYPE):
    """Periodic callback to check if a job has completed.

    When complete, sends result notification and removes the repeating job.
    Uses notified_at flag to prevent duplicate alerts across bot restarts.
    """
    data = ctx.job.data
    job_id = data["job_id"]
    chat_id = data["chat_id"]

    from services.job_server import get_manager
    mgr = get_manager()
    # Run the blocking job lookup (subprocess + file I/O) in a thread pool
    # to avoid blocking the asyncio event loop.
    loop = asyncio.get_event_loop()
    job = await loop.run_in_executor(None, mgr.get_job, job_id)

    if not job or job["status"] == "running":
        return  # Still running, check again later

    # Stop the repeating check first (whether or not we send the notification)
    ctx.job.schedule_removal()

    # Guard against duplicate notifications (e.g. from bot restart + restore)
    if job.get("notified_at"):
        logger.info("Job %s already notified at %s — skipping duplicate", job_id, job["notified_at"])
        return

    # Mark as notified before sending (prevents race with concurrent monitors)
    job["notified_at"] = datetime.now(timezone.utc).isoformat()
    await loop.run_in_executor(None, mgr._save_job, job)

    # Job finished — send notification
    status_icons = {
        "done": "✅", "failed": "❌", "killed": "🛑", "timeout": "⏰",
    }
    icon = status_icons.get(job["status"], "❓")
    skill_tag = f" @{_esc(job['skill_name'])}" if job.get("skill_name") else ""
    prompt_brief = _esc((job.get("prompt") or "")[:100].replace("\n", " "))

    lines = [
        f"{icon} <b>Job Complete</b>{skill_tag}",
        f"{_esc(job['id'])}  •  {_job_elapsed(job)}",
    ]

    # Show prompt context (brief)
    if prompt_brief:
        lines.append(f"<i>{prompt_brief}</i>")

    # Show exit code only if non-zero (failure)
    if job.get("exit_code") and job["exit_code"] != 0:
        lines.append(f"\n⚠️ <b>Exit code:</b> {job['exit_code']}")

    # Result summary — convert markdown to HTML, don't use <pre>
    summary = (job.get("result_summary") or "").strip()
    if summary and summary not in (
        "No output captured.",
        "Job produced no readable output.",
        "Job completed but produced no readable output.",
        "Could not read log.",
    ):
        formatted = _md_to_telegram_html(summary[:2000])
        lines.extend(["", formatted])
    elif job["status"] == "done":
        lines.append("\n✅ Completed successfully (no summary produced)")
    else:
        lines.append(f"\n⚠️ No output captured")

    # Always offer /logs for full output
    lines.append(f"\n📄 <code>/logs {_esc(job['id'])}</code>")

    try:
        await ctx.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error("Failed to send job completion: %s", e)


# ── Helpers ──────────────────────────────────────────────────

SPECS_DIR = PROJECT_ROOT / "specs"


def _get_skill_aliases() -> list[str]:
    """Get available skill alias names."""
    from services.job_server import SKILL_ALIASES
    return list(SKILL_ALIASES.keys())


def _get_spec_names() -> list[str]:
    """Get available spec file names."""
    return [p.stem for p in SPECS_DIR.glob("*.md")] if SPECS_DIR.exists() else []


def _fmt_time(iso_str: Optional[str]) -> str:
    """Format ISO timestamp for display."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso_str[:19]


def _job_elapsed(job: dict) -> str:
    """Format job elapsed time."""
    start = job.get("started_at")
    end = job.get("completed_at")
    if not start:
        return "not started"
    try:
        t0 = datetime.fromisoformat(start)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        t1 = datetime.fromisoformat(end) if end else datetime.now(timezone.utc)
        if t1.tzinfo is None:
            t1 = t1.replace(tzinfo=timezone.utc)
        secs = int((t1 - t0).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    except Exception:
        return "?"


# ═══════════════════════════════════════════════════════════════
# Main — run the bot
# ═══════════════════════════════════════════════════════════════

async def _restore_job_monitors(app) -> None:
    """Re-register job monitors for jobs that were running when the bot last stopped.

    Called during post_init so the job queue is ready. Handles bot-restart recovery:
    without this, any job running across a restart never gets a completion notification.
    """
    try:
        from services.job_server import get_manager
        mgr = get_manager()
        running_jobs = mgr.list_jobs(status="running")
        if not running_jobs:
            return

        try:
            _, owner_chat_id = _load_credentials()
        except Exception:
            logger.warning("Could not load credentials for job monitor restore — skipping")
            return

        logger.info("Restoring completion monitors for %d running job(s) after restart", len(running_jobs))
        for job in running_jobs:
            job_id = job["id"]
            monitor_name = f"job_monitor_{job_id}"
            # Don't double-register if already scheduled (shouldn't happen on startup, but safe)
            if app.job_queue.get_jobs_by_name(monitor_name):
                continue
            app.job_queue.run_repeating(
                _check_job_completion,
                interval=30,
                first=10,  # check quickly after restart
                data={"job_id": job_id, "chat_id": int(owner_chat_id)},
                name=monitor_name,
            )
            logger.info("Restored monitor for job %s", job_id)
    except Exception as e:
        logger.error("Failed to restore job monitors on startup: %s", e)


def main():
    from utils.logging_config import setup_logging
    setup_logging("telegram_bot", extra_log_file="telegram_bot", telegram_errors=False)

    try:
        token, chat_id = _load_credentials()
    except ValueError as e:
        print(f"❌ {e}")
        sys.exit(1)

    logger.info("Starting Atlas Telegram Bot (chat_id=%s)", chat_id)

    app = Application.builder().token(token).post_init(_restore_job_monitors).build()

    # Command handlers
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.add_handler(CommandHandler("unhalt", cmd_unhalt))

    # Job dispatch handlers
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("job", cmd_job))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("specs", cmd_specs))
    app.add_handler(CommandHandler("charts", cmd_charts))

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

    # Callback handler for auto-promotion rollback buttons
    app.add_handler(CallbackQueryHandler(
        handle_rollback_callback,
        pattern=r"^promote:.+:rollback:\w+$",
    ))

    logger.info("Bot polling started. Commands: /status /plan /halt /unhalt /task /jobs /job /kill /logs /specs")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
