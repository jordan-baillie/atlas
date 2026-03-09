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

# Export API keys directly — sourcing .profile under set -u fails because
# .bashrc references $PS1 (unbound in cron), causing instant script death.
export BRAVE_API_KEY="${BRAVE_API_KEY:-BSAHxsnvVgTqZewUgDPpVSMP8SFmJB2}"

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

# ── Step 2: Search latest news (multi-source intelligence) ──
# Sources: Brave API + GDELT API + Google News RSS + live blog scraping
# Parallel fetch, fuzzy dedup, wire service prioritisation
echo "[$(date '+%H:%M:%S')] Searching news (multi-source)..." >> "$LOG_FILE"
NEWS_FILE="/tmp/iran_monitor_news_${TIMESTAMP}.txt"

timeout 180 python3 "$PROJECT/scripts/news_intel.py" --hours 4 > "$NEWS_FILE" 2>>"$LOG_FILE"
SEARCH_EXIT=$?
if [ $SEARCH_EXIT -ne 0 ] || [ ! -s "$NEWS_FILE" ]; then
    echo "[$(date '+%H:%M:%S')] WARNING: news_intel.py failed (exit=$SEARCH_EXIT), falling back to brave_news.js" >> "$LOG_FILE"
    timeout 120 node "$PROJECT/scripts/brave_news.js" --hours 4 > "$NEWS_FILE" 2>>"$LOG_FILE"
    if [ ! -s "$NEWS_FILE" ]; then
        echo "=== BRAVE SEARCH — FALLBACK ===" > "$NEWS_FILE"
        timeout 30 node "$BRAVE_SEARCH" "Iran conflict war latest news" -n 10 --freshness pd >> "$NEWS_FILE" 2>>"$LOG_FILE"
    fi
fi

echo "[$(date '+%H:%M:%S')] News collected ($(wc -c < "$NEWS_FILE") bytes)" >> "$LOG_FILE"

# ── Step 3: Spawn pi agent ──
echo "[$(date '+%H:%M:%S')] Spawning agent..." >> "$LOG_FILE"

read -r -d '' PROMPT << 'AGENTPROMPT'
You are the Atlas Iran Conflict Monitor agent. Every 4 hours you assess geopolitical developments and update position health scores on the Monitor tab.

## YOUR DATA FILES
1. **Position & price data**: /tmp/iran_monitor_data_TS.json — prices, technicals, derived metrics, portfolio checks, rate history, escalation tracking.
2. **Latest news**: /tmp/iran_monitor_news_TS.txt — Multi-endpoint Brave Search (news + web + video). Results are split into two sections:
   - 🔴 **NEW SINCE LAST UPDATE (last 4h)** — these are the developments since your previous run. Focus here first.
   - 🟡 **OLDER CONTEXT (4-24h)** — background from earlier today. Only reference if relevant.

Read BOTH files first using the read tool before making any changes.

## 4-HOUR COMPARISON PROTOCOL
You run every 4 hours. Your job is to identify WHAT CHANGED since the last run:
- Compare the 🔴 RECENT section against the position states and rate_history from the data file.
- If the 🔴 RECENT section is empty, that itself is signal (no breaking developments = stable).
- Only change manual toggles when 🔴 RECENT news clearly justifies a change. Do NOT re-litigate old news.
- In your Telegram briefing, lead with "X new developments since last update" or "No new developments".

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
- **rtx_ceasefire**: Green=no ceasefire. Amber=ONLY if Reuters/AP report BOTH parties (US/Iran) have agreed to sit at a mediation table. Oman "offering off-ramps" is NOT amber — that's one-sided. Hegseth/Rubio escalatory rhetoric = GREEN (confirms no diplomatic off-ramp). Pundit speculation stays GREEN. Geographic expansion (Indian Ocean, Fujairah) = GREEN (escalation, not de-escalation). Red=formal ceasefire announced. Red → trim to 3 shares.
- **rtx_defence**: Green=supplemental appropriations/budget increase, Amber=no change, Red=budget cuts proposed. The $50B supplemental strengthens RTX more than backchannel chatter weakens it.
- **rtx_duration**: Green=<4wk, Amber=4-8wk, Red=>8wk.
- **rtx_sector**: ITA/XAR momentum. Check derived_metrics.defence_sector_5d. >1%=green, -1% to 1%=amber, <-1%=red.
FLOOR: Only <$185 = full exit.

