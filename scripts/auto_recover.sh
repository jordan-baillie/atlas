#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Atlas Auto-Recovery Agent
#
# Spawned by pi-cron.sh when a daily operation fails.
# Diagnoses the failure, attempts targeted fixes, re-runs the
# failed operation, and reports all progress to Telegram.
#
# Usage (called automatically by pi-cron.sh):
#   auto_recover.sh <mode> <market> <logfile> <attempt>
#
# Modes: premarket, postclose, research
# Max 2 recovery attempts before escalating to human.
# ═══════════════════════════════════════════════════════════════
set -uo pipefail  # no -e: we handle errors ourselves

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
NOTIFY="$SCRIPT_DIR/telegram_notify.py"
SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-daily"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MODE="${1:?Usage: auto_recover.sh <mode> <market> <logfile> <attempt>}"
MARKET="${2:-sp500}"
FAILED_LOG="${3:-}"
ATTEMPT="${4:-1}"
MAX_ATTEMPTS=2
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
RECOVER_LOG="$LOG_DIR/recover_${MODE}_${TIMESTAMP}.log"

notify() {
    timeout 60 python3 "$NOTIFY" "$@" 2>>"$LOG_DIR/telegram.log" || true
}

tg() {
    # Quick inline telegram message (30s timeout to prevent hangs)
    timeout 30 python3 -c "
from utils.telegram import send_message
send_message('''$1''')
" 2>>"$LOG_DIR/telegram.log" || true
}

log() {
    echo "$(date -Iseconds) $*" | tee -a "$RECOVER_LOG"
}

# ── Notify start ──
log "Recovery attempt $ATTEMPT/$MAX_ATTEMPTS for $MODE ($MARKET)"
tg "🔧 <b>Atlas Auto-Recovery</b> [attempt $ATTEMPT/$MAX_ATTEMPTS]
<i>$(date '+%Y-%m-%d %H:%M')</i>

<b>Failed mode:</b> $MODE
<b>Diagnosing...</b>"

# ═══════════════════════════════════════════════════════════════
# Phase 1: Diagnose — check infrastructure before spawning agent
# ═══════════════════════════════════════════════════════════════
DIAGNOSIS=""
FIXES_APPLIED=""

# Check 1a: OpenD / Moomoo gateway (for SP500)
# Skip if broker is Alpaca (no OpenD dependency)
BROKER=$(python3 -c "import json; print(json.load(open('$PROJECT/config/active/sp500.json')).get('trading',{}).get('broker',''))" 2>/dev/null || echo "")
OPEND_OK=true
if [ "$BROKER" = "alpaca" ]; then
    log "Broker is Alpaca — skipping OpenD check"
elif ! python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(('127.0.0.1', 11111))
s.close()
print('ok')
" 2>/dev/null | grep -q "ok"; then
    OPEND_OK=false
    DIAGNOSIS="${DIAGNOSIS}🔌 OpenD gateway unreachable on port 11111\n"
    log "OpenD DOWN — attempting restart"

    # Try to restart OpenD
    if [ -f "$PROJECT/scripts/start_opend.sh" ]; then
        bash "$PROJECT/scripts/start_opend.sh" >> "$RECOVER_LOG" 2>&1
        sleep 10  # give it time to initialize
        # Re-check
        if python3 -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(3)
s.connect(('127.0.0.1', 11111))
s.close()
" 2>/dev/null; then
            FIXES_APPLIED="${FIXES_APPLIED}✅ Restarted OpenD successfully\n"
            OPEND_OK=true
            log "OpenD restarted OK"
        else
            FIXES_APPLIED="${FIXES_APPLIED}❌ OpenD restart failed\n"
            log "OpenD restart FAILED"
        fi
    elif command -v FutuOpenD >/dev/null 2>&1; then
        nohup FutuOpenD >> "$LOG_DIR/opend.log" 2>&1 &
        sleep 10
        if python3 -c "
