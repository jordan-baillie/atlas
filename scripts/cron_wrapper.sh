#!/bin/bash
# Atlas-ASX Cron Wrapper
export PATH="/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export HOME=/root
export TZ=UTC

PROJECT="/a0/usr/projects/atlas-asx"
LOG="$PROJECT/logs/cron.log"

echo "" >> "$LOG"
echo "=== CRON RUN: $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===" >> "$LOG"

cd "$PROJECT"
/opt/venv/bin/python scripts/daily_automation.py "$@" >> "$LOG" 2>&1

echo "=== CRON COMPLETE: $(date -u '+%Y-%m-%d %H:%M:%S UTC') ===" >> "$LOG"
