#!/usr/bin/env python3
"""Atlas Telegram Notification CLI.

Called by pi-cron.sh to send alerts after daily runs.

Usage:
    python3 scripts/telegram_notify.py premarket-ok  [plan_path] [market_id]
    python3 scripts/telegram_notify.py premarket-approve [plan_path] [market_id]
    python3 scripts/telegram_notify.py postclose-ok  [market_id]
    python3 scripts/telegram_notify.py error         <mode> [logfile]
    python3 scripts/telegram_notify.py test
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from utils.telegram import (
    send_premarket_summary,
    send_postclose_summary,
    send_error,
    send_startup,
    send_research_complete,
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "premarket-ok":
        plan_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        market_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "asx"
        ok = send_premarket_summary(plan_path=plan_path, market_id=market_id)

    elif cmd == "premarket-approve":
        # Send plan with Approve/Reject inline buttons (requires bot to be running)
        from services.telegram_bot import send_plan_for_approval
        plan_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        market_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "asx"
        ok = send_plan_for_approval(plan_path=plan_path, market_id=market_id)

    elif cmd == "postclose-ok":
        market_id = sys.argv[2] if len(sys.argv) > 2 else "sp500"
        ok = send_postclose_summary(market_id=market_id)

    elif cmd == "error":
        mode = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        logfile = sys.argv[3] if len(sys.argv) > 3 else None
        ok = send_error(mode, f"Cron run '{mode}' exited with non-zero status.", logfile)

    elif cmd == "research-complete":
        market_id = sys.argv[2] if len(sys.argv) > 2 else "sp500"
        ok = send_research_complete(market_id=market_id)

    elif cmd == "research-idle":
        from utils.telegram import send_message
        ok = send_message("🔬 Research cron: queue empty — nothing to run. Seed new experiments to resume.")

    elif cmd == "research-wave-planned":
        from utils.telegram import send_message
        # Read the latest wave brief for summary
        waves_dir = PROJECT_ROOT / "research" / "waves"
        brief_files = sorted(waves_dir.glob("wave_*_brief.json"), reverse=True)
        msg = "🔬 <b>New Research Wave Planned</b>\n\n"
        if brief_files:
            import json as _json
            brief = _json.load(open(brief_files[0]))
            wave_num = brief.get("wave_number", "?")
            theme = brief.get("theme", "not set")
            n_exp = len(brief.get("experiments", []))
            msg += f"Wave {wave_num}: <b>{theme}</b>\n"
            msg += f"Experiments: {n_exp}\n"
            rationale = brief.get("theme_rationale", "")
            if rationale:
                msg += f"\n<i>{rationale[:200]}</i>\n"
            web_findings = brief.get("web_research_findings", [])
            if web_findings:
                msg += f"\nWeb research: {len(web_findings)} sources consulted"
        else:
            msg += "Brief file not found."
        ok = send_message(msg)

    elif cmd == "test":
        ok = send_startup()

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
