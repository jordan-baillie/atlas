#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Atlas Daily Health Check — runs before premarket to catch issues
# early. Sends Telegram alert only on warnings or failures.
#
# Cron: 30 18 * * 1-5 (18:30 AEST, 30min before premarket)
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

PROJECT="/root/atlas"
HEALTHZ="$PROJECT/pi-package/atlas-ops/skills/atlas-healthz/atlas-healthz/scripts/healthz.py"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/healthz_${TIMESTAMP}.log"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"

mkdir -p "$LOG_DIR"

# Run health check (JSON mode for parsing + human mode for log)
JSON=$(cd "$PROJECT" && python3 "$HEALTHZ" --market sp500 --json 2>/dev/null)
HUMAN=$(cd "$PROJECT" && python3 "$HEALTHZ" --market sp500 2>/dev/null)
EXIT_CODE=$?

# Save full report to log
echo "$HUMAN" > "$LOG_FILE"

# Parse summary from JSON
OVERALL=$(echo "$JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['overall'])" 2>/dev/null)
OK_COUNT=$(echo "$JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['ok'])" 2>/dev/null)
WARN_COUNT=$(echo "$JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['warn'])" 2>/dev/null)
FAIL_COUNT=$(echo "$JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['summary']['fail'])" 2>/dev/null)

# Send Telegram alert if not fully healthy
if [ "$EXIT_CODE" -ne 0 ]; then
    # Build alert message from non-ok checks
    ISSUES=$(echo "$JSON" | python3 -c "
import sys, json
d = json.load(sys.stdin)
lines = []
for sec in d['sections'].values():
    for c in sec['checks']:
        if c['verdict'] != 'ok':
            icon = '⚠️' if c['verdict'] == 'warn' else '❌'
            lines.append(f\"{icon} {c['check']}: {c['message']}\")
print('\n'.join(lines[:15]))
" 2>/dev/null)

    if [ "$OVERALL" = "unhealthy" ]; then
        ICON="❌"
    else
        ICON="⚠️"
    fi

    cd "$PROJECT" && python3 -c "
from utils.telegram import send_message
msg = '''${ICON} <b>Atlas Health Check — ${OVERALL^^}</b>
✅ ${OK_COUNT} ok  ⚠️ ${WARN_COUNT} warn  ❌ ${FAIL_COUNT} fail

${ISSUES}

<i>Premarket runs in 30 min. Fix issues now.</i>'''
send_message(msg)
" 2>>"$LOG_DIR/telegram.log"
fi

# Clean old healthz logs (keep 14 days)
find "$LOG_DIR" -name "healthz_*.log" -mtime +14 -delete 2>/dev/null

exit $EXIT_CODE
