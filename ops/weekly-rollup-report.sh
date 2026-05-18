#!/bin/bash
# Weekly Portfolio Rollup Report
# Runs Sunday 08:00 AEST, summarizes entire week across all projects
# Sends formatted report to Telegram

set -euo pipefail

# Load Telegram credentials
SECRETS_FILE="/root/.atlas-secrets.json"
BOT_TOKEN=$(jq -r '.telegram_bot_token' "$SECRETS_FILE")
CHAT_ID=$(jq -r '.telegram_chat_id' "$SECRETS_FILE")
TELEGRAM_API="https://api.telegram.org/bot${BOT_TOKEN}/sendMessage"

# Calculate date range (last 7 days)
END_DATE=$(date +%Y-%m-%d)
START_DATE=$(date -d '7 days ago' +%Y-%m-%d)
WEEK_NUM=$(date +%U)

# ============================================================================
# DATA COLLECTION FUNCTIONS
# ============================================================================

get_atlas_sp500_data() {
    local equity="N/A"
    local pnl="N/A"
    local trades_this_week=0
    local errors_this_week=0
    
    # Get latest portfolio equity
    if [[ -f /root/atlas/data/position_monitor/positions.json ]]; then
        equity=$(jq -r '.equity // "N/A"' /root/atlas/data/position_monitor/positions.json 2>/dev/null || echo "N/A")
    fi
    
    # Count trades this week from trade_ledger.json
    if [[ -f /root/atlas/journal/trade_ledger.json ]]; then
        trades_this_week=$(jq --arg start "$START_DATE" \
            '[.[] | select(.timestamp >= $start)] | length' \
            /root/atlas/journal/trade_ledger.json 2>/dev/null || echo "0")
    fi
    
    # Count errors in logs this week
    if [[ -d /root/atlas/logs ]]; then
        errors_this_week=$(find /root/atlas/logs -name "*.log" -type f \
            -newermt "$START_DATE" -exec grep -ci 'ERROR\|CRITICAL' {} + 2>/dev/null | \
            awk '{sum+=$1} END {print sum+0}')
    fi
    
    # Get total trade count
    local total_trades=0
    if [[ -f /root/atlas/journal/trade_ledger.json ]]; then
        total_trades=$(jq 'length' /root/atlas/journal/trade_ledger.json 2>/dev/null || echo "0")
    fi
    
    echo "$equity|$pnl|$trades_this_week|$errors_this_week|$total_trades"
}

get_nrl_data() {
    local tips_accuracy="N/A"
    local auto_submit_status="Unknown"
    local last_run="Never"
    
    # Check for recent tip submission logs
    if [[ -f /root/NRL-Predict/logs/nrl-cron-tips.log.1 ]]; then
        last_run=$(stat -c %y /root/NRL-Predict/logs/nrl-cron-tips.log.1 | cut -d' ' -f1)
        
        # Check if auto-submit succeeded
        if grep -qi "successfully submitted" /root/NRL-Predict/logs/nrl-cron-tips.log.1 2>/dev/null; then
            auto_submit_status="✅ Success"
        elif grep -qi "error\|fail" /root/NRL-Predict/logs/nrl-cron-tips.log.1 2>/dev/null; then
            auto_submit_status="❌ Failed"
        else
            auto_submit_status="⚠️ Unknown"
        fi
    fi
    
    echo "$tips_accuracy|$auto_submit_status|$last_run"
}

get_infrastructure_data() {
    local service_restarts=0
    local disk_usage=$(df -h / | awk 'NR==2 {print $5}')
    local backup_count=0
    local backup_size="N/A"
    
    # Count service restarts this week
    service_restarts=$(journalctl --since "$START_DATE" --no-pager | \
        grep -ci 'Started\|Stopped\|Restarted' || echo "0")
    
    # Check backup status (if restic is configured)
    if command -v restic &>/dev/null && [[ -n "${RESTIC_REPOSITORY:-}" ]]; then
        backup_count=$(restic snapshots --json 2>/dev/null | jq 'length' || echo "0")
        backup_size=$(restic stats latest --json 2>/dev/null | jq -r '.total_size' | numfmt --to=iec || echo "N/A")
    else
        backup_count="Not configured"
    fi
    
    echo "$service_restarts|$disk_usage|$backup_count|$backup_size"
}

