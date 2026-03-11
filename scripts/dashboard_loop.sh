#!/bin/bash
# Atlas dashboard refresh daemon — runs generate_data.py in a 10s loop.
# Keeps Moomoo connection fresh so equity/P&L/positions update live.
#
# Managed by: systemd atlas-dashboard-refresh.service
# Replaces:   cron */15 refresh_dashboard.sh (for live data during market hours)
set -euo pipefail

cd /root/atlas
INTERVAL=10
LOG="/root/atlas/logs/dashboard-refresh.log"
CONSECUTIVE_FAILS=0
MAX_FAILS=10
TEMPLATE="dashboard/templates/index.html"
OUTPUT="dashboard/data/index.html"

# Log rotation (keep last 1000 lines when > 5000)
rotate_log() {
    if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
        tail -1000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
        echo "[$(date)] Log rotated" >> "$LOG"
    fi
}

echo "[$(date)] Dashboard loop starting (interval=${INTERVAL}s)" >> "$LOG"

while true; do
    START=$(date +%s%N)

    # Run the generator
    if timeout 30 python3 dashboard/generate_data.py >> "$LOG" 2>&1; then
        CONSECUTIVE_FAILS=0
        # Copy template on success
        cp -f "$TEMPLATE" "$OUTPUT" 2>/dev/null
    else
        CONSECUTIVE_FAILS=$((CONSECUTIVE_FAILS + 1))
        echo "[$(date)] Refresh failed ($CONSECUTIVE_FAILS consecutive)" >> "$LOG"

        # Alert after sustained failures
        if [ "$CONSECUTIVE_FAILS" -eq "$MAX_FAILS" ]; then
            python3 -c "
import sys; sys.path.insert(0, '/root/atlas')
from utils.telegram import send_message
send_message('⚠️ Dashboard refresh loop: $MAX_FAILS consecutive failures. Check logs.')
" 2>/dev/null || true
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
