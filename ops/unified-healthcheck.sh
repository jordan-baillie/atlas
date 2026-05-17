#!/bin/bash
# Unified health check for all trading projects
# Sends status summary to Telegram

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
    local snapshots_dir="/root/atlas/data/snapshots"
    if [[ ! -d "$snapshots_dir" ]]; then
        echo "⚠️ no data dir"
        return
    fi
    
    local latest=$(find "$snapshots_dir" -type f -name "*.parquet" -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    if [[ -z "$latest" ]]; then
        echo "⚠️ no snapshots"
        return
    fi
    
    local file_date=$(stat -c %Y "$latest")
    local now=$(date +%s)
    local age_hours=$(( (now - file_date) / 3600 ))
    
    if [[ $age_hours -lt 48 ]]; then
        echo "✅ ${age_hours}h ago"
    else
        echo "⚠️ ${age_hours}h ago"
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
        "/root/_archive/cronus-2026-05-18/logs"
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
    # Read from portfolio snapshots JSONL
    local sp500_snapshots="/root/atlas/logs/portfolio_snapshots.jsonl"
    local asx_snapshots="/root/_archive/cronus-2026-05-18/logs/portfolio_snapshots.jsonl"
    
    local sp500_equity="N/A"
    local asx_equity="N/A"
    
    if [[ -f "$sp500_snapshots" ]]; then
        sp500_equity=$(tail -1 "$sp500_snapshots" 2>/dev/null | jq -r '.equity // "N/A"' 2>/dev/null || echo "N/A")
        if [[ "$sp500_equity" != "N/A" ]] && [[ "$sp500_equity" =~ ^[0-9.]+$ ]]; then
            sp500_equity=$(printf '$%.0f' "$sp500_equity")
        else
            sp500_equity="N/A"
        fi
    fi
    
    if [[ -f "$asx_snapshots" ]]; then
        asx_equity=$(tail -1 "$asx_snapshots" 2>/dev/null | jq -r '.equity // "N/A"' 2>/dev/null || echo "N/A")
        if [[ "$asx_equity" != "N/A" ]] && [[ "$asx_equity" =~ ^[0-9.]+$ ]]; then
            asx_equity=$(printf '$%.0f' "$asx_equity")
        else
            asx_equity="N/A"
        fi
    fi
    
    echo "${sp500_equity}|${asx_equity}"
}

# Collect all status checks
SUPERCOACH_API=$(check_service "supercoach-api")

ATLAS_DASHBOARD=$(check_service "atlas-dashboard")
ATLAS_REFRESH=$(check_service "atlas-dashboard-refresh")
ATLAS_TELEGRAM=$(check_service "atlas-telegram-bot")

CRONUS_TRADER=$(check_service "cronus-trader")
CRONUS_RISK=$(check_service "cronus-risk-guardian")
CRONUS_ALERT=$(check_service "cronus-alert-sender")
CRONUS_TIMERS=$(count_active_timers "cronus")

DATA_FRESH=$(check_data_freshness)
DISK=$(check_disk_usage)
BACKUP=$(check_backup)
LARGE_LOGS=$(check_large_logs)
NRL_CRON=$(check_nrl_cron)

EQUITY=$(get_atlas_equity)
SP500_EQUITY=$(echo "$EQUITY" | cut -d'|' -f1)
ASX_EQUITY=$(echo "$EQUITY" | cut -d'|' -f2)

TIMESTAMP=$(date '+%Y-%m-%d %H:%M AEST')

# Build Telegram message
MESSAGE="🏥 <b>System Health Report</b>
📅 ${TIMESTAMP}

<b>Atlas SP500</b>
${ATLAS_DASHBOARD} dashboard | ${ATLAS_REFRESH} refresh | ${ATLAS_TELEGRAM} telegram-bot
💰 Equity: ${SP500_EQUITY}

<b>Atlas ASX</b>
💰 Equity: ${ASX_EQUITY}

<b>Cronus</b>
${CRONUS_TRADER} trader | ${CRONUS_RISK} risk-guardian | ${CRONUS_ALERT} alert-sender
⏱️  ${CRONUS_TIMERS} timers active

<b>SuperCoach</b>
${SUPERCOACH_API} API

<b>Infrastructure</b>
${DISK} Disk | ${BACKUP} Backup
${DATA_FRESH} Data fresh | ${LARGE_LOGS} Log sizes

<b>NRL-Predict</b>
${NRL_CRON} Cron active"

# Send to Telegram
curl -s -X POST "https://api.telegram.org/bot${TOKEN}/sendMessage" \
    -d chat_id="${CHAT_ID}" \
    -d parse_mode="HTML" \
    -d text="${MESSAGE}" > /dev/null

echo "Health check sent to Telegram at ${TIMESTAMP}"
