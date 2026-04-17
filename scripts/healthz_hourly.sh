#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Atlas Consolidated Hourly Health Check
#
# Runs every hour. Checks EVERYTHING, fixes what it can, alerts
# on what it can't. Replaces:
#   - healthz_autofix.sh (weekday pre-premarket)
#   - pi-cron.sh health-check (Saturday strategy health)
#   - pi-cron.sh reconcile (weekday reconciliation)
#
# Flow:
#   1. Run healthz.py (full system audit)
#   2. Run reconcile_positions.py (broker ↔ local state)
#   3. If issues found → spawn pi agent to fix
#   4. Cooldown: same issue won't re-alert within 4 hours
#
# Cron: 0 * * * * /root/atlas/scripts/healthz_hourly.sh
# ═══════════════════════════════════════════════════════════════
set -uo pipefail
unset ANTHROPIC_API_KEY CLAUDE_API_KEY  # Atlas hardening: force pi to use OAuth (Claude Max)

PROJECT="/root/atlas"
HEALTHZ="$PROJECT/pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py"
RECONCILE="$PROJECT/scripts/reconcile_positions.py"
LOG_DIR="$PROJECT/logs"
COOLDOWN_DIR="$LOG_DIR/healthz-cooldowns"
COOLDOWN_HOURS=4
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/healthz-hourly_${TIMESTAMP}.log"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

mkdir -p "$LOG_DIR" "$COOLDOWN_DIR"

log() { echo "$(date -Iseconds) $*" >> "$LOG_FILE"; }

log "=== Atlas Hourly Health Check ==="

# ── Step 1: Run healthcheck (JSON mode) ──────────────────────
cd "$PROJECT"
HEALTHZ_JSON=$(python3 "$HEALTHZ" --market sp500 --json 2>/dev/null)
HEALTHZ_EXIT=$?

log "Healthcheck exit code: $HEALTHZ_EXIT"

# ── Step 2: Run reconciliation ────────────────────────────────
RECONCILE_OUT=""
RECONCILE_EXIT=0
# Only reconcile during US market hours (Mon-Fri, roughly 00:00-08:00 AEST = US trading session)
DOW=$(date +%u)  # 1=Mon, 7=Sun
HOUR=$(date +%H)
if [ "$DOW" -le 5 ] || [ "$DOW" -eq 6 ]; then
    # Tue-Sat AEST covers Mon-Fri US sessions
    RECONCILE_OUT=$(python3 "$RECONCILE" --market sp500 --fix 2>&1) || RECONCILE_EXIT=$?
    log "Reconcile exit code: $RECONCILE_EXIT"
    if [ -n "$RECONCILE_OUT" ]; then
        log "Reconcile output:"
        echo "$RECONCILE_OUT" >> "$LOG_FILE"
    fi
fi