import socket; s = socket.socket(); s.settimeout(3); s.connect(('127.0.0.1', 11111)); s.close()
" 2>/dev/null; then
            FIXES_APPLIED="${FIXES_APPLIED}✅ Started OpenD process\n"
            OPEND_OK=true
            log "OpenD started OK"
        else
            FIXES_APPLIED="${FIXES_APPLIED}❌ OpenD start failed\n"
            log "OpenD start FAILED"
        fi
    else
        FIXES_APPLIED="${FIXES_APPLIED}⚠️ No OpenD start script found\n"
        log "No OpenD start mechanism available"
    fi
fi

# Check 1b: IB Gateway / IBKR (for ASX)
# Skip if broker is Alpaca (no IBKR dependency, ASX/HK deactivated)
IBGW_OK=true
if [ "$BROKER" = "alpaca" ]; then
    log "Broker is Alpaca — skipping IB Gateway check"
elif ! nc -z localhost 4001 2>/dev/null; then
    IBGW_OK=false
    DIAGNOSIS="${DIAGNOSIS}🔌 IB Gateway unreachable on port 4001\n"
    log "IB Gateway DOWN — attempting restart"

    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "atlas-ibgateway"; then
        docker restart atlas-ibgateway >> "$RECOVER_LOG" 2>&1
        sleep 90  # IB Gateway needs time to authenticate (may need 2FA approval)
        if nc -z localhost 4001 2>/dev/null; then
            FIXES_APPLIED="${FIXES_APPLIED}✅ Restarted IB Gateway container\n"
            IBGW_OK=true
            log "IB Gateway restarted OK"
        else
            FIXES_APPLIED="${FIXES_APPLIED}❌ IB Gateway restart failed (may need 2FA approval on IBKR Mobile)\n"
            log "IB Gateway restart FAILED"
        fi
    else
        FIXES_APPLIED="${FIXES_APPLIED}⚠️ IB Gateway container not found — run: cd /root/atlas/docker && docker compose -f docker-compose-ibgw.yml up -d\n"
        log "No IB Gateway container to restart"
    fi
fi

# Check 2: Network connectivity
NETWORK_OK=true
if ! python3 -c "
import urllib.request
urllib.request.urlopen('https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=1d', timeout=10)
print('ok')
" 2>/dev/null | grep -q "ok"; then
    NETWORK_OK=false
    DIAGNOSIS="${DIAGNOSIS}🌐 Yahoo Finance API unreachable\n"
    log "Network/yfinance DOWN"
    # Network issues auto-resolve — just note it
    FIXES_APPLIED="${FIXES_APPLIED}⚠️ Network issue — will retry (may be transient)\n"
fi

# Check 3: Disk space
DISK_PCT=$(df /root --output=pcent | tail -1 | tr -d ' %')
if [ "$DISK_PCT" -gt 90 ]; then
    DIAGNOSIS="${DIAGNOSIS}💾 Disk ${DISK_PCT}% full\n"
    log "Disk critically full: ${DISK_PCT}%"
    # Emergency cleanup
    bash "$PROJECT/scripts/weekly_maintenance.sh" >> "$RECOVER_LOG" 2>&1
    FIXES_APPLIED="${FIXES_APPLIED}✅ Ran emergency maintenance cleanup\n"
    log "Emergency cleanup done"
fi

