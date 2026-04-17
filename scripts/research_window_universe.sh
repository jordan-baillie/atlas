#!/bin/bash
# Atlas Research Window — per-universe sweep + optional LLM loop
# Usage: research_window_universe.sh <universe>
# Called by atlas-research-window@<universe>.service (systemd templated unit)

set -euo pipefail

UNIVERSE="${1:-}"
if [ -z "$UNIVERSE" ]; then
    echo "ERROR: universe argument required" >&2
    exit 2
fi

# Trap SIGTERM from systemd
cleanup() {
    echo "$(date -Iseconds) SIGTERM received ($UNIVERSE) — killing child processes" | tee -a "${LOGFILE:-/dev/null}"
    kill $(jobs -p) 2>/dev/null || true
    wait 2>/dev/null || true
    exit 143
}
trap cleanup SIGTERM SIGINT

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/research_window_${UNIVERSE}_${TIMESTAMP}.log"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

# Per-universe params: hours, workers, do_llm
case "$UNIVERSE" in
    sp500)           HOURS=1.0;  WORKERS=3; DO_LLM=1; SWEEP_TIMEOUT=4200 ;;
    commodity_etfs)  HOURS=0.5;  WORKERS=2; DO_LLM=1; SWEEP_TIMEOUT=2400 ;;
    sector_etfs|gold_etfs|treasury_etfs|defensive_etfs|crypto)
                     HOURS=0.25; WORKERS=1; DO_LLM=0; SWEEP_TIMEOUT=1200 ;;
    *)
        echo "ERROR: unknown universe '$UNIVERSE'" >&2
        exit 2
        ;;
esac

echo "$(date -Iseconds) [$UNIVERSE] sweep start (hours=$HOURS, workers=$WORKERS, llm=$DO_LLM)" | tee -a "$LOGFILE"

timeout --signal=TERM --kill-after=60 "$SWEEP_TIMEOUT" python3 research/autoresearch_nightly.py \
    --universe "$UNIVERSE" \
    --hours "$HOURS" \
    --workers "$WORKERS" \
    >> "$LOGFILE" 2>&1 || true

echo "$(date -Iseconds) [$UNIVERSE] sweep done" | tee -a "$LOGFILE"

if [ "$DO_LLM" = "1" ]; then
    echo "$(date -Iseconds) [$UNIVERSE] checking Pi CLI for LLM loop" | tee -a "$LOGFILE"
    if python3 scripts/claude_auth_check.py >> "$LOGFILE" 2>&1; then
        echo "$(date -Iseconds) [$UNIVERSE] starting LLM loop (25 min)" | tee -a "$LOGFILE"
        python3 research/llm_loop_runner.py \
            --minutes 25 \
            --universe "$UNIVERSE" \
            >> "$LOGFILE" 2>&1 || true
        echo "$(date -Iseconds) [$UNIVERSE] LLM loop done" | tee -a "$LOGFILE"
    else
        echo "$(date -Iseconds) [$UNIVERSE] LLM SKIPPED — Pi CLI not available" | tee -a "$LOGFILE"
    fi
fi

echo "$(date -Iseconds) [$UNIVERSE] research window finished" | tee -a "$LOGFILE"
exit 0
