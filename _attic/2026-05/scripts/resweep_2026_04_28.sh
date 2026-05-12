#!/bin/bash
# Resweep launcher — refresh research_best for 5 universe×strategy targets.
# Targets: connors_rsi2/sp500, momentum_breakout+mean_reversion in
#          sector_etfs / gold_etfs / treasury_etfs / defensive_etfs
#
# Run via: systemctl start atlas-resweep-20260428.service
#
# IMPORTANT: This script only updates research_best (research state).
# It does NOT modify config/active/*.json or activate any universe
# for live trading. Promotion is gated separately by human approval.
set -euo pipefail

LOG_DIR=/root/atlas/logs
TS=$(date '+%Y%m%d_%H%M%S')
SUMMARY=$LOG_DIR/resweep_summary_${TS}.log

mkdir -p "$LOG_DIR"
cd /root/atlas

echo "$(date -Iseconds) ===== RESWEEP 2026-04-28 START =====" | tee -a "$SUMMARY"
echo "$(date -Iseconds) Targets: connors_rsi2/sp500 + momentum_breakout+mean_reversion in 4 ETF universes" | tee -a "$SUMMARY"
echo "$(date -Iseconds) Note: sp500 sweep uses --strategies connors_rsi2 (targeted, not full sweep)" | tee -a "$SUMMARY"
echo "" | tee -a "$SUMMARY"

run_sweep() {
    local universe=$1
    local hours=$2
    local workers=$3
    local strategies_arg="${4:-}"   # optional --strategies flag
    local label="$universe"
    local log="$LOG_DIR/resweep_${universe}_${TS}.log"

    # Compute timeout = budget_seconds + 30min grace; use python for float math
    local timeout_sec
    timeout_sec=$(python3 -c "print(int(${hours} * 3600 + 1800))")

    echo "$(date -Iseconds) [$label] sweep START (hours=${hours}, workers=${workers}, timeout=${timeout_sec}s${strategies_arg:+, strategies=${strategies_arg}})" | tee -a "$SUMMARY"
    echo "$(date -Iseconds) [$label] log: $log" | tee -a "$SUMMARY"

    local sweep_cmd=(
        python3 research/autoresearch_nightly.py
        --universe "$universe"
        --market   "$universe"
        --hours    "$hours"
        --workers  "$workers"
    )
    if [ -n "$strategies_arg" ]; then
        sweep_cmd+=(--strategies "$strategies_arg")
    fi

    timeout --signal=TERM --kill-after=120 "$timeout_sec" \
        "${sweep_cmd[@]}" \
        >> "$log" 2>&1 \
        && echo "$(date -Iseconds) [$label] sweep DONE (ok)" | tee -a "$SUMMARY" \
        || echo "$(date -Iseconds) [$label] sweep DONE (timed-out or non-zero exit — check log)" | tee -a "$SUMMARY"
}

# ─── Sequential sweeps — single shared compute pool ──────────────────────────
# sp500: targeted single-strategy sweep (connors_rsi2 only — wasteful to run
#        all sp500 strategies when only one row needs refreshing).
#        2h budget, 2 workers (sp500 has 242 tickers → more parallelism helps).
run_sweep sp500          2.0  2  connors_rsi2

# ETF universes: run DEFAULT_STRATEGIES which includes momentum_breakout +
#                mean_reversion (the contaminated rows) plus mean_reversion,
#                trend_following, opening_gap, sector_rotation.
#                1.5h budget, 1 worker each (small universe, limited tickers).
run_sweep sector_etfs    1.5  1
run_sweep gold_etfs      1.5  1
run_sweep treasury_etfs  1.5  1
run_sweep defensive_etfs 1.5  1

echo "" | tee -a "$SUMMARY"
echo "$(date -Iseconds) ===== ALL SWEEPS DONE =====" | tee -a "$SUMMARY"
touch "$LOG_DIR/resweep_${TS}.done"
echo "$(date -Iseconds) Done sentinel written: $LOG_DIR/resweep_${TS}.done" | tee -a "$SUMMARY"
