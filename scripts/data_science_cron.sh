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

PROMPT="You are the Atlas Data Scientist agent. Your job is to interpret trading system analytics and produce a concise, actionable weekly briefing.

Read the analysis results from: $ANALYSIS_FILE

Then produce a Telegram-formatted briefing following this structure:

1. **REGIME** — Current market regime and whether our strategy allocation matches it
2. **SIGNAL QUALITY** — Are our signals actually profitable? Win rate, avg return by strategy
3. **KEY FINDING** — The single most important insight this week (could be positive or negative)
4. **ACTION ITEMS** — Concrete numbered list of what to change/investigate (max 3 items)
5. **DATA QUALITY NOTE** — How much data we have, confidence in the results

Rules:
- Be direct and quantitative. No filler.
- If data is insufficient (e.g. <20 testable signals), say so clearly and don't over-interpret.
- If strategies are misaligned with regime, flag it as priority #1.
- Format for Telegram: use <b>bold</b>, bullet points, keep under 3000 chars.
- The briefing should help a trader decide what to do THIS week.

After writing the briefing text, send it to Telegram by running:
  cd /root/atlas && python3 -c \"
import sys; sys.path.insert(0, '.'); from utils.telegram import send_message
send_message('''YOUR_BRIEFING_TEXT_HERE''')
\"

If the Telegram send fails, that's ok — the briefing is still in the log."

timeout 180 pi -p --no-session --model anthropic/claude-haiku-4-5 "$PROMPT" >> "$LOG_FILE" 2>&1
PI_EXIT=$?

# Cleanup
rm -f "$ANALYSIS_FILE"

echo "" >> "$LOG_FILE"
echo "Pi agent exit: $PI_EXIT" >> "$LOG_FILE"

# Clean old logs (keep 30 days)
find "$LOG_DIR" -name "data-science_*.log" -mtime +30 -delete 2>/dev/null

exit 0
