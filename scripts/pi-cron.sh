#!/bin/bash
# Atlas Pi Cron Wrapper
# Triggers pi in non-interactive mode for scheduled daily operations.
# Sends Telegram alerts on success and failure.
#
# Cron schedule (AEST via TZ=Australia/Brisbane in crontab):
#   30 8  * * 1-5  /root/atlas/scripts/pi-cron.sh premarket
#   00 08 * * 2-6  /root/atlas/scripts/pi-cron.sh postclose
#
# Setup:
#   1. Ensure pi is logged in: pi (interactive) — OAuth login persists in ~/.pi/agent/auth.json
#   2. Credentials in ~/.atlas-secrets.json (telegram_bot_token, telegram_chat_id)
#   3. Run: crontab -e  (see schedule above)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"

# Global error trap — log + alert on any unhandled crash (prevents silent failures)
_cron_error_trap() {
    local exit_code=$?
    local line_no=$1
    mkdir -p "$LOG_DIR"
    echo "$(date -Iseconds) FATAL: pi-cron.sh crashed at line $line_no (exit=$exit_code)" >> "$LOG_DIR/pi-cron.log"
    python3 "$PROJECT/scripts/telegram_notify.py" error "cron-crash" "" 2>/dev/null || true
}
trap '_cron_error_trap $LINENO' ERR
SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-daily"
RESEARCH_SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-research-loop"
NOTIFY="$SCRIPT_DIR/telegram_notify.py"
RESEARCH_LOCK="/tmp/atlas-research.lock"

mkdir -p "$LOG_DIR"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MODE="${1:-}"
MARKET="${2:-${ATLAS_MARKET:-sp500}}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

# --- Helper: send Telegram alert (best-effort, never blocks cron exit) ---
# 60s timeout prevents hangs (e.g. Moomoo socket stuck in send_postclose_summary)
notify() {
    timeout 60 python3 "$NOTIFY" "$@" 2>>"$LOG_DIR/telegram.log" || true
}

AGENT_ID="${3:-atlas-research}"