# ── Step 3: Extract issues ───────────────────────────────────
ISSUES=$(echo "$HEALTHZ_JSON" | python3 -c "
import sys, json
try:
    report = json.load(sys.stdin)
except:
    sys.exit(1)

# Issues to IGNORE (expected state, not actionable)
IGNORE = {
    'cron_research',       # Research disabled intentionally
    'cron_dashboard',      # Dashboard refresh via service, not cron
}

issues = []
for sec_name, sec in report['sections'].items():
    for c in sec['checks']:
        if c['verdict'] != 'ok' and c['check'] not in IGNORE:
            issues.append(f\"[{c['verdict'].upper()}] {sec_name}/{c['check']}: {c['message']}\")
if not issues:
    sys.exit(1)
print('\n'.join(issues))
" 2>/dev/null)

# Add reconcile issues if any
if [ "$RECONCILE_EXIT" -ne 0 ] && [ -n "$RECONCILE_OUT" ]; then
    RECONCILE_ISSUES=$(echo "$RECONCILE_OUT" | grep -i "DISCREPANCY\|MISMATCH\|ERROR\|missing" | head -5)
    if [ -n "$RECONCILE_ISSUES" ]; then
        ISSUES="${ISSUES}
[WARN] reconcile: $RECONCILE_ISSUES"
    fi
fi

# ── Step 4: Check cooldowns ──────────────────────────────────
# Filter out issues that were already alerted within COOLDOWN_HOURS
filter_cooldowns() {
    local issues="$1"
    local new_issues=""
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        # Create a stable hash for the issue (strip timestamps/numbers for dedup)
        local hash=$(echo "$line" | sed 's/[0-9]*\.[0-9]*/N/g; s/[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}/DATE/g' | md5sum | cut -c1-12)
        local cooldown_file="$COOLDOWN_DIR/$hash"
        if [ -f "$cooldown_file" ]; then
            local file_age=$(( ($(date +%s) - $(stat -c %Y "$cooldown_file")) / 3600 ))
            if [ "$file_age" -lt "$COOLDOWN_HOURS" ]; then
                continue  # Skip — still in cooldown
            fi
        fi
        # Not in cooldown — include it and set cooldown
        echo "$line" > "$cooldown_file"
        new_issues="${new_issues}${line}
"
    done <<< "$issues"
    echo "$new_issues"
}

if [ -z "$ISSUES" ] && [ "$HEALTHZ_EXIT" -eq 0 ]; then
    log "System healthy — no action needed"
    # Clean old logs (keep 3 days of hourly logs)
    find "$LOG_DIR" -name "healthz-hourly_*.log" -mtime +3 -delete 2>/dev/null
    find "$COOLDOWN_DIR" -mtime +1 -delete 2>/dev/null
    exit 0
fi

ISSUE_COUNT=$(echo "$ISSUES" | grep -c '.' || true)
log "Found $ISSUE_COUNT issue(s) before cooldown filter"

NEW_ISSUES=$(filter_cooldowns "$ISSUES")
NEW_ISSUE_COUNT=$(echo "$NEW_ISSUES" | grep -c '.' || true)
log "After cooldown filter: $NEW_ISSUE_COUNT new issue(s)"

if [ "$NEW_ISSUE_COUNT" -eq 0 ]; then
    log "All issues in cooldown — skipping agent"
    find "$LOG_DIR" -name "healthz-hourly_*.log" -mtime +3 -delete 2>/dev/null
    exit 0
fi

log "Issues to fix:"
echo "$NEW_ISSUES" >> "$LOG_FILE"

# ── Step 4.5: Try fixing trivial issues in bash ──────────────
try_bash_fixes() {
    local issues="$1"
    local fixed=""
    local remaining=""

    while IFS= read -r line; do
        [ -z "$line" ] && continue
        case "$line" in
            *pycache*)
                find "$PROJECT" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
                fixed="${fixed}Cleaned __pycache__ dirs\n"
                log "Auto-fixed: cleaned __pycache__"
                ;;
            *large_logs*|*Large\ log*)
                # Compress rotated logs over 1MB
                find "$LOG_DIR" -name "*.log-*" -size +1M ! -name "*.gz" -exec gzip -f {} \; 2>/dev/null
                # Truncate active logs over 50MB
                find "$LOG_DIR" -name "*.log" -size +50M -exec truncate -s 0 {} \; 2>/dev/null
                fixed="${fixed}Compressed/truncated large logs\n"
                log "Auto-fixed: compressed large logs"
                ;;
            *broker_orders*)
                # Open orders (stop/limit orders) are normal — skip
                log "Skipped: open broker orders are expected (protective stops)"
                ;;
            *dashboard_data*)
                # Try refreshing dashboard data
                if systemctl is-active --quiet atlas-dashboard; then
                    systemctl restart atlas-dashboard 2>/dev/null
                    fixed="${fixed}Restarted dashboard refresh\n"
                    log "Auto-fixed: restarted atlas-dashboard"
                else
                    remaining="${remaining}${line}\n"
                fi
                ;;
            *)
                remaining="${remaining}${line}\n"
                ;;
        esac
    done <<< "$issues"

    # Send Telegram if we fixed things
    if [ -n "$fixed" ]; then
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message('🔧 <b>Watchdog auto-fixed:</b>\n$(echo -e "$fixed" | head -5)')
" 2>/dev/null || true
    fi

    # Return remaining unfixed issues (or empty)
    echo -e "$remaining"
}

