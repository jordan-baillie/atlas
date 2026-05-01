#!/usr/bin/env python3
"""Atlas Telegram Notification CLI.

Called by pi-cron.sh to send alerts after daily runs.

Usage:
    python3 scripts/telegram_notify.py premarket-ok  [plan_path] [market_id]
    python3 scripts/telegram_notify.py premarket-approve [plan_path] [market_id]
    python3 scripts/telegram_notify.py postclose-ok  [market_id]
    python3 scripts/telegram_notify.py error         <mode> [logfile]
    python3 scripts/telegram_notify.py promotion-request <experiment_id> <market_id>
    python3 scripts/telegram_notify.py test
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas_bootstrap import PROJECT_ROOT

from utils.telegram import (
    send_premarket_summary,
    send_postclose_summary,
    send_error,
    send_startup,
    send_research_complete,
    send_message,
)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "premarket-ok":
        plan_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        market_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "sp500"
        ok = send_premarket_summary(plan_path=plan_path, market_id=market_id)

    elif cmd == "premarket-approve":
        # Buffer the plan summary for the daily rollup; auto-approve if configured.
        # No longer sends Telegram directly — use premarket-rollup for the message.
        from services.telegram_bot import send_plan_for_approval
        plan_path = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
        market_id = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else "sp500"
        ok = send_plan_for_approval(plan_path=plan_path, market_id=market_id)

    elif cmd == "premarket-rollup":
        # Send ONE consolidated Telegram message for all markets' plans.
        # Called by the 19:45 AEST cron after all 3 premarket runs complete.
        from services.telegram_bot import send_plan_rollup
        ok = send_plan_rollup()

    elif cmd == "postclose-ok":
        market_id = sys.argv[2] if len(sys.argv) > 2 else "sp500"
        ok = send_postclose_summary(market_id=market_id)

    elif cmd == "volatility-block":
        market_id = sys.argv[2] if len(sys.argv) > 2 else "sp500"
        import json, glob
        # Read the latest volatility gate log
        gate_files = sorted(glob.glob(f"{os.path.dirname(__file__)}/../logs/volatility_gate_*.json"))
        detail = ""
        if gate_files:
            try:
                with open(gate_files[-1]) as f:
                    gate = json.load(f)
                flags = gate.get("flags", [])
                details = gate.get("details", {})
                lines = [f"⚠️ <b>Volatility Gate — {market_id.upper()} entries BLOCKED</b>"]
                lines.append(f"Flags: {len(flags)} triggered\n")
                for flag in flags:
                    d = details.get(flag, {})
                    if flag == "vix":
                        lines.append(f"  📊 VIX spike {d.get('spike_pct', 0):.1f}% (threshold {d.get('vix_spike_threshold_pct', 20)}%)")
                    else:
                        lines.append(f"  {'🛢️' if flag == 'oil' else '🥇'} {flag.upper()} gap {d.get('gap_pct', 0):.1f}% (threshold {d.get('threshold_pct', 0)}%)")
                lines.append(f"\n✅ Existing positions unaffected.")
                lines.append(f"No new entries until next session.")
                detail = "\n".join(lines)
            except Exception:
                detail = f"⚠️ Volatility gate blocked all {market_id.upper()} entries. Check logs."
        ok = send_message(detail or f"⚠️ Volatility gate blocked {market_id.upper()} entries.")

    elif cmd == "error":
        mode = sys.argv[2] if len(sys.argv) > 2 else "unknown"
        logfile = sys.argv[3] if len(sys.argv) > 3 else None
        ok = send_error(mode, f"Cron run '{mode}' exited with non-zero status.", logfile)

    elif cmd == "research-complete":
        market_id = sys.argv[2] if len(sys.argv) > 2 else "sp500"
        ok = send_research_complete(market_id=market_id)

    elif cmd == "promotion-request":
        # Send promotion request with Approve/Reject inline buttons
        experiment_id = sys.argv[2] if len(sys.argv) > 2 else None
        market_id = sys.argv[3] if len(sys.argv) > 3 else "sp500"
        if not experiment_id:
            print("Error: experiment_id required")
            print("Usage: python3 scripts/telegram_notify.py promotion-request <experiment_id> <market_id>")
            sys.exit(1)

        # Load experiment and validation data to build the rich message
        from scripts.research_promote import validate_candidate, CANDIDATES_DIR, send_promotion_request
        from research.models import load_experiment

        candidate_path = CANDIDATES_DIR / f'{market_id}_{experiment_id}.json'
        if not candidate_path.exists():
            print(f"Error: Candidate config not found at {candidate_path}")
            print("Run --stage first: python3 scripts/research_promote.py --stage --experiment-id <id> --market <market>")
            sys.exit(1)

        # Run validation (skip OOS if already done — check for existing results)
        oos_path = PROJECT_ROOT / 'backtest' / 'results' / f'oos_promotion_{experiment_id}.json'
        skip_oos = oos_path.exists()
        if skip_oos:
            print(f"OOS results already exist at {oos_path}, using cached results")

        validation = validate_candidate(experiment_id, market_id, skip_oos=skip_oos)
        ok = send_promotion_request(experiment_id, market_id, validation)
        if ok:
            print(f"Promotion request sent with Approve/Reject buttons for {experiment_id}")
        else:
            print("Failed to send promotion request")

    elif cmd == "research-idle":
        ok = send_message("🔬 Research cron: queue empty — nothing to run. Seed new experiments to resume.")

    elif cmd == "research-wave-planned":
        # Read the latest wave brief for summary
        waves_dir = PROJECT_ROOT / "research" / "waves"
        brief_files = sorted(waves_dir.glob("wave_*_brief.json"), reverse=True)
        msg = "🔬 <b>New Research Wave Planned</b>\n\n"
        if brief_files:
            import json as _json
            with open(brief_files[0]) as _f:
                brief = _json.load(_f)
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