case "$MODE" in
    premarket)
        # ── Pre-flight: volatility gate check ──────────────────────────────
        # Check macro indicators before generating the plan.
        # Exit codes: 0=ok, 1=reduce(50%), 2=block(skip entries)
        VOL_GATE_LOG="$LOG_DIR/volatility_gate_${TIMESTAMP}.json"
        VOL_GATE_EXIT=0
        set +e
        python3 "$PROJECT/scripts/volatility_gate.py" \
            --check --market "$MARKET" --json \
            > "$VOL_GATE_LOG" 2>>"$LOG_DIR/pi-cron.log"
        VOL_GATE_EXIT=$?
        set -e

        # Parse gate result for prompt context
        VOL_GATE_ACTION="none"
        VOL_GATE_FLAGS=""
        if [ -f "$VOL_GATE_LOG" ]; then
            VOL_GATE_ACTION=$(python3 -c "import json,sys; d=json.load(open('$VOL_GATE_LOG')); print(d.get('action','none'))" 2>/dev/null || echo "none")
            VOL_GATE_FLAGS=$(python3 -c "import json,sys; d=json.load(open('$VOL_GATE_LOG')); print(','.join(d.get('flags',[])))" 2>/dev/null || echo "")
        fi
        echo "$(date -Iseconds) Volatility gate: action=$VOL_GATE_ACTION flags=$VOL_GATE_FLAGS" >> "$LOG_DIR/pi-cron.log"

        # If gate BLOCKS, send alert immediately and skip the planning agent
        if [ "$VOL_GATE_EXIT" -eq 2 ] || [ "$VOL_GATE_ACTION" = "block" ]; then
            echo "$(date -Iseconds) Volatility gate BLOCKED entries — sending alert, skipping plan" >> "$LOG_DIR/pi-cron.log"
            python3 "$PROJECT/scripts/volatility_gate.py" \
                --check --market "$MARKET" --alert \
                >> "$LOG_DIR/pi-cron.log" 2>&1 || true
            notify error "volatility-gate-block" "" 2>/dev/null || true
            exit 0   # Not an error — clean exit, entries intentionally paused
        fi

        # Build volatility context for the planning prompt
        if [ "$VOL_GATE_ACTION" = "reduce" ]; then
            VOL_CONTEXT="⚠️ VOLATILITY GATE: 1 indicator flagged ($VOL_GATE_FLAGS) — position sizes will be reduced 50% at execution. Note this in the plan summary."
        else
            VOL_CONTEXT="✅ Volatility gate: OK — no macro flags."
        fi

        PROMPT="Run the atlas-daily pre-market workflow for the ${MARKET} market ONLY: check data freshness and run cli_ingest if stale (pass -m ${MARKET}), then run cli_plan (pass -m ${MARKET}). ${VOL_CONTEXT} Summarize the plan and stop — do NOT approve or execute. Write results to logs/pi-cron-premarket-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-premarket-${TIMESTAMP}.log"
        ;;
    postclose)
        PROMPT="Run the atlas-daily post-close workflow for the ${MARKET} market: run cli_eod_settlement (pass -m ${MARKET}), then dashboard_generate_data. Summarize any exits triggered and the final equity snapshot. Write results to logs/pi-cron-postclose-${TIMESTAMP}.md"
        LOGFILE="$LOG_DIR/pi-cron-postclose-${TIMESTAMP}.log"
        ;;
    research)
        LOGFILE="$LOG_DIR/research_${TIMESTAMP}.log"
        SKILL_DIR="$RESEARCH_SKILL_DIR"

        # Quick check: any queued experiments? Skip spawning agent if nothing to do.
        QUEUED_COUNT=$(cd "$PROJECT" && python3 -c "
import json
q = json.load(open('research/queue.json'))
print(sum(1 for e in q if e.get('status') == 'queued'))
" 2>/dev/null || echo "0")

        if [ "$QUEUED_COUNT" = "0" ]; then
            echo "$(date -Iseconds) Research queue empty — planning next wave" >> "$LOG_DIR/pi-cron.log"

            # Generate the wave brief (analyzes journal, config, gaps)
            # Wrapped in set +e to prevent silent death on wave_planner crash
            cd "$PROJECT"
            set +e
            python3 scripts/wave_planner.py --generate >> "$LOG_DIR/pi-cron.log" 2>&1
            WAVE_EXIT=$?
            set -e
            if [ $WAVE_EXIT -ne 0 ]; then
                echo "$(date -Iseconds) ERROR: wave_planner.py --generate failed (exit=$WAVE_EXIT)" >> "$LOG_DIR/pi-cron.log"
                notify error "research" "" 2>/dev/null || true
                exit 1
            fi

            # Find the brief file
            WAVE_BRIEF=$(ls -t research/waves/wave_*_brief.json 2>/dev/null | head -1)
            WAVE_NUM=$(echo "$WAVE_BRIEF" | grep -oP 'wave_\K\d+' || echo "next")

            PROMPT="You are planning Research Wave ${WAVE_NUM} for the Atlas trading system.

A wave brief has been generated at ${WAVE_BRIEF} — read it first to understand previous findings, patterns, and gaps.

THE GOAL: Make the live trading system more profitable. Every wave must either:
  A) Find new profitable trading strategies to add to the portfolio, OR
  B) Optimise existing strategies to improve returns (higher Sharpe, higher CAGR, lower drawdown)
Everything else is secondary. Do not waste experiments on diagnostics or infrastructure — focus on profit.

Your task:
1. READ the wave brief to understand what was tested, what passed/failed, and key learnings
2. SEARCH the web (use brave-search) for profitable trading strategy ideas. Run 3-5 searches:
   - Search for backtested swing trading strategies with published results (quantifiedstrategies.com, quantpedia.com, alphaarchitect.com)
   - Search for the specific opportunity identified in the brief (e.g. if position sizing is the bottleneck, search for position sizing research)
   - Search for new strategy types that could complement what we already run (mean reversion + trend following + opening gap)
   - Look for strategies with Sharpe > 0.5 and at least 50 trades in backtests
