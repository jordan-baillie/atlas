#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# Iran Conflict Monitor — 4-hourly geopolitical intelligence
#
# 1. Collects prices, technicals, portfolio checks (Python)
# 2. Searches latest Iran news (Brave Search, 5 queries)
# 3. Spawns pi agent to interpret news + update Monitor tab
#
# Cron: every 4 hours (00,04,08,12,16,20 AEST)
# Cost: ~$0.05-0.10 per run (sonnet)
# ═══════════════════════════════════════════════════════════════
set -uo pipefail

PROJECT="/root/atlas"
LOG_DIR="$PROJECT/logs"
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOG_DIR/iran-monitor_${TIMESTAMP}.log"
BRAVE_SEARCH="/root/.pi/agent/skills/pi-skills/brave-search/search.js"

export TZ="Australia/Brisbane"
export HOME="${HOME:-/root}"
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

mkdir -p "$LOG_DIR"
cd "$PROJECT"

echo "=== Iran Monitor — $TIMESTAMP ===" > "$LOG_FILE"

# ── Step 1: Collect position + price + portfolio data ──
echo "[$(date '+%H:%M:%S')] Collecting data..." >> "$LOG_FILE"
DATA_FILE="/tmp/iran_monitor_data_${TIMESTAMP}.json"
python3 scripts/iran_monitor.py --json > "$DATA_FILE" 2>>"$LOG_FILE"
if [ ! -s "$DATA_FILE" ]; then
    echo "ERROR: iran_monitor.py returned empty" >> "$LOG_FILE"
    rm -f "$DATA_FILE"
    exit 1
fi
echo "[$(date '+%H:%M:%S')] Data collected ($(wc -c < "$DATA_FILE") bytes)" >> "$LOG_FILE"

# ── Step 2: Search latest news (5 targeted queries) ──
echo "[$(date '+%H:%M:%S')] Searching news..." >> "$LOG_FILE"
NEWS_FILE="/tmp/iran_monitor_news_${TIMESTAMP}.txt"
echo "=== BRAVE SEARCH — Iran Conflict Intelligence ===" > "$NEWS_FILE"
echo "Search time: $(date)" >> "$NEWS_FILE"
echo "" >> "$NEWS_FILE"

search_query() {
    local label="$1"; shift
    echo "── $label ──" >> "$NEWS_FILE"
    timeout 30 node "$BRAVE_SEARCH" "$@" >> "$NEWS_FILE" 2>>"$LOG_FILE" || echo "(search failed)" >> "$NEWS_FILE"
    echo "" >> "$NEWS_FILE"
}

search_query "MILITARY / STRIKES / CEASEFIRE" "Iran military strikes ceasefire latest today" -n 8 --freshness pd
search_query "OIL / HORMUZ / TANKER RATES" "oil price Iran Hormuz strait tanker VLCC rates" -n 6 --freshness pd
search_query "GOLD / SAFE HAVEN / FED POLICY" "gold price safe haven Fed rate expectations central bank buying" -n 5 --freshness pd
search_query "CYBER / INFRASTRUCTURE THREATS" "Iran cyber attack US infrastructure CISA warning" -n 4 --freshness pd
search_query "DEFENCE / BUDGET / DIPLOMACY" "US defence spending supplemental Iran diplomacy negotiations" -n 4 --freshness pd

echo "[$(date '+%H:%M:%S')] News collected ($(wc -c < "$NEWS_FILE") bytes)" >> "$LOG_FILE"

# ── Step 3: Spawn pi agent ──
echo "[$(date '+%H:%M:%S')] Spawning agent..." >> "$LOG_FILE"

read -r -d '' PROMPT << 'AGENTPROMPT'
You are the Atlas Iran Conflict Monitor agent. Every 4 hours you assess geopolitical developments and update position health scores on the Monitor tab.

## YOUR DATA FILES
1. **Position & price data**: /tmp/iran_monitor_data_TS.json — contains current prices, technicals, derived metrics (gold/oil ratio, trailing stops, sector momentum, portfolio concentration), and all position conditions with their current statuses.
2. **Latest news**: /tmp/iran_monitor_news_TS.txt — Brave Search results from 5 targeted queries.

Read BOTH files first using the read tool before making any changes.

## SCORING SYSTEM
Health = (sum of passing_weights + 0.5 × warning_weights) / total_weight × 10.
Each condition is green (passing), amber (warning), or red (failing).
Auto-evaluated conditions (price_above, price_below, ma_position) are handled by the evaluator.
YOU are responsible for all manual_toggle conditions — assess them from NEWS + DATA.

