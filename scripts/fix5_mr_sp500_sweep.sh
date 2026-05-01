#!/bin/bash
# Fix 5 — sweep mean_reversion / sp500 for solo Sharpe >= 0.5
# Validated-strategies audit 2026-05-01.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/fix5_mr_sp500_${TS}.log"
DONE_MARKER="$LOG_DIR/fix5_mr_sp500_${TS}.done"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

echo "$(date -Iseconds) [fix5] starting mean_reversion/sp500 sweep (4h)" | tee -a "$LOGFILE"
echo "$(date -Iseconds) [fix5] log: $LOGFILE" | tee -a "$LOGFILE"

timeout --signal=TERM --kill-after=60 15600 \
    python3 research/autoresearch_runner.py \
        --strategy mean_reversion \
        --market sp500 \
        --hours 4 \
        >> "$LOGFILE" 2>&1

rc=$?
echo "$(date -Iseconds) [fix5] DONE (rc=$rc)" | tee -a "$LOGFILE"
touch "$DONE_MARKER"
echo "$(date -Iseconds) [fix5] sentinel: $DONE_MARKER" | tee -a "$LOGFILE"
exit 0
