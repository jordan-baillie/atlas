#!/bin/bash
# Atlas Research Window — sweep + LLM loop
# Triggered by atlas-research-window.timer (systemd)
# Phase 1: Headless parameter sweep (1.5 hours, 3 workers)
# Phase 2: LLM-driven research loop (25 minutes, Claude reasons about experiments)
# Service timeout: 8100s (2h15m buffer). Nice=10 so trading crons preempt.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/research_window_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"

echo "$(date -Iseconds) Research window starting (sweep=1.5h + LLM=25m)" | tee -a "$LOGFILE"

cd "$PROJECT"

# ── Phase 1: Headless parameter sweep (1.5 hours) ────────────────────────
echo "$(date -Iseconds) Phase 1: Starting headless sweep (1.5h, 3 workers)" | tee -a "$LOGFILE"

python3 research/autoresearch_nightly.py \
    --hours 1.5 \
    --workers 3 \
    >> "$LOGFILE" 2>&1 || true

echo "$(date -Iseconds) Phase 1: Sweep complete" | tee -a "$LOGFILE"

# ── Phase 2: LLM-driven research loop (25 minutes) ──────────────────────
echo "$(date -Iseconds) Phase 2: Checking Claude auth for LLM loop" | tee -a "$LOGFILE"

if python3 scripts/claude_auth_check.py >> "$LOGFILE" 2>&1; then
    echo "$(date -Iseconds) Phase 2: Claude authenticated — starting LLM research loop (25 min)" | tee -a "$LOGFILE"

    python3 research/llm_loop_runner.py \
        --minutes 25 \
        >> "$LOGFILE" 2>&1 || true

    PHASE2_EXIT=$?
    echo "$(date -Iseconds) Phase 2: LLM loop complete (exit=$PHASE2_EXIT)" | tee -a "$LOGFILE"
else
    echo "$(date -Iseconds) Phase 2: SKIPPED — Claude not authenticated (run 'claude setup-token')" | tee -a "$LOGFILE"
fi

echo "$(date -Iseconds) Research window finished" | tee -a "$LOGFILE"
exit 0
