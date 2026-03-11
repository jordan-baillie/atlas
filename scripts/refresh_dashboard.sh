#!/bin/bash
# Atlas dashboard refresh — hardened with timeout, alerting, and log rotation.
# Called by cron: */15 1-18 * * 1-6
cd /root/atlas

LOG="/root/atlas/logs/dashboard-refresh.log"

# ── Log rotation (keep last 1000 lines when > 5000) ──────────
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 5000 ]; then
    tail -1000 "$LOG" > "${LOG}.tmp" && mv "${LOG}.tmp" "$LOG"
    echo "[$(date)] Log rotated (kept last 1000 lines)" >> "$LOG"
fi

# ── Run with timeout — don't let IBKR hang the entire cron ───
timeout 180 python3 dashboard/generate_data.py 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    echo "[$(date)] Dashboard refresh FAILED (exit $EXIT_CODE)" >> "$LOG"

    # Alert via Telegram (throttled: max once per 30 minutes)
    ALERT_FLAG="/tmp/atlas-dashboard-alert"
    if [ ! -f "$ALERT_FLAG" ] || [ $(($(date +%s) - $(stat -c %Y "$ALERT_FLAG" 2>/dev/null || echo 0))) -gt 1800 ]; then
        python3 -c "
import sys; sys.path.insert(0, '/root/atlas')
from utils.telegram import send_message
send_message('⚠️ Dashboard refresh failed (exit $EXIT_CODE). Check logs/dashboard-refresh.log')
" 2>/dev/null
        touch "$ALERT_FLAG"
    fi
else
    # Success — clear alert flag
    rm -f /tmp/atlas-dashboard-alert
fi

# Always copy template — stale data is better than a broken page
cp -f dashboard/templates/index.html dashboard/data/index.html 2>/dev/null
