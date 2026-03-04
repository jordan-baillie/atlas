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
1. **Position & price data**: /tmp/iran_monitor_data_TS.json — prices, technicals, derived metrics, portfolio checks, rate history, escalation tracking.
2. **Latest news**: /tmp/iran_monitor_news_TS.txt — Brave Search results from 5 targeted queries.

Read BOTH files first using the read tool before making any changes.

## SCORING SYSTEM
Health = (sum of passing_weights + 0.5 × warning_weights) / total_weight × 10.
Each condition is green (passing), amber (warning), or red (failing).
Auto-evaluated conditions (price_above, price_below, ma_position) are handled by the evaluator.
YOU are responsible for all manual_toggle conditions — assess them from NEWS + DATA.

## CRITICAL RULES FOR SOURCE QUALITY
- **Ceasefire/diplomacy claims**: Only set amber if sourced from Reuters, AP, or named diplomats. Media speculation, think-tank commentary, or pundit opinions = noise = stay green. Qualify the source in your note.
- **VLCC rates**: When you find rate data in the news, RECORD it: `python3 scripts/iran_monitor_update.py rate vlcc <value_in_thousands>`. Check rate_history.vlcc_3cycle_trend for direction.
- **Escalation events**: When you find a confirmed military action from Reuters/AP, RECORD it: `python3 scripts/iran_monitor_update.py escalation "description" "source"`. The data file shows hours_since_escalation — a 12h+ gap with no new strikes is a leading de-escalation indicator.

## POSITION-BY-POSITION ASSESSMENT

### XOP (id: 7d94c11d41e2) — Oil E&P
- **xop_hormuz**: Hormuz status. Green=closed/restricted, Amber=partial escorts, Red=fully reopened.
- **xop_backwd**: WTI backwardation. Green=backwardation (Brent-WTI spread normal), Amber=flat, Red=contango. Check portfolio_checks.oil_summary.brent_wti_spread.
- **xop_geopol**: Escalation trend. Green=escalating/holding, Amber=stalemate, Red=de-escalation confirmed.
RED TRIGGERS: WTI <$65 OR XOP <$142 = auto-flag for immediate review.

### RTX (id: 0053ac1a7b04) — Defence
- **rtx_ceasefire**: Green=no ceasefire. Amber=ONLY if Reuters/AP/named diplomats confirm Oman/Qatar mediation with BOTH parties at the table. Pundit speculation stays GREEN. Red=formal ceasefire announced. Red → trim to 3 shares.
- **rtx_defence**: Green=supplemental appropriations/budget increase, Amber=no change, Red=budget cuts proposed. The $50B supplemental strengthens RTX more than backchannel chatter weakens it.
- **rtx_duration**: Green=<4wk, Amber=4-8wk, Red=>8wk.
- **rtx_sector**: ITA/XAR momentum. Check derived_metrics.defence_sector_5d. >1%=green, -1% to 1%=amber, <-1%=red.
FLOOR: Only <$185 = full exit.

### INSW (id: 0a70124996f3) — Tankers (BINARY — amber must never persist >1 cycle)
- **insw_hormuz**: CRITICAL (w4). Green=closed (<20%), Amber=partial (20-60%), Red=open (>60%) = IMMEDIATE EXIT.
- **insw_vlcc**: VLCC spot rates. Green=>$300k, Amber=$150-300k, Red=<$150k. ALWAYS record the rate: `rate vlcc <value>`. Check rate_history for 3-cycle trend — a 17% single-cycle drop is a warning sign even if still above threshold.
- **insw_insurance**: War risk insurance. Green=cancelled/suspended, Amber=elevated (>5x), Red=normal.
- **insw_trail**: 10% trailing stop. Check derived_metrics.insw_trail_status.
- **insw_entry**: Fill status. Red="not filled" is a PROCESS issue, not thesis weakness. The thesis conditions (Hormuz, VLCC, insurance) are what matter.
- **insw_sector**: FRO/DHT peers. Check derived_metrics.tanker_sector_5d.

### NEM (id: 131755aa1899) — Gold Miner
- **nem_fed**: Fed expectations. Green=cuts priced in, Amber=hold, Red=hikes priced in.
- **nem_cbgold**: CB gold buying. Green=accelerating, Amber=stable, Red=net selling.
- **nem_ryield**: Real yields. Green=falling, Amber=flat, Red=rising >2.5%.
- **nem_goldoil**: Gold/oil ratio. Check derived_metrics.gold_oil_ratio_direction. Green=rising, Amber=neutral, Red=falling.
SPECIAL RULE: If nem_ryield AND nem_goldoil BOTH red → recommend trim regardless of gold spot. Oil shock → inflation → rate hikes → gold down. Flag this explicitly.

### CIBR (id: 7adf5478dec9) — Cybersecurity
- **cibr_cyber**: Iran cyber activity. Green=elevated, Amber=stable, Red=reduced post-ceasefire.
- **cibr_cisa**: CISA staffing. Green=<50%, Amber=partial, Red=restored.
- **cibr_earnings**: CRWD/PANW. Green=beat+raised, Amber=inline, Red=miss+lowered.
- **cibr_spending**: Enterprise security spend. Green=accelerating, Amber=stable, Red=decelerating.
- **cibr_entry**: Fill status (process, not thesis).
Most forgiving position. Only exit below $50.

