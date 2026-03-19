#!/bin/bash
# Atlas dashboard refresh daemon — runs generate_data.py in a 10s loop.
# Keeps broker data fresh so equity/P&L/positions update live.
#
# Managed by: systemd atlas-dashboard-refresh.service
# Replaces:   cron */15 refresh_dashboard.sh (for live data during market hours)
set -euo pipefail

cd /root/atlas
INTERVAL=10
LOG="/root/atlas/logs/dashboard-refresh.log"
ERR_LOG="/root/atlas/logs/dashboard-errors.log"
CONSECUTIVE_FAILS=0
MAX_FAILS=10
ALERT_COOLDOWN=300  # seconds between repeated alerts
LAST_ALERT_TIME=0
TEMPLATE="dashboard/templates/index.html"
OUTPUT="dashboard/data/index.html"

# Log rotation (keep last 3000 lines when > 10000)
# ~22 lines/cycle × 10s = ~75 min of history at 10k threshold
rotate_log() {
    if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 10000 ]; then
        tail -3000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
        echo "[$(date)] Log rotated" >> "$LOG"
    fi
}

echo "[$(date)] Dashboard loop starting (interval=${INTERVAL}s)" >> "$LOG"

while true; do
    START=$(date +%s%N)

    # Run the generator
    if timeout 30 python3 dashboard/generate_data.py >> "$LOG" 2>&1; then
        if [ "$CONSECUTIVE_FAILS" -gt 0 ]; then
            echo "[$(date)] Recovered after $CONSECUTIVE_FAILS consecutive failures" >> "$LOG"
        fi
        CONSECUTIVE_FAILS=0
        # Copy template on success
        cp -f "$TEMPLATE" "$OUTPUT" 2>/dev/null
    else
        EXIT_CODE=$?
        CONSECUTIVE_FAILS=$((CONSECUTIVE_FAILS + 1))
        echo "[$(date)] Refresh failed (exit=$EXIT_CODE, $CONSECUTIVE_FAILS consecutive)" >> "$LOG"

        # Alert after sustained failures (with cooldown to avoid spam)
        if [ "$CONSECUTIVE_FAILS" -eq "$MAX_FAILS" ]; then
            NOW_EPOCH=$(date +%s)
            SINCE_LAST=$((NOW_EPOCH - LAST_ALERT_TIME))
            if [ "$SINCE_LAST" -ge "$ALERT_COOLDOWN" ]; then
                LAST_ALERT_TIME=$NOW_EPOCH
                # Include last error from dedicated error log for context
                LAST_ERR=""
                if [ -f "$ERR_LOG" ]; then
                    LAST_ERR=$(tail -5 "$ERR_LOG" 2>/dev/null | head -3)
                fi
                python3 -c "
import sys; sys.path.insert(0, '/root/atlas')
from utils.telegram import send_message
err_ctx = '''$LAST_ERR'''
msg = '⚠️ Dashboard refresh loop: $MAX_FAILS consecutive failures.'
if err_ctx.strip():
    msg += f'\n\nLast error:\n<code>{err_ctx[:200]}</code>'
send_message(msg)
" 2>/dev/null || true
            fi
        fi
    fi

    # Rotate log periodically
    rotate_log

    # Sleep for remainder of interval
    END=$(date +%s%N)
    ELAPSED_MS=$(( (END - START) / 1000000 ))
    SLEEP_MS=$(( INTERVAL * 1000 - ELAPSED_MS ))
    if [ "$SLEEP_MS" -gt 0 ]; then
        sleep "$(echo "scale=3; $SLEEP_MS / 1000" | bc)"
    fi
done
