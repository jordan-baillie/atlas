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
# NOTE (2026-04-22): --fix is disabled by default until the reconcile_positions
# universe∪state-file filter is validated across 2 daily cycles. Set
# ATLAS_RECONCILE_AUTOFIX=1 in the environment (or cron line) to re-enable.
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
    # E2 mitigation: only pass --fix when explicitly enabled.
    # Until the universe∪state filter is validated across 2 full daily cycles,
    # auto-fix runs in REPORT-ONLY mode.
    RECONCILE_FIX_FLAG=""
    if [ "${ATLAS_RECONCILE_AUTOFIX:-0}" = "1" ]; then
        RECONCILE_FIX_FLAG="--fix"
    fi
    # Only reconcile markets with live_enabled=true in their config.
    # sector_etfs and commodity_etfs archived 2026-05-25 (live_enabled=false, 0 open positions).
    # This dynamic check prevents "get_live_broker returned None" error spam.
    ENABLED_MARKETS=()
    for MKT in sp500; do
        if [ -f "$PROJECT/config/active/${MKT}.json" ]; then
            LE=$(python3 -c "import json; print(json.load(open('$PROJECT/config/active/${MKT}.json')).get('trading',{}).get('live_enabled',False))" 2>/dev/null)
            if [ "$LE" = "True" ]; then
                ENABLED_MARKETS+=("$MKT")
            fi
        fi
    done
    for MKT in "${ENABLED_MARKETS[@]}"; do
        MKT_OUT=$(python3 "$RECONCILE" --market "$MKT" $RECONCILE_FIX_FLAG 2>&1) || MKT_EXIT=$?
        log "Reconcile $MKT exit code: ${MKT_EXIT:-0}"
        if [ -n "$MKT_OUT" ]; then
            log "Reconcile $MKT output:"
            echo "$MKT_OUT" >> "$LOG_FILE"
        fi
        # Accumulate output for the drift-detection step below
        RECONCILE_OUT="${RECONCILE_OUT}${MKT_OUT}
"
        # Track worst exit code
        if [ "${MKT_EXIT:-0}" -ne 0 ]; then
            RECONCILE_EXIT="${MKT_EXIT}"
        fi
        MKT_EXIT=0
    done
fi

