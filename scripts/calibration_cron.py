"""Monthly confidence calibration — run 1st of month, 10:00 AEST.

Runs calibration analysis on backtest results, saves report,
and sends a Telegram summary with Brier score and per-strategy quality.

Usage:
    python3 scripts/calibration_cron.py --market sp500
    python3 scripts/calibration_cron.py --market sp500 --dry-run

Cron schedule (AEST):
    00 10 1 * *   /root/atlas/scripts/pi-cron.sh calibrate sp500
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path so imports work regardless of cwd
PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from utils.config import get_active_config

logger = logging.getLogger(__name__)


# ── Quality icons ─────────────────────────────────────────────────────────────

_QUALITY_ICON = {
    "well_calibrated": "🟢",
    "slightly_over": "🟡",
    "slightly_under": "🟡",
    "over_confident": "🔴",
    "under_confident": "🔴",
    "poor": "🔴",
}


def _build_telegram_message(report, market_id: str) -> str:
    """Format calibration report as a Telegram HTML message."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines = [
        f"🎯 <b>Confidence Calibration Report [{market_id.upper()}]</b>",
        f"<i>{now}</i>",
        "",
        "<b>Overall:</b>",
        f"  Brier Score: <b>{report.overall_brier:.3f}</b> (lower = better, 0.25 = random)",
        f"  Correlation: <b>{report.overall_correlation:.3f}</b>",
        f"  Quality: {report.overall_quality}",
        f"  Total Trades: {report.total_trades}",
        "",
    ]

    # Per-bucket summary
    if report.overall_buckets:
        lines.append("<b>Confidence Buckets:</b>")
        for b in report.overall_buckets:
            if b.count == 0:
                continue
            icon = "✅" if b.ev_per_trade > 0 else "❌"
            lines.append(
                f"  {icon} [{b.lower:.0%}–{b.upper:.0%}]: "
                f"win={b.win_rate:.0%} | "
                f"EV=${b.ev_per_trade:.2f} | "
                f"n={b.count}"
            )
        lines.append("")

    # Per-strategy
    if report.strategies:
        lines.append("<b>Per-Strategy:</b>")
        for name, sc in sorted(report.strategies.items()):
            icon = _QUALITY_ICON.get(sc.calibration_quality, "❓")
            lines.append(
                f"  {icon} <b>{name}</b>: "
                f"Brier={sc.brier_score:.3f} | "
                f"thresh={sc.recommended_threshold:.2f} | "
                f"n={sc.total_trades}"
            )

    return "\n".join(lines)


def run_calibration(market_id: str, dry_run: bool = False):
    """Run confidence calibration, save report, send Telegram."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info("Confidence calibration starting (market=%s, dry_run=%s)", market_id, dry_run)

    # Load config and data
    try:
        config = get_active_config(market_id)
    except FileNotFoundError:
        logger.error("No active config found for market '%s'", market_id)
        sys.exit(1)

    # Load data using the same pattern as cli.py cmd_calibrate
    sys.path.insert(0, str(PROJECT / "scripts"))
    from cli import load_data, get_tickers
    tickers = get_tickers(market_id)
    data = load_data(tickers, config)
    if not data:
        logger.error("No cached data available for '%s' — run 'atlas ingest' first", market_id)
        sys.exit(1)

    logger.info("Running calibration on %d tickers...", len(data))

    from research.calibration import calibrate_from_backtest, print_report, save_report

    report = calibrate_from_backtest(config, market_id, data=data)

    # Log summary
    logger.info(
        "Calibration: Brier=%.3f, Correlation=%.3f, Quality=%s, Trades=%d",
        report.overall_brier,
        report.overall_correlation,
        report.overall_quality,
        report.total_trades,
    )

    if dry_run:
        logger.info("Dry run — printing report to stdout")
        print_report(report)
        print()
        print(_build_telegram_message(report, market_id))
        return report

    # Save report
    try:
        report_path = save_report(report, market_id)
        logger.info("Report saved to %s", report_path)
    except Exception as exc:
        logger.error("Failed to save report: %s", exc)

    # Send Telegram
    try:
        from utils.telegram import send_message
        msg = _build_telegram_message(report, market_id)
        ok = send_message(msg)
        if ok:
            logger.info("Telegram calibration summary sent")
        else:
            logger.warning("Telegram message failed to send")
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas monthly confidence calibration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--market", default="sp500", help="Market (default: sp500)")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Telegram")
    args = parser.parse_args()

    run_calibration(args.market, dry_run=args.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
