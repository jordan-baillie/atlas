#!/bin/bash
# Atlas Pi Cron Wrapper
# Triggers pi in non-interactive mode for scheduled daily operations.
# Sends Telegram alerts on success and failure.
#
# NOTE: Canonical prompt templates live in .pi/prompts/*.md for interactive use.
# The PROMPT= variables below are inline copies with dynamic context injection
# (volatility gate, config validation) that slash commands can't provide.
# Keep these in sync with the .pi/prompts/ versions.
#
# Cron schedule (AEST via TZ=Australia/Brisbane in crontab):
#   30 8  * * 1-5  /root/atlas/scripts/pi-cron.sh premarket
#   00 08 * * 2-6  /root/atlas/scripts/pi-cron.sh postclose
#   00 9  1 * *    /root/atlas/scripts/pi-cron.sh slippage-cal
#   00 9  * * 6    /root/atlas/scripts/pi-cron.sh health-check
#   55 18 * * 1-5  /root/atlas/scripts/pi-cron.sh reconcile sp500
#   00 10 1 * *    /root/atlas/scripts/pi-cron.sh calibrate sp500
#   00 8  * * 0    /root/atlas/scripts/pi-cron.sh rejected-signals sp500
#   00 7  * * *   /root/atlas/scripts/cleanup_research_locks.sh   # purge research/locks/*.json >7d old
#   00 17 * * *  python3 /root/atlas/scripts/cleanup_stale_plans.py >> /root/atlas/logs/cleanup_stale_plans.log 2>&1  # expire pending plans >14d old (17:00 AEST = 07:00 UTC)
#
# Setup:
#   1. Ensure pi is logged in: pi (interactive) — OAuth login persists in ~/.pi/agent/auth.json
#   2. Credentials in ~/.atlas-secrets.json (telegram_bot_token, telegram_chat_id)
#   3. Run: crontab -e  (see schedule above)

set -euo pipefail
unset ANTHROPIC_API_KEY CLAUDE_API_KEY  # Atlas hardening: force pi to use OAuth (Claude Max)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"

# Global error trap — log + alert on any unhandled crash (prevents silent failures)
# _IN_SET_PLUS_E guards against false alarms when `set +e` is active (e.g. volatility gate)
_IN_SET_PLUS_E=0
_cron_error_trap() {
    local exit_code=$?
    local line_no=$1
    [ "$_IN_SET_PLUS_E" -eq 1 ] && return 0
    mkdir -p "$LOG_DIR"
    echo "$(date -Iseconds) FATAL: pi-cron.sh crashed at line $line_no (exit=$exit_code)" >> "$LOG_DIR/pi-cron.log"
    python3 "$PROJECT/scripts/telegram_notify.py" error "cron-crash" "" 2>/dev/null || true
}
trap '_cron_error_trap $LINENO' ERR
SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-daily"
RESEARCH_SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-research-loop"
NOTIFY="$SCRIPT_DIR/telegram_notify.py"

# ── Skill library paths ────────────────────────────────────────
# Phase 2-3 skills loaded alongside the primary skill per mode.
# --skill can be repeated; each loads the SKILL.md content into the prompt.
SKILLS_ROOT="$PROJECT/pi-package/atlas-ops/skills"
SKILL_INCIDENT="$SKILLS_ROOT/atlas-incident"
SKILL_STATE="$SKILLS_ROOT/atlas-state-queries"
SKILL_LESSONS="$SKILLS_ROOT/atlas-lessons"
SKILL_CODEBASE="$SKILLS_ROOT/atlas-codebase"
SKILL_BRAIN="$SKILLS_ROOT/atlas-brain"
SKILL_BACKTEST="$SKILLS_ROOT/atlas-backtest"

# Per-mode skill sets (primary + supporting skills)
# These are assembled into SKILL_FLAGS below each case branch.
build_skill_flags() {
    # Usage: build_skill_flags dir1 dir2 dir3 ...
    # Outputs: --skill dir1 --skill dir2 --skill dir3
    local flags=""
    for skill_dir in "$@"; do
        flags="$flags --skill $skill_dir"
    done
    echo "$flags"
}
RESEARCH_LOCK="/tmp/atlas-research.lock"

mkdir -p "$LOG_DIR"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

MODE="${1:-}"
MARKET="${2:-${ATLAS_MARKET:-sp500}}"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"