REMAINING_ISSUES=$(try_bash_fixes "$NEW_ISSUES")
REMAINING_COUNT=$(echo "$REMAINING_ISSUES" | grep -c '.' || true)

if [ "$REMAINING_COUNT" -eq 0 ]; then
    log "All issues fixed by bash handlers — no agent needed"
    find "$LOG_DIR" -name "healthz-hourly_*.log" -mtime +3 -delete 2>/dev/null
    find "$COOLDOWN_DIR" -mtime +1 -delete 2>/dev/null
    exit 0
fi

log "$REMAINING_COUNT issue(s) need agent intervention:"
echo "$REMAINING_ISSUES" >> "$LOG_FILE"

# ── Step 5: Spawn pi agent to fix remaining issues ───────────
log "Spawning pi agent for autofix..." 

PROMPT="You are the Atlas infrastructure watchdog. The hourly health check found these issues:

$REMAINING_ISSUES

Fix every issue you can. Be fast and decisive.

ALLOWED (do these automatically):
- Restart services: systemctl restart atlas-telegram-bot, atlas-dashboard
- Truncate large logs: truncate -s 0 /root/atlas/logs/atlas.log (or tail -2000 to preserve recent)
- Clean __pycache__: find /root/atlas -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
- Run weekly maintenance: bash /root/atlas/scripts/weekly_maintenance.sh
- Refresh data cache: cd /root/atlas && python3 scripts/cli.py -m sp500 ingest
- Fix pip/package issues: pip install --break-system-packages --upgrade <package>
- Clean old log files: find /root/atlas/logs -name '*.log' -mtime +30 -delete
- Fix file permissions
- Sync protective orders: python3 /root/atlas/scripts/sync_protective_orders.py --market sp500