3. DESIGN a themed wave: pick ONE central theme. The theme must directly target profit improvement:
   - 'New strategy: <name> — adds uncorrelated returns to portfolio'
   - 'Optimise <strategy>: parameter tuning for higher Sharpe'
   - 'Position allocation overhaul — unlock capacity for more strategies'
   - 'Adaptive exit rules — capture more profit per trade'
   Do NOT pick themes like 'diagnostics' or 'infrastructure' or 'monitoring'.
4. CREATE 6-12 experiments that explore different aspects of the theme. Use research/models.py to seed them:
   - Each experiment should have clear hypothesis, acceptance criteria, and method
   - Use dependency chains where experiments build on each other (solo → optimise → combined → OOS)
   - Set appropriate priorities (P2 for high-impact, P3 for standard, P4 for exploratory)
   - If adding a new strategy: implement it in strategies/ following the BaseStrategy pattern
5. UPDATE the wave brief file with theme, rationale, web research findings, and experiment list
6. VERIFY the queue has been seeded by running: python3 scripts/wave_planner.py --status
7. Send a summary via: python3 scripts/telegram_notify.py research-wave-planned

IMPORTANT:
- The wave theme must have a clear path to improving live P&L
- Every experiment must have measurable acceptance criteria tied to profitability (Sharpe, CAGR, PF)
- Do NOT re-test ideas that already failed unless you have a genuinely new approach from web research
- Maximum 12 experiments per wave to keep scope manageable
- If web research reveals a promising published strategy, implement it and test it"

            LOGFILE="$LOG_DIR/wave_plan_${TIMESTAMP}.log"
        else
            echo "$(date -Iseconds) Research queue has $QUEUED_COUNT experiments" >> "$LOG_DIR/pi-cron.log"
            PROMPT="Run one full atlas-research-loop cycle: read current state (journal, health check, queue — $QUEUED_COUNT experiments queued), execute queued experiments via research_runner.py --run-all --agent-id ${AGENT_ID}, evaluate results, and send promotion requests for any passing experiments. Summarize all outcomes at the end."
        fi

        # Lock file check — prevent concurrent research sessions
        if [ -f "$RESEARCH_LOCK" ]; then
            LOCK_PID=$(grep -oP '"pid":\s*\K\d+' "$RESEARCH_LOCK" 2>/dev/null || echo "")
            LOCK_TIME=$(grep -oP '"started_at":\s*"\K[^"]+' "$RESEARCH_LOCK" 2>/dev/null || echo "unknown")
            if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
                echo "$(date -Iseconds) Research already running (PID=$LOCK_PID, started=$LOCK_TIME)" >> "$LOG_DIR/pi-cron.log"
                notify error "research" "" 2>/dev/null || true
                exit 0
            else
                echo "$(date -Iseconds) Stale research lock found (PID=$LOCK_PID), removing" >> "$LOG_DIR/pi-cron.log"
                rm -f "$RESEARCH_LOCK"
            fi
        fi

        # Acquire lock
        cat > "$RESEARCH_LOCK" <<LOCKEOF
{
    "pid": $$,
    "owner": "${AGENT_ID}",
    "started_at": "$(date -Iseconds)",
    "logfile": "${LOGFILE}"
}
LOCKEOF
        # Clean up lock on exit
        trap 'rm -f "$RESEARCH_LOCK"' EXIT
        ;;
    research-status)
        if [ -f "$RESEARCH_LOCK" ]; then
            echo "Research lock held:"
            cat "$RESEARCH_LOCK"
            LOCK_PID=$(grep -oP '"pid":\s*\K\d+' "$RESEARCH_LOCK" 2>/dev/null || echo "")
            if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
                echo "Process is RUNNING (PID=$LOCK_PID)"
            else
                echo "Process is DEAD (stale lock)"
            fi
        else
            echo "No research session running."
        fi
        exit 0
        ;;
    recover)
        # Manual recovery trigger: pi-cron.sh recover <mode-to-recover> [market]
        RECOVER_MODE="${2:?Usage: $0 recover <premarket|postclose|research> [market]}"
        RECOVER_MARKET="${3:-sp500}"
        echo "$(date -Iseconds) Manual recovery triggered for $RECOVER_MODE" >> "$LOG_DIR/pi-cron.log"
        exec "$SCRIPT_DIR/auto_recover.sh" "$RECOVER_MODE" "$RECOVER_MARKET" "" 1
        ;;
    *)
        echo "Usage: $0 {premarket|postclose|research|research-status|recover} [market] [agent-id]"
        exit 1
        ;;
