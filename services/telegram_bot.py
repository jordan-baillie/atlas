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

# ── Regime state emoji mapping ────────────────────────────────
REGIME_EMOJI: dict[str, str] = {
    "bull_risk_on": "🟢",
    "bull_risk_off": "🟡",
    "transition_uncertain": "🟠",
    "bear_risk_off": "🔴",
    "bear_capitulation": "⛔",
    "recovery_early": "🔵",
}

logger = logging.getLogger("atlas.telegram_bot")


def _load_prev_regime_state(trade_date: str, market_id: str) -> Optional[str]:
    """Return the regime_state from the most recent prior plan for this market.

    Used for transition detection — returns None if no prior plan is found or
    if it has no regime data.
    """
    plans_dir = PROJECT_ROOT / "plans"
    if not plans_dir.exists() or not market_id or not trade_date:
        return None

    try:
        # Find all plan files for this market that pre-date trade_date.
        plan_files = sorted(
            [
                f
                for f in plans_dir.glob(f"plan_{market_id}_*.json")
                if f.stem[len(f"plan_{market_id}_"):] < trade_date
            ],
            reverse=True,
        )
        if not plan_files:
            return None
        with open(plan_files[0]) as fh:
            prev = json.load(fh)
        return prev.get("regime_state") or None
    except Exception as e:
        logger.debug("Could not read previous regime state: %s", e)
        return None


def _format_regime_section(plan: dict, market_id: str = "") -> str:
    """Build an HTML regime context block for a plan notification.

    Returns an empty string when regime data is absent from the plan
    (e.g. regime_enabled=False or SP500-only fallback) so callers can
    append it safely without adding blank lines.
    """
    regime_state: Optional[str] = plan.get("regime_state")
    if not regime_state:
        return ""

    emoji = REGIME_EMOJI.get(regime_state, "⚪")
    universes: list = plan.get("active_universes") or []
    universes_str = ", ".join(universes) if universes else "sp500"
    sizing: float = plan.get("sizing_multiplier", 1.0)

    lines = [
        "",
        f"<b>📊 Regime:</b> {emoji} <b>{_esc(regime_state)}</b>",
        f"   Universes: {_esc(universes_str)}",
        f"   Sizing: {sizing:.1f}x",
    ]

    # Transition detection — compare with previous day's plan.
    trade_date: str = plan.get("trade_date", "")
    prev_state = _load_prev_regime_state(trade_date, market_id)
    if prev_state and prev_state != regime_state:
        prev_emoji = REGIME_EMOJI.get(prev_state, "⚪")
        lines.append(
            f"   ⚡ Changed from {prev_emoji} {_esc(prev_state)} (prev day)"
        )

    # Optional brief reasoning (truncated for Telegram readability).
    reasoning: str = plan.get("regime_reasoning", "") or ""
    if reasoning:
        if len(reasoning) > 120:
            reasoning = reasoning[:117] + "..."
        lines.append(f"   <i>{_esc(reasoning)}</i>")

    return "\n".join(lines)


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

    # Regime context (only present when regime_enabled=True in config)
    regime_section = _format_regime_section(plan, market_id)
    if regime_section:
        lines.append(regime_section)
        lines.append("")

    # Mode indicator
    config = get_active_config(market_id)
    mode = config.get("trading", {}).get("mode", "live")
    dry_run = config.get("trading", {}).get("live_safety", {}).get("dry_run_first", True)
    broker = config.get("trading", {}).get("broker", "alpaca")

    if not broker or broker not in ("alpaca",):
        mode_str = "📝 PAPER"
    elif mode == "passive":
        mode_str = "⏸ PASSIVE"
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
                "✅ Approve",
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

def _do_approve_only(trade_date: str, market_id: str) -> str:
    """Approve plan WITHOUT executing. Execution is deferred to the
    23:15 AEST cron (15 min before US market open) so LIMIT orders
    use fresh pre-open prices instead of sitting for hours.

    This runs in a thread (file I/O only, no broker).
    """
    config = get_active_config(market_id)

    plan_gen = TradePlanGenerator(None, config)
    plan = plan_gen.approve_plan(trade_date, market_id=market_id)
    if not plan:
        return "❌ No plan found for %s (%s)" % (trade_date, market_id)

    n_entries = len(plan.get("proposed_entries", []))
    n_exits = len(plan.get("proposed_exits", []))
    return (
        f"✅ <b>Plan APPROVED</b> ({market_id.upper()} {trade_date})\n"
        f"  Entries: {n_entries} | Exits: {n_exits}\n\n"
        f"⏰ <b>Orders will be submitted at 23:15 AEST</b> "
        f"(15 min before US market open)."
    )