NOT ALLOWED (never do these):
- Do NOT edit any Python source code
- Do NOT modify config files (config/active/*.json)
- Do NOT place broker orders or modify positions
- Do NOT change crontab entries
- Do NOT run git operations
- Do NOT modify secrets files
- Do NOT restart/reboot the system
- Do NOT touch research services (intentionally disabled)

After fixing, run the healthcheck again:
  cd /root/atlas && python3 pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py --market sp500

TELEGRAM: Only send a notification if you actually FIXED something:
  python3 -c \"import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''YOUR_MSG''')\"

Rules:
- Send Telegram if you applied a fix (restarted a service, cleaned logs, refreshed data, etc.)
- Send Telegram if there are issues you CANNOT fix (needs manual intervention)
- Do NOT send Telegram for warnings that are informational only (weekend gaps, expected states)
- Keep it under 10 lines, use HTML (<b>, <code>)
- Be concise — this runs every hour, nobody wants a novel"

SKILLS_ROOT="$PROJECT/pi-package/atlas-ops/skills"

# ── Circuit breaker pre-check ────────────────────────────────
BREAKER_FILE="${CLAUDE_BREAKER_FILE:-/tmp/claude_breaker.json}"
if [ -f "$BREAKER_FILE" ]; then
    BREAKER_AGE=$(( $(date +%s) - $(stat -c %Y "$BREAKER_FILE" 2>/dev/null || echo 0) ))
    if [ "$BREAKER_AGE" -lt 18000 ]; then
        REMAINING_MIN=$(( (18000 - BREAKER_AGE) / 60 ))
        log "Claude circuit breaker tripped (${REMAINING_MIN}m remaining) — skipping pi watchdog agent"
        exit 0
    else
        log "Claude circuit breaker expired — removing stale breaker file"
        rm -f "$BREAKER_FILE"
    fi
fi

timeout 300 pi -p \
    --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
    --no-session --model anthropic/claude-sonnet-4-6 \
    --skill "$SKILLS_ROOT/atlas-incident" \
    --skill "$SKILLS_ROOT/atlas-state-queries" \
    --skill "$SKILLS_ROOT/atlas-codebase" \
    "$PROMPT" >> "$LOG_FILE" 2>&1
PI_EXIT=$?

# ── Trip breaker if exhaustion detected ──────────────────────
if grep -qiE "out of extra usage|rate_limit_error|insufficient_quota|usage_limit" "$LOG_FILE" 2>/dev/null; then
    log "Detected Claude usage exhaustion in pi output — tripping circuit breaker"
    python3 -c "import sys; sys.path.insert(0,'$PROJECT'); from utils.claude_circuit_breaker import trip; trip('healthz_hourly')" 2>/dev/null || true
fi

log "Pi agent exit code: $PI_EXIT"

# If agent itself failed
if [ $PI_EXIT -ne 0 ]; then
    # Check if it was an API billing issue (don't spam alerts for this)
    if grep -q "out of extra usage\|billing\|rate_limit\|overloaded" "$LOG_FILE" 2>/dev/null; then
        log "Pi agent unavailable (API limit/billing) — skipping alert"
    else
        python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message('🚨 <b>Hourly watchdog agent crashed</b> (exit $PI_EXIT).\nCheck: <code>$LOG_FILE</code>')
" 2>/dev/null || true
    fi
fi

# ── Stale lock cleanup ───────────────────────────────────────
# Cron jobs use flock(1) advisory locks on /tmp/*.lock files.
# If a job crashes or times out, the kernel releases the flock,
# but the file persists. Clean locks >6h old with no holder.
for lockfile in /tmp/compute_daily_risk.lock /tmp/reconcile.lock \
                /tmp/sync_protective.lock /tmp/execute_approved.lock \
                /tmp/intraday_sp500.lock; do
    if [ -f "$lockfile" ]; then
        # Only remove if file is >6 hours old AND no process holds it
        file_age_hours=$(( ($(date +%s) - $(stat -c %Y "$lockfile")) / 3600 ))
        if [ "$file_age_hours" -ge 6 ] && ! fuser "$lockfile" >/dev/null 2>&1; then
            rm -f "$lockfile"
            log "Cleaned stale lock: $lockfile (age: ${file_age_hours}h)"
        fi
    fi
done

# ── Restic backup verification ───────────────────────────────
# Check that restic backups are completing. Alert if last snapshot >48h old.
RESTIC_REPOSITORY="/root/backups/restic-repo"
RESTIC_PASSWORD="atlas-backup-2026"
export RESTIC_PASSWORD RESTIC_REPOSITORY

if command -v restic &>/dev/null && [ -d "$RESTIC_REPOSITORY" ]; then
    LAST_SNAPSHOT_TIME=$(restic -r "$RESTIC_REPOSITORY" snapshots --latest 1 --json 2>/dev/null \
        | python3 -c "import sys,json; snaps=json.load(sys.stdin); snaps.sort(key=lambda x: x['time']); print(snaps[-1]['time'][:19] if snaps else '')" 2>/dev/null)
    if [ -n "$LAST_SNAPSHOT_TIME" ]; then
        SNAP_EPOCH=$(date -d "$LAST_SNAPSHOT_TIME" +%s 2>/dev/null || echo 0)
        NOW_EPOCH=$(date +%s)
        SNAP_AGE_HOURS=$(( (NOW_EPOCH - SNAP_EPOCH) / 3600 ))
        if [ "$SNAP_AGE_HOURS" -gt 48 ]; then
            log "WARNING: Last restic backup is ${SNAP_AGE_HOURS}h old (>48h threshold)"
            ISSUES="${ISSUES:-}
[WARN] backup/restic: Last snapshot is ${SNAP_AGE_HOURS}h old"
        else
            log "Restic backup OK: last snapshot ${SNAP_AGE_HOURS}h ago"
        fi
    else
        log "WARNING: Could not read restic snapshots"
    fi
fi
unset RESTIC_PASSWORD

# ── Cleanup ──────────────────────────────────────────────────
find "$LOG_DIR" -name "healthz-hourly_*.log" -mtime +3 -delete 2>/dev/null
find "$LOG_DIR" -name "healthz-autofix_*.log" -mtime +14 -delete 2>/dev/null
find "$COOLDOWN_DIR" -mtime +1 -delete 2>/dev/null

# Echo summary to stdout for cron log capture
cat "$LOG_FILE" 2>/dev/null

exit 0