### INSW (id: 0a70124996f3) — Tankers (BINARY — amber must never persist >1 cycle)
- **insw_hormuz**: CRITICAL (w4). Green=closed (<20%), Amber=partial (20-60%), Red=open (>60%) = IMMEDIATE EXIT.
  GEOGRAPHIC EXPANSION NOTE: Fujairah port strike + Indian Ocean naval engagement = danger zone extends BEYOND Hormuz. Even if Hormuz partially reopens, Gulf of Oman/Arabian Sea disruption keeps tanker thesis intact. insw_hormuz stays GREEN as long as ANY major shipping zone is disrupted. Only go amber if ALL zones normalise.
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

## CONFLICT GEOGRAPHIC SCOPE
Check portfolio_checks.conflict_geographic_scope. Report in EVERY briefing.
The data file auto-detects active zones from escalation history keywords:
- **persian_gulf** + **hormuz**: always active in Iran conflict (baseline)
- **gulf_of_oman**: Fujairah strikes, UAE east coast, bunkering disruption
- **arabian_sea**: Indian Ocean naval engagements, submarine/frigate actions
- **red_sea**: Houthi corridor, Bab el-Mandeb, Suez disruption
- **mediterranean**: Lebanon/Cyprus/Crete base attacks

Scope statuses: contained (≤2 zones), expanding (3), regional (4-5), global (6).
**When news confirms action in a NEW zone, RECORD it as an escalation event** so the auto-detector picks it up:
```bash
python3 scripts/iran_monitor_update.py escalation "Fujairah port struck by Iranian drones — Gulf of Oman" "Reuters"
```
Geographic expansion is BULLISH for thesis (XOP, INSW, NEM, RTX, CIBR, PSQ) — it means:
- More shipping lanes disrupted → higher tanker rates, oil premium
- Less containment likelihood → longer conflict duration → longer position hold
- Broader insurance repricing → INSW Hormuz stays green even if Hormuz partially reopens

## KILL SWITCH PROXIMITY
Check portfolio_checks.kill_switch_proximity. Include in EVERY briefing: "Kill switch: X/4 [details]".
Statuses: clear (0), monitoring (1), elevated (2), imminent (3+).

## YOUR ACTIONS (in order)

CRITICAL: You MUST execute ALL steps A through H using bash tool calls. Do NOT just output text analysis — that achieves nothing. Every step requires running actual commands. If you skip any step, the monitor is broken.

### A. Read all data files (use read tool, NOT cat)

Also read `data/position_monitor/ceasefire_factors.json` to get the current ceasefire probability. Extract `probability`, `probability_label`, and `timeline` from it for use in the briefing.

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

### G. ALWAYS send Telegram briefing
```bash
cd /root/atlas && python3 -c "
import sys; sys.path.insert(0, '.')
from utils.telegram import send_message
send_message('''YOUR_MESSAGE_HERE''')
"
```

CRITICAL: You MUST send a Telegram message EVERY cycle. No exceptions. Even if no toggles changed, the briefing confirms the situation is being monitored. "No new developments" IS a valid briefing — send it.

Required briefing format — oil prices FRONT AND CENTRE:
```
🌍 <b>Iran Monitor [HH:MM AEST]</b>

<b>Oil:</b> WTI $XX.XX (Xd%) | Brent $XX.XX (Xd%) | Spread $X.XX
<b>Escalation:</b> Xh since last confirmed action [source]
<b>Geographic scope:</b> X zones [zone list] — STATUS

<b>Situation:</b> 1-2 sentences — lead with "X new developments" or "No new developments"

<b>Changes:</b>
• TICKER condition: old → new (reason + SOURCE)
(or "No toggle changes" if none)

<b>Health:</b> XOP X | RTX X | INSW X | NEM X | CIBR X | PSQ X | WDS X | CHTR X
<b>Energy exposure:</b> XX% [XOP+WDS+INSW] (status)
<b>Kill switch:</b> X/4 [details or "clear"]
<b>Ceasefire:</b> X% — LABEL (timeline)

Threat: 🟢/🟡/🔴/⚫
```

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

# ── Verify Telegram was sent (agent sometimes skips it) ──
if ! grep -qi "send_message\|Telegram.*sent\|telegram.*ok\|200.*ok" "$LOG_FILE" 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] WARNING: No Telegram send detected in agent output — sending fallback" >> "$LOG_FILE"
    python3 -c "
import sys; sys.path.insert(0, '$PROJECT')
from utils.telegram import send_message
send_message('⚠️ <b>Iran Monitor [$(date '+%H:%M') AEST]</b>\n\nAgent completed but did not send briefing. Check logs.\nExit code: $PI_EXIT')
" >> "$LOG_FILE" 2>&1
fi

# ── Cleanup ──
rm -f "$DATA_FILE" "$NEWS_FILE"
find "$LOG_DIR" -name "iran-monitor_*.log" -mtime +7 -delete 2>/dev/null

echo "[$(date '+%H:%M:%S')] Done" >> "$LOG_FILE"
exit 0
