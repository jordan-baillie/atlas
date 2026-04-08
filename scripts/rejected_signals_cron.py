"""Weekly rejected signal analysis — run Sunday 08:00 AEST.

Analyses rejected signals from the past week's trade plans,
computes opportunity cost, and sends a Telegram summary.

Usage:
    python3 scripts/rejected_signals_cron.py --market sp500
    python3 scripts/rejected_signals_cron.py --market sp500 --days 30 --dry-run

Cron schedule (AEST):
    00 8 * * 0   /root/atlas/scripts/pi-cron.sh rejected-signals sp500
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logger = logging.getLogger(__name__)


def run_rejected_signals(market_id: str, days_back: int = 7, dry_run: bool = False):
    """Run rejected signal analysis, save report, send Telegram."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info(
        "Rejected signal analysis starting (market=%s, days=%d, dry_run=%s)",
        market_id, days_back, dry_run,
    )

    from research.rejected_signal_analysis import RejectedSignalAnalyzer

    # Plans are stored in plans/ directory as plan_{market}_{date}.json
    plans_dir = PROJECT / "plans"
    analyzer = RejectedSignalAnalyzer(plans_dir=plans_dir)

    # Compute date range for the lookback period
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    # Extract rejected signals from plan files in date range
    signals = analyzer.extract_rejected(date_range=(start_date, end_date))
    logger.info("Found %d rejected signals in %s → %s", len(signals), start_date, end_date)

    if not signals:
        logger.info("No rejected signals in this period — skipping report")
        return None

    # Run analysis (without hypothetical P&L unless price data is available)
    report = analyzer.analyze(signals)

    logger.info(
        "Analysis complete: %d rejected, %d categories",
        report.total_rejected,
        len(report.category_distribution),
    )

    # Format message
    msg = analyzer.format_telegram(report)

    if dry_run:
        logger.info("Dry run — printing Telegram message to stdout")
        print(msg)
        return report

    # Save report
    try:
        report_path = analyzer.save_report(report)
        logger.info("Report saved to %s", report_path)
    except Exception as exc:
        logger.error("Failed to save report: %s", exc)

    # Send Telegram
    try:
        from utils.telegram import send_message
        ok = send_message(msg)
        if ok:
            logger.info("Telegram rejected signal summary sent")
        else:
            logger.warning("Telegram message failed to send")
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atlas weekly rejected signal analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--market", default="sp500", help="Market (default: sp500)")
    parser.add_argument("--days", type=int, default=7, help="Days to look back (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Telegram")
    args = parser.parse_args()

    run_rejected_signals(args.market, days_back=args.days, dry_run=args.dry_run)
    sys.exit(0)


if __name__ == "__main__":
    main()
