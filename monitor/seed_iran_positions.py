#!/usr/bin/env python3
"""Seed the position monitor with Iran conflict tactical allocation positions.

Entered ~Mar 4 2026. Positions: RTX, INSW, NEM, CIBR + update existing XOP/CHTR.
Global kill switch rules documented in notes.

Run once: python3 monitor/seed_iran_positions.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.models import Position, Condition, PositionStore

store = PositionStore()
existing = store.load_positions()
existing_tickers = {p.ticker: p for p in existing if p.status == "open"}

KILL_SWITCH_NOTE = (
    "GLOBAL KILL SWITCH: If confirmed ceasefire or Iran capitulation headlines: "
    "exit INSW in full, sell 50% XOP, trim RTX to 3 shares. "
    "Rotate freed cash into oversold sectors (JETS, XLY) for relief rally. "
    "NEM and CIBR hold through de-escalation."
)


def _upsert(pos: Position):
    """Add or replace a position by ticker."""
    pos.update_health()
    if pos.ticker in existing_tickers:
        old = existing_tickers[pos.ticker]
        pos.id = old.id  # keep same id
        pos.created_at = old.created_at
        store.update_position(pos)
        print(f"  ✏️  Updated {pos.ticker} (id={pos.id}, health={pos.health_score})")
    else:
        store.add_position(pos)
        print(f"  ✅ Added {pos.ticker} (id={pos.id}, health={pos.health_score})")


# ══════════════════════════════════════════════════════════════════════════════
# RTX — Defence munitions replenishment
# ══════════════════════════════════════════════════════════════════════════════
rtx = Position(
    ticker="RTX",
    asset_type="stock",
    entry_price=135.0,
    entry_date="2026-03-04",
    quantity=6,
    direction="long",
    thesis=(
        "Defence munition replenishment + $68B backlog + structural budget tailwinds. "
        "Iran conflict catalyst. Trim 3 shares at $135 (MS target). "
        "Let remaining 3 ride as structural hold."
    ),
    timeframe="3-12 months",
    invalidation_price=118.5,
    target_price=135.0,
    tags=["defence", "geopolitical", "iran-conflict"],
    conditions=[
        Condition(
            label="RTX above pre-strike level ($119.50)",
            type="price_above",
            source="RTX",
            threshold=119.5,
            warning_threshold=121.0,
            weight=3,
            notes="Full exit only if price breaks below $118.50",
        ),
        Condition(
            label="No ceasefire announced",
            type="manual_toggle",
            weight=3,
            status="passing",
            notes="Ceasefire + RTX < $119.50 = full exit. Defence budget intact regardless.",
        ),
        Condition(
            label="RTX above 50-day MA",
            type="ma_position",
            source="RTX",
            threshold=50,
            weight=2,
        ),
        Condition(
            label="Defence budget narrative intact",
            type="manual_toggle",
            weight=2,
            status="passing",
            notes="Watch Pentagon supplemental appropriations, munition contract announcements",
        ),
    ],
    notes=[{"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE}],
)

# ══════════════════════════════════════════════════════════════════════════════
# INSW — Tanker rates (Hormuz closure)
# ══════════════════════════════════════════════════════════════════════════════
insw = Position(
    ticker="INSW",
    asset_type="stock",
    entry_price=80.0,
    entry_date="2026-03-04",
    quantity=7,
    direction="long",
    thesis=(
        "Hormuz closure → record VLCC rates ($423k/day), best balance sheet in tankers. "
        "Sell 4 at $95 (B. Riley target), sell 3 at $105 or on Hormuz reopening. "
        "MOST BINARY POSITION — treat as leveraged trade."
    ),
    timeframe="1-6 months",
    invalidation_price=72.0,
    target_price=95.0,
    tags=["tanker", "energy", "geopolitical", "iran-conflict", "binary"],
    conditions=[
        Condition(
            label="Strait of Hormuz still closed/restricted",
            type="manual_toggle",
            weight=3,
            status="passing",
            notes=(
                "ANY confirmed reopening or escorted transit resumption = "
                "IMMEDIATE FULL EXIT at market. No exceptions. "
                "Watch: Argus Media daily transit counts, war risk insurance, "
                "backchannel diplomacy via Oman/Qatar."
            ),
        ),
        Condition(
            label="INSW 10% trailing stop from highest close",
            type="manual_toggle",
            weight=3,
            status="passing",
            notes="Track manually — most binary position, tight stop required",
        ),
        Condition(
            label="VLCC spot rates above $200k/day",
            type="manual_toggle",
            weight=2,
            status="passing",
            notes="Currently ~$423k/day. Below $200k signals rate normalisation.",
        ),
        Condition(
            label="INSW above 50-day MA",
            type="ma_position",
            source="INSW",
            threshold=50,
            weight=1,
        ),
    ],
    notes=[
        {"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE},
        {"timestamp": "2026-03-04", "text": "Hormuz reopening = immediate full exit. No negotiation."},
    ],
)

# ══════════════════════════════════════════════════════════════════════════════
# NEM — Gold safe haven
# ══════════════════════════════════════════════════════════════════════════════
nem = Position(
    ticker="NEM",
    asset_type="stock",
    entry_price=52.0,
    entry_date="2026-03-04",
    quantity=5,
    direction="long",
    thesis=(
        "Gold safe haven + operational leverage ($1,680 AISC vs $5,300+ spot = "
        "$3,600+ margin/oz). Trim 2 at gold $5,500, 2 more above $6,000. "
        "Keep 1 as structural gold hedge. Bernstein $157 target."
    ),
    timeframe="3-12 months",
    invalidation_price=50.0,
    target_price=80.0,
    tags=["gold", "mining", "safe-haven", "iran-conflict"],
    conditions=[
        Condition(
            label="Gold (GC=F) above $4,800",
            type="price_above",
            source="GC=F",
            threshold=4800,
            warning_threshold=4900,
            weight=3,
            notes="3+ consecutive closes below $4,800 = safe haven thesis unwinding",
        ),
        Condition(
            label="NEM above $50",
            type="price_above",
            source="NEM",
            threshold=50.0,
            warning_threshold=51.0,
            weight=2,
            notes="Below $50 = exit, thesis broken",
        ),
        Condition(
            label="NEM above 50-day MA",
            type="ma_position",
            source="NEM",
            threshold=50,
            weight=1,
        ),
        Condition(
            label="Fed rate cut expectations intact",
            type="manual_toggle",
            weight=2,
            status="passing",
            notes="Gold headwind if rate cuts priced out. Watch FOMC dots.",
        ),
        Condition(
            label="Central bank gold buying continues",
            type="manual_toggle",
            weight=1,
            status="passing",
            notes="Watch WGC quarterly reports, PBOC/RBI reserve data",
        ),
    ],
    notes=[{"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE}],
)

# ══════════════════════════════════════════════════════════════════════════════
# CIBR — Cybersecurity (Iran cyber retaliation)
# ══════════════════════════════════════════════════════════════════════════════
cibr = Position(
    ticker="CIBR",
    asset_type="ETF",
    entry_price=62.0,
    entry_date="2026-03-04",
    quantity=10,
    direction="long",
    thesis=(
        "Iran cyber retaliation + CISA understaffed (38% capacity) → "
        "non-discretionary enterprise security spend. "
        "Trim 5 at $77 (analyst consensus). Hold 5 medium-term. "
        "Lowest binary risk position — only exit to raise cash."
    ),
    timeframe="6-18 months",
    invalidation_price=55.0,
    target_price=77.0,
    tags=["cybersecurity", "tech", "iran-conflict"],
    conditions=[
        Condition(
            label="CIBR above $55 soft floor",
            type="price_above",
            source="CIBR",
            threshold=55.0,
            warning_threshold=57.0,
            weight=2,
            notes="Below $55 = broader tech selloff, not thesis-specific",
        ),
        Condition(
            label="CIBR above 50-day MA",
            type="ma_position",
            source="CIBR",
            threshold=50,
            weight=1,
        ),
        Condition(
            label="Iran cyber threat elevated",
            type="manual_toggle",
            weight=2,
            status="passing",
            notes="Watch CISA advisories, Unit 42, CrowdStrike threat reports",
        ),
        Condition(
            label="CRWD/PANW sector sentiment positive",
            type="manual_toggle",
            weight=1,
            status="passing",
            notes="Sector proxy earnings as sentiment gauge",
        ),
    ],
    notes=[{"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE}],
)

# ══════════════════════════════════════════════════════════════════════════════
# XOP — Update existing position (5.4 shares, updated rules)
# ══════════════════════════════════════════════════════════════════════════════
xop = Position(
    ticker="XOP",
    asset_type="ETF",
    entry_price=142.48,
    entry_date="2026-02-26",
    quantity=5.4,
    direction="long",
    thesis=(
        "Oil E&P geopolitical upside. Invalidation at WTI $65 (geopolitical premium unwound). "
        "Breakeven stop at $142.48 on remaining shares. "
        "Re-entry zone $130s if pullback on de-escalation but holds."
    ),
    timeframe="6-12 months",
    invalidation_price=130.0,
    target_price=180.0,
    tags=["energy", "oil", "geopolitical", "iran-conflict"],
    conditions=[
        Condition(
            label="WTI above $65 (geopolitical premium intact)",
            type="price_above",
            source="CL=F",
            threshold=65.0,
            warning_threshold=68.0,
            weight=3,
            notes="Below $65 = geopolitical premium fully unwound",
        ),
        Condition(
            label="XOP above breakeven ($142.48)",
            type="price_above",
            source="XOP",
            threshold=142.48,
            weight=2,
            notes="Breakeven stop on remaining shares",
        ),
        Condition(
            label="XOP above 50-day MA",
            type="ma_position",
            source="XOP",
            threshold=50,
            weight=2,
        ),
        Condition(
            label="WTI curve in backwardation",
            type="manual_toggle",
            weight=1,
            status="passing",
        ),
        Condition(
            label="Geopolitical risk premium active",
            type="manual_toggle",
            weight=1,
            status="passing",
        ),
    ],
    notes=[
        {"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE},
        {"timestamp": "2026-03-04", "text": "Kill switch: sell 50% XOP on ceasefire."},
    ],
)

# ══════════════════════════════════════════════════════════════════════════════
# CHTR — Existing deep value hold (no changes)
# ══════════════════════════════════════════════════════════════════════════════
chtr = Position(
    ticker="CHTR",
    asset_type="stock",
    entry_price=332.80,
    entry_date="2026-02-27",
    quantity=1,
    direction="long",
    thesis=(
        "Uncorrelated deep value hold. FCF inflection in 2026 as capex declines. "
        "No action required — structural position."
    ),
    timeframe="12+ months",
    invalidation_price=280.0,
    target_price=450.0,
    tags=["value", "telecom"],
    conditions=[
        Condition(
            label="CHTR above $300 support",
            type="price_above",
            source="CHTR",
            threshold=300.0,
            warning_threshold=310.0,
            weight=2,
        ),
        Condition(
            label="CHTR above 200-day MA",
            type="ma_position",
            source="CHTR",
            threshold=200,
            weight=1,
        ),
        Condition(
            label="FCF inflection thesis intact",
            type="manual_toggle",
            weight=2,
            status="passing",
            notes="Capex declining through 2026, watch quarterly FCF guidance",
        ),
    ],
)


# ══════════════════════════════════════════════════════════════════════════════
# Execute
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("Seeding Iran conflict tactical allocation positions...\n")
    for pos in [rtx, insw, nem, cibr, xop, chtr]:
        _upsert(pos)

    # Summary
    print()
    all_pos = store.get_open_positions()
    total_value = sum(p.quantity * p.entry_price for p in all_pos)
    print(f"Total open positions: {len(all_pos)}")
    print(f"Total entry value: ${total_value:,.2f}")
    for p in all_pos:
        val = p.quantity * p.entry_price
        print(f"  {p.ticker:5s}  {p.quantity:5.1f} × ${p.entry_price:>7.2f} = ${val:>8.2f}  "
              f"health={p.health_score:4.1f}  tags={p.tags}")
    print(f"\n⚠️  GLOBAL KILL SWITCH documented in all position notes.")
    print("Done.")