# ── Step 2b: Hard-gate — immediate Telegram CRITICAL on drift ──
# Parses reconcile output for PHANTOM/UNTRACKED/MISMATCH/DRIFT keywords.
# If drift is detected we send a CRITICAL alert directly (NOT via the
# pi-agent cooldown path) so the operator knows even when --fix auto-corrects.
# This fires even if the fix succeeded — awareness is the goal.
if [ -n "$RECONCILE_OUT" ]; then
    DRIFT_LINES=$(echo "$RECONCILE_OUT" | grep -E "PHANTOM|UNTRACKED|MISMATCH|DRIFT" | head -10 || true)
    if [ -n "$DRIFT_LINES" ]; then
        # 4h cooldown: same drift signature won't re-alert within 4 hours
        DRIFT_HASH=$(echo "$DRIFT_LINES" | sed 's/[0-9]*\.[0-9]*/N/g; s/[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}/DATE/g' | md5sum | cut -c1-12)
        DRIFT_COOLDOWN_FILE="$COOLDOWN_DIR/drift_$DRIFT_HASH"
        DRIFT_SHOULD_ALERT=1
        if [ -f "$DRIFT_COOLDOWN_FILE" ]; then
            DRIFT_AGE_HRS=$(( ($(date +%s) - $(stat -c %Y "$DRIFT_COOLDOWN_FILE")) / 3600 ))
            if [ "$DRIFT_AGE_HRS" -lt "$COOLDOWN_HOURS" ]; then
                DRIFT_SHOULD_ALERT=0
                log "Drift alert in cooldown (${DRIFT_AGE_HRS}h old) — skipping Telegram for hash=$DRIFT_HASH"
            fi
        fi

        DRIFT_COUNT=$(echo "$DRIFT_LINES" | grep -c . || true)
        log "CRITICAL: ledger/broker drift detected (${DRIFT_COUNT} line(s))"

        if [ "$DRIFT_SHOULD_ALERT" -eq 1 ]; then
            touch "$DRIFT_COOLDOWN_FILE"
            log "Sending drift alert (hash=$DRIFT_HASH)"
            DRIFT_ESCAPED=$(echo "$DRIFT_LINES" | head -600 | python3 -c "
import sys
data = sys.stdin.read()
data = data.replace('\\\\', '\\\\\\\\').replace(\"'\", \"\\\\'\")[:500]
print(data)
" 2>/dev/null || echo "(see reconcile log)")
            python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
drift_msg = '$DRIFT_ESCAPED'
send_message(
    '\U0001f6a8 <b>Ledger\u2194Broker Drift Detected</b>\n\n'
    '<pre>' + drift_msg + '</pre>\n\n'
    '<i>Auto-fix was applied. Verify brokers/state/live_sp500.json '
    'and confirm strategy attribution is correct.</i>'
)
" 2>/dev/null || true
        fi
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
# P1-E (2026-04-24): research_queue removed (queue-retirement migration, file deleted);
#   dashboard_data removed (legacy vanilla-JS dashboard replaced by React/Vite served
#   dynamically — no static dashboard-data.json exists); cron_dashboard suppressed
#   because the refresh is now a systemd timer (atlas-dashboard-refresh.timer), not cron.
IGNORE = {
    'cron_research',       # Research disabled intentionally
    'cron_dashboard',      # Dashboard refresh via systemd timer, not cron (see below)
    'research_queue',      # Removed: queue file deleted in March queue-retirement migration
    'dashboard_data',      # Removed: legacy vanilla-JS path; React dashboard has no static file
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
            *)
                remaining="${remaining}${line}\n"
                ;;
        esac
    done <<< "$issues"

    # NOTE: pycache cleanup and log rotation are logged above — no Telegram needed.
    # Telegram only for things that wake the operator (see Step 2b / Step 5).

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

PROMPT=$(cat <<PROMPT_EOF
You are the Atlas infrastructure watchdog. The hourly health check found these issues:

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

TELEGRAM: Send a notification ONLY in these specific cases:
  1. A SERVICE RESTART succeeded after being DOWN (not just running 'restart')
  2. A repair you CANNOT complete that requires the operator (specific manual step needed)
  3. A new ERROR class never seen before that may indicate a real bug

Do NOT Telegram for:
  - Routine maintenance (pycache, log rotation, refresh)
  - Reconcile auto-fixes (UNTRACKED → fixed is routine, drift Telegram already sent)
  - Restarting a service that was already running (that is a no-op)
  - Confirming "all checks passed"
  - Reporting what you did when nothing was broken

If in doubt: SILENT. The Telegram channel is for things that wake the operator.

Use:
  python3 -c "import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''YOUR_MSG''')"

Keep messages under 8 lines, use HTML (<b>, <code>).
PROMPT_EOF
)

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

# ── Ledger integrity regression tests ───────────────────────
# Run the ledger integrity test suite to catch poison strategies /
# zero-stop / duplicate open rows before they accumulate again.
LEDGER_TEST_OUT=$(cd "$PROJECT" && python3 -m pytest tests/test_ledger_integrity.py -x --timeout=30 -q 2>&1)
LEDGER_TEST_RC=$?
if [ "$LEDGER_TEST_RC" -ne 0 ]; then
    log "CRITICAL: ledger integrity tests FAILED (rc=$LEDGER_TEST_RC)"
    log "$LEDGER_TEST_OUT"
    python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message(
    '❌ <b>Ledger integrity tests FAILED</b>
'
    'Run: python3 -m pytest tests/test_ledger_integrity.py -v
'
    'Check for poison strategies or duplicate open rows in trades table.'
)
" 2>/dev/null || true
    exit 1
else
    log "Ledger integrity tests OK"
fi

# ── Dashboard refresh timer check (replaces cron_dashboard check) ──
# P1-E (2026-04-24): healthz.py's cron_dashboard check reports "NOT scheduled"
# because the refresh moved to a systemd timer (atlas-dashboard-refresh.timer).
# The old cron check is ignored above; we do a direct systemctl check instead.
# 6h cooldown: avoid alerting every hour if the timer is transiently inactive.
if ! systemctl is-active --quiet atlas-dashboard-refresh.timer 2>/dev/null; then
    log "WARN: atlas-dashboard-refresh.timer is not active (dashboard rebuild may not run hourly)"
    TIMER_COOLDOWN_FILE="$COOLDOWN_DIR/dashboard_timer_inactive"
    TIMER_SHOULD_ALERT=1
    if [ -f "$TIMER_COOLDOWN_FILE" ]; then
        TIMER_AGE_HRS=$(( ($(date +%s) - $(stat -c %Y "$TIMER_COOLDOWN_FILE")) / 3600 ))
        if [ "$TIMER_AGE_HRS" -lt 6 ]; then
            TIMER_SHOULD_ALERT=0
            log "Dashboard timer alert in cooldown (${TIMER_AGE_HRS}h) — skipping Telegram"
        fi
    fi
    if [ "$TIMER_SHOULD_ALERT" -eq 1 ]; then
        touch "$TIMER_COOLDOWN_FILE"
        python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message(
    '\u26a0\ufe0f <b>atlas-dashboard-refresh.timer not active</b>\n'
    'The hourly React dashboard rebuild may not be running.\n'
    'Check: <code>systemctl status atlas-dashboard-refresh.timer</code>'
)
" 2>/dev/null || true
    fi
else
    log "Dashboard refresh timer OK: atlas-dashboard-refresh.timer active"
fi

# ── Dashboard dist staleness check ──────────────────────────
# Silent rebuild at >6h (expected — hourly timer may lag).
# Only alert if dist is >24h stale (timer may be broken).
DASHBOARD_DIST="$PROJECT/dashboard-ui/dist/index.html"
if [ -f "$DASHBOARD_DIST" ]; then
    DIST_AGE_H=$(( ($(date +%s) - $(stat -c %Y "$DASHBOARD_DIST")) / 3600 ))
    if [ "$DIST_AGE_H" -gt 24 ]; then
        log "WARNING: dashboard dist is ${DIST_AGE_H}h old (>24h — possible timer failure)"
        systemctl start atlas-dashboard-refresh.service 2>/dev/null || true
        python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
age = int('${DIST_AGE_H}')
send_message(
    '\u26a0\ufe0f <b>Dashboard dist is {}h stale</b>\n'
    'Expected hourly refresh via atlas-dashboard-refresh.timer.\n'
    'Triggered manual rebuild.'.format(age)
)
" 2>/dev/null || true
    elif [ "$DIST_AGE_H" -gt 6 ]; then
        log "Dashboard dist ${DIST_AGE_H}h old — triggering silent rebuild"
        systemctl start atlas-dashboard-refresh.service 2>/dev/null || true
    else
        log "Dashboard dist OK: ${DIST_AGE_H}h old"
    fi
fi


# ── Signal-write divergence check ───────────────────────────
# P1-D (2026-04-24): guard against silent-failure recurrence (d0b939d0 / P1-9).
# Compares proposed_entries in today's plan JSON vs SQLite signals rows.
# Alert fires within 1 hour if signals are generated but not persisted.
SIGNAL_WRITE_RC=0
python3 "$PROJECT/scripts/check_signal_writes.py" >> "$LOG_FILE" 2>&1 || SIGNAL_WRITE_RC=$?
if [ "$SIGNAL_WRITE_RC" -ne 0 ]; then
    log "WARN: signal-write divergence detected (check_signal_writes.py exit $SIGNAL_WRITE_RC) — alert sent"
else
    log "Signal-write check OK"
fi

# ── Overlay evaluator backlog check ──────────────────────────
# Wave B-2 (2026-04-28): alerts when >5 unevaluated overlay decisions are
# older than 2 days. Wraps overlay.evaluator.check_evaluator_backlog().
OVERLAY_BACKLOG_RC=0
python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from scripts.health_check import _check_overlay_backlog
sys.exit(0 if _check_overlay_backlog(threshold=5) else 1)
" >> "$LOG_FILE" 2>&1 || OVERLAY_BACKLOG_RC=$?
if [ "$OVERLAY_BACKLOG_RC" -ne 0 ]; then
    log "WARN: overlay evaluator backlog (exit $OVERLAY_BACKLOG_RC) — Telegram alert sent"
else
    log "Overlay evaluator backlog check OK"
fi

# ── SuperCoach API reachability check ────────────────────────
# Layered check: localhost direct (bypasses Caddy) + basicauth-authed public.
# Direct failure = app is down; public-only failure = Caddy routing broken.
SC_DIRECT_CODE=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/ --max-time 10 2>/dev/null || echo "000")
SC_PUBLIC_CODE="skip"
SC_PASS=$(jq -r '.caddy_basic_auth.password // .dashboard_pass // empty' /root/.atlas-secrets.json 2>/dev/null)
if [ -n "$SC_PASS" ]; then
    SC_PUBLIC_CODE=$(curl -sS -o /dev/null -w '%{http_code}' -u "atlas:$SC_PASS" http://127.0.0.1/api/ --max-time 10 2>/dev/null || echo "000")
fi

if [ "$SC_DIRECT_CODE" != "200" ]; then
    log "CRITICAL: SuperCoach API direct (localhost:8000/) returned $SC_DIRECT_CODE"
    python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message(
    '\U0001f6a8 <b>SuperCoach API DOWN</b>\n'
    'localhost:8000/ returned HTTP <code>$SC_DIRECT_CODE</code>\n'
    'Check: <code>systemctl status supercoach-api</code>\n'
    'Watchdog log: <code>/var/log/supercoach-watchdog.log</code>'
)
" 2>/dev/null || true
elif [ "$SC_PUBLIC_CODE" != "skip" ] && [ "$SC_PUBLIC_CODE" != "200" ]; then
    log "WARN: SuperCoach API public (Caddy /api/) returned $SC_PUBLIC_CODE (direct OK)"
    python3 -c "
import sys
sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message(
    '\u26a0\ufe0f <b>SuperCoach API public route broken</b>\n'
    'Direct localhost:8000/ = 200, Caddy /api/ = <code>$SC_PUBLIC_CODE</code>\n'
    'Check <code>/etc/caddy/Caddyfile</code> and <code>systemctl status caddy</code>'
)
" 2>/dev/null || true
else
    log "SuperCoach API OK: direct=$SC_DIRECT_CODE public=$SC_PUBLIC_CODE"
fi

# ── Cleanup ──────────────────────────────────────────────────
find "$LOG_DIR" -name "healthz-hourly_*.log" -mtime +3 -delete 2>/dev/null
find "$LOG_DIR" -name "healthz-autofix_*.log" -mtime +14 -delete 2>/dev/null
find "$COOLDOWN_DIR" -mtime +1 -delete 2>/dev/null

# Echo summary to stdout for cron log capture
cat "$LOG_FILE" 2>/dev/null

exit 0
