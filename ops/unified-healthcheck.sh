#!/bin/bash
# Unified health check for all trading projects.
# Severity-routed (operator directive 2026-06-12: critical-only Telegram):
# silent when everything is green; sends ONE alert listing only the ❌ lines
# when something is actually broken. Full status always printed to journal.

set -euo pipefail

# Read Telegram credentials
SECRETS_FILE="/root/.atlas-secrets.json"
if [[ ! -f "$SECRETS_FILE" ]]; then
    echo "Error: $SECRETS_FILE not found"
    exit 1
fi

TOKEN=$(jq -r '.telegram_bot_token' "$SECRETS_FILE")
CHAT_ID=$(jq -r '.telegram_chat_id' "$SECRETS_FILE")

# Helper functions
check_service() {
    local service=$1
    if systemctl is-active --quiet "$service"; then
        echo "✅"
    else
        echo "❌"
    fi
}

check_timer() {
    local timer=$1
    if systemctl is-active --quiet "$timer"; then
        echo "✅"
    else
        echo "❌"
    fi
}

count_active_timers() {
    local pattern=$1
    systemctl list-timers --no-pager --no-legend | grep -c "$pattern" || echo "0"
}


check_data_freshness() {
    # Post-refactor (2026-06-11): the live data artery is the forward book's
    # returns.jsonl, not the retired snapshots dir.
    local rj="/root/atlas/data/live/val_mom_trend_smallcap/returns.jsonl"
    if [[ ! -f "$rj" ]]; then
        echo "❌ no returns.jsonl"
        return
    fi
    local file_date=$(stat -c %Y "$rj")
    local now=$(date +%s)
    local age_hours=$(( (now - file_date) / 3600 ))
    # 4 calendar days covers weekend + one missed run; beyond that the daily cycle is dead
    if [[ $age_hours -lt 96 ]]; then
        echo "✅ ${age_hours}h ago"
    else
        echo "❌ ${age_hours}h ago"
    fi
}

check_disk_usage() {
    local usage=$(df -h / | awk 'NR==2{print $5}' | sed 's/%//')
    if [[ $usage -lt 80 ]]; then
        echo "✅ ${usage}%"
    elif [[ $usage -lt 90 ]]; then
        echo "⚠️ ${usage}%"
    else
        echo "❌ ${usage}%"
    fi
}

check_backup() {
    local repo="/root/backups/restic-repo"
    if [[ ! -d "$repo" ]]; then
        echo "❌ no repo"
        return
    fi
    
    # Get last snapshot date from restic (with password)
    export RESTIC_PASSWORD="atlas-backup-2026"
    local last_snapshot=$(restic -r "$repo" snapshots --json 2>/dev/null | jq -r '.[-1].time // empty' 2>/dev/null || echo "")
    unset RESTIC_PASSWORD
    
    if [[ -z "$last_snapshot" ]]; then
        echo "⚠️ no snapshots"
        return
    fi
    
    local snapshot_date=$(date -d "$last_snapshot" +%s 2>/dev/null || echo "0")
    local now=$(date +%s)
    local age_days=$(( (now - snapshot_date) / 86400 ))
    
    if [[ $age_days -eq 0 ]]; then
        echo "✅ today"
    elif [[ $age_days -eq 1 ]]; then
        echo "✅ 1d ago"
    elif [[ $age_days -lt 7 ]]; then
        echo "⚠️ ${age_days}d ago"
    else
        echo "❌ ${age_days}d ago"
    fi
}

check_large_logs() {
    local count=0
    local log_dirs=(
        "/root/atlas/logs"
        "/tmp"
        "/var/log"
    )
    
    for dir in "${log_dirs[@]}"; do
        if [[ -d "$dir" ]]; then
            count=$((count + $(find "$dir" -type f -name "*.log" -size +100M 2>/dev/null | wc -l)))
        fi
    done
    
    if [[ $count -eq 0 ]]; then
        echo "✅"
    else
        echo "⚠️ ${count} logs >100MB"
    fi
}

check_nrl_cron() {
    if crontab -l 2>/dev/null | grep -q "nrl"; then
        echo "✅"
    else
        echo "⚠️ no cron"
    fi
}

get_atlas_equity() {
    # Post-refactor: live forward book state (the retired swing book's
    # portfolio_snapshots.jsonl is frozen history).
    local eq_state="/root/atlas/data/live/val_mom_trend_smallcap/equity_state.json"
    local eq="N/A"
    if [[ -f "$eq_state" ]]; then
        eq=$(jq -r '.equity // "N/A"' "$eq_state" 2>/dev/null || echo "N/A")
        if [[ "$eq" != "N/A" ]] && [[ "$eq" =~ ^[0-9.]+$ ]]; then
            eq=$(printf '$%.0f' "$eq")
        else
            eq="N/A"
        fi
    fi
    echo "${eq}"
}

# Collect all status checks
SUPERCOACH_API=$(check_service "supercoach-api")

ATLAS_DASHBOARD=$(check_service "atlas-dashboard")
# atlas-dashboard-refresh removed in the 2026-06-11 great-deletion refactor

DATA_FRESH=$(check_data_freshness)
DISK=$(check_disk_usage)
BACKUP=$(check_backup)
LARGE_LOGS=$(check_large_logs)
NRL_CRON=$(check_nrl_cron)

SP500_EQUITY=$(get_atlas_equity)

TIMESTAMP=$(date '+%Y-%m-%d %H:%M AEST')

# Full status -> journal (always)
STATUS="Atlas dashboard ${ATLAS_DASHBOARD} | book equity ${SP500_EQUITY}
SuperCoach API ${SUPERCOACH_API} | NRL cron ${NRL_CRON}
Disk ${DISK} | Backup ${BACKUP} | Data ${DATA_FRESH} | Logs ${LARGE_LOGS}"
echo "${TIMESTAMP}"
echo "${STATUS}"

# Telegram ONLY on hard failures (❌). Warnings (⚠️) stay in the journal — the
# crucible sentinel/morning-report cover drift on the money path.
FAILURES=$(echo "${STATUS}" | grep '❌' || true)
if [[ -n "${FAILURES}" ]]; then
    MESSAGE="🚨 <b>Health check FAILURES</b> — ${TIMESTAMP}
${FAILURES}"
    curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
        -d chat_id="${CHAT_ID}" \
        -d parse_mode="HTML" \
        -d text="${MESSAGE}" > /dev/null
    echo "FAILURE alert sent to Telegram"
else
    echo "all green — no Telegram (critical-only policy)"
fi
