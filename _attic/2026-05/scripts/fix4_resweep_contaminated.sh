#!/bin/bash
# Fix 4 — re-sweep 8 contaminated research_best rows
# Validated-strategies audit 2026-05-01.
#
# NOTE: passes BOTH --market and --universe for non-sp500 combos so the
# build_from_definition() path (line 619 of autoresearch_runner.py) fires.
# Without --universe, the runner defaults to 'sp500' and tries to load a
# snapshot instead of using the live cache — reproducing Bug B.

set -uo pipefail   # NOT -e: one combo failing must not abort the rest

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT="$(dirname "$SCRIPT_DIR")"
LOG_DIR="$PROJECT/logs"
TS="$(date '+%Y%m%d_%H%M%S')"
LOGFILE="$LOG_DIR/fix4_resweep_${TS}.log"
DONE_MARKER="$LOG_DIR/fix4_resweep_${TS}.done"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

echo "$(date -Iseconds) [fix4] starting re-sweep of 8 contaminated combos" | tee -a "$LOGFILE"
echo "$(date -Iseconds) [fix4] log: $LOGFILE" | tee -a "$LOGFILE"

# (strategy, universe) pairs — sequential
declare -a COMBOS=(
    "mean_reversion defensive_etfs"
    "mean_reversion gold_etfs"
    "mean_reversion sector_etfs"
    "mean_reversion treasury_etfs"
    "momentum_breakout defensive_etfs"
    "momentum_breakout gold_etfs"
    "momentum_breakout sector_etfs"
    "momentum_breakout treasury_etfs"
)

PASSED=0
FAILED=0
for combo in "${COMBOS[@]}"; do
    read -r STRAT UNI <<< "$combo"
    echo "$(date -Iseconds) [fix4] === ${STRAT} / ${UNI} ===" | tee -a "$LOGFILE"
    if timeout --signal=TERM --kill-after=60 2400 \
        python3 research/autoresearch_runner.py \
            --strategy "$STRAT" \
            --market   "$UNI" \
            --universe "$UNI" \
            --hours 0.5 \
            >> "$LOGFILE" 2>&1; then
        echo "$(date -Iseconds) [fix4] ✅ ${STRAT} / ${UNI} OK" | tee -a "$LOGFILE"
        PASSED=$((PASSED + 1))
    else
        rc=$?
        echo "$(date -Iseconds) [fix4] ❌ ${STRAT} / ${UNI} FAILED (rc=$rc) — continuing" | tee -a "$LOGFILE"
        FAILED=$((FAILED + 1))
    fi
done

echo "$(date -Iseconds) [fix4] DONE — passed=$PASSED failed=$FAILED of ${#COMBOS[@]}" | tee -a "$LOGFILE"
touch "$DONE_MARKER"
echo "$(date -Iseconds) [fix4] sentinel: $DONE_MARKER" | tee -a "$LOGFILE"
exit 0
