#!/usr/bin/env python3
"""Daily graduation engine cron — evaluates ASSIST→AUTO_FIX promotions and
AUTO_FIX→PERMANENT_ASSIST demotions. Writes audit-log entries. Does NOT
modify config; operator manually ratifies via runbook procedure.
"""
import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("ATLAS_SQLITE_ERROR_WRITER", "0")

from utils.logging_config import setup_logging
from core import graduation

logger = setup_logging("graduation_engine", telegram_errors=False)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    summary = graduation.run(db_path=args.db, dry_run=args.dry_run)
    print(json.dumps(summary, indent=2))
    if summary["promotions"] or summary["demotions"]:
        # Telegram alert ONLY when something changes (failure-only philosophy doesn't
        # apply here — class transitions are operator-action signals)
        try:
            from utils.telegram import send_message
            msg = f"📈 Graduation engine: promotions={summary['promotions']}, demotions={summary['demotions']}"
            send_message(msg)
        except Exception as e:
            logger.warning("Telegram alert failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