esac

echo "$(date -Iseconds) Starting pi-cron $MODE" >> "$LOG_DIR/pi-cron.log"

cd "$PROJECT"
if [ "$MODE" = "research" ]; then
    # Research sessions get an 8-hour timeout
    timeout 28800 pi --print \
       --skill "$SKILL_DIR" \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1
else
    pi --print \
       --skill "$SKILL_DIR" \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1
fi

EXIT_CODE=$?
echo "$(date -Iseconds) pi-cron $MODE finished (exit=$EXIT_CODE)" >> "$LOG_DIR/pi-cron.log"

# --- Post-check: detect code errors in logs even if agent exited 0 ---
# The pi agent may exit 0 even when research_runner had code errors,
# because the agent "successfully" ran the command (it just reported errors).
# Check the actual research log for code-level errors — but ONLY today's log.
# Stale logs (from previous days) contain errors already handled by that session.
if [ $EXIT_CODE -eq 0 ] && [ "$MODE" = "research" ]; then
    TODAY=$(date '+%Y-%m-%d')
    RESEARCH_LOG=$(ls -t "$LOG_DIR"/research_run_*.log 2>/dev/null | head -1)
    # Only check if the log was created/modified today
    if [ -n "$RESEARCH_LOG" ]; then
        LOG_DATE=$(date -r "$RESEARCH_LOG" '+%Y-%m-%d' 2>/dev/null || echo "")
        if [ "$LOG_DATE" = "$TODAY" ] && grep -qE "CODE ERRORS in [0-9]+ experiment|takes [0-9]+ positional argument|has no attribute|is not defined|unexpected keyword|TypeError:|AttributeError:|NameError:|SyntaxError:" "$RESEARCH_LOG" 2>/dev/null; then
            echo "$(date -Iseconds) Code errors detected in today's research log despite agent exit 0" >> "$LOG_DIR/pi-cron.log"
            EXIT_CODE=2
            LOGFILE="$RESEARCH_LOG"  # Point auto-recovery at the actual error log
        elif [ "$LOG_DATE" != "$TODAY" ]; then
            echo "$(date -Iseconds) Research log is from $LOG_DATE (stale) — skipping error check" >> "$LOG_DIR/pi-cron.log"
        fi
    fi
fi

# --- Dashboard refresh (always, regardless of pi exit) ---
python3 dashboard/generate_data.py >> "$LOG_DIR/dashboard-refresh.log" 2>&1 || true

# --- Telegram alerts + auto-recovery ---
if [ $EXIT_CODE -eq 0 ]; then
    case "$MODE" in
        premarket)
            # Send plan with Approve/Reject buttons (bot handles callbacks)
            notify premarket-approve "" "$MARKET"
            # Fallback: also send plain summary if approve fails
            [ $? -ne 0 ] && notify premarket-ok "" "$MARKET"
            ;;
        postclose)  notify postclose-ok "$MARKET" ;;
        research)   notify research-complete "$MARKET" ;;
    esac
else
    # Send error alert first (immediate notification)
    notify error "$MODE" "$LOGFILE"

    # Spawn auto-recovery agent (background, won't block cron)
    # Recovery handles its own Telegram updates and retries.
    echo "$(date -Iseconds) Spawning auto-recovery for $MODE" >> "$LOG_DIR/pi-cron.log"
    nohup "$SCRIPT_DIR/auto_recover.sh" "$MODE" "$MARKET" "$LOGFILE" 1 \
        >> "$LOG_DIR/auto_recover.log" 2>&1 &
    RECOVER_PID=$!
    echo "$(date -Iseconds) Recovery PID=$RECOVER_PID" >> "$LOG_DIR/pi-cron.log"
fi

exit $EXIT_CODE