# --- Helper: send Telegram alert (best-effort, never blocks cron exit) ---
# 60s timeout prevents hangs (e.g. broker socket stuck in send_postclose_summary)
notify() {
    timeout 60 python3 "$NOTIFY" "$@" 2>>"$LOG_DIR/telegram.log" || true
}

# --- Helper: record heartbeat to SQLite (best-effort, never blocks cron) ---
hb() {
    python3 "$PROJECT/scripts/health_heartbeat.py" "$@" 2>/dev/null || true
}

AGENT_ID="${3:-atlas-research}"

case "$MODE" in
    premarket)
        hb "premarket" "started" '{"market":"'"$MARKET"'"}'
        # ── Pre-flight: config validation ──────────────────────────────────
        # Validate config schema before doing anything. Warn on errors but don't block.
        CONFIG_ERRORS=""
        _IN_SET_PLUS_E=1
        set +e
        CONFIG_ERRORS=$(python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from config.schema import validate_config_file
errors = validate_config_file('$PROJECT/config/active/${MARKET}.json')
if errors:
    for e in errors:
        print(f'  ⚠ {e}')
" 2>>"$LOG_DIR/pi-cron.log")
        set -e
        _IN_SET_PLUS_E=0

        if [ -n "$CONFIG_ERRORS" ]; then
            echo "$(date -Iseconds) Config validation warnings:" >> "$LOG_DIR/pi-cron.log"
            echo "$CONFIG_ERRORS" >> "$LOG_DIR/pi-cron.log"
            CONFIG_CONTEXT="⚠️ CONFIG VALIDATION: Issues found — check logs. Proceeding anyway.
${CONFIG_ERRORS}"
        else
            echo "$(date -Iseconds) Config validation: OK" >> "$LOG_DIR/pi-cron.log"
            CONFIG_CONTEXT="✅ Config validation: OK"
        fi
        hb "premarket" "running" '{"stage":"config_validation","result":"'"${CONFIG_ERRORS:+warn}${CONFIG_ERRORS:-ok}"'"}'

        # ── Pre-flight: volatility gate check ──────────────────────────────
        # Check macro indicators before generating the plan.
        # Exit codes: 0=ok, 1=reduce(50%), 2=block(skip entries)
        VOL_GATE_LOG="$LOG_DIR/volatility_gate_${TIMESTAMP}.json"
        VOL_GATE_EXIT=0
        _IN_SET_PLUS_E=1
        set +e
        python3 "$PROJECT/scripts/volatility_gate.py" \
            --check --market "$MARKET" --json \
            > "$VOL_GATE_LOG" 2>>"$LOG_DIR/pi-cron.log"
        VOL_GATE_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0

        # Parse gate result for prompt context
        VOL_GATE_ACTION="none"
        VOL_GATE_FLAGS=""
        if [ -f "$VOL_GATE_LOG" ]; then
            VOL_GATE_ACTION=$(python3 -c "import json,sys; d=json.load(open('$VOL_GATE_LOG')); print(d.get('action','none'))" 2>/dev/null || echo "none")
            VOL_GATE_FLAGS=$(python3 -c "import json,sys; d=json.load(open('$VOL_GATE_LOG')); print(','.join(d.get('flags',[])))" 2>/dev/null || echo "")
        fi
        echo "$(date -Iseconds) Volatility gate: action=$VOL_GATE_ACTION flags=$VOL_GATE_FLAGS" >> "$LOG_DIR/pi-cron.log"
        hb "premarket" "running" '{"stage":"volatility_gate","action":"'"$VOL_GATE_ACTION"'"}'

        # If gate BLOCKS, send alert immediately and skip the planning agent
        if [ "$VOL_GATE_EXIT" -eq 2 ] || [ "$VOL_GATE_ACTION" = "block" ]; then
            echo "$(date -Iseconds) Volatility gate BLOCKED entries — sending alert, skipping plan" >> "$LOG_DIR/pi-cron.log"
            python3 "$PROJECT/scripts/volatility_gate.py" \
                --check --market "$MARKET" --alert \
                >> "$LOG_DIR/pi-cron.log" 2>&1 || true
            notify volatility-block "$MARKET" 2>/dev/null || true
            exit 0   # Not an error — clean exit, entries intentionally paused
        fi

        # Build volatility context for the planning prompt
        if [ "$VOL_GATE_ACTION" = "reduce" ]; then
            VOL_CONTEXT="⚠️ VOLATILITY GATE: 1 indicator flagged ($VOL_GATE_FLAGS) — position sizes will be reduced 50% at execution. Note this in the plan summary."
        else
            VOL_CONTEXT="✅ Volatility gate: OK — no macro flags."
        fi

        # ── ETF universe data refresh ───────────────────────────────────────
        # Refresh all ETF universe data so regime-aware plan generation has
        # current prices for sector/treasury/gold/commodity/defensive ETFs.
        echo "$(date -Iseconds) Refreshing ETF universe data..." >> "$LOG_DIR/pi-cron.log"
        _IN_SET_PLUS_E=1
        set +e
        python3 <<PY >> "$LOG_DIR/pi-cron.log" 2>&1
import sys; sys.path.insert(0, '$PROJECT')
from data.ingest import ingest_universe
for u in ['sector_etfs', 'treasury_etfs', 'gold_etfs', 'commodity_etfs', 'defensive_etfs', 'crypto']:
    try:
        r = ingest_universe(u)
        print(f'{u}: {len(r.get("tickers_fetched",[]))} tickers refreshed')
    except Exception as e:
        print(f'{u}: FAILED — {e}')
PY
        ETF_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0
        if [ "$ETF_EXIT" -ne 0 ]; then
            echo "$(date -Iseconds) WARNING: ETF data refresh exited $ETF_EXIT — continuing" >> "$LOG_DIR/pi-cron.log"
        else
            echo "$(date -Iseconds) ETF data refresh complete" >> "$LOG_DIR/pi-cron.log"
        fi
        hb "premarket" "running" '{"stage":"etf_refresh","exit_code":'"$ETF_EXIT"'}'

        # ── Macro indicator refresh ────────────────────────────────────────
        # Refresh macro_indicators table (VIX, treasury yields, put/call, etc.)
        # so regime detection and overlay have current macro context.
        echo "$(date -Iseconds) Refreshing macro indicators..." >> "$LOG_DIR/pi-cron.log"
        _IN_SET_PLUS_E=1
        set +e
        python3 <<PY >> "$LOG_DIR/pi-cron.log" 2>&1
import sys; sys.path.insert(0, '$PROJECT')
from data.ingest import refresh_macro_data
ok = refresh_macro_data(cache_max_age_hours=0)
print(f'macro_indicators refresh: {"ok" if ok else "FAILED"}')
PY
        MACRO_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0
        if [ "$MACRO_EXIT" -ne 0 ]; then
            echo "$(date -Iseconds) WARNING: macro indicator refresh exited $MACRO_EXIT — continuing" >> "$LOG_DIR/pi-cron.log"
        else
            echo "$(date -Iseconds) Macro indicator refresh complete" >> "$LOG_DIR/pi-cron.log"
        fi

        # ── VIX parquet refresh (incremental 7-day) ───────────────────────────
        echo "$(date -Iseconds) Refreshing VIX parquet..." >> "$LOG_DIR/pi-cron.log"
        (cd "$PROJECT" && python3 -m scripts.backfill_vix --days 7) >> "$LOG_DIR/pi-cron.log" 2>&1 || true

        # ── AI overlay (log-only mode) ─────────────────────────────────────
        # Runs BEFORE the planning agent so the decision is in SQLite when
        # generate_regime_plan() annotates the plan with overlay_context.
        echo "$(date -Iseconds) Running AI overlay (log_only)..." >> "$LOG_DIR/pi-cron.log"
        _IN_SET_PLUS_E=1
        set +e
        cd "$PROJECT" && python3 -m overlay.cron --mode log_only \
            >> "$LOG_DIR/overlay_$(date +%Y%m%d).log" 2>&1
        OVERLAY_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0
        if [ "$OVERLAY_EXIT" -ne 0 ]; then
            echo "$(date -Iseconds) WARNING: overlay cron exited $OVERLAY_EXIT — continuing" >> "$LOG_DIR/pi-cron.log"
        else
            echo "$(date -Iseconds) Overlay run complete" >> "$LOG_DIR/pi-cron.log"
        fi
        hb "premarket" "running" '{"stage":"overlay","exit_code":'"$OVERLAY_EXIT"'}'

        # Canonical prompt: .pi/prompts/premarket.md
        # This inline version adds dynamic context (VOL_CONTEXT, CONFIG_CONTEXT)
        # that can't be passed via slash commands in non-interactive mode.
        PROMPT="You have these skills loaded — read them FIRST before acting:
- atlas-daily: main workflow (READ THIS FIRST: /skill:atlas-daily)
- atlas-state-queries: how to check data freshness, services, broker
- atlas-incident: if any service is down, diagnose using this
- atlas-lessons: critical pitfalls to avoid

Run the atlas-daily pre-market workflow for the ${MARKET} market ONLY: check data freshness and run cli_ingest if stale (pass -m ${MARKET}), then run cli_plan (pass -m ${MARKET}). ${VOL_CONTEXT} ${CONFIG_CONTEXT} Summarize the plan and stop — do NOT approve or execute. Write results to logs/pi-cron-premarket-${TIMESTAMP}.md

NOTE: A Telegram summary is sent automatically after this workflow completes — you do NOT need to send one. Focus on the workflow only."
        LOGFILE="$LOG_DIR/pi-cron-premarket-${TIMESTAMP}.log"
        SKILL_FLAGS=$(build_skill_flags "$SKILL_DIR" "$SKILL_STATE" "$SKILL_INCIDENT" "$SKILL_LESSONS")
        ;;
    postclose)
        hb "postclose" "started" '{"market":"'"$MARKET"'"}'
        # Canonical prompt: .pi/prompts/postclose.md
        # This inline version adds dynamic context (MARKET, TIMESTAMP)
        # that can't be passed via slash commands in non-interactive mode.
        PROMPT="You have these skills loaded — read them FIRST before acting:
- atlas-daily: main workflow (READ THIS FIRST: /skill:atlas-daily)
- atlas-state-queries: how to check equity, broker, settlement
- atlas-incident: if any service is down or settlement fails, diagnose using this
- atlas-lessons: critical pitfalls to avoid

Run the atlas-daily post-close workflow for the ${MARKET} market: run cli_eod_settlement (pass -m ${MARKET}). Summarize any exits triggered and the final equity snapshot. Write results to logs/pi-cron-postclose-${TIMESTAMP}.md

NOTE: A Telegram summary is sent automatically after this workflow completes — you do NOT need to send one. Focus on the workflow only."
        LOGFILE="$LOG_DIR/pi-cron-postclose-${TIMESTAMP}.log"
        SKILL_FLAGS=$(build_skill_flags "$SKILL_DIR" "$SKILL_STATE" "$SKILL_INCIDENT" "$SKILL_LESSONS")

        # Check research daemon health
        if systemctl is-active --quiet atlas-research-daemon 2>/dev/null; then
            HEARTBEAT=$(cat /tmp/research-daemon-heartbeat.json 2>/dev/null || echo '{}')
            echo "$(date -Iseconds) Research daemon health: $HEARTBEAT" >> "$LOG_DIR/pi-cron.log"
        else
            echo "$(date -Iseconds) WARNING: Research daemon is not running" >> "$LOG_DIR/pi-cron.log"
        fi

        # ── Weekly overlay evaluation (Saturdays — end of trading week) ───
        if [ "$(date +%u)" = "6" ]; then
            echo "$(date -Iseconds) Running weekly overlay evaluation..." >> "$LOG_DIR/pi-cron.log"
            _IN_SET_PLUS_E=1
            set +e
            cd "$PROJECT" && python3 -m overlay.cron --evaluate \
                >> "$LOG_DIR/overlay_eval_$(date +%Y%m%d).log" 2>&1
            OVERLAY_EVAL_EXIT=$?
            set -e
            _IN_SET_PLUS_E=0
            if [ "$OVERLAY_EVAL_EXIT" -ne 0 ]; then
                echo "$(date -Iseconds) WARNING: overlay evaluation exited $OVERLAY_EVAL_EXIT" >> "$LOG_DIR/pi-cron.log"
            else
                echo "$(date -Iseconds) Overlay evaluation complete" >> "$LOG_DIR/pi-cron.log"
            fi
            # ── Weekly A/B vision review (same Saturday slot) ─────────────
            cd "$PROJECT" && python3 -m scripts.review_vision_ab --days 7 --telegram >> "$LOG_DIR/overlay_eval_$(date +%Y%m%d).log" 2>&1 || true
        fi
        ;;
    research)
        hb "research" "started"
        LOGFILE="$LOG_DIR/research_${TIMESTAMP}.log"
        SKILL_DIR="$RESEARCH_SKILL_DIR"

        # Collect sweeper status for the prompt
        SWEEPER_STATUS="not running"
        if systemctl is-active --quiet atlas-autoresearch 2>/dev/null; then
            SWEEPER_STATUS="running"
        fi
        HEARTBEAT=""
        if [ -f /tmp/autoresearch-heartbeat.json ]; then
            HEARTBEAT=$(cat /tmp/autoresearch-heartbeat.json 2>/dev/null || echo "{}")
        fi

        # Canonical prompt: .pi/prompts/research-session.md
        # This inline version adds dynamic context (SWEEPER_STATUS, HEARTBEAT)
        # that can't be passed via slash commands in non-interactive mode.
        PROMPT="You have these skills loaded — read them FIRST before acting:
- atlas-research-loop: main workflow (READ THIS FIRST: /skill:atlas-research-loop)
- atlas-brain: check prior results and closed decisions BEFORE running experiments
- atlas-backtest: how to run backtests, interpret results, and record findings
- atlas-lessons: critical pitfalls to avoid (degenerate solutions, solo vs combined, etc.)
- atlas-codebase: system architecture reference

TELEGRAM: You own all notifications. Send via:
  python3 -c \"import sys; sys.path.insert(0,'/root/atlas'); from utils.telegram import send_message; send_message('''YOUR_MSG''')\"

Rules:
- Send ONE summary at the END of your session, not during
- ONLY send if you found something significant:
  * A strategy improved (new Sharpe > previous best) — include the numbers
  * A promotion candidate was staged — include strategy and metrics
  * A previously unknown pattern was discovered
  * Infrastructure blocked research (service down, data stale)
- If all experiments were discards and nothing improved: do NOT send
- Include: experiments run, improvements found, top finding
- Keep it under 20 lines. Use HTML formatting (<b>, <i>, <code>)

Run a daily autoresearch session. Read research/program.md first.

SWEEPER STATUS: ${SWEEPER_STATUS}
HEARTBEAT: ${HEARTBEAT}

Your daily research tasks (in order):

1. REVIEW SWEEPER RESULTS
   - Run: leaderboard('sp500') — see what the 24/7 sweeper found
   - Check research/results/*.tsv for recent keep/discard history
   - Identify which strategies improved and which are stuck

2. CREATIVE RESEARCH (what the sweeper can't do)
   Pick 2-3 of these based on what the leaderboard shows:
   a) Screen untested sandbox strategies: quick_check() then baseline if alive
   b) Try parameter combos the grid missed (pairs, triples, unusual values)
   c) Run combined_test() on any strategy with Sharpe > 0.3
   d) Test radical changes (disable filters, flip directions, extreme values)

3. PROMOTION CHECK
   Any strategy with solo Sharpe > 0.3 AND passing combined test:
   - Stage candidate config in config/candidates/
   - Send promotion request via Telegram (NEVER auto-promote)

4. SEND SUMMARY via Telegram:
   - Sweeper overnight results (experiments run, improvements found)
   - Your creative research results
   - Current leaderboard top 5
   - What needs human attention (promotions, stuck strategies)

Budget: up to 8 hours. Run as many experiments as time allows.
Focus on strategies the sweeper hasn't cracked yet."

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
        SKILL_FLAGS=$(build_skill_flags "$SKILL_DIR" "$SKILL_BRAIN" "$SKILL_BACKTEST" "$SKILL_LESSONS" "$SKILL_CODEBASE")
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
    slippage-cal)
        # Monthly slippage calibration (1st of month, 09:00 AEST)
        echo "$(date -Iseconds) Running slippage calibration for $MARKET" >> "$LOG_DIR/pi-cron.log"
        python3 "$PROJECT/scripts/slippage_calibration.py" --market "$MARKET" \
            >> "$LOG_DIR/pi-cron.log" 2>&1
        exit $?
        ;;
    health-check)
        hb "health-check" "started" '{"market":"'"$MARKET"'"}'
        echo "$(date -Iseconds) Running strategy health check for $MARKET" >> "$LOG_DIR/pi-cron.log"
        _IN_SET_PLUS_E=1
        set +e
        python3 "$PROJECT/scripts/strategy_health_cron.py" --market "$MARKET" \
            >> "$LOG_DIR/pi-cron.log" 2>&1
        HC_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0
        if [ $HC_EXIT -eq 0 ]; then
            hb "health-check" "completed" '{"market":"'"$MARKET"'"}'
        else
            hb "health-check" "failed" '{"market":"'"$MARKET"'","exit_code":'"$HC_EXIT"'}'
        fi
        exit $HC_EXIT
        ;;
    reconcile)
        hb "reconcile" "started" '{"market":"'"$MARKET"'"}'
        echo "$(date -Iseconds) Running reconciliation for $MARKET" >> "$LOG_DIR/pi-cron.log"
        _IN_SET_PLUS_E=1
        set +e
        python3 "$PROJECT/scripts/reconcile_positions.py" --market "$MARKET" --auto-fix \
            >> "$LOG_DIR/pi-cron.log" 2>&1
        RECONCILE_EXIT=$?
        set -e
        _IN_SET_PLUS_E=0
        if [ $RECONCILE_EXIT -eq 0 ]; then
            hb "reconcile" "completed" '{"market":"'"$MARKET"'"}'
        else
            hb "reconcile" "failed" '{"market":"'"$MARKET"'","exit_code":'"$RECONCILE_EXIT"'}'
        fi
        exit $RECONCILE_EXIT
        ;;
    calibrate)
        echo "$(date -Iseconds) Running confidence calibration for $MARKET" >> "$LOG_DIR/pi-cron.log"
        python3 "$PROJECT/scripts/calibration_cron.py" --market "$MARKET" \
            >> "$LOG_DIR/pi-cron.log" 2>&1
        exit $?
        ;;
    rejected-signals)
        echo "$(date -Iseconds) Running rejected signal analysis for $MARKET" >> "$LOG_DIR/pi-cron.log"
        python3 "$PROJECT/scripts/rejected_signals_cron.py" --market "$MARKET" \
            >> "$LOG_DIR/pi-cron.log" 2>&1
        exit $?
        ;;
    *)
        echo "Usage: $0 {premarket|postclose|research|research-status|recover|slippage-cal|health-check|reconcile|calibrate|rejected-signals} [market] [agent-id]"
        exit 1
        ;;
esac

echo "$(date -Iseconds) Starting pi-cron $MODE" >> "$LOG_DIR/pi-cron.log"

cd "$PROJECT"

# ── Circuit breaker pre-check ────────────────────────────────
BREAKER_FILE="${CLAUDE_BREAKER_FILE:-/tmp/claude_breaker.json}"
if [ -f "$BREAKER_FILE" ]; then
    BREAKER_AGE=$(( $(date +%s) - $(stat -c %Y "$BREAKER_FILE" 2>/dev/null || echo 0) ))
    if [ "$BREAKER_AGE" -lt 18000 ]; then
        REMAINING_MIN=$(( (18000 - BREAKER_AGE) / 60 ))
        echo "$(date -Iseconds) Claude circuit breaker tripped (${REMAINING_MIN}m remaining) — skipping pi agent" >> "$LOG_DIR/pi-cron.log"
        exit 0
    else
        echo "$(date -Iseconds) Claude circuit breaker expired — removing stale breaker file" >> "$LOG_DIR/pi-cron.log"
        rm -f "$BREAKER_FILE"
    fi
fi

if [ "$MODE" = "research" ]; then
    # Research sessions get an 8-hour timeout
    # shellcheck disable=SC2086
    timeout 28800 pi --print \
       --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
       $SKILL_FLAGS \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1
else
    # shellcheck disable=SC2086
    pi --print \
       --system-prompt "You are Claude Code, Anthropic's official CLI for Claude." \
       $SKILL_FLAGS \
       --no-session \
       "$PROMPT" \
       >> "$LOGFILE" 2>&1 || {
        PI_EXIT=$?
        echo "$(date -Iseconds) WARNING: pi agent failed (exit=$PI_EXIT), falling back to direct CLI" >> "$LOG_DIR/pi-cron.log"
        # Direct CLI fallback — runs the core pipeline without the AI agent
        case "$MODE" in
            premarket)
                python3 scripts/cli.py -m "$MARKET" ingest >> "$LOGFILE" 2>&1 || true
                python3 scripts/cli.py -m "$MARKET" plan >> "$LOGFILE" 2>&1
                ;;
            postclose)
                python3 scripts/eod_settlement.py --market "$MARKET" >> "$LOGFILE" 2>&1
                ;;
        esac
    }
fi

EXIT_CODE=$?
echo "$(date -Iseconds) pi-cron $MODE finished (exit=$EXIT_CODE)" >> "$LOG_DIR/pi-cron.log"

# ── Trip breaker if exhaustion detected ──────────────────────
if grep -qiE "out of extra usage|rate_limit_error|insufficient_quota|usage_limit" "$LOGFILE" 2>/dev/null; then
    echo "$(date -Iseconds) Detected Claude usage exhaustion in pi output — tripping circuit breaker" >> "$LOG_DIR/pi-cron.log"
    python3 -c "import sys; sys.path.insert(0,'$PROJECT'); from utils.claude_circuit_breaker import trip; trip('pi_cron')" 2>/dev/null || true
fi

# --- Post-check: detect code errors in logs even if agent exited 0 ---
# The pi agent may exit 0 even when research_runner had code errors,
# because the agent "successfully" ran the command (it just reported errors).
# Check the actual research log for code-level errors — but ONLY today's log.
# Stale logs (from previous days) contain errors already handled by that session.
if [ $EXIT_CODE -eq 0 ] && [ "$MODE" = "research" ]; then
    TODAY=$(date '+%Y-%m-%d')
    RESEARCH_LOG=$(ls -t "$LOG_DIR"/research_run_*.log "$LOG_DIR"/research_*.log 2>/dev/null | head -1 || true)
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

# --- Regenerate research knowledge base after successful research ---
if [ $EXIT_CODE -eq 0 ] && [ "$MODE" = "research" ]; then
    echo "$(date -Iseconds) Regenerating research knowledge base..." >> "$LOG_DIR/pi-cron.log"
    python3 scripts/build_obsidian_vault.py --force >> "$LOG_DIR/pi-cron.log" 2>&1 || true
fi

# --- Dashboard refresh ---
# generate_data.py retired in Phase 5 — dashboard now served via SQLite API endpoints
# python3 dashboard/generate_data.py >> "$LOG_DIR/dashboard-refresh.log" 2>&1 || true

# --- Guaranteed Telegram notifications ---
# The pi agent is unreliable at sending its own messages, so the script
# always sends a structured notification using telegram_notify.py.
# This uses dashboard-data.json (just refreshed above) for accurate data.
if [ $EXIT_CODE -eq 0 ]; then
    case "$MODE" in
        premarket)
            PLAN_FILE="$PROJECT/plans/plan_${MARKET}_$(date '+%Y-%m-%d').json"
            notify premarket-approve "$PLAN_FILE" "$MARKET" 2>/dev/null || true
            ;;
        postclose)
            notify postclose-ok "$MARKET" 2>/dev/null || true
            ;;
        # research: handled by agent (session-end summary only when significant)
    esac
fi

# --- Error recovery ---
if [ $EXIT_CODE -ne 0 ]; then
    # Agent crashed — alert via shell
    notify error "$MODE" "$LOGFILE"

    # Spawn auto-recovery agent (background, won't block cron)
    echo "$(date -Iseconds) Spawning auto-recovery for $MODE" >> "$LOG_DIR/pi-cron.log"
    nohup "$SCRIPT_DIR/auto_recover.sh" "$MODE" "$MARKET" "$LOGFILE" 1 \
        >> "$LOG_DIR/auto_recover.log" 2>&1 &
    RECOVER_PID=$!
    echo "$(date -Iseconds) Recovery PID=$RECOVER_PID" >> "$LOG_DIR/pi-cron.log"
fi

# --- Record pipeline completion heartbeat ---
if [ $EXIT_CODE -eq 0 ]; then
    hb "$MODE" "completed"
else
    hb "$MODE" "failed" '{"exit_code":'"$EXIT_CODE"'}'
fi

exit $EXIT_CODE

# ══════════════════════════════════════════════════════════════
# Autoresearch — all 5 strategies in parallel, Mon-Fri 09:00 AEST
# Starts after postclose settles (~08:30), 8h sessions finish by 17:00
# (1 hour before 18:00 healthz). Uses frozen snapshot — no conflicts.
# Top 5 by portfolio weight: SR 24%, OG 22%, TF 21%, MR 17%, MB 9%
# Each worker: ~1-2 cores, ~2GB RAM. 5 workers on 8-core = 3 cores free.
# ══════════════════════════════════════════════════════════════
# 0 9 * * 1-5  python3 /root/atlas/research/autoresearch_nightly.py --hours 8 --workers 5 --notify > /root/atlas/logs/autoresearch_nightly_$(date +\%Y\%m\%d).log 2>&1