def _do_approve_and_execute(trade_date: str, market_id: str) -> str:
    """Approve plan and execute via live broker. Returns result text.

    This runs in a thread (blocking broker I/O).
    Kept for manual/emergency use — daily flow uses _do_approve_only.
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

        # Mark plan as EXECUTED on disk (prevents re-execution)
        if not report.get("error"):
            try:
                plan["status"] = "EXECUTED"
                plan["executed_at"] = datetime.now().isoformat()
                plan_gen = TradePlanGenerator(None, config)
                plan_gen._save_plan(plan, trade_date)
            except Exception as _e:
                logger.warning("Failed to mark plan as EXECUTED: %s", _e)
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



async def cmd_halt_remediation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /halt_remediation — suspend auto-remediation via L2 kill-switch.

    Creates data/AUTO_REMEDIATION_HALT with reason + metadata.
    All auto-remediation systemd units with ConditionPathExists=! will refuse to start.

    Usage:
        /halt_remediation                 — halt with reason 'manual'
        /halt_remediation detected loop   — halt with custom reason
    """
    if not _authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Not authorized")
        return

    try:
        from core.remediation_kill_switch import halt as ks_halt

        reason = " ".join(ctx.args) if ctx.args else "manual"
        username = update.effective_user.username or "unknown"
        halt_path = ks_halt(
            reason,
            source=f"telegram:{username}",
        )
        await update.message.reply_text(
            f"🛑 <b>Auto-remediation HALTED</b>\n\n"
            f"Halt file: <code>{_esc(str(halt_path))}</code>\n"
            f"Reason: <code>{_esc(reason)}</code>\n"
            f"By: {_esc(username)}\n\n"
            f"systemd units with <code>ConditionPathExists=!</code> will refuse to start.\n\n"
            f"Use /resume_remediation to clear.",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception("cmd_halt_remediation error")
        await update.message.reply_text(f"❌ Failed to create halt file: {_esc(str(e))}", parse_mode="HTML")


async def cmd_resume_remediation(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /resume_remediation — clear the L2 AUTO_REMEDIATION_HALT file.

    Does NOT clear data/HALT (trading kill-switch — use /unhalt for that).
    """
    if not _authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Not authorized")
        return

    try:
        from core.remediation_kill_switch import resume as ks_resume

        cleared = ks_resume()
        if cleared:
            msg = "✅ <b>Auto-remediation RESUMED</b>\n\nHalt file removed. systemd units may now start."
        else:
            msg = "ℹ️ Auto-remediation was already running (no halt file found)."
        await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_resume_remediation error")
        await update.message.reply_text(f"❌ Error: {_esc(str(e))}", parse_mode="HTML")


async def cmd_approve_fix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle /approve_fix <fix_id> — approve a pending ASSIST-mode fix.

    Sets review_verdict='APPROVE' on the fix_attempts row so the automation
    pipeline can proceed past the human-review gate.

    Note: the fix_attempts.status CHECK constraint does not include 'approved'.
    Approval is recorded via review_verdict='APPROVE' + notes; the automation
    polls for this to continue the merge pipeline.

    Usage:
        /approve_fix 42
    """
    if not _authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Not authorized")
        return

    if not ctx.args:
        await update.message.reply_text(
            "Usage: <code>/approve_fix &lt;fix_id&gt;</code>\n\n"
            "Example: <code>/approve_fix 42</code>",
            parse_mode="HTML",
        )
        return

    fix_id_str = ctx.args[0]
    try:
        fix_id = int(fix_id_str)
    except ValueError:
        await update.message.reply_text(f"❌ fix_id must be an integer, got: <code>{_esc(fix_id_str)}</code>", parse_mode="HTML")
        return

    import sqlite3 as _sqlite3

    db_path = str(PROJECT_ROOT / "data" / "atlas.db")
    username = update.effective_user.username or "unknown"
    now_iso = datetime.now(timezone.utc).isoformat()

    _TERMINAL_STATUSES = ("merged", "reverted", "failed", "escalated", "aborted")

    try:
        with _sqlite3.connect(db_path, timeout=10) as conn:
            conn.row_factory = _sqlite3.Row
            row = conn.execute(
                "SELECT id, status, review_verdict, classification, notes FROM fix_attempts WHERE id = ?",
                (fix_id,),
            ).fetchone()

            if not row:
                await update.message.reply_text(
                    f"❌ Fix <code>{fix_id}</code> not found in fix_attempts",
                    parse_mode="HTML",
                )
                return

            current_status = row["status"]
            current_verdict = row["review_verdict"]

            if current_status in _TERMINAL_STATUSES:
                await update.message.reply_text(
                    f"ℹ️ Fix <code>{fix_id}</code> already has terminal status "
                    f"<code>{_esc(current_status)}</code> — no action taken.",
                    parse_mode="HTML",
                )
                return

            if current_verdict == "APPROVE":
                await update.message.reply_text(
                    f"ℹ️ Fix <code>{fix_id}</code> already has review_verdict=APPROVE "
                    f"(status: <code>{_esc(current_status)}</code>).",
                    parse_mode="HTML",
                )
                return

            # Record approval: set review_verdict + append to notes
            approval_note = f"[telegram approval by {username} at {now_iso}]"
            existing_notes = row["notes"] or ""
            new_notes = f"{existing_notes} {approval_note}".strip() if existing_notes else approval_note

            conn.execute(
                "UPDATE fix_attempts SET review_verdict = 'APPROVE', notes = ? WHERE id = ?",
                (new_notes, fix_id),
            )
            conn.commit()

        await update.message.reply_text(
            f"✅ Fix <code>{fix_id}</code> approved\n\n"
            f"Approver: {_esc(username)}\n"
            f"review_verdict → <code>APPROVE</code>\n"
            f"Status: <code>{_esc(current_status)}</code> (automation will advance)\n"
            f"Timestamp: <code>{_esc(now_iso)}</code>",
            parse_mode="HTML",
        )
    except _sqlite3.Error as e:
        logger.exception("cmd_approve_fix DB error for fix_id=%s", fix_id)
        await update.message.reply_text(f"❌ DB error: {_esc(str(e))}", parse_mode="HTML")
    except Exception as e:
        logger.exception("cmd_approve_fix unexpected error")
        await update.message.reply_text(f"❌ Unexpected error: check server logs.", parse_mode="HTML")


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
        # Approve only — execution deferred to 23:15 AEST cron
        await query.edit_message_text(
            query.message.text_html + f"\n\n⏳ <b>Approving [{market_id.upper()}]…</b>",
            parse_mode="HTML",
        )

        try:
            loop = asyncio.get_event_loop()
            result_text = await loop.run_in_executor(
                None,
                partial(_do_approve_only, trade_date, market_id),
            )
        except Exception as e:
            logger.error("Approval failed: %s", e, exc_info=True)
            result_text = "❌ <b>Approval failed</b> — check server logs for details."

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
            result_text = "❌ <b>Rejection failed</b> — check server logs for details."

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
            result_text = "❌ <b>Promotion failed</b> — check server logs for details."

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
        result_text = "❌ <b>Rollback failed</b> — check server logs for details."

    await query.edit_message_text(
        query.message.text_html.replace("⏳ <b>Rolling back…</b>", result_text),
        parse_mode="HTML",
    )

# ═══════════════════════════════════════════════════════════════
# Sweep auto-promotion approval callback handler
# ═══════════════════════════════════════════════════════════════

def _do_sweep_promote_approve(pending_id: str, market_id: str) -> str:
    """Execute pending sweep promotion in a thread (blocking I/O)."""
    from research.promoter import complete_pending_promotion
    result = complete_pending_promotion(pending_id)
    if result.get("promoted"):
        return (
            f"✅ <b>Promoted!</b>\n\n"
            f"Strategy: {_esc(result.get('strategy', '?'))}\n"
            f"Market: {_esc(result.get('market', '?').upper())}\n"
            f"Version: <code>{_esc(str(result.get('version', '?')))}</code>\n\n"
            f"Active config updated."
        )
    else:
        return f"❌ <b>Promotion failed</b>\n\n{_esc(result.get('reason', 'Unknown error'))}"


def _do_sweep_promote_reject(pending_id: str, market_id: str) -> str:
    """Reject pending sweep promotion in a thread (blocking I/O)."""
    from research.promoter import reject_pending_promotion
    result = reject_pending_promotion(pending_id, reason="User rejected via Telegram")
    if result.get("rejected"):
        return f"❌ <b>Rejected</b>: {_esc(result.get('strategy', '?'))}\nPromotion cancelled."
    else:
        return f"⚠️ <b>Rejection failed</b>: {_esc(result.get('reason', 'Unknown error'))}"


async def handle_sweep_promotion_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle Approve / Reject for sweep auto-promotion requests.

    Callback data format: sweep_promote:{pending_id}:{action}:{market}
    """
    query = update.callback_query
    await query.answer()

    if not _authorized(query.message.chat_id):
        await query.edit_message_reply_markup(reply_markup=None)
        return

    data = query.data  # e.g. "sweep_promote:abc123def456:approve:sp500"
    parts = data.split(":")
    if len(parts) < 4 or parts[0] != "sweep_promote":
        return

    pending_id = parts[1]
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
                partial(_do_sweep_promote_reject, pending_id, market_id),
            )
        except Exception as e:
            logger.error("Sweep promotion rejection failed: %s", e, exc_info=True)
            result_text = "❌ <b>Rejection failed</b> — check server logs."

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
                partial(_do_sweep_promote_approve, pending_id, market_id),
            )
        except Exception as e:
            logger.error("Sweep promotion approval failed: %s", e, exc_info=True)
            result_text = "❌ <b>Promotion failed</b> — check server logs."

        await query.message.reply_text(result_text, parse_mode="HTML")




# ═══════════════════════════════════════════════════════════════
# Plan notification buffer — collects per-market summaries for rollup
# ═══════════════════════════════════════════════════════════════

#: Directory where per-market JSON buffers are written before the daily rollup.
_BUFFER_DIR = PROJECT_ROOT / "data" / "plan_notifications_buffer"

#: All active markets expected in the rollup (used for display ordering).
_ROLLUP_MARKETS = ("sp500", "sector_etfs", "commodity_etfs")


def _write_plan_buffer(market_id: str, trade_date: str, data: dict) -> None:
    """Atomically write a per-market plan buffer file.

    Uses a .tmp + os.replace pattern so partial writes are never visible to
    a concurrently-running rollup.
    """
    _BUFFER_DIR.mkdir(parents=True, exist_ok=True)
    buf_path = _BUFFER_DIR / f"{market_id}_{trade_date}.json"
    tmp_path = buf_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2))
        os.replace(str(tmp_path), str(buf_path))
        logger.info("Plan buffer written: %s (status=%s)", buf_path.name, data.get("plan_status"))
    except Exception as e:
        logger.error("Failed to write plan buffer %s: %s", buf_path, e)
        tmp_path.unlink(missing_ok=True)


def _check_market_halt(market_id: str) -> tuple[bool, str]:
    """Return (halted, reason) for market_id.

    Checks (in order): global HALT files, then per-market DB state.
    """
    # Global halt files
    for halt_file in (PROJECT_ROOT / "data" / "HALT", PROJECT_ROOT / ".live_halt"):
        if halt_file.exists():
            reason = halt_file.read_text().strip() or "manual halt"
            return True, reason

    # Per-market halt from market_state table
    try:
        from db.atlas_db import get_db as _get_db
        with _get_db() as _db:
            _row = _db.execute(
                "SELECT halted, halt_reason FROM market_state WHERE market_id = ?",
                (market_id,),
            ).fetchone()
        if _row and int(_row[0]) == 1:
            return True, _row[1] or "market halted"
    except Exception as _e:
        logger.debug("Could not check halt state for %s: %s", market_id, _e)

    return False, ""


def _get_halt_diagnostics(market_id: str) -> dict:
    """Pull drawdown/snapshot diagnostics for a halted market.

    Reads:
    - ``brokers/state/live_<market>.json`` for HWM + equity_history
    - ``market_equity_history`` table in atlas.db for snapshot columns
    - ``config/active/<market>.json`` for dd_limit_pct + trading_mode

    All accesses are wrapped in try/except; any missing data → None.
    """
    diag: dict = {
        "hwm": None,
        "hwm_date": None,
        "current_eq_estimate": None,
        "dd_pct": None,
        "dd_limit_pct": None,
        "snap_allocated_equity": None,
        "snap_position_mv": None,
        "snap_cash_attributed": None,
        "snap_broker_equity": None,
        "snap_date": None,
    }

    # ── Live state file ────────────────────────────────────────────────────
    try:
        state_path = PROJECT_ROOT / "brokers" / "state" / f"live_{market_id}.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            diag["hwm"] = state.get("daily_high_water")
            diag["hwm_date"] = state.get("daily_high_water_date")
            eq_hist = state.get("equity_history") or []
            if eq_hist:
                last = eq_hist[-1]
                diag["current_eq_estimate"] = (
                    float(last.get("equity") or 0) or None
                )
    except Exception as _e:
        logger.debug("_get_halt_diagnostics: state file error for %s: %s", market_id, _e)

    # ── market_equity_history (DB snapshot) ────────────────────────────────
    try:
        from db.atlas_db import get_db as _get_db
        with _get_db() as _db:
            _row = _db.execute(
                """SELECT allocated_equity, position_mv, cash_attributed,
                          broker_equity, date
                   FROM market_equity_history
                   WHERE market_id = ?
                   ORDER BY date DESC, id DESC LIMIT 1""",
                (market_id,),
            ).fetchone()
        if _row:
            diag["snap_allocated_equity"] = _row[0]
            diag["snap_position_mv"] = _row[1]
            diag["snap_cash_attributed"] = _row[2]
            diag["snap_broker_equity"] = _row[3]
            diag["snap_date"] = _row[4]
    except Exception as _e:
        logger.debug("_get_halt_diagnostics: DB error for %s: %s", market_id, _e)

    # ── Config: dd_limit_pct + trading_mode ───────────────────────────────
    try:
        _cfg = get_active_config(market_id)
        _risk = _cfg.get("risk", {}) or {}
        _dd = _risk.get("max_daily_drawdown_pct")
        if _dd is not None:
            diag["dd_limit_pct"] = float(_dd) * 100
        diag["trading_mode"] = (_cfg.get("trading", {}) or {}).get("mode", "")
    except Exception as _e:
        logger.debug("_get_halt_diagnostics: config error for %s: %s", market_id, _e)

    # ── Parse dd_pct from halt_reason if available ────────────────────────
    # e.g. "daily_drawdown 49.78% on sector_etfs"
    try:
        halted, halt_reason_str = _check_market_halt(market_id)
        if halt_reason_str:
            import re as _re
            _m = _re.search(r"([\d.]+)%", halt_reason_str)
            if _m:
                diag["dd_pct"] = float(_m.group(1))
    except Exception as _e:
        logger.debug("_get_halt_diagnostics: halt_reason parse error for %s: %s", market_id, _e)

    return diag


def _maybe_write_halt_buffer(market_id: str, trade_date: str | None = None) -> None:
    """Write a HALTED buffer file if the market is currently halted.

    Called when the plan file is missing — lets the rollup report HALTED
    rather than silently omitting the market. Includes halt diagnostics
    and open positions from the live state file.
    """
    today = trade_date or datetime.now().strftime("%Y-%m-%d")
    halted, halt_reason = _check_market_halt(market_id)
    if not halted:
        return

    # Collect diagnostics (non-fatal)
    halt_diagnostics = _get_halt_diagnostics(market_id)
    trading_mode = halt_diagnostics.pop("trading_mode", "")

    # Collect open positions from live state file (non-fatal)
    open_positions_full: list[dict] = []
    try:
        state_path = PROJECT_ROOT / "brokers" / "state" / f"live_{market_id}.json"
        if state_path.exists():
            _state = json.loads(state_path.read_text())
            eq_hist = _state.get("equity_history") or []
            if eq_hist:
                last_entry = eq_hist[-1]
                for _p in last_entry.get("positions") or []:
                    open_positions_full.append({
                        "ticker": _p.get("ticker", "?"),
                        "shares": int(_p.get("shares", 0)),
                        "entry_price": float(_p.get("entry_price") or 0.0),
                        "current_price": float(_p.get("current_price") or 0.0),
                        "unrealized_pnl": float(_p.get("unrealized_pnl") or 0.0),
                        "unrealized_pnl_pct": float(_p.get("unrealized_pnl_pct") or 0.0),
                        "stop_price": None,
                        "take_profit": None,
                        "strategy": _p.get("strategy", "?"),
                    })
    except Exception as _e:
        logger.debug("_maybe_write_halt_buffer: open_positions error for %s: %s", market_id, _e)

    _write_plan_buffer(market_id, today, {
        "market_id": market_id,
        "trade_date": today,
        "plan_status": "HALTED",
        "halt_reason": halt_reason,
        "n_entries": 0,
        "n_approved": 0,
        "n_exits": 0,
        "total_risk_pct": 0.0,
        "total_position_value": 0.0,
        "leverage_pct": 0.0,
        "summary_lines": [],
        "rejection_reason": None,
        "halt_diagnostics": halt_diagnostics,      # NEW
        "trading_mode": trading_mode,              # NEW
        "open_positions_full": open_positions_full, # NEW
        "written_at": datetime.now(timezone.utc).isoformat(),
    })
    logger.info("HALTED buffer written for %s: %s", market_id, halt_reason)


def _cleanup_old_buffers(days: int = 7) -> None:
    """Delete buffer files and sentinel files older than *days* days."""
    if not _BUFFER_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - (days * 86_400)
    for fp in list(_BUFFER_DIR.iterdir()):
        if fp.is_file() and fp.stat().st_mtime < cutoff:
            try:
                fp.unlink()
                logger.debug("Cleaned up old buffer: %s", fp.name)
            except Exception as _e:
                logger.warning("Could not clean buffer %s: %s", fp.name, _e)


# ═══════════════════════════════════════════════════════════════
# External API — called by cron/scripts to send plan for approval
# ═══════════════════════════════════════════════════════════════

def send_plan_for_approval(
    plan_path: Optional[str] = None,
    market_id: str = "sp500",
) -> bool:
    """Buffer plan summary for later rollup; auto-approve if configured.

    **No longer sends a Telegram message directly.** The Telegram notification
    is deferred to ``send_plan_rollup()``, which consolidates all 3 markets
    into one daily message (called at 19:45 AEST by cron).

    The **auto-approval side-effect is preserved**: if ``auto_approve=True`` in
    the active config, the plan is approved here so ``execute_approved.py`` can
    pick it up at 23:15 AEST.

    Writes ``data/plan_notifications_buffer/<market>_<date>.json``.
    """
    # Resolve plan path
    if plan_path is None:
        today = datetime.now().strftime("%Y-%m-%d")
        plan_path = str(PROJECT_ROOT / f"plans/plan_{market_id}_{today}.json")

    plan_file = Path(plan_path)
    if not plan_file.exists():
        logger.error("Plan file not found: %s", plan_path)
        # Best-effort: write a HALTED buffer if market is actually halted
        _maybe_write_halt_buffer(market_id)
        return False

    with open(plan_file) as _f:
        plan = json.load(_f)

    trade_date = plan.get("trade_date", datetime.now().strftime("%Y-%m-%d"))
    entries = plan.get("proposed_entries", [])
    exits = plan.get("proposed_exits", [])

    # ── Empty plan — write EMPTY buffer, no Telegram ─────────────────────────
    if not entries and not exits:
        logger.info(
            "send_plan_for_approval: %s plan empty (0 entries, 0 exits) — writing EMPTY buffer",
            market_id,
        )
        _write_plan_buffer(market_id, trade_date, {
            "market_id": market_id,
            "trade_date": trade_date,
            "plan_status": "EMPTY",
            "halt_reason": None,
            "n_entries": 0,
            "n_approved": 0,
            "n_exits": 0,
            "total_risk_pct": 0.0,
            "total_position_value": 0.0,
            "leverage_pct": 0.0,
            "summary_lines": [],
            "rejection_reason": None,
            "written_at": datetime.now(timezone.utc).isoformat(),
        })
        return True  # success — nothing to send is the correct outcome

    # ── Auto-approve if configured (CRITICAL side-effect) ────────────────────
    config = get_active_config(market_id)
    auto_approve = config.get("trading", {}).get("auto_approve", False)

    if auto_approve:
        plan_gen = TradePlanGenerator(None, config)
        approved = plan_gen.approve_plan(trade_date, market_id=market_id)
        if approved:
            logger.info("Auto-approved plan for %s (%s)", trade_date, market_id)
            # Re-read so plan_status reflects the freshly-written APPROVED status
            with open(plan_file) as _f:
                plan = json.load(_f)
        else:
            logger.warning("Auto-approve failed for %s — plan remains PENDING", trade_date)

    # ── Build buffer payload ──────────────────────────────────────────────────
    plan_status = plan.get("status", "PENDING")
    if plan_status not in ("APPROVED", "REJECTED", "PENDING"):
        plan_status = "PENDING"

    n_entries = len(entries)
    n_exits = len(exits)
    n_approved = n_entries if plan_status == "APPROVED" else 0

    risk = plan.get("risk_summary", {})
    snap = plan.get("portfolio_snapshot", {})

    # ─── Existing summary_lines (kept for backwards compat) ────────────────
    summary_lines: list[str] = []
    for _e in entries[:8]:
        _ticker = _e.get("ticker", "?")
        _size = _e.get("position_size", 0)
        _price = _e.get("entry_price", 0)
        _stop = _e.get("stop_price")
        if _stop:
            summary_lines.append(f"{_ticker} × {_size} @ ${_price:.2f} → stop ${_stop:.2f}")
        else:
            summary_lines.append(f"{_ticker} × {_size} @ ${_price:.2f}")

    # ─── NEW: full entry detail for rich rollup ────────────────────────────
    entries_full: list[dict] = []
    for _e in entries:
        entries_full.append({
            "ticker": _e.get("ticker", "?"),
            "side": "BUY",  # entries are always buys; shorts not supported
            "qty": int(_e.get("position_size", 0)),
            "entry_price": float(_e.get("entry_price") or 0.0),
            "stop_price": (float(_e["stop_price"]) if _e.get("stop_price") is not None else None),
            "take_profit": (float(_e["take_profit"]) if _e.get("take_profit") is not None else None),
            "risk_amount": float(_e.get("risk_amount") or 0.0),
            "position_value": float(_e.get("position_value") or 0.0),
            "strategy": _e.get("strategy", "?"),
            "sector": _e.get("sector", ""),
            "confidence": float(_e.get("confidence") or 0.0),
        })

    exits_full: list[dict] = []
    for _x in exits:
        exits_full.append({
            "ticker": _x.get("ticker", "?"),
            "side": "SELL",
            "exit_price": float(_x.get("exit_price") or 0.0),
            "reason": _x.get("exit_reason") or _x.get("reason", ""),
            "current_pnl": float(_x.get("current_pnl") or _x.get("unrealized_pnl") or 0.0),
            "shares": int(_x.get("shares", 0)),
            "strategy": _x.get("strategy", "?"),
        })

    open_positions_full: list[dict] = []
    for _p in plan.get("open_positions", []):
        open_positions_full.append({
            "ticker": _p.get("ticker", "?"),
            "shares": int(_p.get("shares", 0)),
            "entry_price": float(_p.get("entry_price") or 0.0),
            "current_price": float(_p.get("current_price") or 0.0),
            "unrealized_pnl": float(_p.get("unrealized_pnl") or 0.0),
            "unrealized_pnl_pct": float(_p.get("unrealized_pnl_pct") or 0.0),
            "stop_price": (float(_p["stop_price"]) if _p.get("stop_price") is not None else None),
            "take_profit": (float(_p["take_profit"]) if _p.get("take_profit") is not None else None),
            "strategy": _p.get("strategy", "?"),
        })

    snap = plan.get("portfolio_snapshot", {}) or {}
    equity_snapshot = {
        "equity": float(snap.get("equity") or 0.0),
        "cash": float(snap.get("cash") or 0.0),
        "total_pnl": float(snap.get("total_pnl") or 0.0),
        "total_pnl_pct": float(snap.get("total_pnl_pct") or 0.0),
    }

    trading_mode = (config.get("trading", {}) or {}).get("mode", "")

    rejection_reason: Optional[str] = (
        plan.get("rejection_reason") if plan_status == "REJECTED" else None
    )

    _write_plan_buffer(market_id, trade_date, {
        "market_id": market_id,
        "trade_date": trade_date,
        "plan_status": plan_status,
        "halt_reason": None,
        "n_entries": n_entries,
        "n_approved": n_approved,
        "n_exits": n_exits,
        "total_risk_pct": float(risk.get("risk_pct_of_equity") or 0),
        "total_position_value": float(risk.get("total_proposed_cost") or 0),
        "leverage_pct": float(risk.get("portfolio_exposure_pct") or 0),
        "summary_lines": summary_lines,
        "entries_full": entries_full,                # NEW
        "exits_full": exits_full,                    # NEW
        "open_positions_full": open_positions_full,  # NEW
        "equity_snapshot": equity_snapshot,          # NEW
        "trading_mode": trading_mode,                # NEW
        "rejection_reason": rejection_reason,
        "written_at": datetime.now(timezone.utc).isoformat(),
    })
    return True


def _fmt_entry_line(e: dict) -> str:
    """Format one entry dict as a fixed-width detail line for Telegram HTML."""
    ticker = _esc(e.get("ticker", "?"))
    side = _esc(e.get("side", "BUY"))
    qty = e.get("qty", 0)
    entry = e.get("entry_price", 0.0)
    stop = e.get("stop_price")
    tp = e.get("take_profit")
    risk = e.get("risk_amount", 0.0)
    strategy = _esc(e.get("strategy", "?"))

    stop_str = "no stop"
    if stop is not None and entry:
        stop_pct = (stop - entry) / entry * 100
        stop_str = f"stop ${stop:.2f} ({stop_pct:+.1f}%)"

    tp_str = "no target"
    if tp is not None and entry:
        tp_pct = (tp - entry) / entry * 100
        tp_str = f"target ${tp:.2f} ({tp_pct:+.1f}%)"

    return (
        f"  {ticker:<6} {side}  {qty:>3} @ ${entry:<10.2f}"
        f"{stop_str:<26}{tp_str:<26}  risk ${risk:.2f}   {strategy}"
    )


def _fmt_exit_line(x: dict) -> str:
    """Format one planned exit dict as a detail line."""
    ticker = _esc(x.get("ticker", "?"))
    shares = x.get("shares", 0)
    price = x.get("exit_price", 0.0)
    reason = _esc(x.get("reason", ""))
    pnl = x.get("current_pnl", 0.0)
    pnl_str = f"PnL {pnl:+.2f}" if pnl else ""
    return f"  {ticker} SELL {shares}sh @ ${price:.2f}  {reason}  {pnl_str}".rstrip()


def _fmt_open_position_line(p: dict) -> str:
    """Format one open position as a compact summary."""
    ticker = _esc(p.get("ticker", "?"))
    shares = p.get("shares", 0)
    pnl = p.get("unrealized_pnl", 0.0)
    pnl_pct = p.get("unrealized_pnl_pct", 0.0)
    stop = p.get("stop_price")
    pnl_str = f"{pnl:+.2f} {pnl_pct:+.2f}%"
    stop_str = f", stop ${stop:.2f}" if stop is not None else ""
    return f"{ticker} {shares}sh, {pnl_str}{stop_str}"


def send_plan_rollup() -> bool:
    """Send ONE consolidated Telegram message summarising today's plans.

    Reads all ``data/plan_notifications_buffer/*_<today>.json`` files written
    by ``send_plan_for_approval()`` and sends a single HTML message.

    Idempotent: skips (returns True) if a rollup was already sent today.
    Cleans up buffer files older than 7 days on each successful send.

    Format: rich per-entry detail with halt diagnostics + passive-mode warning.
    Falls back to summary_lines style if buffer lacks entries_full.
    Truncates at 4000 chars (Telegram cap is 4096).
    """
    import urllib.request
    import urllib.error

    try:
        token, chat_id = _load_credentials()
    except ValueError as _e:
        logger.error("Cannot send rollup: %s", _e)
        return False

    today = datetime.now().strftime("%Y-%m-%d")

    # ── Idempotency guard ─────────────────────────────────────────────────────
    _BUFFER_DIR.mkdir(parents=True, exist_ok=True)
    sentinel = _BUFFER_DIR / f"rollup_sent_{today}.txt"
    if sentinel.exists():
        logger.info("Plan rollup already sent today (%s) — skipping", today)
        return True

    # ── Collect today's buffer files ──────────────────────────────────────────
    buffers_by_market: dict[str, dict] = {}
    for buf_path in _BUFFER_DIR.glob(f"*_{today}.json"):
        try:
            with open(buf_path) as _f:
                buf = json.load(_f)
            market = buf.get("market_id") or buf_path.stem.rsplit("_", 3)[0]
            buffers_by_market[market] = buf
        except Exception as _e:
            logger.warning("Could not read buffer %s: %s", buf_path, _e)

    # ── Build rich message ────────────────────────────────────────────────────
    def _render_markets(max_entries_per_market: int | None = None) -> tuple[str, int, float]:
        """Render all market blocks. Returns (body_lines_text, n_approved, total_risk)."""
        n_approved_plans = 0
        total_risk_pct = 0.0
        sections: list[str] = []

        ordered = list(_ROLLUP_MARKETS) + [
            m for m in sorted(buffers_by_market) if m not in _ROLLUP_MARKETS
        ]
        for market in ordered:
            if market not in buffers_by_market:
                continue
            buf = buffers_by_market[market]

            status = buf.get("plan_status", "UNKNOWN")
            n_entries: int = buf.get("n_entries", 0)
            n_exits: int = buf.get("n_exits", 0)
            leverage: float = buf.get("leverage_pct", 0)
            halt_reason: str = buf.get("halt_reason") or ""
            rejection_reason: str = buf.get("rejection_reason") or ""
            summary_lines: list[str] = buf.get("summary_lines", [])
            risk_pct: float = buf.get("total_risk_pct", 0)
            entries_full: list[dict] = buf.get("entries_full", [])
            exits_full: list[dict] = buf.get("exits_full", [])
            open_positions_full: list[dict] = buf.get("open_positions_full", [])
            equity_snap: dict = buf.get("equity_snapshot", {}) or {}
            trading_mode: str = buf.get("trading_mode", "")
            halt_diag: dict = buf.get("halt_diagnostics", {}) or {}

            mkt_esc = _esc(market)
            block: list[str] = ["──────"]

            # ── Status header line ───────────────────────────────────────
            if status == "EMPTY":
                block.append(f"📋 <b>{mkt_esc}</b> — no signals today")

            elif status == "HALTED":
                halt_short = _esc((halt_reason[:80] + "…") if len(halt_reason) > 80 else halt_reason)
                block.append(f"🛑 <b>{mkt_esc}</b> — HALTED · {halt_short}")
                block.append("")

                # Halt diagnostics block
                block.append(f"  Halt reason: {_esc(halt_reason)}")

                dd_limit = halt_diag.get("dd_limit_pct")
                if dd_limit is not None:
                    block.append(f"  Configured limit: {dd_limit:.2f}% daily drawdown")

                hwm = halt_diag.get("hwm")
                hwm_date = halt_diag.get("hwm_date")
                if hwm is not None:
                    hwm_date_str = f" (set {_esc(hwm_date)})" if hwm_date else ""
                    block.append(f"  HWM today: ${hwm:,.2f}{hwm_date_str}")

                cur_eq = halt_diag.get("current_eq_estimate")
                snap_eq = halt_diag.get("snap_allocated_equity")
                snap_date = halt_diag.get("snap_date")
                if cur_eq is not None or snap_eq is not None:
                    parts_eq: list[str] = []
                    if cur_eq is not None:
                        parts_eq.append(f"${cur_eq:,.2f} (live state)")
                    if snap_eq is not None:
                        snap_date_str = f" {_esc(snap_date)}" if snap_date else ""
                        parts_eq.append(f"${snap_eq:,.2f} (DB snapshot{snap_date_str})")
                    block.append(f"  Current equity estimate: {' / '.join(parts_eq)}")

                # ⚠️ Stale HWM warning: HWM > 1.5× starting_equity
                try:
                    _cfg_halted = get_active_config(market)
                    _starting = (
                        (_cfg_halted.get("risk", {}) or {}).get("starting_equity")
                        or _cfg_halted.get("starting_equity")
                    )
                    if _starting and hwm is not None and hwm > float(_starting) * 1.5:
                        ratio = hwm / float(_starting)
                        block.append(
                            f"  ⚠️ HWM looks stale — {mkt_esc} starting_equity is"
                            f" ${float(_starting):,.0f} but HWM is ${hwm:,.2f}"
                            f" ({ratio:.1f}× starting)"
                        )
                except Exception:
                    pass

                if trading_mode:
                    block.append(
                        f"  Trading mode: {_esc(trading_mode)}"
                        + (" (would not execute even if unhalted)" if trading_mode == "passive" else "")
                    )

                # Open positions from halt buffer
                if open_positions_full:
                    pos_strs = ", ".join(
                        f"{_esc(p.get('ticker','?'))} {p.get('shares',0)}sh"
                        for p in open_positions_full
                    )
                    block.append(f"  Open positions: {pos_strs}")

            elif status == "APPROVED":
                n_approved_plans += 1
                total_risk_pct += risk_pct
                lev_str = f" · {leverage:.0f}% leverage" if leverage else ""
                risk_str = f" · {risk_pct:.2f}% equity at risk" if risk_pct else ""
                block.append(
                    f"📋 <b>{mkt_esc}</b> — APPROVED · {n_entries} {'entry' if n_entries == 1 else 'entries'}{lev_str}{risk_str}"
                )
                # Equity snapshot
                if equity_snap.get("equity"):
                    eq = equity_snap["equity"]
                    cash = equity_snap.get("cash", 0)
                    tpnl = equity_snap.get("total_pnl", 0)
                    tpnl_pct = equity_snap.get("total_pnl_pct", 0)
                    pnl_sign = "+" if tpnl >= 0 else ""
                    block.append(
                        f"Equity ${eq:,.2f} · Cash ${cash:,.2f} · PnL {pnl_sign}${tpnl:.2f} ({pnl_sign}{tpnl_pct:.2f}%)"
                    )
                block.append("")

                if entries_full:
                    block.append(f"ENTRIES ({n_entries}):")
                    shown = entries_full if max_entries_per_market is None else entries_full[:max_entries_per_market]
                    for _e in shown:
                        block.append(_fmt_entry_line(_e))
                    if max_entries_per_market and len(entries_full) > max_entries_per_market:
                        block.append(f"  ... +{len(entries_full) - max_entries_per_market} more")
                elif summary_lines:
                    # Backwards compat: no entries_full
                    block.append(f"ENTRIES ({n_entries}):")
                    for sl in (summary_lines[:max_entries_per_market] if max_entries_per_market else summary_lines):
                        block.append(f"  {_esc(sl)}")
                    if max_entries_per_market and len(summary_lines) > max_entries_per_market:
                        block.append(f"  ... +{len(summary_lines) - max_entries_per_market} more")

                # Exits
                if exits_full:
                    block.append("")
                    block.append(f"EXITS ({n_exits}):")
                    for _x in exits_full:
                        block.append(_fmt_exit_line(_x))
                else:
                    block.append("EXITS: none — riding existing protective stops")

                # Open positions
                if open_positions_full:
                    pos_strs = ", ".join(_fmt_open_position_line(p) for p in open_positions_full)
                    block.append(f"OPEN: {pos_strs}")

            elif status == "REJECTED":
                rej_trunc = (rejection_reason[:60] + "…") if len(rejection_reason) > 60 else rejection_reason
                rej_suffix = f" — {_esc(rej_trunc)}" if rej_trunc else ""
                entry_str = f"{n_entries} {'entry' if n_entries == 1 else 'entries'}"
                extra = f" · lvg {leverage:.0f}%" if leverage else ""
                block.append(
                    f"❌ <b>{mkt_esc}</b> — REJECTED · {entry_str}{extra}{rej_suffix}"
                )

            else:  # PENDING — awaiting manual approval
                lev_str = f" · {leverage:.0f}% leverage" if leverage else ""
                entry_str = f"{n_entries} {'entry' if n_entries == 1 else 'entries'}" if n_entries else "no signals"
                block.append(
                    f"⏳ <b>{mkt_esc}</b> — PENDING · {entry_str}{lev_str}"
                )
                if equity_snap.get("equity"):
                    eq = equity_snap["equity"]
                    cash = equity_snap.get("cash", 0)
                    tpnl = equity_snap.get("total_pnl", 0)
                    tpnl_pct = equity_snap.get("total_pnl_pct", 0)
                    pnl_sign = "+" if tpnl >= 0 else ""
                    block.append(
                        f"Equity ${eq:,.2f} · Cash ${cash:,.2f} · PnL {pnl_sign}${tpnl:.2f} ({pnl_sign}{tpnl_pct:.2f}%)"
                    )
                block.append("")

                if entries_full:
                    shown = entries_full if max_entries_per_market is None else entries_full[:max_entries_per_market]
                    for _e in shown:
                        block.append(_fmt_entry_line(_e))
                    if max_entries_per_market and len(entries_full) > max_entries_per_market:
                        block.append(f"  ... +{len(entries_full) - max_entries_per_market} more")
                elif summary_lines:
                    for sl in (summary_lines[:max_entries_per_market] if max_entries_per_market else summary_lines):
                        block.append(f"  {_esc(sl)}")
                    if max_entries_per_market and len(summary_lines) > max_entries_per_market:
                        block.append(f"  ... +{len(summary_lines) - max_entries_per_market} more")

                if trading_mode == "passive":
                    block.append("")
                    block.append(
                        f"  ⚠️ Trading mode is 'passive' — these will NOT execute."
                        f" execute_approved.py will skip."
                    )

                # Open positions
                if open_positions_full:
                    pos_strs = ", ".join(_fmt_open_position_line(p) for p in open_positions_full)
                    block.append(f"  Open positions: {pos_strs}")

            sections.append("\n".join(block))

        return "\n".join(sections), n_approved_plans, total_risk_pct

    # ── First render attempt (full detail) ────────────────────────────────────
    body, n_approved_plans, total_risk_pct = _render_markets()

    if not body:
        body = "No plans generated today."

    # Summary line
    n_halted = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "HALTED")
    n_pending = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "PENDING")
    n_empty = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "EMPTY")
    n_rejected = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "REJECTED")

    summary_parts: list[str] = []
    if n_approved_plans:
        summary_parts.append(f"{n_approved_plans} approved")
    if n_pending:
        summary_parts.append(f"{n_pending} pending")
    if n_halted:
        summary_parts.append(f"{n_halted} halted")
    if n_empty:
        summary_parts.append(f"{n_empty} empty")
    if n_rejected:
        summary_parts.append(f"{n_rejected} rejected")
    if not summary_parts:
        summary_parts.append("0 plans")

    risk_str = f" · total approved risk {total_risk_pct:.2f}% equity" if total_risk_pct else ""
    summary_line = f"<b>Summary:</b> {', '.join(summary_parts)}{risk_str}"

    lines = [f"📋 <b>Daily Plans — {_esc(today)}</b>", ""]
    lines.append(body)
    lines.append("")
    lines.append(summary_line)
    msg_text = "\n".join(lines)

    # ── 4000-char cap: truncate to 4 entries/market if needed ─────────────────
    if len(msg_text) > 4000:
        logger.warning(
            "Plan rollup message too long (%d chars) — truncating to 4 entries/market",
            len(msg_text),
        )
        body2, n_approved_plans, total_risk_pct = _render_markets(max_entries_per_market=4)
        n_halted = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "HALTED")
        n_pending = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "PENDING")
        n_empty = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "EMPTY")
        n_rejected = sum(1 for b in buffers_by_market.values() if b.get("plan_status") == "REJECTED")
        summary_parts2: list[str] = []
        if n_approved_plans:
            summary_parts2.append(f"{n_approved_plans} approved")
        if n_pending:
            summary_parts2.append(f"{n_pending} pending")
        if n_halted:
            summary_parts2.append(f"{n_halted} halted")
        if n_empty:
            summary_parts2.append(f"{n_empty} empty")
        if n_rejected:
            summary_parts2.append(f"{n_rejected} rejected")
        if not summary_parts2:
            summary_parts2.append("0 plans")
        risk_str2 = f" · total approved risk {total_risk_pct:.2f}% equity" if total_risk_pct else ""
        summary_line2 = f"<b>Summary:</b> {', '.join(summary_parts2)}{risk_str2}"
        lines2 = [f"📋 <b>Daily Plans — {_esc(today)}</b>", ""]
        lines2.append(body2 or "No plans generated today.")
        lines2.append("")
        lines2.append(summary_line2)
        msg_text = "\n".join(lines2)

    # ── Final fallback: summary_lines only ────────────────────────────────────
    if len(msg_text) > 4000:
        logger.error(
            "Plan rollup still too long after truncation (%d chars) — falling back to summary_lines",
            len(msg_text),
        )
        fb_lines: list[str] = [f"📋 <b>Daily Plans — {_esc(today)}</b>", ""]
        n_approved_fb = 0
        total_risk_fb = 0.0
        ordered_fb = list(_ROLLUP_MARKETS) + [
            m for m in sorted(buffers_by_market) if m not in _ROLLUP_MARKETS
        ]
        for market in ordered_fb:
            if market not in buffers_by_market:
                continue
            buf = buffers_by_market[market]
            status = buf.get("plan_status", "UNKNOWN")
            n_ent: int = buf.get("n_entries", 0)
            lev: float = buf.get("leverage_pct", 0)
            hr: str = buf.get("halt_reason") or ""
            rr: str = buf.get("rejection_reason") or ""
            sl: list[str] = buf.get("summary_lines", [])
            rp: float = buf.get("total_risk_pct", 0)
            mkt_e = _esc(market)
            if status == "EMPTY":
                fb_lines.append(f"<b>{mkt_e}</b>: no signals")
            elif status == "HALTED":
                hr_s = (hr[:60] + "…") if len(hr) > 60 else hr
                fb_lines.append(f"<b>{mkt_e}</b>: 🛑 HALTED — {_esc(hr_s)}")
            elif status == "APPROVED":
                n_approved_fb += 1
                total_risk_fb += rp
                if n_ent == 1 and sl:
                    fb_lines.append(f"<b>{mkt_e}</b>: {n_ent} entry — {_esc(sl[0])} ✅")
                else:
                    parts_fb = []
                    if n_ent:
                        parts_fb.append(f"{n_ent} {'entry' if n_ent == 1 else 'entries'}")
                    if lev:
                        parts_fb.append(f"lvg {lev:.0f}%")
                    fb_lines.append(f"<b>{mkt_e}</b>: {', '.join(parts_fb)} ✅ APPROVED")
            elif status == "REJECTED":
                rr_t = (rr[:60] + "…") if len(rr) > 60 else rr
                rr_sfx = f" — {_esc(rr_t)}" if rr_t else ""
                e_s = f"{n_ent} {'entry' if n_ent == 1 else 'entries'}"
                ex = f", lvg {lev:.0f}%" if lev else ""
                fb_lines.append(f"<b>{mkt_e}</b>: {e_s}{ex}, 0 approved ❌ REJECTED{rr_sfx}")
            else:
                parts_fb = []
                if n_ent:
                    parts_fb.append(f"{n_ent} {'entry' if n_ent == 1 else 'entries'}")
                fb_lines.append(f"<b>{mkt_e}</b>: {', '.join(parts_fb) or 'no signals'} ⏳ PENDING")
        fb_lines.append("")
        if n_approved_fb:
            r_str = f", {total_risk_fb:.2f}% equity" if total_risk_fb else ""
            fb_lines.append(f"<b>Total approved:</b> {n_approved_fb} plan{'s' if n_approved_fb > 1 else ''}{r_str}")
        else:
            fb_lines.append("<b>Total approved:</b> 0 plans today")
        msg_text = "\n".join(fb_lines)

    # ── Send via raw HTTP ─────────────────────────────────────────────────────
    payload = {
        "chat_id": chat_id,
        "text": msg_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body_resp = json.loads(resp.read())
            if body_resp.get("ok"):
                logger.info(
                    "Plan rollup sent for %s (%d markets)", today, len(buffers_by_market)
                )
                sentinel.write_text(
                    f"sent at {datetime.now(timezone.utc).isoformat()}\n"
                    f"markets: {list(buffers_by_market.keys())}\n"
                )
                _cleanup_old_buffers(days=7)
                return True
            logger.warning("Telegram API returned ok=false: %s", body_resp)
            return False
    except urllib.error.HTTPError as _e:
        body_resp = _e.read().decode(errors="replace")
        logger.error("Telegram HTTP %d: %s", _e.code, body_resp)
        return False
    except Exception as _e:
        logger.error("Failed to send rollup: %s", _e)
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
    except Exception as e:
        logger.debug("Time format parse failed: %s", e)
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
    except Exception as e:
        logger.debug("Job elapsed time calculation failed: %s", e)
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
    app.add_handler(CommandHandler("halt_remediation", cmd_halt_remediation))
    app.add_handler(CommandHandler("resume_remediation", cmd_resume_remediation))
    app.add_handler(CommandHandler("approve_fix", cmd_approve_fix))

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

    # Callback handler for sweep auto-promotion approval buttons
    app.add_handler(CallbackQueryHandler(
        handle_sweep_promotion_callback,
        pattern=r"^sweep_promote:.+:(approve|reject):\w+$",
    ))

    async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Suppress transient NetworkError tracebacks; log all other errors normally."""
        from telegram.error import NetworkError
        err = context.error
        if isinstance(err, NetworkError):
            logger.warning("Transient Telegram network error (auto-recovering): %s", err)
            return
        logger.error("Unhandled telegram bot error", exc_info=err)

    app.add_error_handler(_error_handler)

    logger.info("Bot polling started. Commands: /status /plan /halt /unhalt /halt_remediation /resume_remediation /approve_fix /task /jobs /job /kill /logs /specs")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
