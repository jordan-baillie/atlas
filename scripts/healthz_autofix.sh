#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Atlas Health Autofix — runs healthcheck, spawns pi agent to fix
# any failures/warnings automatically.
#
# Only spawns an agent if there are actionable issues.
# The agent is constrained to safe infrastructure fixes only —
# no config changes, no broker operations, no code changes.
#
# Cron: 00 18 * * 1-5 (18:00 AEST, 1h before premarket)
# ═══════════════════════════════════════════════════════════════
set -uo pipefail
unset ANTHROPIC_API_KEY CLAUDE_API_KEY  # Atlas hardening: force pi to use OAuth (Claude Max)

PROJECT="/root/atlas"
HEALTHZ="$PROJECT/pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/healthz-autofix_${TIMESTAMP}.log"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

mkdir -p "$LOG_DIR"

echo "=== Atlas Health Autofix — $TIMESTAMP ===" > "$LOG_FILE"

# ── Step 1: Run healthcheck (JSON mode) ──
cd "$PROJECT"
HEALTHZ_JSON=$(python3 "$HEALTHZ" --market sp500 --json 2>/dev/null)
EXIT_CODE=$?

echo "Healthcheck exit code: $EXIT_CODE" >> "$LOG_FILE"

# Exit 0 = healthy — nothing to fix
if [ "$EXIT_CODE" -eq 0 ]; then
    echo "System healthy — no action needed" >> "$LOG_FILE"
    exit 0
fi

# ── Step 2: Extract issues ──
ISSUES=$(echo "$HEALTHZ_JSON" | python3 -c "
import sys, json
try:
    report = json.load(sys.stdin)
except:
    sys.exit(1)

issues = []
for sec_name, sec in report['sections'].items():
    for c in sec['checks']:
        if c['verdict'] != 'ok':
            issues.append(f\"[{c['verdict'].upper()}] {sec_name}/{c['check']}: {c['message']}\")

if not issues:
    sys.exit(1)

print('\n'.join(issues))
" 2>/dev/null)

if [ -z "$ISSUES" ]; then
    echo "No actionable issues found (JSON parse returned empty)" >> "$LOG_FILE"
    exit 0
fi

ISSUE_COUNT=$(echo "$ISSUES" | wc -l)
echo "Found $ISSUE_COUNT issue(s):" >> "$LOG_FILE"
echo "$ISSUES" >> "$LOG_FILE"
echo "" >> "$LOG_FILE"

# ── Step 3: Spawn pi agent to fix issues ──
echo "Spawning pi agent for autofix..." >> "$LOG_FILE"

PROMPT="You are the Atlas infrastructure autofix agent. The healthcheck found these issues:

$ISSUES

Fix every issue you can. Here are the SAFE fixes you are allowed to perform:

ALLOWED (do these automatically):
- Restart services: systemctl restart atlas-telegram-bot, systemctl restart atlas-dashboard
- Restart research services: systemctl restart atlas-director, systemctl restart atlas-research-runner, systemctl restart atlas-research-window
- Truncate large logs: tail -1000 logs/atlas.log > logs/atlas.log.tmp && mv logs/atlas.log.tmp logs/atlas.log
- Clean __pycache__: find /root/atlas -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
- Run weekly maintenance: bash /root/atlas/scripts/weekly_maintenance.sh
- Refresh data cache: cd /root/atlas && python3 scripts/cli.py -m sp500 ingest
- Fix pip/numpy issues: pip install --break-system-packages --upgrade <package>
- Clean old log files: find /root/atlas/logs -name '*.log' -mtime +30 -delete
- Kill orphan research processes: ps aux | grep research | grep -v grep

NOT ALLOWED (never do these):
- Do NOT edit any Python source code
- Do NOT modify config files (config/active/*.json)
- Do NOT place broker orders or modify positions
- Do NOT change crontab entries
- Do NOT run git operations (commit, push, reset)
- Do NOT modify secrets files
- Do NOT restart the system or reboot

After fixing, run the healthcheck again to verify:
  cd /root/atlas && python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --market sp500

TELEGRAM: Send a Telegram notification ONLY if you actually fixed something or something remains broken.
  python3 -c \"import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''YOUR_MSG''')\"

Rules:
- If you fixed issues and system is now healthy: send a short success message (what was fixed)
- If issues remain unfixed: send an alert listing what needs manual attention
- If the healthcheck only found minor warnings (pycache, log size): do NOT send anything
- Keep it under 10 lines. Use HTML formatting (<b>, <i>)"

# Run pi headless — 5 min timeout, capture output
# Load incident + state-queries skills for diagnostic knowledge
SKILLS_ROOT="$PROJECT/pi-package/atlas-ops/skills"
timeout 300 pi -p \
    --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
    --no-session --model anthropic/claude-haiku-4-5 \
    --skill "$SKILLS_ROOT/atlas-incident" \
    --skill "$SKILLS_ROOT/atlas-state-queries" \
    --skill "$SKILLS_ROOT/atlas-lessons" \
    "$PROMPT" >> "$LOG_FILE" 2>&1
PI_EXIT=$?

echo "" >> "$LOG_FILE"
echo "Pi agent exit code: $PI_EXIT" >> "$LOG_FILE"

# ── Step 4: Re-run healthcheck to verify ──
echo "" >> "$LOG_FILE"
echo "=== Post-fix verification ===" >> "$LOG_FILE"
python3 "$HEALTHZ" --market sp500 2>/dev/null >> "$LOG_FILE"
VERIFY_EXIT=$?
echo "Post-fix exit code: $VERIFY_EXIT" >> "$LOG_FILE"

# Agent handles its own Telegram notifications based on significance.
# Only send a shell-level alert if the agent itself crashed.
if [ $PI_EXIT -ne 0 ]; then
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message('🚨 <b>Healthz autofix agent crashed</b> (exit $PI_EXIT).\nVerify exit: $VERIFY_EXIT\nCheck logs: $LOG_FILE')
" 2>>"$LOG_DIR/telegram.log"
fi

# Clean old autofix logs (keep 14 days)
find "$LOG_DIR" -name "healthz-autofix_*.log" -mtime +14 -delete 2>/dev/null

exit $VERIFY_EXIT