## POSITION-BY-POSITION ASSESSMENT

### XOP (id: 7d94c11d41e2) — Oil E&P
Manual toggles to assess:
- **xop_hormuz**: Strait of Hormuz status. Green=closed/restricted, Amber=partial escorts, Red=fully reopened.
- **xop_backwd**: WTI curve backwardation. Green=backwardation, Amber=flat, Red=contango. Check BZ=F vs CL=F spread or news.
- **xop_geopol**: Geopolitical escalation. Green=escalating/holding, Amber=stalemate, Red=de-escalation confirmed.
RED TRIGGERS: WTI <$65 OR XOP <$142 = auto-flag for immediate review. Two+ ambers = cap health at 6.

### RTX (id: 0053ac1a7b04) — Defence
- **rtx_ceasefire**: Green=no ceasefire, Amber=backchannel talks reported, Red=formal ceasefire. Red doesn't mean auto-exit → trim to 3 shares.
- **rtx_defence**: Green=supplemental appropriations/budget increase, Amber=no change, Red=budget cuts proposed.
- **rtx_duration**: Green=<4wk ongoing, Amber=4-8wk (munition depletion strengthens), Red=>8wk (market drag).
- **rtx_sector**: Defence sector momentum (ITA/XAR). Check 5d change in derived_metrics. Green=up, Amber=flat, Red=down.
FLOOR: RTX has structural value even in de-escalation. Only <$185 = full exit.

### INSW (id: 0a70124996f3) — Tankers (BINARY POSITION)
- **insw_hormuz**: CRITICAL (weight 4). Green=closed (<20% normal), Amber=partial (20-60%), Red=open (>60%) = IMMEDIATE EXIT. NEVER sit at amber >1 cycle.
- **insw_vlcc**: VLCC spot rates. Green=>$300k/day, Amber=$150-300k, Red=<$150k.
- **insw_insurance**: War risk insurance. Green=cancelled/suspended, Amber=elevated (>5x), Red=normal rates.
- **insw_trail**: 10% trailing stop from highest close. Check derived_metrics.insw_trail_status. Green=above, Amber=within 3%, Red=below = IMMEDIATE EXIT.
- **insw_entry**: Fill status. Green=filled at target, Amber=gapped >10%, Red=not filled.
- **insw_sector**: Tanker peers FRO/DHT. Check derived_metrics. Green=up 5d, Amber=flat, Red=down.

### NEM (id: 131755aa1899) — Gold Miner
- **nem_fed**: Fed expectations. Green=cuts priced in, Amber=hold, Red=hikes priced in.
- **nem_cbgold**: Central bank gold buying. Green=continued/accelerating, Amber=stable, Red=net selling.
- **nem_ryield**: Real yields direction. Green=falling, Amber=flat, Red=rising >2.5%.
- **nem_goldoil**: Gold/oil ratio. Check derived_metrics.gold_oil_ratio_direction. Green=rising, Amber=neutral, Red=falling.
SPECIAL RULE: If nem_ryield AND nem_goldoil BOTH go red → recommend trim regardless of gold spot price.

### CIBR (id: 7adf5478dec9) — Cybersecurity
- **cibr_cyber**: Iran cyber activity. Green=elevated/active incidents, Amber=stable, Red=reduced post-ceasefire.
- **cibr_cisa**: CISA staffing. Green=understaffed (<50%), Amber=partial, Red=fully restored.
- **cibr_earnings**: Sector earnings CRWD/PANW. Green=beat+raised, Amber=inline, Red=miss+lowered.
- **cibr_spending**: Enterprise security spending. Green=accelerating, Amber=stable, Red=decelerating.
- **cibr_entry**: Fill status. Green=filled, Amber=gapped, Red=not filled.
Most forgiving position. Only exit below $50 (broader tech meltdown).

### PSQ (id: d3759efc6f95) — Inverse QQQ Hedge
- **psq_qqq_ma**: QQQ vs 50-day MA — INVERTED for PSQ. Check derived_metrics.psq_qqq_status. QQQ BELOW MA = green, above = red.
- **psq_spx5d**: S&P 5d trend. Check derived_metrics.psq_spx_status. Declining=green, flat=amber, rising=red.
- **psq_escalation**: Conflict trend. Green=escalating, Amber=stalemate, Red=de-escalating.
- **psq_oil5d**: Oil 5d trend. Check derived_metrics.psq_oil_status. Rising=green (inflationary), flat=amber, falling=red.
- **psq_days**: Days held. Check derived_metrics.psq_days_status.
AUTO-FLAG for exit if health <5. Inverse ETFs DECAY — if VIX <18 and QQQ above 50d MA, recommend closing.

