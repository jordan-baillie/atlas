#!/bin/bash
# Atlas Autoresearch — unified 24/7 loop
#
# For each strategy (in priority order):
#   1. SWEEP: mechanical parameter grid (Python, no LLM, cheap)
#   2. AGENT: pi session reviews results + does creative work (LLM, expensive)
#   → next strategy → repeat forever
#
# One process, no conflicts, no wasted work.
#
# Usage:
#   systemctl start atlas-autoresearch
#   tail -f /tmp/autoresearch.log
#   systemctl stop atlas-autoresearch

set -uo pipefail

PROJECT="/root/atlas"
SKILL_DIR="$PROJECT/pi-package/atlas-ops/skills/atlas-research-loop"
LOG="/tmp/autoresearch.log"

# Sweep: top-N tickers for speed, full cycle takes ~20 min per strategy
SWEEP_TOP_N=50
# Agent: time budget per strategy (seconds)
AGENT_TIMEOUT=3600  # 1 hour

export TZ="Australia/Brisbane"
export HOME="/root"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

STRATEGIES=(
    mean_reversion
    trend_following
    opening_gap
    connors_rsi2
    momentum_breakout
    short_term_mr
    bb_squeeze
)

log() { echo "$(date -Iseconds) $*" >> "$LOG"; }

rotate_log() {
    local size
    size=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if [ "$size" -gt 52428800 ]; then
        mv "$LOG" "${LOG}.old"
    fi
}

write_heartbeat() {
    cat > /tmp/autoresearch-heartbeat.json <<EOF
{
  "timestamp": "$(date -Iseconds)",
  "pid": $$,
  "phase": "$1",
  "strategy": "$2",
  "cycle": $3,
  "status": "running"
}
EOF
}

# ─── Main loop ───────────────────────────────────────────────────────────────

cycle=0

while true; do
    cycle=$((cycle + 1))
    rotate_log
    log "════════════════════ Cycle $cycle ════════════════════"

    for strategy in "${STRATEGIES[@]}"; do
        log "── Strategy: $strategy ──"

        # ── Phase 1: SWEEP (mechanical, no LLM) ─────────────────────────
        write_heartbeat "sweep" "$strategy" "$cycle"
        log "SWEEP: $strategy (top $SWEEP_TOP_N tickers)"

        timeout 3600 python3 "$PROJECT/research/sweep.py" \
            --strategy "$strategy" \
            --top-n "$SWEEP_TOP_N" \
            --max-fails 5 \
            --cycles 1 \
            >> "$LOG" 2>&1
        SWEEP_EXIT=$?
        log "SWEEP done: $strategy (exit=$SWEEP_EXIT)"

        # ── Phase 2: AGENT (LLM creative research) ──────────────────────
        write_heartbeat "agent" "$strategy" "$cycle"
        log "AGENT: $strategy (budget ${AGENT_TIMEOUT}s)"

        # Build prompt with current state for this specific strategy
        BEST_INFO=$(cd "$PROJECT" && python3 -c "
import sys, json; sys.path.insert(0, '.')
from research.loop import load_best, read_results
best = load_best('$strategy')
if best:
    m = best.get('metrics', {})
    print(f'Best Sharpe: {m.get(\"sharpe\", 0):.4f}, Trades: {m.get(\"total_trades\", 0)}, Runs: {best.get(\"experiments_run\", 0)}, Kept: {best.get(\"experiments_kept\", 0)}')
    print(f'Params: {json.dumps(best.get(\"params\", {}), default=str)[:200]}')
else:
    print('No results yet — needs baseline.')
print()
print('Recent history:')
print(read_results('$strategy', 10))
" 2>/dev/null || echo "(failed to load)")

        LEADERBOARD=$(cd "$PROJECT" && python3 -c "
import sys; sys.path.insert(0, '.')
from research.loop import leaderboard
print(leaderboard())
" 2>/dev/null || echo "(failed)")

        PROMPT="You are an autonomous researcher. Read research/program.md first.

CURRENT STRATEGY: $strategy (cycle $cycle)
The mechanical sweeper just finished a parameter grid pass on this strategy.
Now it's your turn to do what the sweeper can't.

SWEEPER RESULTS FOR $strategy:
$BEST_INFO

FULL LEADERBOARD:
$LEADERBOARD

YOUR TASK (budget: 1 hour on $strategy):
1. Start a ResearchSession('$strategy', 'sp500') and baseline()
2. Look at the sweep history — what params improved? What patterns?
3. Try things the grid missed:
   - Parameter COMBINATIONS (pairs that interact, e.g. RSI period + oversold threshold)
   - Values BETWEEN grid points (the sweeper tried 7,10,14 — try 8,9,11,12)
   - Radical changes (disable a filter, flip a boolean, extreme values)
   - If Sharpe > 0.3: run combined_test() to check portfolio fit
4. When stuck (5+ discards): stop and let the loop move to the next strategy

RULES:
- NEVER ask the human — you are fully autonomous
- NEVER stop early — use your budget
- Move fast between experiments
- Follow keep/discard recommendations from the system
- The next cycle will sweep the grid again with your improvements as the new baseline"

        timeout "$AGENT_TIMEOUT" pi --print \
            --skill "$SKILL_DIR" \
            --no-session \
            "$PROMPT" \
            >> "$LOG" 2>&1
        AGENT_EXIT=$?
        log "AGENT done: $strategy (exit=$AGENT_EXIT)"

        # Brief pause between strategies
        sleep 10
    done

    log "Cycle $cycle complete"

    # Telegram summary after each full cycle
    cd "$PROJECT" && python3 -c "
import sys; sys.path.insert(0, '.')
try:
    from research.loop import leaderboard
    from utils.telegram import send_message
    lb = leaderboard()
    send_message(f'🔬 <b>Autoresearch cycle $cycle complete</b>\n<pre>{lb[:3000]}</pre>')
except Exception as e:
    print(f'Telegram failed: {e}')
" >> "$LOG" 2>&1 || true

    sleep 10
done
