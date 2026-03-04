#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Atlas Data Scientist Agent — weekly analysis + Telegram report
#
# Runs all analyses via data_scientist.py, then spawns a pi agent
# (haiku — cheap) to interpret results, identify actionable insights,
# and send a formatted Telegram briefing.
#
# Cron: 07:00 AEST every Sunday
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

PROJECT="/root/atlas"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/data-science_${TIMESTAMP}.log"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

echo "=== Atlas Data Scientist — $TIMESTAMP ===" > "$LOG_FILE"

# ── Step 1: Run all analyses (JSON for pi agent) ──
echo "Running analyses..." >> "$LOG_FILE"
ANALYSIS_JSON=$(python3 scripts/data_scientist.py --analysis weekly_digest --json 2>>"$LOG_FILE")
ANALYSIS_EXIT=$?

if [ "$ANALYSIS_EXIT" -ne 0 ] || [ -z "$ANALYSIS_JSON" ]; then
    echo "Analysis failed (exit $ANALYSIS_EXIT)" >> "$LOG_FILE"
    exit 1
fi

# Also generate the human report for the log
python3 scripts/data_scientist.py --analysis weekly_digest >> "$LOG_FILE" 2>&1

# ── Step 2: Spawn pi agent to interpret ──
echo "" >> "$LOG_FILE"
echo "=== Pi Agent Interpretation ===" >> "$LOG_FILE"

# Save analysis to temp file (too large for shell variable in prompt)
ANALYSIS_FILE=$(mktemp /tmp/atlas-ds-XXXXXX.json)
echo "$ANALYSIS_JSON" > "$ANALYSIS_FILE"

PROMPT="You are the Atlas Data Scientist agent (Opus). Your job is to interpret trading system analytics and research results, then produce a concise, actionable weekly briefing.

Read the analysis results from: $ANALYSIS_FILE

The file contains a weekly digest with these sections:
- regime_state: Current market regime (trending/mean-reverting/volatile)
- signal_accuracy: Forward-tested signal win rates and returns
- confidence_model: Whether confidence scores predict profitability
- strategy_mix: Strategy signal generation balance
- rejection_impact: Opportunity cost of rejected signals
- alpha_decay: Rolling performance degradation
- research_insights: Full research journal analysis — strategy scorecard (A/B/C/D grades), infrastructure blockers, key learnings from 56 experiments
- wave_recommendations: Prioritized list of what Wave 3 should focus on, cross-referencing regime + research + signals

Produce a Telegram-formatted briefing with these sections:

1. **REGIME & ALIGNMENT** — Current regime, whether strategy allocation matches, what to shift
2. **SIGNAL QUALITY** — Win rates, returns by strategy. Be honest about data sufficiency.
3. **RESEARCH SCORECARD** — Which strategies are promising (grade A/B), which are failing (D), which have infrastructure blockers
4. **WAVE 3 DIRECTION** — What the next research wave should prioritize and why. Be specific about experiments to run. Cross-reference regime (what the market needs) with research results (what shows promise).
5. **ACTION ITEMS** — Max 5 concrete numbered items covering: live trading adjustments, research priorities, infrastructure fixes
6. **DATA CONFIDENCE** — How much data we have, what conclusions are solid vs. preliminary

Rules:
- Be direct and quantitative. No filler. You are Opus — think deeply about cross-cutting insights.
- If strategies that work well in the current regime were tested and showed promise in research, flag them as high-priority promotion candidates.
- If infrastructure blockers are preventing valid experiments, flag fix-first before more research.
- If the confidence model is broken, recommend specific remediation.
- Format for Telegram: use <b>bold</b>, bullet points, keep under 4000 chars.
- The briefing should help a trader decide what to trade AND what to research THIS week.

After writing the briefing text, send it to Telegram by running:
  cd /root/atlas && python3 -c \"
import sys; sys.path.insert(0, '.'); from utils.telegram import send_message
send_message('''YOUR_BRIEFING_TEXT_HERE''')
\"

If the Telegram send fails, that's ok — the briefing is still in the log."

timeout 300 pi -p --no-session --model anthropic/claude-opus-4-6 "$PROMPT" >> "$LOG_FILE" 2>&1
PI_EXIT=$?

# Cleanup
rm -f "$ANALYSIS_FILE"

echo "" >> "$LOG_FILE"
echo "Pi agent exit: $PI_EXIT" >> "$LOG_FILE"

# Clean old logs (keep 30 days)
find "$LOG_DIR" -name "data-science_*.log" -mtime +30 -delete 2>/dev/null

exit 0
