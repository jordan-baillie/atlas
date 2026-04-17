#!/bin/bash
# Atlas Research Window — sweep + LLM loop
# Triggered by atlas-research-window.timer (systemd)
# Phase 1:   SP500 parameter sweep (1h/worker, 3 workers, ~2h)
# Phase 1.5: Commodity ETFs sweep (0.5h/worker, 2 workers, ~30m) — LIVE
# Phase 1.6: Passive universe rotation (sector/gold/treasury/defensive/crypto, ~15m)
# Phase 2:   LLM research loop (25 min, alternates sp500/commodity_etfs)
# Total: ~2h45m per window. Service timeout: 9900s. Nice=10 so trading crons preempt.

set -euo pipefail

# Trap SIGTERM from systemd — kill all children and wait for cleanup
cleanup() {
    echo "$(date -Iseconds) SIGTERM received — killing child processes" | tee -a "${LOGFILE:-/dev/null}"
    kill $(jobs -p) 2>/dev/null || true
    wait 2>/dev/null || true
    exit 143
}
trap cleanup SIGTERM SIGINT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/research_window_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"

echo "$(date -Iseconds) Research window starting (sweep=~2h + ETF=15m + LLM=25m)" | tee -a "$LOGFILE"

cd "$PROJECT"

# ── Phase 1: SP500 parameter sweep (1h/worker, ~2h total) ────────────────
echo "$(date -Iseconds) Phase 1: Starting sp500 sweep (1h/worker, 3 workers, ~2h max)" | tee -a "$LOGFILE"

timeout --signal=TERM --kill-after=60 7200 python3 research/autoresearch_nightly.py \
    --hours 1.0 \
    --workers 3 \
    >> "$LOGFILE" 2>&1 || true

echo "$(date -Iseconds) Phase 1 complete: sp500 sweep done" | tee -a "$LOGFILE"

# ── Phase 1.5: Commodity ETFs sweep (live — dedicated window) ────────────
echo "$(date -Iseconds) Phase 1.5: Starting commodity_etfs sweep (0.5h/worker, 2 workers)" | tee -a "$LOGFILE"

timeout --signal=TERM --kill-after=60 2400 python3 research/autoresearch_nightly.py \
    --universe commodity_etfs \
    --hours 0.5 \
    --workers 2 \
    >> "$LOGFILE" 2>&1 || true

echo "$(date -Iseconds) Phase 1.5 complete: commodity_etfs sweep done" | tee -a "$LOGFILE"

# ── Phase 1.6: Passive universe rotation (daily cycle through 5) ─────────
PASSIVE_UNIVERSES=("sector_etfs" "gold_etfs" "treasury_etfs" "defensive_etfs" "crypto")
PASSIVE_INDEX=$(( ($(date +%j) - 1) % 5 ))
PASSIVE_UNIVERSE="${PASSIVE_UNIVERSES[$PASSIVE_INDEX]}"

echo "$(date -Iseconds) Phase 1.6: Passive sweep — universe=$PASSIVE_UNIVERSE (day-of-year rotation index=$PASSIVE_INDEX)" | tee -a "$LOGFILE"

timeout --signal=TERM --kill-after=60 1800 python3 research/autoresearch_nightly.py \
    --universe "$PASSIVE_UNIVERSE" \
    --hours 0.25 \
    --workers 1 \
    >> "$LOGFILE" 2>&1 || true

echo "$(date -Iseconds) Phase 1.6 complete: $PASSIVE_UNIVERSE sweep done" | tee -a "$LOGFILE"

# ── Phase 2: LLM-driven research loop (25 minutes) ──────────────────────
# Alternate LLM loop between live universes
LLM_UNIVERSES=("sp500" "commodity_etfs")
LLM_INDEX=$(( ($(date +%j) - 1) % 2 ))
LLM_UNIVERSE="${LLM_UNIVERSES[$LLM_INDEX]}"

echo "$(date -Iseconds) Phase 2: Checking Pi CLI for LLM loop (universe=$LLM_UNIVERSE)" | tee -a "$LOGFILE"

if python3 scripts/claude_auth_check.py >> "$LOGFILE" 2>&1; then
    echo "$(date -Iseconds) Phase 2: Pi CLI ready — starting LLM research loop (25 min, universe=$LLM_UNIVERSE)" | tee -a "$LOGFILE"

    python3 research/llm_loop_runner.py \
        --minutes 25 \
        --universe "$LLM_UNIVERSE" \
        >> "$LOGFILE" 2>&1 || true

    PHASE2_EXIT=$?
    echo "$(date -Iseconds) Phase 2 complete: LLM loop done (exit=$PHASE2_EXIT, universe=$LLM_UNIVERSE)" | tee -a "$LOGFILE"
else
    echo "$(date -Iseconds) Phase 2: SKIPPED — Pi CLI not available" | tee -a "$LOGFILE"
fi

echo "$(date -Iseconds) Research window finished (sp500 + commodity_etfs + $PASSIVE_UNIVERSE passive + LLM/$LLM_UNIVERSE)" | tee -a "$LOGFILE"
exit 0
