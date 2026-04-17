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
unset ANTHROPIC_API_KEY CLAUDE_API_KEY  # Atlas hardening: force pi to use OAuth (Claude Max)

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

# ── Start ──
log "Recovery attempt $ATTEMPT/$MAX_ATTEMPTS for $MODE ($MARKET)"

# ═══════════════════════════════════════════════════════════════
# Phase 1: Diagnose — check infrastructure before spawning agent
# ═══════════════════════════════════════════════════════════════
DIAGNOSIS=""
FIXES_APPLIED=""

# Check 1: Alpaca broker connectivity
BROKER_OK=true
if ! python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from brokers.registry import get_broker
b = get_broker('sp500')
eq = b.get_account_equity()
assert eq > 0, f'Equity is {eq}'
print('ok')
" 2>/dev/null | grep -q "ok"; then
    BROKER_OK=false
    DIAGNOSIS="${DIAGNOSIS}🔌 Alpaca broker unreachable or returning \$0 equity\n"
    log "Broker connectivity FAILED"
    FIXES_APPLIED="${FIXES_APPLIED}⚠️ Alpaca broker unreachable — check API keys and /etc/hosts DNS entry\n"
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


log "Diagnosis: $(echo -e "$DIAGNOSIS" | tr '\n' ' ')"
log "Fixes: $(echo -e "${FIXES_APPLIED:-none}" | tr '\n' ' ')"

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
    log "Spawning code-fix agent..."

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
   - postclose: python3 scripts/eod_settlement.py --market $MARKET
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
        --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
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
            log "Re-running postclose: eod_settlement"
            python3 scripts/eod_settlement.py --market "$MARKET" >> "$RECOVER_LOG" 2>&1
            EOD_RC=$?
            log "EOD settlement exit: $EOD_RC"

            # generate_data.py retired in Phase 5 — dashboard served via SQLite API
            # python3 dashboard/generate_data.py >> "$RECOVER_LOG" 2>&1

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
# Phase 3: Report results — single Telegram message on final outcome
# ═══════════════════════════════════════════════════════════════
if $RERUN_OK; then
    log "Recovery SUCCEEDED"
    # Only notify if we actually fixed something (not just a transient retry)
    if [ -n "${FIXES_APPLIED:-}" ]; then
        tg "✅ <b>Atlas Auto-Recovery</b> — $MODE fixed
$(echo -e "$FIXES_APPLIED")"
    fi
else
    log "Recovery FAILED (attempt $ATTEMPT)"

    if [ "$ATTEMPT" -lt "$MAX_ATTEMPTS" ]; then
        log "Scheduling retry (attempt $((ATTEMPT + 1)))"
        sleep 60
        exec "$0" "$MODE" "$MARKET" "$FAILED_LOG" "$((ATTEMPT + 1))"
    else
        # Max attempts exhausted — this is the only mandatory alert
        log "MAX ATTEMPTS REACHED — escalating"
        tg "🚨 <b>Atlas Recovery FAILED</b> — $MODE
Attempts: $ATTEMPT exhausted
$(echo -e "$DIAGNOSIS" | head -5)
<code>scripts/pi-cron.sh $MODE $MARKET</code>"
    fi
fi

log "Auto-recovery complete"
exit $($RERUN_OK && echo 0 || echo 1)