### PSQ (id: d3759efc6f95) — Inverse QQQ Hedge (DECAYS DAILY)
- **psq_qqq_ma**: QQQ vs MA50 — INVERTED. Check derived_metrics.psq_qqq_status. QQQ BELOW = green.
- **psq_spx5d**: S&P 5d. Check derived_metrics.psq_spx_status.
- **psq_escalation**: Conflict trend. Green=escalating, Amber=stalemate, Red=de-escalating.
- **psq_oil5d**: Oil 5d. Check derived_metrics.psq_oil_status.
- **psq_days**: Days held. Check derived_metrics.psq_days_status. >20d = red (decay).
AUTO-FLAG exit if health <5. VIX <18 + QQQ above 50d = close it.

### WDS.AX (id: 20a45fa0c57c) — Woodside Energy
- **wds_lng**: LNG JKM. Green=>$15, Amber=$10-15, Red=<$10.
- **wds_qatar**: Qatar LNG. Green=disrupted, Amber=partial, Red=restored.
- **wds_conc**: Energy concentration = XOP + WDS + INSW (tanker is energy-adjacent). Check portfolio_checks.energy_concentration_status and energy_tickers list. Once INSW fills, recalculate — may exceed 55%.
- **wds_audusd**: AUD/USD. Check derived_metrics.wds_audusd_status.

### CHTR (id: 24c23b54f453) — Charter Communications (quarterly cadence)
- **chtr_fcf**: Capex. Green=declining, Amber=flat, Red=increasing.
- **chtr_broadband**: Subs. Green=moderating, Amber=stable losses, Red=accelerating.
- **chtr_mobile**: Growth. Green=>1.5M, Amber=1-1.5M, Red=<1M.
Only escalate if <$200 or capex reversal.

## GLOBAL KILL SWITCHES
1. **Ceasefire/capitulation** → exit INSW full, sell 50% XOP, trim RTX to 3, exit PSQ. Flag NEM/CIBR.
2. **Energy concentration >55%** (now includes INSW as energy-adjacent) → rebalance.
3. **VIX >35** → review deploying reserve cash.
4. **VIX <18** → exit PSQ, review hedges.
5. **3+ positions health <6** → portfolio stress → full manual review.
6. **Any INVALIDATION** → immediate alert.

## KILL SWITCH PROXIMITY
Check portfolio_checks.kill_switch_proximity. Include in EVERY briefing: "Kill switch: X/4 [details]".
Statuses: clear (0), monitoring (1), elevated (2), imminent (3+).

## YOUR ACTIONS (in order)

### A. Read both data files (use read tool, NOT cat)

### B. Assess and update manual toggles
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py toggle <position_id> <condition_id> <passing|warning|failing>
```

### C. Record any VLCC rates found in news
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py rate vlcc <value_in_thousands>
```

### D. Record any new escalation events from Reuters/AP
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py escalation "description" "Reuters/AP"
```

### E. Add situation note to ALL positions
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py note-all "[4h update HH:MM] summary"
```

### F. Re-evaluate auto conditions
```bash
cd /root/atlas && python3 scripts/iran_monitor_update.py evaluate
```

### G. Send Telegram briefing IF material changes occurred
```bash
cd /root/atlas && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.telegram import send_message
send_message('''YOUR_MESSAGE_HERE''')
"
```

Required briefing format — oil prices FRONT AND CENTRE:
```
🌍 <b>Iran Monitor [HH:MM AEST]</b>

<b>Oil:</b> WTI $XX.XX (Xd%) | Brent $XX.XX (Xd%) | Spread $X.XX
<b>Escalation:</b> Xh since last confirmed action [source]

<b>Situation:</b> 1-2 sentences

<b>Changes:</b>
• TICKER condition: old → new (reason + SOURCE)

<b>Health:</b> XOP X | RTX X | INSW X | NEM X | CIBR X | PSQ X | WDS X | CHTR X
<b>Energy exposure:</b> XX% [XOP+WDS+INSW] (status)
<b>Kill switch:</b> X/4 [details or "clear"]

Threat: 🟢/🟡/🔴/⚫
```

If nothing material changed, SKIP Telegram — just add note-all.

### H. Refresh dashboard
```bash
cd /root/atlas && python3 dashboard/generate_data.py 2>/dev/null
```

## RULES
- Be factual. Only change toggles when news CLEARLY indicates a change.
- Source quality matters: Reuters/AP/named officials = signal. Pundits/speculation = noise.
- When uncertain → warning (not failing).
- INSW amber must NEVER persist >1 cycle. Escalate immediately.
- VLCC rate direction matters as much as level — always record the rate.
- Oil prices are the master variable for half the portfolio — feature them prominently.
- NEM paradox: oil shock → inflation → rate hikes → gold down. Watch nem_ryield + nem_goldoil pair.
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
