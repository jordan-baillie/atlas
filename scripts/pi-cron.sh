#!/bin/bash
# Atlas Pi Cron Wrapper
# Triggers pi in non-interactive mode for scheduled daily operations.
# Sends Telegram alerts on success and failure.
#
# Cron schedule (AEST via TZ=Australia/Brisbane in crontab):
#   30 8  * * 1-5  /root/atlas/scripts/pi-cron.sh premarket
#   30 17 * * 1-5  /root/atlas/scripts/pi-cron.sh postclose
#
# Setup:
#   1. Ensure pi is logged in: pi (interactive) — OAuth login persists in ~/.pi/agent/auth.json
#   2. Credentials in ~/.atlas-secrets.json (telegram_bot_token, telegram_chat_id)
#   3. Run: crontab -e  (see schedule above)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-daily"
NOTIFY="$SCRIPT_DIR/telegram_notify.py"

mkdir -p "$LOG_DIR"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MARKET="${ATLAS_MARKET:-asx}"
MODE="${1:-}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

# --- Helper: send Telegram alert (best-effort, never blocks cron exit) ---
notify() {
    python3 "$NOTIFY" "$@" 2>>"$LOG_DIR/telegram.log" || true
}

case "$MODE" in
    premarket)
        PROMPT="Run the atlas-daily pre-market workflow for today: check data freshness and run cli_ingest if stale, then run cli_plan. Summarize the plan and stop — do NOT approve or execute. Write results to logs/pi-cron-premarket-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-premarket-${TIMESTAMP}.log"
        ;;
    postclose)
        PROMPT="Run the atlas-daily post-close workflow: run cli_eod_settlement, then dashboard_generate_data. Summarize any exits triggered and the final equity snapshot. Write results to logs/pi-cron-postclose-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-postclose-${TIMESTAMP}.log"
        ;;
    *)
        echo "Usage: $0 {premarket|postclose}"
        exit 1
        ;;
esac

echo "$(date -Iseconds) Starting pi-cron $MODE" >> "$LOG_DIR/pi-cron.log"

cd "$PROJECT"
pi --print \
   --skill "$SKILL_DIR" \
   --no-session \
   "$PROMPT" \
   >> "$LOGFILE" 2>&1

EXIT_CODE=$?
echo "$(date -Iseconds) pi-cron $MODE finished (exit=$EXIT_CODE)" >> "$LOG_DIR/pi-cron.log"

# --- Telegram alerts ---
if [ $EXIT_CODE -eq 0 ]; then
    case "$MODE" in
        premarket)  notify premarket-ok "" "$MARKET" ;;
        postclose)  notify postclose-ok "$MARKET" ;;
    esac
else
    notify error "$MODE" "$LOGFILE"
fi

exit $EXIT_CODE