get_open_tasks() {
    local task_file="/root/tasks/portfolio-gap-analysis-2026-q1.md"
    local open_tasks=""
    
    if [[ -f "$task_file" ]]; then
        # Extract unchecked tasks from Phase 1 (highest priority)
        open_tasks=$(grep '^\[ \]' "$task_file" | head -5 | sed 's/^\[ \] /• /' || echo "• No open tasks found")
    else
        open_tasks="• Task file not found"
    fi
    
    echo "$open_tasks"
}

get_failed_services() {
    local failed=$(systemctl list-units --state=failed --no-pager --no-legend | wc -l)
    local failed_list=""
    
    if [[ $failed -gt 0 ]]; then
        failed_list=$(systemctl list-units --state=failed --no-pager --no-legend | \
            awk '{print "• " $1}' | head -5)
    fi
    
    echo "$failed|$failed_list"
}

# ============================================================================
# BUILD TELEGRAM MESSAGE
# ============================================================================

build_message() {
    # Collect all data
    local atlas_data=$(get_atlas_sp500_data)
    local nrl_data=$(get_nrl_data)
    local infra_data=$(get_infrastructure_data)
    local open_tasks=$(get_open_tasks)
    local failed_data=$(get_failed_services)
    
    # Parse data
    IFS='|' read -r atlas_equity atlas_pnl atlas_trades atlas_errors atlas_total <<< "$atlas_data"
    IFS='|' read -r nrl_accuracy nrl_submit nrl_last_run <<< "$nrl_data"
    IFS='|' read -r infra_restarts infra_disk infra_backups infra_backup_size <<< "$infra_data"
    IFS='|' read -r failed_count failed_list <<< "$failed_data"
    
    # Format equity/PnL
    if [[ "$atlas_equity" != "N/A" ]]; then
        atlas_equity=$(printf "\$%.2f" "$atlas_equity")
    fi
    
    # Build HTML message
    cat << EOF
📊 <b>WEEKLY PORTFOLIO ROLLUP</b>
Week ${WEEK_NUM} | ${START_DATE} → ${END_DATE}

━━━━━━━━━━━━━━━━━━━━━━━━

🔵 <b>ATLAS SP500</b>

<b>Equity:</b> ${atlas_equity}
<b>Trades this week:</b> ${atlas_trades}
<b>Total trades (all-time):</b> ${atlas_total} of 30 target
<b>Errors logged:</b> ${atlas_errors}

━━━━━━━━━━━━━━━━━━━━━━━━

🏉 <b>NRL-PREDICT</b>

<b>Tips accuracy:</b> ${nrl_accuracy}
<b>Auto-submit:</b> ${nrl_submit}
<b>Last run:</b> ${nrl_last_run}

━━━━━━━━━━━━━━━━━━━━━━━━

⚙️ <b>INFRASTRUCTURE</b>

<b>Service restarts:</b> ${infra_restarts}
<b>Failed services:</b> ${failed_count}
<b>Disk usage:</b> ${infra_disk}
<b>Backup snapshots:</b> ${infra_backups}
<b>Backup size:</b> ${infra_backup_size}

EOF

    # Add failed services list if any
    if [[ -n "$failed_list" && "$failed_count" -gt 0 ]]; then
        cat << EOF

<blockquote><b>⚠️ Failed Services:</b>
${failed_list}</blockquote>

EOF
    fi

    # Add open action items
    cat << EOF
━━━━━━━━━━━━━━━━━━━━━━━━

📋 <b>TOP PRIORITY ACTIONS</b>

${open_tasks}

━━━━━━━━━━━━━━━━━━━━━━━━

<i>Generated: $(date '+%Y-%m-%d %H:%M AEST')</i>
EOF
}

# ============================================================================
# SEND TO TELEGRAM
# ============================================================================

send_telegram() {
    local message="$1"
    
    # Escape HTML special characters in text (but not in tags)
    # Note: Already properly formatted HTML, just send it
    
    local response=$(curl -s -X POST "$TELEGRAM_API" \
        -d "chat_id=$CHAT_ID" \
        -d "parse_mode=HTML" \
        --data-urlencode "text=$message")
    
    if echo "$response" | jq -e '.ok' > /dev/null 2>&1; then
        echo "[$(date)] ✅ Weekly rollup sent successfully"
        return 0
    else
        echo "[$(date)] ❌ Failed to send message: $response"
        return 1
    fi
}

# ============================================================================
# MAIN EXECUTION
# ============================================================================

main() {
    echo "[$(date)] Starting weekly rollup report generation..."
    
    # Build message
    local message=$(build_message)
    
    # Send to Telegram
    send_telegram "$message"
    
    echo "[$(date)] Weekly rollup report complete"
}

main "$@"
