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
RESEARCH_SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-research-loop"
NOTIFY="$SCRIPT_DIR/telegram_notify.py"
RESEARCH_LOCK="/tmp/atlas-research.lock"

mkdir -p "$LOG_DIR"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MODE="${1:-}"
MARKET="${2:-${ATLAS_MARKET:-sp500}}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

# --- Helper: send Telegram alert (best-effort, never blocks cron exit) ---
notify() {
    python3 "$NOTIFY" "$@" 2>>"$LOG_DIR/telegram.log" || true
}

AGENT_ID="${3:-atlas-research}"

case "$MODE" in
    premarket)
        PROMPT="Run the atlas-daily pre-market workflow for today: check data freshness and run cli_ingest if stale, then run cli_plan. Summarize the plan and stop — do NOT approve or execute. Write results to logs/pi-cron-premarket-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-premarket-${TIMESTAMP}.log"
        ;;
    postclose)
        PROMPT="Run the atlas-daily post-close workflow: run cli_eod_settlement, then dashboard_generate_data. Summarize any exits triggered and the final equity snapshot. Write results to logs/pi-cron-postclose-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-postclose-${TIMESTAMP}.log"
        ;;
    research)
        LOGFILE="$LOG_DIR/research_${TIMESTAMP}.log"
        SKILL_DIR="$RESEARCH_SKILL_DIR"
        PROMPT="Run one full atlas-research-loop cycle: read current state (journal, health check, queue), generate hypotheses if queue is empty, execute queued experiments via research_runner.py --run-all --agent-id ${AGENT_ID}, evaluate results, and send promotion requests for any passing experiments. Summarize all outcomes at the end."

        # Lock file check — prevent concurrent research sessions
        if [ -f "$RESEARCH_LOCK" ]; then
            LOCK_PID=$(grep -oP '"pid":\s*\K\d+' "$RESEARCH_LOCK" 2>/dev/null || echo "")
            LOCK_TIME=$(grep -oP '"started_at":\s*"\K[^"]+' "$RESEARCH_LOCK" 2>/dev/null || echo "unknown")
            if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
                echo "$(date -Iseconds) Research already running (PID=$LOCK_PID, started=$LOCK_TIME)" >> "$LOG_DIR/pi-cron.log"
                notify error "research" "" 2>/dev/null || true
                exit 0
            else
                echo "$(date -Iseconds) Stale research lock found (PID=$LOCK_PID), removing" >> "$LOG_DIR/pi-cron.log"
                rm -f "$RESEARCH_LOCK"
            fi
        fi

        # Acquire lock
        cat > "$RESEARCH_LOCK" <<LOCKEOF
{
    "pid": $$,
    "owner": "${AGENT_ID}",
    "started_at": "$(date -Iseconds)",
    "logfile": "${LOGFILE}"
}
LOCKEOF
        # Clean up lock on exit
        trap 'rm -f "$RESEARCH_LOCK"' EXIT
        ;;
    research-status)
        if [ -f "$RESEARCH_LOCK" ]; then
            echo "Research lock held:"
            cat "$RESEARCH_LOCK"
            LOCK_PID=$(grep -oP '"pid":\s*\K\d+' "$RESEARCH_LOCK" 2>/dev/null || echo "")
            if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
                echo "Process is RUNNING (PID=$LOCK_PID)"
            else
                echo "Process is DEAD (stale lock)"
            fi
        else
            echo "No research session running."
        fi
        exit 0
        ;;
    *)
        echo "Usage: $0 {premarket|postclose|research|research-status} [market] [agent-id]"
        exit 1
        ;;
esac

echo "$(date -Iseconds) Starting pi-cron $MODE" >> "$LOG_DIR/pi-cron.log"

cd "$PROJECT"
if [ "$MODE" = "research" ]; then
    # Research sessions get an 8-hour timeout
    timeout 28800 pi --print \
       --skill "$SKILL_DIR" \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1
else
    pi --print \
       --skill "$SKILL_DIR" \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1
fi

EXIT_CODE=$?
echo "$(date -Iseconds) pi-cron $MODE finished (exit=$EXIT_CODE)" >> "$LOG_DIR/pi-cron.log"

# --- Dashboard refresh (always, regardless of pi exit) ---
python3 dashboard/generate_data.py >> "$LOG_DIR/dashboard-refresh.log" 2>&1 || true

# --- Telegram alerts ---
if [ $EXIT_CODE -eq 0 ]; then
    case "$MODE" in
        premarket)
            # Send plan with Approve/Reject buttons (bot handles callbacks)
            notify premarket-approve "" "$MARKET"
            # Fallback: also send plain summary if approve fails
            [ $? -ne 0 ] && notify premarket-ok "" "$MARKET"
            ;;
        postclose)  notify postclose-ok "$MARKET" ;;
        research)   notify research-complete "$MARKET" ;;
    esac
else
    notify error "$MODE" "$LOGFILE"
fi

exit $EXIT_CODE