### WDS.AX (id: 20a45fa0c57c) — Woodside Energy
- **wds_lng**: LNG JKM price. Green=>$15/MMBtu, Amber=$10-15, Red=<$10.
- **wds_qatar**: Qatar LNG exports. Green=disrupted, Amber=partial, Red=restored.
- **wds_conc**: Energy concentration (XOP+WDS). Check portfolio_checks.energy_concentration_status.
- **wds_audusd**: AUD/USD direction. Check derived_metrics.wds_audusd_status. Weakening AUD=green.

### CHTR (id: 24c23b54f453) — Charter Communications
- **chtr_fcf**: Capex trend. Green=declining, Amber=flat, Red=increasing. QUARTERLY check only.
- **chtr_broadband**: Sub trend. Green=losses moderating, Amber=stable, Red=accelerating losses.
- **chtr_mobile**: Mobile growth. Green=>1.5M adds, Amber=1-1.5M, Red=<1M.
Lowest maintenance. Only escalate if <$200 or capex goes wrong.

## GLOBAL PORTFOLIO KILL SWITCHES
Check these EVERY cycle from portfolio_checks and news:
1. **Ceasefire/capitulation confirmed** → KILL SWITCH: exit INSW full, sell 50% XOP, trim RTX to 3, exit PSQ. Flag NEM/CIBR for review.
2. **Iran-conflict exposure >60%** → Concentration warning → flag for manual rebalance.
3. **VIX >35** → Extreme fear → review deploying reserve cash.
4. **VIX <18** → Low vol → exit PSQ, review all hedges.
5. **3+ positions health <6** → Portfolio stress → flag for full manual review.
6. **Any INVALIDATION condition hit** → Immediate alert, don't wait.

## YOUR ACTIONS (in order)

### A. Read both data files
```bash
# Read them with the read tool — do NOT use cat
```

### B. Assess and update manual toggles
For each manual_toggle, decide: passing, warning, or failing. Use the update script:
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py toggle <position_id> <condition_id> <passing|warning|failing>
```
Only change toggles when NEWS clearly supports the change. When uncertain, use "warning".

### C. Add situation note to ALL positions
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py note-all "[4h update] Your 1-3 sentence summary"
```

### D. Re-evaluate auto conditions (prices + MAs)
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py evaluate
```

### E. Send Telegram briefing IF material changes occurred
If ANY toggle changed, or significant price moves (>3%), or kill switch triggered:
```bash
cd /root/atlas && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.telegram import send_message
send_message('''YOUR_MESSAGE_HERE''')
"
```

Format:
```
🌍 <b>Iran Monitor [HH:MM AEST]</b>

<b>Situation:</b> 1-2 sentence summary

<b>Changes:</b>
• TICKER condition: old → new (reason)

<b>Portfolio:</b>
Health: XOP X/10 | RTX X/10 | INSW X/10 | ...
Energy exposure: XX%
Kill switches: none / [list]

Threat level: 🟢 Low / 🟡 Moderate / 🔴 High / ⚫ Critical
```

If nothing material changed, SKIP the Telegram message — just add the note-all.

### F. Refresh dashboard
```bash
cd /root/atlas && python3 dashboard/generate_data.py 2>/dev/null
```

## RULES
- Be factual. Only change toggles when news CLEARLY indicates a change.
- When uncertain → warning (not failing).
- INSW is binary — if Hormuz goes amber, it should NEVER persist to next cycle. Escalate immediately.
- Check derived_metrics for pre-computed values (gold/oil ratio, trailing stops, sector momentum, PSQ status).
- Check portfolio_checks for concentration and kill switch triggers.
- Always run evaluate at the end to refresh auto-conditions.
AGENTPROMPT

# Substitute timestamp in file paths
PROMPT="${PROMPT//TS/$TIMESTAMP}"

timeout 600 pi -p --no-session --model anthropic/claude-sonnet-4-6 "$PROMPT" >> "$LOG_FILE" 2>&1
PI_EXIT=$?

echo "[$(date '+%H:%M:%S')] Agent exit: $PI_EXIT" >> "$LOG_FILE"

# ── Cleanup ──
rm -f "$DATA_FILE" "$NEWS_FILE"
find "$LOG_DIR" -name "iran-monitor_*.log" -mtime +7 -delete 2>/dev/null

echo "[$(date '+%H:%M:%S')] Done" >> "$LOG_FILE"
exit 0
