#!/usr/bin/env bash
# Daily forward-paper cycle for deployed shadow strategies (board 2026-06-09 gate).
# Order matters: (1) record realized return from PRE-rebalance equity, (2) refresh today's target
# weights from live data (Crucible), (3) run the shadow loop (paper orders + track-vs-expectation).
set -uo pipefail
LOG=/root/atlas/data/live/forward_paper.log
echo "=== forward-paper cycle $(date -Is) ===" >> "$LOG"
cd /root/atlas    && python3 -m atlas.execution.record_returns      >> "$LOG" 2>&1 || echo "record_returns FAILED" >> "$LOG"
cd /root/crucible && python3 live/deploy.py refresh                 >> "$LOG" 2>&1 || echo "weight refresh FAILED" >> "$LOG"
cd /root/atlas    && python3 -m atlas.execution.daily --mode shadow >> "$LOG" 2>&1 || echo "daily shadow FAILED" >> "$LOG"
echo "=== done $(date -Is) ===" >> "$LOG"
