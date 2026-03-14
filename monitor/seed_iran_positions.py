#!/usr/bin/env python3
"""Seed/update position monitor with user-defined rules.

Defines thesis, conditions, and monitoring rules per user specification.
Live broker data is pulled via Alpaca where available; positions with no
live data are seeded with fallback values.

Run: python3 monitor/seed_iran_positions.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.models import Position, Condition, PositionStore

store = PositionStore()
existing = store.load_positions()
existing_by_ticker = {p.ticker: p for p in existing if p.status == "open"}

KILL_SWITCH_NOTE = (
    "GLOBAL KILL SWITCH: If confirmed ceasefire or Iran capitulation headlines: "
    "exit INSW in full, sell 50% XOP, trim RTX to 3 shares. "
    "Rotate freed cash into oversold sectors (JETS, XLY) for relief rally. "
    "NEM and CIBR hold through de-escalation."
)


def _get_broker_positions() -> dict:
    """Pull live positions from Alpaca broker. Returns {ticker: row_dict}."""
    try:
        from brokers.alpaca.broker import AlpacaBroker
        from utils.config import get_active_config
        config = get_active_config("sp500")
        broker = AlpacaBroker(config)
        if not broker.connect():
            print("  ⚠️ Alpaca connection failed")
            return {}
        result = {}
        for p in broker.get_positions():
            if p.shares > 0:
                result[p.ticker] = {
                    'qty': float(p.shares),
                    'cost_price': float(p.entry_price),
                    'current_price': float(p.current_price or 0),
                    'market_val': float(p.market_value or 0),
                    'unrealized_pnl': float(p.unrealized_pnl or 0),
                    'pnl_pct': float(p.unrealized_pnl_pct or 0),
                }
        broker.disconnect()
        return result
    except Exception as e:
        print(f"  ⚠️ Broker connection failed: {e}")
        return {}


def _upsert(pos: Position):
    """Add or replace a position by ticker."""
    pos.update_health()
    if pos.ticker in existing_by_ticker:
        old = existing_by_ticker[pos.ticker]
        pos.id = old.id
        pos.created_at = old.created_at
        store.update_position(pos)
        print(f"  ✏️  Updated {pos.ticker} (id={pos.id}, health={pos.health_score})")
    else:
        store.add_position(pos)
        print(f"  ✅ Added {pos.ticker} (id={pos.id}, health={pos.health_score})")


# ══════════════════════════════════════════════════════════════════════════════
# Pull live data from broker
# ══════════════════════════════════════════════════════════════════════════════
print("Pulling live positions from broker...")
broker_data = _get_broker_positions()
for ticker, d in broker_data.items():
    print(f"  {ticker:8s}  qty={d['qty']:>5.0f}  cost=${d['cost_price']:>8.2f}  "
          f"current=${d['current_price']:>8.2f}  pnl=${d['unrealized_pnl']:>+8.2f}")
print()

# ══════════════════════════════════════════════════════════════════════════════
# Define positions with live broker data + user monitoring rules
# ══════════════════════════════════════════════════════════════════════════════

def _m(ticker: str, fallback_qty=0, fallback_cost=0):
    """Get broker data for ticker, with fallbacks for positions not yet bought."""
    d = broker_data.get(ticker, {})
    return {
        'qty': d.get('qty', fallback_qty),
        'cost': d.get('cost_price', fallback_cost),
        'price': d.get('current_price', 0),
        'pnl': d.get('unrealized_pnl', 0),
    }


# ── RTX — Defence munitions ──────────────────────────────────────────────────
m = _m('RTX')
rtx = Position(
    ticker="RTX",
    asset_type="stock",
    entry_price=m['cost'],
    entry_date="2026-03-04",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "Defence munition replenishment + $68B backlog + structural budget tailwinds. "
        "Iran conflict catalyst. Trim 3 shares at Morgan Stanley target. "
        "Let remaining 3 ride as structural hold."
    ),
    timeframe="3-12 months",
    invalidation_price=195.0,
    target_price=235.0,
    tags=["defence", "geopolitical", "iran-conflict"],
    conditions=[
        Condition(
            label="RTX above pre-strike level ($195)",
            type="price_above",
            source="RTX",
            threshold=195.0,
            warning_threshold=198.0,
            weight=3,
            notes="Full exit only if ceasefire AND price breaks below $185",
        ),
        Condition(
            label="No ceasefire announced",
            type="manual_toggle",
            weight=3,
            status="passing",
            notes="Ceasefire + RTX < $195 = full exit. Defence budget intact regardless.",
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

# ── INSW — Tanker rates (PENDING ORDER) ─────────────────────────────────────
m = _m('INSW', fallback_qty=0, fallback_cost=0)
insw = Position(
    ticker="INSW",
    asset_type="stock",
    entry_price=m['cost'] if m['cost'] > 0 else 80.0,  # target entry
    entry_date="2026-03-04",
    quantity=m['qty'] if m['qty'] > 0 else 7,  # planned qty
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    direction="long",
    thesis=(
        "Hormuz closure → record VLCC rates ($423k/day), best balance sheet in tankers. "
        "Sell 4 at $95 (B. Riley target), sell 3 at $105 or on Hormuz reopening. "
        "MOST BINARY POSITION — treat as leveraged trade. "
        "⚠️ NOT YET PURCHASED — pending order."
    ),
    timeframe="1-6 months",
    invalidation_price=72.0,
    target_price=95.0,
    tags=["tanker", "energy", "geopolitical", "iran-conflict", "binary", "pending"],
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
        {"timestamp": "2026-03-04", "text": "⚠️ NOT YET PURCHASED — pending order."},
    ],
)

# ── NEM — Gold safe haven ────────────────────────────────────────────────────
m = _m('NEM')
nem = Position(
    ticker="NEM",
    asset_type="stock",
    entry_price=m['cost'],
    entry_date="2026-03-04",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "Gold safe haven + operational leverage ($1,680 AISC vs $5,300+ spot = "
        "$3,600+ margin/oz). Trim 2 at gold $5,500, 2 more above $6,000. "
        "Keep 1 as structural gold hedge."
    ),
    timeframe="3-12 months",
    invalidation_price=100.0,
    target_price=157.0,
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
            label="NEM above $100",
            type="price_above",
            source="NEM",
            threshold=100.0,
            warning_threshold=105.0,
            weight=2,
            notes="Below $100 = thesis broken, exit",
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

# ── CIBR — Cybersecurity (PENDING ORDER) ────────────────────────────────────
m = _m('CIBR', fallback_qty=0, fallback_cost=0)
cibr = Position(
    ticker="CIBR",
    asset_type="ETF",
    entry_price=m['cost'] if m['cost'] > 0 else 62.0,  # target entry
    entry_date="2026-03-04",
    quantity=m['qty'] if m['qty'] > 0 else 10,  # planned qty
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    direction="long",
    thesis=(
        "Iran cyber retaliation + CISA understaffed (38% capacity) → "
        "non-discretionary enterprise security spend. "
        "Trim 5 at $77 (analyst consensus). Hold 5 medium-term. "
        "Lowest binary risk. ⚠️ NOT YET PURCHASED — pending order."
    ),
    timeframe="6-18 months",
    invalidation_price=55.0,
    target_price=77.0,
    tags=["cybersecurity", "tech", "iran-conflict", "pending"],
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
    notes=[
        {"timestamp": "2026-03-04", "text": KILL_SWITCH_NOTE},
        {"timestamp": "2026-03-04", "text": "⚠️ NOT YET PURCHASED — pending order."},
    ],
)

# ── XOP — Existing energy position ──────────────────────────────────────────
m = _m('XOP')
xop = Position(
    ticker="XOP",
    asset_type="ETF",
    entry_price=m['cost'],
    entry_date="2026-02-26",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "Oil E&P geopolitical upside. Invalidation at WTI $65 (geopolitical premium unwound). "
        "Breakeven stop at cost basis on remaining shares. "
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
            label=f"XOP above breakeven (${m['cost']:.2f})",
            type="price_above",
            source="XOP",
            threshold=m['cost'],
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

# ── CHTR — Deep value hold ──────────────────────────────────────────────────
m = _m('CHTR')
chtr = Position(
    ticker="CHTR",
    asset_type="stock",
    entry_price=m['cost'],
    entry_date="2026-02-27",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "Uncorrelated deep value hold. FCF inflection in 2026 as capex declines. "
        "No action required — structural position."
    ),
    timeframe="12+ months",
    invalidation_price=190.0,
    target_price=350.0,
    tags=["value", "telecom"],
    conditions=[
        Condition(
            label="CHTR above $200 support",
            type="price_above",
            source="CHTR",
            threshold=200.0,
            warning_threshold=210.0,
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

# ── PSQ — Inverse QQQ hedge ─────────────────────────────────────────────────
m = _m('PSQ')
psq = Position(
    ticker="PSQ",
    asset_type="ETF",
    entry_price=m['cost'],
    entry_date="2026-03-04",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "Inverse QQQ hedge — portfolio protection against tech selloff / "
        "broader market drawdown during Iran conflict escalation."
    ),
    timeframe="1-6 months",
    invalidation_price=28.0,
    target_price=36.0,
    tags=["hedge", "inverse", "tech"],
    conditions=[
        Condition(
            label="Conflict/uncertainty thesis still active",
            type="manual_toggle",
            weight=3,
            status="passing",
            notes="Close on ceasefire or confirmed de-escalation",
        ),
        Condition(
            label="QQQ below recent highs (hedge still relevant)",
            type="manual_toggle",
            weight=2,
            status="passing",
        ),
    ],
)

# ── WDS.AX — Woodside Energy (ASX) ──────────────────────────────────────────
m = _m('WDS.AX')
wds = Position(
    ticker="WDS.AX",
    asset_type="stock",
    entry_price=m['cost'],
    entry_date="2026-03-01",
    quantity=m['qty'],
    current_price=m['price'],
    unrealized_pnl=m['pnl'],
    unrealized_pnl_pct=round(m['pnl'] / (m['qty'] * m['cost']) * 100, 2) if m['qty'] * m['cost'] > 0 else 0,
    direction="long",
    thesis=(
        "ASX energy major — LNG/oil exposure with geopolitical upside. "
        "Strong dividends. Held as energy diversification alongside US E&P."
    ),
    timeframe="6-12 months",
    invalidation_price=22.0,
    target_price=38.0,
    tags=["energy", "oil", "asx", "dividend"],
    conditions=[
        Condition(
            label="WDS above A$24 support",
            type="price_above",
            source="WDS.AX",
            threshold=24.0,
            warning_threshold=25.0,
            weight=2,
        ),
        Condition(
            label="Oil thesis intact (WTI > $60)",
            type="price_above",
            source="CL=F",
            threshold=60.0,
            weight=2,
        ),
        Condition(
            label="WDS above 50-day MA",
            type="ma_position",
            source="WDS.AX",
            threshold=50,
            weight=1,
        ),
    ],
)


# ══════════════════════════════════════════════════════════════════════════════
# Execute
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("Seeding positions from broker live data + monitoring rules...\n")
    for pos in [rtx, insw, nem, cibr, xop, chtr, psq, wds]:
        _upsert(pos)

    # Summary
    print()
    all_pos = store.get_open_positions()
    total_value = sum(p.quantity * p.entry_price for p in all_pos if p.entry_price > 0)
    total_pnl = sum(p.unrealized_pnl or 0 for p in all_pos)
    print(f"Total open positions: {len(all_pos)}")
    print(f"Total entry value: ${total_value:,.2f}")
    print(f"Total unrealized P&L: ${total_pnl:+,.2f}")
    print()
    for p in all_pos:
        val = p.quantity * p.entry_price
        pnl = p.unrealized_pnl or 0
        pending = " ⚠️ PENDING" if "pending" in p.tags else ""
        print(f"  {p.ticker:8s}  {p.quantity:>5.0f} × ${p.entry_price:>8.2f} = ${val:>9.2f}  "
              f"pnl=${pnl:>+8.2f}  health={p.health_score:4.1f}{pending}")
    print(f"\n⚠️  GLOBAL KILL SWITCH documented in all iran-conflict position notes.")
    print("Done.")