# Check 4: Scan failed log for known error patterns
CODE_ERROR=false
CODE_ERROR_DETAIL=""
if [ -n "$FAILED_LOG" ] && [ -f "$FAILED_LOG" ]; then
    if grep -qi "MemoryError\|OOM\|Killed" "$FAILED_LOG"; then
        DIAGNOSIS="${DIAGNOSIS}🧠 Process killed (OOM)\n"
    fi
    if grep -qi "ModuleNotFoundError\|ImportError" "$FAILED_LOG"; then
        DIAGNOSIS="${DIAGNOSIS}📦 Missing Python module\n"
        MODULE=$(grep -oP "No module named '\K[^']+" "$FAILED_LOG" | head -1)
        if [ -n "$MODULE" ]; then
            # SAFETY: never auto-install numpy/scipy/pandas — ABI-sensitive.
            # Use requirements.txt: pip install -r requirements.txt
            if echo "$MODULE" | grep -qiE '^(numpy|scipy|pandas)$'; then
                log "REFUSING auto-install of $MODULE (ABI-sensitive). Run: pip install --break-system-packages -r /root/atlas/requirements.txt"
                DIAGNOSIS="${DIAGNOSIS}⚠️ $MODULE needs manual reinstall via requirements.txt\n"
            else
                log "Attempting pip install $MODULE"
                pip install --break-system-packages "$MODULE" >> "$RECOVER_LOG" 2>&1 && \
                    FIXES_APPLIED="${FIXES_APPLIED}✅ Installed missing module: $MODULE\n"
            fi
        fi
    fi
    if grep -qi "JSONDecodeError\|json.decoder" "$FAILED_LOG"; then
        DIAGNOSIS="${DIAGNOSIS}📄 Corrupted JSON file\n"
    fi
    # Check for code errors that need an agent to fix
    if grep -qiE "TypeError|AttributeError|NameError|SyntaxError|KeyError|IndexError|ValueError|takes [0-9]+ positional argument|has no attribute|is not defined" "$FAILED_LOG"; then
        # Extract the traceback + error
        CODE_ERROR_DETAIL=$(python3 -c "
import re, sys
text = open('$FAILED_LOG').read()
# Find the last traceback
tbs = list(re.finditer(r'Traceback \(most recent call last\):.*?(?=\n[^\s]|\Z)', text, re.DOTALL))
if tbs:
    print(tbs[-1].group()[:2000])
else:
    # Fallback: grep ERROR/FAIL lines with error type keywords
    error_kws = ['TypeError','AttributeError','NameError','SyntaxError',
                 'KeyError','IndexError','ValueError','takes','positional',
                 'has no attribute','is not defined','failed:','ERROR']
    seen = set()
    for line in text.split('\n'):
        if any(e in line for e in error_kws) and line.strip() not in seen:
            seen.add(line.strip())
            print(line)
            if len(seen) >= 20:
                break
" 2>/dev/null)
        # Only set CODE_ERROR if we actually extracted something useful
        if [ -n "$CODE_ERROR_DETAIL" ] && [ "$(echo "$CODE_ERROR_DETAIL" | wc -w)" -gt 2 ]; then
            CODE_ERROR=true
            DIAGNOSIS="${DIAGNOSIS}🐛 Code error detected — will spawn agent to fix\n"
            log "Code error detected: $(echo "$CODE_ERROR_DETAIL" | head -3)"
        else
            DIAGNOSIS="${DIAGNOSIS}⚠️ Error pattern matched in log but no actionable traceback found\n"
            log "Error pattern matched but no extractable detail — skipping agent"
        fi
    fi
fi

# Default diagnosis if nothing specific found
if [ -z "$DIAGNOSIS" ]; then
    DIAGNOSIS="No infrastructure issues detected — likely a transient error\n"
fi


# ── Notify diagnosis ──
DIAG_MSG="🔧 <b>Atlas Auto-Recovery</b> [attempt $ATTEMPT/$MAX_ATTEMPTS]

<b>Diagnosis:</b>
$(echo -e "$DIAGNOSIS")
<b>Fixes applied:</b>
$(echo -e "${FIXES_APPLIED:-None yet — proceeding to re-run}")

<b>Re-running $MODE...</b>"

tg "$DIAG_MSG"

# ═══════════════════════════════════════════════════════════════
# Phase 2: Fix and re-run
#
# Two paths:
#   A) Code error → spawn a pi agent to diagnose and fix the bug,
#      then re-run the operation to verify.
#   B) Infrastructure / transient → re-run directly (fast path).
# ═══════════════════════════════════════════════════════════════
cd "$PROJECT"
RERUN_OK=false

if $CODE_ERROR; then
    # ── Path A: Spawn pi agent to fix code error ──
    log "Code error — spawning pi agent to diagnose and fix"
    tg "🤖 <b>Spawning code-fix agent…</b>
Error: <pre>$(python3 -c "from utils.telegram import _esc; print(_esc('''$(echo "$CODE_ERROR_DETAIL" | head -10)'''))" 2>/dev/null || echo "$CODE_ERROR_DETAIL" | head -5)</pre>"

    AGENT_LOG="$LOG_DIR/agent_fix_${MODE}_${TIMESTAMP}.log"

    # Build the agent prompt with error context
    AGENT_PROMPT="You are fixing a code error in the Atlas trading system.

MODE: $MODE
MARKET: $MARKET

ERROR (from $FAILED_LOG):
\`\`\`
$(echo "$CODE_ERROR_DETAIL" | head -40)
\`\`\`

INSTRUCTIONS:
1. Read the traceback carefully. Identify the file and line where the error occurs.
2. Read that file to understand the context.
3. Fix the bug with a minimal, targeted edit. Do NOT refactor or change unrelated code.
4. After fixing, verify the fix by running the relevant command:
   - premarket: python3 scripts/cli.py ingest --market $MARKET && python3 scripts/cli.py plan --market $MARKET
   - postclose: python3 scripts/eod_settlement.py --market $MARKET && python3 dashboard/generate_data.py
   - research: python3 scripts/research_runner.py --run-all --max-experiments 1 --market $MARKET
5. If the operation succeeds, output EXACTLY: FIX_SUCCESS
6. If you cannot fix it, output EXACTLY: FIX_FAILED followed by a brief explanation.

SAFETY RULES:
- Do NOT modify config/active/*.json (live trading configs)
- Do NOT modify paper_engine/state/*.json (live state)
- Do NOT place or cancel any broker orders
- Only edit Python source files in strategies/, scripts/, utils/, backtest/, data/, services/, research/
- Make the smallest possible fix"

    # Run pi agent with timeout (non-interactive mode)
    # Load incident + state-queries + lessons skills for diagnostic knowledge
    SKILLS_ROOT="$PROJECT/pi-package/atlas-ops/skills"
    timeout 600 pi -p \
        --skill "$SKILLS_ROOT/atlas-incident" \
        --skill "$SKILLS_ROOT/atlas-state-queries" \
        --skill "$SKILLS_ROOT/atlas-lessons" \
        --skill "$SKILLS_ROOT/atlas-codebase" \
        "$AGENT_PROMPT" \
        >> "$AGENT_LOG" 2>&1
    AGENT_RC=$?
    log "Agent exit code: $AGENT_RC"

    if [ $AGENT_RC -eq 0 ] && grep -q "FIX_SUCCESS" "$AGENT_LOG"; then
        RERUN_OK=true
        FIXES_APPLIED="${FIXES_APPLIED}🤖 Pi agent diagnosed and fixed the code error\n"
        # Extract what the agent did (look for summary lines)
        AGENT_SUMMARY=$(grep -A2 "FIX_SUCCESS\|Fixed\|Edited\|Changed" "$AGENT_LOG" | head -5)
        log "Agent fix succeeded: $AGENT_SUMMARY"
    else
        log "Agent fix failed or timed out (rc=$AGENT_RC)"
        FIXES_APPLIED="${FIXES_APPLIED}❌ Pi agent could not fix the error (rc=$AGENT_RC)\n"

        # Fall through to direct re-run as last resort
        log "Falling through to direct re-run..."
    fi
fi

if ! $RERUN_OK; then
    # ── Path B: Direct re-run (infrastructure fix or agent fallback) ──
    log "Direct re-run: $MODE"

    case "$MODE" in
        premarket)
            log "Re-running premarket: ingest + plan"
            python3 scripts/cli.py ingest --market "$MARKET" >> "$RECOVER_LOG" 2>&1
            INGEST_RC=$?
            log "Ingest exit: $INGEST_RC"

            if [ $INGEST_RC -eq 0 ]; then
                python3 scripts/cli.py plan --market "$MARKET" >> "$RECOVER_LOG" 2>&1
                PLAN_RC=$?
                log "Plan exit: $PLAN_RC"
                [ $PLAN_RC -eq 0 ] && RERUN_OK=true
            fi
            ;;

        postclose)
            log "Re-running postclose: eod_settlement + dashboard"
            python3 scripts/eod_settlement.py --market "$MARKET" >> "$RECOVER_LOG" 2>&1
            EOD_RC=$?
            log "EOD settlement exit: $EOD_RC"

            python3 dashboard/generate_data.py >> "$RECOVER_LOG" 2>&1
            DASH_RC=$?
            log "Dashboard exit: $DASH_RC"

            cp -f dashboard/templates/index.html dashboard/data/index.html
            cp -f dashboard/templates/atlas.css dashboard/data/atlas.css 2>/dev/null

            [ $EOD_RC -eq 0 ] && RERUN_OK=true
            ;;

        research)
            log "Re-running research: research_runner --run-all --max-experiments 1"
            python3 scripts/research_runner.py --run-all --max-experiments 1 --market "$MARKET" >> "$RECOVER_LOG" 2>&1
            RR_RC=$?
            log "Research runner exit: $RR_RC"
            [ $RR_RC -eq 0 ] && RERUN_OK=true
            ;;
    esac
fi

# ═══════════════════════════════════════════════════════════════
# Phase 3: Report results
# ═══════════════════════════════════════════════════════════════
if $RERUN_OK; then
    log "Recovery SUCCEEDED"

    # Send success alerts (same as normal cron success)
    case "$MODE" in
        premarket)  notify premarket-approve "" "$MARKET" || notify premarket-ok "" "$MARKET" ;;
        postclose)  notify postclose-ok "$MARKET" ;;
        research)   notify research-complete "$MARKET" ;;
    esac

    SUCC_MSG="✅ <b>Atlas Auto-Recovery SUCCEEDED</b>

<b>Mode:</b> $MODE
<b>Attempt:</b> $ATTEMPT/$MAX_ATTEMPTS
<b>Fixes applied:</b>
$(echo -e "${FIXES_APPLIED:-Direct re-run fixed the issue}")

Operations resumed normally."

    # If agent fixed a code error, include summary
    if [ -n "${AGENT_LOG:-}" ] && [ -f "${AGENT_LOG:-/dev/null}" ]; then
        SUCC_MSG="${SUCC_MSG}

<b>Agent log:</b> <code>$(basename "$AGENT_LOG")</code>"
    fi

    tg "$SUCC_MSG"

else
    log "Recovery FAILED (attempt $ATTEMPT)"

    if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then
        # Retry with incremented attempt counter
        log "Scheduling retry (attempt $((ATTEMPT + 1)))"
        tg "⚠️ <b>Atlas Recovery attempt $ATTEMPT failed</b>
Retrying in 60 seconds (attempt $((ATTEMPT + 1))/$MAX_ATTEMPTS)..."

        sleep 60
        exec "$0" "$MODE" "$MARKET" "$FAILED_LOG" "$((ATTEMPT + 1))"
    else
        # Max attempts reached — escalate to human
        log "MAX ATTEMPTS REACHED — escalating"

        # Extract last errors from recovery log
        TAIL=$(tail -15 "$RECOVER_LOG" | head -10)

        tg "🚨 <b>Atlas Auto-Recovery FAILED</b>

<b>Mode:</b> $MODE
<b>Attempts:</b> $ATTEMPT/$MAX_ATTEMPTS exhausted
<b>Diagnosis:</b>
$(echo -e "$DIAGNOSIS")
<b>Fixes tried:</b>
$(echo -e "$FIXES_APPLIED")
<b>Recovery log tail:</b>
<pre>$(python3 -c "from utils.telegram import _esc; print(_esc('''$TAIL'''))")</pre>

<b>Manual intervention required.</b>
Run: <code>cd /root/atlas &amp;&amp; scripts/pi-cron.sh $MODE $MARKET</code>"
    fi
fi

log "Auto-recovery complete"
exit $($RERUN_OK && echo 0 || echo 1)
