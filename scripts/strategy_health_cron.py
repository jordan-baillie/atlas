"""Weekly strategy health check — run Saturday 09:00 AEST.

Compares live strategy performance against backtest expectations and sends
a Telegram summary with per-strategy traffic lights.

Usage:
    python3 scripts/strategy_health_cron.py --market sp500
    python3 scripts/strategy_health_cron.py --market sp500 --dry-run

Cron schedule (AEST):
    00 9 * * 6   /root/atlas/scripts/pi-cron.sh health-check
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from utils.config import get_active_config
from monitor.strategy_health import (
    StrategyHealthMonitor,
    HealthReport,
    HEALTHY,
    WARNING,
    DEGRADED,
    INSUFFICIENT_DATA,
    DEGRADED_CONSECUTIVE_WEEKS,
)

logger = logging.getLogger(__name__)


# ── Traffic light icons ───────────────────────────────────────────────────────

_STATUS_ICON = {
    HEALTHY: "🟢",
    WARNING: "🟡",
    DEGRADED: "🔴",
    INSUFFICIENT_DATA: "⚪",
}


def _fmt_float(value, fmt=".3f") -> str:
    if value is None:
        return "N/A"
    try:
        return format(float(value), fmt)
    except (TypeError, ValueError):
        return "N/A"


def _build_telegram_message(report: HealthReport) -> str:
    """Format the health report into a Telegram HTML message."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    market = report.market_id.upper()

    lines = [
        f"📊 <b>Strategy Health Report [{market}]</b>",
        f"<i>{now}</i>",
        "",
        f"<b>Summary:</b>",
        f"  🟢 Healthy: {report.summary.get(HEALTHY, 0)}"
        f"  🟡 Warning: {report.summary.get(WARNING, 0)}"
        f"  🔴 Degraded: {report.summary.get(DEGRADED, 0)}"
        f"  ⚪ No data: {report.summary.get(INSUFFICIENT_DATA, 0)}",
        "",
        "<b>Per-Strategy:</b>",
    ]

    for a in report.assessments:
        icon = _STATUS_ICON.get(a.status, "❓")
        sharpe_str = _fmt_float(a.live_sharpe)
        bt_str = _fmt_float(a.backtest_sharpe)
        count_str = str(a.live_trade_count)

        if a.status == INSUFFICIENT_DATA:
            lines.append(
                f"  {icon} <b>{a.strategy}</b> — {count_str} trades"
                f" (need {10})"
            )
        else:
            lines.append(
                f"  {icon} <b>{a.strategy}</b>"
                f" | live Sharpe: {sharpe_str}"
                f" | bt: {bt_str}"
                f" | {count_str} trades"
            )

    if report.alerts:
        lines.append("")
        lines.append("<b>⚠️ Alerts:</b>")
        for alert in report.alerts:
            icon = _STATUS_ICON.get(alert.status, "❓")
            lines.append(f"  {icon} {alert.message}")

    return "\n".join(lines)


