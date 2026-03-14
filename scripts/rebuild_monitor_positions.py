#!/usr/bin/env python3
"""Rebuild Monitor positions with comprehensive Iran conflict health framework.

Run once to set up the scoring system. Preserves existing notes and metadata.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

POSITIONS_FILE = PROJECT / "data" / "position_monitor" / "positions.json"
NOW = datetime.now().isoformat(timespec="seconds")


def cond(id, label, type, source="", threshold=0.0, warning_threshold=None,
         direction="above", weight=1, status="passing", notes=""):
    """Build a condition dict."""
    return {
        "id": id, "label": label, "type": type, "source": source,
        "threshold": threshold, "warning_threshold": warning_threshold,
        "direction": direction, "weight": weight, "status": status,
        "current_value": None, "last_checked": NOW, "notes": notes,
    }


# Load existing to preserve notes
existing = {}
if POSITIONS_FILE.exists():
    with open(POSITIONS_FILE) as f:
        for p in json.load(f):
            existing[p["id"]] = p

def get_notes(pid):
    return existing.get(pid, {}).get("notes", [])


positions = []

# ═══════════════════════════════════════════════════════════════════════════
# XOP — Oil E&P ETF (5.4 shares @ $142.49)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "7d94c11d41e2",
    "ticker": "XOP",
    "asset_type": "ETF",
    "entry_price": 142.49,
    "entry_date": "2026-02-26",
    "quantity": 5.4,
    "direction": "long",
    "thesis": "Oil E&P geopolitical upside. WTI <$65 = geopolitical premium unwound = invalidation. Breakeven stop at cost basis on remaining shares.",
    "timeframe": "6-12 months",
    "invalidation_price": 130.0,
    "target_price": 180.0,
    "tags": ["energy", "oil", "geopolitical", "iran-conflict"],
    "status": "open",
    "conditions": [
        # Weight 3: WTI spot — primary driver
        cond("xop_wti", "WTI spot price", "price_above",
             source="CL=F", threshold=65.0, warning_threshold=70.0, weight=3,
             notes="<$65 = INVALIDATION (geopolitical premium unwound)"),
        # Weight 2: XOP vs cost basis
        cond("xop_cost", "XOP vs cost basis ($142.49)", "price_above",
             source="XOP", threshold=142.0, warning_threshold=155.0, weight=2,
             notes="<$142 = BREAKEVEN STOP. >$155 = green"),
        # Weight 2: Hormuz status (manual — agent assesses from news)
        cond("xop_hormuz", "Hormuz status", "manual_toggle",
             weight=2, notes="Green=closed/restricted, Amber=partial escorts, Red=fully reopened"),
        # Weight 1: WTI curve shape (manual)
        cond("xop_backwd", "WTI curve in backwardation", "manual_toggle",
             weight=1, notes="Green=backwardation, Amber=flat, Red=contango"),
        # Weight 1: XOP vs 50-day MA (auto)
        cond("xop_ma50", "XOP above 50-day MA", "ma_position",
             source="XOP", threshold=50, weight=1),
        # Weight 1: Geopolitical escalation trend (manual)
        cond("xop_geopol", "Geopolitical escalation trend", "manual_toggle",
             weight=1, notes="Green=escalating/holding, Amber=stalemate, Red=de-escalation confirmed"),
    ],
    "notes": get_notes("7d94c11d41e2"),
    "created_at": "2026-03-02T18:39:39", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# RTX — Defence / Raytheon (6 shares @ ~$212)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "0053ac1a7b04",
    "ticker": "RTX",
    "asset_type": "stock",
    "entry_price": 207.59,
    "entry_date": "2026-03-04",
    "quantity": 6,
    "direction": "long",
    "thesis": "Defence munition replenishment + $68B backlog + structural budget tailwinds. Iran conflict accelerates spend. Floor at $185 even in de-escalation.",
    "timeframe": "6-12 months",
    "invalidation_price": 185.0,
    "target_price": 235.0,
    "tags": ["defence", "geopolitical", "iran-conflict"],
    "status": "open",
    "conditions": [
        # Weight 3: Ceasefire status (manual)
        cond("rtx_ceasefire", "No ceasefire announced", "manual_toggle",
             weight=3, notes="Green=no ceasefire, Amber=backchannel talks reported, Red=formal ceasefire announced. Red ≠ auto-exit → trim to 3 shares."),
        # Weight 2: RTX vs $195 pre-strike (auto)
        cond("rtx_prestrike", "RTX above $195 (pre-strike level)", "price_above",
             source="RTX", threshold=195.0, warning_threshold=205.0, weight=2,
             notes="<$195 = INVALIDATION. $185 = full exit."),
        # Weight 2: US defence spending (manual)
        cond("rtx_defence", "US defence spending narrative intact", "manual_toggle",
             weight=2, notes="Green=supplemental appropriations/budget increase, Amber=no change, Red=budget cuts proposed"),
        # Weight 1: RTX vs 50-day MA (auto)
        cond("rtx_ma50", "RTX above 50-day MA", "ma_position",
             source="RTX", threshold=50, weight=1),
        # Weight 1: Conflict duration (manual)
        cond("rtx_duration", "Conflict duration assessment", "manual_toggle",
             weight=1, notes="Green=<4wk ongoing, Amber=4-8wk (munition depletion thesis strengthens), Red=>8wk (broader market drag)"),
        # Weight 1: Sector momentum ITA/XAR (manual)
        cond("rtx_sector", "Defence sector momentum (ITA/XAR)", "manual_toggle",
             weight=1, notes="Green=up week-over-week, Amber=flat, Red=down week-over-week"),
    ],
    "notes": get_notes("0053ac1a7b04"),
    "created_at": "2026-03-04T10:00:00", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# INSW — Tanker / International Seaways (7 shares @ ~$80, PENDING)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "0a70124996f3",
    "ticker": "INSW",
    "asset_type": "stock",
    "entry_price": 80.0,
    "entry_date": "2026-03-04",
    "quantity": 7,
    "direction": "long",
    "thesis": "Hormuz closure → record VLCC rates. Best balance sheet in tankers. IMMEDIATE EXIT if Hormuz reopens. Binary position — amber should never persist >1 cycle without action.",
    "timeframe": "1-3 months",
    "invalidation_price": 72.0,
    "target_price": 95.0,
    "tags": ["tanker", "energy", "geopolitical", "iran-conflict", "binary", "pending"],
    "status": "open",
    "conditions": [
        # Weight 4: Strait of Hormuz (manual — CRITICAL)
        cond("insw_hormuz", "Strait of Hormuz closed/restricted", "manual_toggle",
             weight=4, notes="Green=closed (<20% normal traffic), Amber=partial (20-60% normal, escorted), Red=open (>60%) = IMMEDIATE EXIT. Never sit at amber >1 cycle."),
        # Weight 2: VLCC spot rates (manual)
        cond("insw_vlcc", "VLCC spot rates above $150k/day", "manual_toggle",
             weight=2, notes="Green=>$300k/day, Amber=$150-300k/day, Red=<$150k/day"),
        # Weight 1: War risk insurance (manual)
        cond("insw_insurance", "War risk insurance cancelled/elevated", "manual_toggle",
             weight=1, notes="Green=cancelled/suspended in Gulf, Amber=elevated premiums (>5x normal), Red=reinstated at normal rates"),
        # Weight 1: INSW trailing stop (manual — agent checks 10% from high)
        cond("insw_trail", "INSW within 10% trailing stop of highest close", "manual_toggle",
             weight=1, notes="Green=above, Amber=within 3% of trigger, Red=below = IMMEDIATE EXIT. Agent: check 30d high from price data."),
        # Weight 1: Entry status (manual)
        cond("insw_entry", "Entry status", "manual_toggle",
             weight=1, notes="Green=filled at target, Amber=filled but gapped up >10%, Red=not yet filled"),
        # Weight 1: Tanker sector sentiment FRO/DHT (manual)
        cond("insw_sector", "Tanker sector sentiment (FRO/DHT)", "manual_toggle",
             weight=1, notes="Green=up week-over-week, Amber=flat, Red=down week-over-week"),
    ],
    "notes": get_notes("0a70124996f3"),
    "created_at": "2026-03-04T10:00:00", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# NEM — Gold Miner / Newmont (5 shares @ ~$120)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "131755aa1899",
    "ticker": "NEM",
    "asset_type": "stock",
    "entry_price": 122.16,
    "entry_date": "2026-03-04",
    "quantity": 5,
    "direction": "long",
    "thesis": "Gold safe haven + operational leverage ($1,680 AISC vs $5,300+ spot = $3,600+ margin/oz). If real yields AND gold/oil ratio both go red simultaneously → trim regardless of spot.",
    "timeframe": "6-12 months",
    "invalidation_price": 100.0,
    "target_price": 157.0,
    "tags": ["gold", "mining", "safe-haven", "iran-conflict"],
    "status": "open",
    "conditions": [
        # Weight 3: Gold spot (auto)
        cond("nem_gold", "Gold spot price (GC=F)", "price_above",
             source="GC=F", threshold=4800.0, warning_threshold=5200.0, weight=3,
             notes="<$4800 (3 consecutive closes) = INVALIDATION"),
        # Weight 2: NEM vs $100 floor (auto)
        cond("nem_floor", "NEM above $100 floor", "price_above",
             source="NEM", threshold=100.0, warning_threshold=115.0, weight=2,
             notes="<$100 = INVALIDATION"),
        # Weight 1: Fed rate expectations (manual)
        cond("nem_fed", "Fed rate cut expectations intact", "manual_toggle",
             weight=1, notes="Green=cuts priced in (dovish), Amber=hold (neutral), Red=hikes priced in (hawkish)"),
        # Weight 1: NEM vs 50-day MA (auto)
        cond("nem_ma50", "NEM above 50-day MA", "ma_position",
             source="NEM", threshold=50, weight=1),
        # Weight 1: Central bank gold buying (manual)
        cond("nem_cbgold", "Central bank gold buying continues", "manual_toggle",
             weight=1, notes="Green=continued/accelerating, Amber=stable, Red=net selling reported"),
        # Weight 1: Real yields 10Y TIPS (manual)
        cond("nem_ryield", "Real yields (10Y TIPS) direction", "manual_toggle",
             weight=1, notes="Green=falling, Amber=flat, Red=rising >2.5%"),
        # Weight 1: Gold vs oil ratio (manual)
        cond("nem_goldoil", "Gold/oil ratio stable or rising", "manual_toggle",
             weight=1, notes="Green=rising (gold outpacing oil), Amber=neutral, Red=falling (oil outpacing gold → stagflation shift). If this + real yields both red → trim."),
    ],
    "notes": get_notes("131755aa1899"),
    "created_at": "2026-03-04T10:00:00", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# CIBR — Cybersecurity ETF (10 shares @ ~$62, PENDING)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "7adf5478dec9",
    "ticker": "CIBR",
    "asset_type": "ETF",
    "entry_price": 62.0,
    "entry_date": "2026-03-04",
    "quantity": 10,
    "direction": "long",
    "thesis": "Iran cyber retaliation + CISA understaffed (38% capacity) → non-discretionary enterprise spend. Most forgiving position — no hard invalidation. Only exit below $50 (broader tech meltdown, not thesis failure).",
    "timeframe": "6-12 months",
    "invalidation_price": 50.0,
    "target_price": 77.0,
    "tags": ["cybersecurity", "tech", "iran-conflict", "pending"],
    "status": "open",
    "conditions": [
        # Weight 2: Iran cyber activity (manual)
        cond("cibr_cyber", "Iran cyber activity level elevated", "manual_toggle",
             weight=2, notes="Green=elevated/active incidents, Amber=stable/background, Red=significantly reduced post-ceasefire"),
        # Weight 2: CIBR vs $55 floor (auto)
        cond("cibr_floor", "CIBR above $55 soft floor", "price_above",
             source="CIBR", threshold=55.0, warning_threshold=59.0, weight=2,
             notes="$55-59 = amber, <$55 = red"),
        # Weight 2: CIBR vs 50-day MA (auto)
        cond("cibr_ma50", "CIBR above 50-day MA", "ma_position",
             source="CIBR", threshold=50, weight=2),
        # Weight 1: CISA staffing (manual)
        cond("cibr_cisa", "CISA staffing still understaffed", "manual_toggle",
             weight=1, notes="Green=still understaffed (<50%), Amber=partial restoration, Red=fully restored"),
        # Weight 1: Sector earnings CRWD/PANW (manual)
        cond("cibr_earnings", "Sector earnings CRWD/PANW", "manual_toggle",
             weight=1, notes="Green=beat + raised guidance, Amber=in-line, Red=miss + lowered"),
        # Weight 1: Enterprise security spending (manual)
        cond("cibr_spending", "Enterprise security spending signals", "manual_toggle",
             weight=1, notes="Green=accelerating, Amber=stable, Red=decelerating"),
        # Weight 1: Entry status (manual)
        cond("cibr_entry", "Entry status", "manual_toggle",
             weight=1, notes="Green=filled at target, Amber=filled but gapped, Red=not yet filled"),
    ],
    "notes": get_notes("7adf5478dec9"),
    "created_at": "2026-03-04T10:00:00", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# PSQ — Inverse QQQ Hedge (33 shares @ $31.20)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "d3759efc6f95",
    "ticker": "PSQ",
    "asset_type": "ETF",
    "entry_price": 31.20,
    "entry_date": "2026-03-04",
    "quantity": 33,
    "direction": "long",
    "thesis": "Inverse QQQ hedge — portfolio protection against tech selloff. Auto-flag for exit review if health <5. Inverse ETFs decay — if VIX <18 and QQQ reclaims 50d MA, close it.",
    "timeframe": "1-4 weeks",
    "invalidation_price": 28.0,
    "target_price": 36.0,
    "tags": ["hedge", "inverse", "tech", "iran-conflict"],
    "status": "open",
    "conditions": [
        # Weight 3: VIX level (auto — higher VIX = hedge working)
        cond("psq_vix", "VIX elevated (>18)", "price_above",
             source="^VIX", threshold=18.0, warning_threshold=25.0, weight=3,
             notes="<18 = hedge unnecessary, EXIT. 18-25 amber. >25 green."),
        # Weight 2: QQQ below 50-day MA (auto — QQQ below = hedge working)
        # NOTE: For PSQ (inverse), QQQ BELOW its MA = passing (hedge working)
        # We use price_below on QQQ: passing when QQQ is low
        cond("psq_qqq_ma", "QQQ below 50-day MA", "ma_position",
             source="QQQ", threshold=50, weight=2,
             notes="Agent: INVERT this — QQQ below MA = GREEN for PSQ, above = RED. Set toggle accordingly."),
        # Weight 2: S&P 500 5-day trend (manual — agent checks from data)
        cond("psq_spx5d", "S&P 500 5-day trend declining", "manual_toggle",
             weight=2, notes="Green=declining, Amber=flat, Red=rising (hedge is a drag). Agent: check ^GSPC 5d change."),
        # Weight 1: Conflict escalation (manual)
        cond("psq_escalation", "Conflict escalation trend", "manual_toggle",
             weight=1, notes="Green=escalating, Amber=stalemate, Red=de-escalating"),
        # Weight 1: Oil 5-day trend (manual — agent checks from data)
        cond("psq_oil5d", "Oil price 5-day trend rising", "manual_toggle",
             weight=1, notes="Green=rising (inflationary pressure on tech), Amber=flat, Red=falling. Agent: check CL=F 5d change."),
        # Weight 1: Days held (manual — agent calculates)
        cond("psq_days", "Days held (<10 days)", "manual_toggle",
             weight=1, notes="Green=<10 days, Amber=10-20 days, Red=>20 days (decay risk). Agent: calculate from entry_date."),
    ],
    "notes": get_notes("d3759efc6f95"),
    "created_at": "2026-03-04T10:00:00", "updated_at": NOW,
})

# ═══════════════════════════════════════════════════════════════════════════
# CHTR — Charter Communications (1 share @ $232.80)
# ═══════════════════════════════════════════════════════════════════════════
positions.append({
    "id": "24c23b54f453",
    "ticker": "CHTR",
    "asset_type": "stock",
    "entry_price": 228.38,
    "entry_date": "2026-02-27",
    "quantity": 1,
    "direction": "long",
    "thesis": "Uncorrelated deep value hold. FCF inflection in 2026 as capex declines. Lowest-maintenance position — quarterly check, not 4-hourly. Only escalate if breaks $200 or capex goes wrong direction.",
    "timeframe": "12-24 months",
    "invalidation_price": 180.0,
    "target_price": 350.0,
    "tags": ["value", "telecom", "iran-conflict"],
    "status": "open",
    "conditions": [
        # Weight 3: CHTR vs $200 support (auto)
        cond("chtr_support", "CHTR above $200 support", "price_above",
             source="CHTR", threshold=200.0, warning_threshold=215.0, weight=3,
             notes="<$200 = structural break (red)"),
        # Weight 2: CHTR vs $180 52-week low zone (auto)
        cond("chtr_lowzone", "CHTR above $180 (52-week low zone)", "price_above",
             source="CHTR", threshold=180.0, warning_threshold=200.0, weight=2,
             notes="<$180 = INVALIDATION"),
        # Weight 2: FCF inflection thesis (manual — quarterly check)
        cond("chtr_fcf", "FCF thesis: capex declining per guidance", "manual_toggle",
             weight=2, notes="Green=capex declining, Amber=flat, Red=increasing (thesis broken). Quarterly check only."),
        # Weight 1: CHTR vs 50-day MA (auto)
        cond("chtr_ma50", "CHTR above 50-day MA", "ma_position",
             source="CHTR", threshold=50, weight=1),
        # Weight 1: Broadband subscriber trend (manual)
        cond("chtr_broadband", "Broadband subscriber trend", "manual_toggle",
             weight=1, notes="Green=losses moderating/gains, Amber=stable losses, Red=accelerating losses"),
        # Weight 1: Mobile segment growth (manual)
        cond("chtr_mobile", "Mobile segment growth", "manual_toggle",
             weight=1, notes="Green=>1.5M net adds annualized, Amber=1-1.5M, Red=<1M (growth stalling)"),
    ],
    "notes": get_notes("24c23b54f453"),
    "created_at": "2026-02-27T00:00:00", "updated_at": NOW,
})


# ── Write output ──
# Add computed fields
for p in positions:
    if "health_score" not in p:
        p["health_score"] = 10.0
    if "current_price" not in p:
        p["current_price"] = None
    if "unrealized_pnl" not in p:
        p["unrealized_pnl"] = None
    if "unrealized_pnl_pct" not in p:
        p["unrealized_pnl_pct"] = None
    if "closed_at" not in p:
        p["closed_at"] = None
    if "close_price" not in p:
        p["close_price"] = None
    if "close_reason" not in p:
        p["close_reason"] = None
    if "template_id" not in p:
        p["template_id"] = None

# Backup
import shutil
if POSITIONS_FILE.exists():
    backup = POSITIONS_FILE.with_suffix(".json.bak")
    shutil.copy2(POSITIONS_FILE, backup)
    print(f"Backup: {backup}")

with open(POSITIONS_FILE, "w") as f:
    json.dump(positions, f, indent=2, default=str)

print(f"Wrote {len(positions)} positions to {POSITIONS_FILE}")
for p in positions:
    n_conds = len(p["conditions"])
    total_weight = sum(c["weight"] for c in p["conditions"])
    print(f"  {p['ticker']:8s} {n_conds} conditions, total weight={total_weight}, tags={p['tags']}")