def _build_degradation_alert_message(report: HealthReport) -> str:
    """Build a high-priority Telegram message for 3+ consecutive DEGRADED strategies."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    market = report.market_id.upper()

    degraded_alerts = [
        a for a in report.alerts
        if a.status == DEGRADED and a.consecutive_degraded_weeks >= DEGRADED_CONSECUTIVE_WEEKS
    ]

    if not degraded_alerts:
        return ""

    lines = [
        f"🚨 <b>STRATEGY DEGRADATION ALERT [{market}]</b>",
        f"<i>{now}</i>",
        "",
        f"The following strategies have been DEGRADED for "
        f"{DEGRADED_CONSECUTIVE_WEEKS}+ consecutive weeks:",
        "",
    ]

    for alert in degraded_alerts:
        lines.append(
            f"  🔴 <b>{alert.strategy}</b> — "
            f"{alert.consecutive_degraded_weeks} consecutive degraded reports"
        )
        # Find the assessment for this strategy
        for assessment in report.assessments:
            if assessment.strategy == alert.strategy:
                lines.append(
                    f"     Live Sharpe: {_fmt_float(assessment.live_sharpe)} | "
                    f"Backtest Sharpe: {_fmt_float(assessment.backtest_sharpe)}"
                )
                break

    lines.extend([
        "",
        "<b>Recommended actions:</b>",
        "  1. Review recent live trades for this strategy",
        "  2. Check for market regime changes",
        "  3. Consider disabling strategy pending review",
        "  4. Run: <code>python3 scripts/strategy_health_cron.py --market "
        + report.market_id + "</code>",
    ])

    return "\n".join(lines)


def _fmt_lifecycle_transitions(transitions: list) -> str:
    """Format lifecycle transitions for appending to a Telegram message."""
    if not transitions:
        return ""
    lines = [
        "",
        "",
        "<b>🔄 Lifecycle Transitions:</b>",
    ]
    _STATE_ICON = {
        "ACTIVE":     "🟢",
        "WATCH":      "🟡",
        "PROBATION":  "🟠",
        "SUSPENDED":  "🔴",
        "RAMP_UP":    "🔵",
    }
    for t in transitions:
        from_icon = _STATE_ICON.get(t["from"], "❓")
        to_icon   = _STATE_ICON.get(t["to"],   "❓")
        lines.append(
            f"  <b>{t['strategy']}</b>: "
            f"{from_icon} {t['from']} → {to_icon} {t['to']}"
        )
    return "\n".join(lines)


def run_health_check(market_id: str, dry_run: bool = False) -> HealthReport:
    """Load config, run health check, save report, send Telegram notifications.

    Args:
        market_id: Market to check (e.g. 'sp500', 'asx').
        dry_run: If True, skip Telegram and file writes; print to stdout only.

    Returns:
        HealthReport dataclass.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Strategy health check starting (market=%s, dry_run=%s)", market_id, dry_run)

    # Load config
    try:
        config = get_active_config(market_id)
    except FileNotFoundError:
        logger.error("No active config found for market '%s'", market_id)
        sys.exit(1)

    # Run health check
    monitor = StrategyHealthMonitor(config, market_id)
    report = monitor.full_health_report(market_id)

    # ── Logging summary ───────────────────────────────────────────────────────
    logger.info(
        "Health report: %s — healthy=%d warning=%d degraded=%d no_data=%d",
        market_id,
        report.summary.get(HEALTHY, 0),
        report.summary.get(WARNING, 0),
        report.summary.get(DEGRADED, 0),
        report.summary.get(INSUFFICIENT_DATA, 0),
    )

    for a in report.assessments:
        icon = _STATUS_ICON.get(a.status, "?")
        logger.info(
            "  %s %-30s | live_sharpe=%-8s | bt_sharpe=%-8s | trades=%d",
            icon,
            a.strategy,
            _fmt_float(a.live_sharpe),
            _fmt_float(a.backtest_sharpe),
            a.live_trade_count,
        )

    # ── Process lifecycle transitions ─────────────────────────────────────────
    lifecycle_transitions: list = []
    try:
        from monitor.lifecycle import StrategyLifecycleManager
        lifecycle = StrategyLifecycleManager(config, market_id=market_id)
        lifecycle_transitions = lifecycle.process_health_report(report)
        if lifecycle_transitions:
            logger.info(
                "Lifecycle transitions (%d): %s",
                len(lifecycle_transitions),
                [(t["strategy"], t["from"], "→", t["to"]) for t in lifecycle_transitions],
            )
    except Exception as exc:
        logger.warning("Lifecycle processing failed (non-fatal): %s", exc)

    # ── Save report to disk ────────────────────────────────────────────────────
    date_str = datetime.now().strftime("%Y-%m-%d")
    reports_dir = PROJECT / "logs" / "health_reports"

    if dry_run:
        logger.info("Dry run — skipping file write and Telegram")
        # Print the formatted Telegram message to stdout
        msg = _build_telegram_message(report)
        if lifecycle_transitions:
            msg += _fmt_lifecycle_transitions(lifecycle_transitions)
        print(msg)
        return report

    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"health_{market_id}_{date_str}.json"
    try:
        with open(report_path, "w") as fh:
            json.dump(report.to_dict(), fh, indent=2, default=str)
        logger.info("Report saved to %s", report_path)
    except Exception as exc:
        logger.error("Failed to save report: %s", exc)

    # ── Send Telegram summary (with lifecycle transitions appended) ───────────
    try:
        from utils.telegram import send_message
        msg = _build_telegram_message(report)
        if lifecycle_transitions:
            msg += _fmt_lifecycle_transitions(lifecycle_transitions)
        ok = send_message(msg)
        if ok:
            logger.info("Telegram health summary sent")
        else:
            logger.warning("Telegram message failed to send")
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)

    # ── High-priority alert for 3+ consecutive DEGRADED ───────────────────────
    severe_alerts = [
        a for a in report.alerts
        if a.status == DEGRADED and a.consecutive_degraded_weeks >= DEGRADED_CONSECUTIVE_WEEKS
    ]
    if severe_alerts:
        try:
            from utils.telegram import send_message
            alert_msg = _build_degradation_alert_message(report)
            if alert_msg:
                ok = send_message(alert_msg)
                if ok:
                    logger.info(
                        "Degradation alert sent for %d strategies",
                        len(severe_alerts),
                    )
        except Exception as exc:
            logger.warning("Failed to send degradation alert: %s", exc)

    # ── High-priority alert for newly SUSPENDED strategies ────────────────────
    suspended_transitions = [t for t in lifecycle_transitions if t["to"] == "SUSPENDED"]
    if suspended_transitions:
        try:
            from utils.telegram import send_message
            alert_msg = "🚨 <b>STRATEGY SUSPENDED</b>\n\n"
            for t in suspended_transitions:
                alert_msg += f"  🔴 <b>{t['strategy']}</b> — {t['reason']}\n"
            alert_msg += "\nImmediate review required."
            ok = send_message(alert_msg)
            if ok:
                logger.info(
                    "Suspension alert sent for: %s",
                    [t["strategy"] for t in suspended_transitions],
                )
        except Exception as exc:
            logger.warning("Failed to send suspension alert: %s", exc)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas weekly strategy health check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--market",
        default="sp500",
        help="Market to check (default: sp500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print report without saving or sending Telegram messages",
    )
    args = parser.parse_args()

    report = run_health_check(args.market, dry_run=args.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
