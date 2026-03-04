#!/usr/bin/env python3
"""Iran Conflict Monitor — data collector for pi agent.

Gathers all datapoints the agent needs to assess manual toggles:
  - Prices & technicals for all positions + underlying drivers
  - Sector ETFs for week-over-week momentum
  - Portfolio-level concentration & kill-switch checks
  - Position health states from Monitor tab

Outputs JSON for the pi agent to interpret + update.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from monitor.models import PositionStore


# All instruments the agent needs to evaluate conditions
INSTRUMENTS = {
    # ── Position tickers ──
    "XOP":    "Oil E&P ETF (position)",
    "RTX":    "Raytheon / Defence (position)",
    "INSW":   "International Seaways / Tanker (position)",
    "NEM":    "Newmont / Gold Miner (position)",
    "CIBR":   "Cybersecurity ETF (position)",
    "PSQ":    "Inverse QQQ hedge (position)",
    "WDS.AX": "Woodside Energy / ASX (position)",
    "CHTR":   "Charter Communications (position)",
    # ── Underlying drivers ──
    "CL=F":   "WTI Crude Oil futures",
    "BZ=F":   "Brent Crude Oil futures",
    "GC=F":   "Gold futures",
    "^VIX":   "CBOE Volatility Index",
    "^GSPC":  "S&P 500 Index",
    "QQQ":    "Invesco QQQ (Nasdaq-100)",
    "AUDUSD=X": "AUD/USD exchange rate",
    # ── Sector ETFs for momentum checks ──
    "ITA":    "iShares US Aerospace & Defence ETF",
    "XAR":    "SPDR S&P Aerospace & Defence",
    "FRO":    "Frontline (tanker peer)",
    "DHT":    "DHT Holdings (tanker peer)",
    "HACK":   "ETFMG Prime Cyber Security ETF",
    "GDX":    "VanEck Gold Miners ETF",
    "USO":    "United States Oil Fund",
    "BNO":    "United States Brent Oil Fund",
}


def get_price_data() -> dict:
    """Fetch prices + technicals for all instruments."""
    results = {}
    try:
        import yfinance as yf
        for ticker, label in INSTRUMENTS.items():
            try:
                t = yf.Ticker(ticker)
                hist = t.history(period="60d")
                if hist.empty:
                    results[ticker] = {"label": label, "error": "no data"}
                    continue

                close = float(hist["Close"].iloc[-1])
                prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else close

                # 5-day and 20-day changes
                close_5d = float(hist["Close"].iloc[-5]) if len(hist) >= 5 else close
                close_20d = float(hist["Close"].iloc[-20]) if len(hist) >= 20 else close

                # Moving averages
                ma5 = float(hist["Close"].iloc[-5:].mean()) if len(hist) >= 5 else close
                ma20 = float(hist["Close"].iloc[-20:].mean()) if len(hist) >= 20 else close
                ma50 = float(hist["Close"].iloc[-50:].mean()) if len(hist) >= 50 else None

                # 30-day high/low
                high_30d = float(hist["Close"].iloc[-30:].max()) if len(hist) >= 5 else close
                low_30d = float(hist["Close"].iloc[-30:].min()) if len(hist) >= 5 else close

                # Distance from 30d high (for trailing stop checks)
                pct_from_high = (close - high_30d) / high_30d * 100 if high_30d > 0 else 0

                r = {
                    "label": label,
                    "price": round(close, 2),
                    "prev_close": round(prev_close, 2),
                    "change_1d_pct": round((close - prev_close) / prev_close * 100, 2),
                    "change_5d_pct": round((close - close_5d) / close_5d * 100, 2) if close_5d else 0,
                    "change_20d_pct": round((close - close_20d) / close_20d * 100, 2) if close_20d else 0,
                    "high_30d": round(high_30d, 2),
                    "low_30d": round(low_30d, 2),
                    "pct_from_30d_high": round(pct_from_high, 2),
                    "ma5": round(ma5, 2),
                    "ma20": round(ma20, 2),
                    "ma50": round(ma50, 2) if ma50 else None,
                    "above_ma50": close > ma50 if ma50 else None,
                    "above_ma20": close > ma20,
                }
                results[ticker] = r
            except Exception as e:
                results[ticker] = {"label": label, "error": str(e)}
    except Exception as e:
        results["_error"] = str(e)

    return results


def get_derived_metrics(prices: dict) -> dict:
    """Compute derived metrics the agent needs for condition checks."""
    metrics = {}

    # Gold/Oil ratio
    gold = prices.get("GC=F", {}).get("price")
    oil = prices.get("CL=F", {}).get("price")
    if gold and oil and oil > 0:
        metrics["gold_oil_ratio"] = round(gold / oil, 2)
        # 5d ago ratio for direction
        gold_5d = prices.get("GC=F", {}).get("price", 0) / (1 + prices.get("GC=F", {}).get("change_5d_pct", 0) / 100) if prices.get("GC=F", {}).get("change_5d_pct") else None
        oil_5d = prices.get("CL=F", {}).get("price", 0) / (1 + prices.get("CL=F", {}).get("change_5d_pct", 0) / 100) if prices.get("CL=F", {}).get("change_5d_pct") else None
        if gold_5d and oil_5d and oil_5d > 0:
            ratio_5d = gold_5d / oil_5d
            metrics["gold_oil_ratio_5d_ago"] = round(ratio_5d, 2)
            metrics["gold_oil_ratio_direction"] = "rising" if metrics["gold_oil_ratio"] > ratio_5d else "falling"

    # INSW trailing stop check (10% from 30d high)
    insw = prices.get("INSW", {})
    if insw.get("high_30d") and insw.get("price"):
        trail_trigger = insw["high_30d"] * 0.90
        metrics["insw_trail_trigger"] = round(trail_trigger, 2)
        metrics["insw_trail_pct_from_high"] = insw.get("pct_from_30d_high", 0)
        metrics["insw_trail_status"] = (
            "failing" if insw["price"] < trail_trigger else
            "warning" if insw["price"] < insw["high_30d"] * 0.93 else
            "passing"
        )

    # PSQ days held
    try:
        entry = datetime.strptime("2026-03-04", "%Y-%m-%d")
        days = (datetime.now() - entry).days
        metrics["psq_days_held"] = days
        metrics["psq_days_status"] = (
            "passing" if days < 10 else
            "warning" if days <= 20 else
            "failing"
        )
    except Exception:
        pass

    # QQQ vs 50d MA — inverted for PSQ (below = good)
    qqq = prices.get("QQQ", {})
    if qqq.get("above_ma50") is not None:
        metrics["qqq_below_ma50"] = not qqq["above_ma50"]
        metrics["psq_qqq_status"] = "passing" if not qqq["above_ma50"] else "failing"
        # Warning zone: within 2% of MA
        if qqq.get("ma50") and qqq.get("price"):
            pct = (qqq["price"] - qqq["ma50"]) / qqq["ma50"] * 100
            if -2 <= pct <= 2:
                metrics["psq_qqq_status"] = "warning"

    # S&P 5-day trend for PSQ
    spx = prices.get("^GSPC", {})
    if spx.get("change_5d_pct") is not None:
        c5 = spx["change_5d_pct"]
        metrics["spx_5d_change"] = c5
        metrics["psq_spx_status"] = (
            "passing" if c5 < -0.5 else
            "warning" if -0.5 <= c5 <= 0.5 else
            "failing"
        )

    # Oil 5-day trend for PSQ
    if oil:
        c5 = prices.get("CL=F", {}).get("change_5d_pct", 0)
        metrics["oil_5d_change"] = c5
        metrics["psq_oil_status"] = (
            "passing" if c5 > 1 else
            "warning" if -1 <= c5 <= 1 else
            "failing"
        )

    # Sector momentum (week-over-week)
    for etf, key in [("ITA", "defence"), ("FRO", "tanker"), ("DHT", "tanker2")]:
        d = prices.get(etf, {})
        if d.get("change_5d_pct") is not None:
            metrics[f"{key}_sector_5d"] = d["change_5d_pct"]

    # AUD/USD direction
    aud = prices.get("AUDUSD=X", {})
    if aud.get("change_5d_pct") is not None:
        c5 = aud["change_5d_pct"]
        metrics["audusd_5d_change"] = c5
        metrics["wds_audusd_status"] = (
            "passing" if c5 < -0.5 else  # weakening AUD = good for WDS
            "warning" if -0.5 <= c5 <= 0.5 else
            "failing"
        )

    return metrics


def get_rate_history() -> dict:
    """Load cross-cycle rate history for trend tracking."""
    history_file = PROJECT / "data" / "position_monitor" / "rate_history.json"
    try:
        with open(history_file) as f:
            return json.load(f)
    except Exception:
        return {"vlcc_spot": [], "wti": [], "brent": [], "gold": [], "vix": [],
                "last_escalation_event": None}


def update_rate_history(prices: dict) -> dict:
    """Append current cycle's rates to history and return updated history."""
    history = get_rate_history()
    history_file = PROJECT / "data" / "position_monitor" / "rate_history.json"
    now = datetime.now().isoformat(timespec="seconds")

    # Append current values (keep last 18 cycles = 72 hours at 4h intervals)
    MAX_HISTORY = 18

    for series, ticker in [("wti", "CL=F"), ("brent", "BZ=F"), ("gold", "GC=F"), ("vix", "^VIX")]:
        price = prices.get(ticker, {}).get("price")
        if price:
            history.setdefault(series, []).append({"t": now, "v": round(price, 2)})
            history[series] = history[series][-MAX_HISTORY:]

    # VLCC is manual — agent updates via iran_monitor_update.py rate command
    # But we compute a 3-cycle trend if we have data
    vlcc_entries = history.get("vlcc_spot", [])
    if len(vlcc_entries) >= 2:
        latest = vlcc_entries[-1]["v"]
        prev = vlcc_entries[-2]["v"]
        history["vlcc_3cycle_trend"] = (
            "rising" if latest > prev * 1.02 else
            "declining" if latest < prev * 0.98 else
            "stable"
        )
    if len(vlcc_entries) >= 3:
        latest = vlcc_entries[-1]["v"]
        three_ago = vlcc_entries[-3]["v"]
        pct = (latest - three_ago) / three_ago * 100 if three_ago else 0
        history["vlcc_3cycle_change_pct"] = round(pct, 1)

    # Hours since last escalation event
    esc = history.get("last_escalation_event")
    if esc and esc.get("timestamp"):
        try:
            esc_time = datetime.fromisoformat(esc["timestamp"])
            hours = (datetime.now() - esc_time).total_seconds() / 3600
            history["hours_since_escalation"] = round(hours, 1)
            history["escalation_gap_status"] = (
                "active" if hours < 6 else
                "recent" if hours < 24 else
                "cooling" if hours < 48 else
                "stale"
            )
        except Exception:
            pass

    # Save
    try:
        with open(history_file, "w") as f:
            json.dump(history, f, indent=2, default=str)
    except Exception:
        pass

    return history


def get_portfolio_level_checks(positions: list, prices: dict) -> dict:
    """Portfolio-wide risk checks."""
    checks = {}

    # Energy-adjacent tickers: oil, energy, AND tanker (INSW)
    ENERGY_ADJACENT_TAGS = {"energy", "oil", "tanker"}

    total_value = 0
    energy_value = 0
    energy_tickers = []
    for p in positions:
        price = prices.get(p["ticker"], {}).get("price", p.get("current_price") or p["entry_price"])
        pos_value = price * p["quantity"]
        total_value += pos_value
        if ENERGY_ADJACENT_TAGS & set(p.get("tags", [])):
            energy_value += pos_value
            energy_tickers.append(f"{p['ticker']} ${pos_value:.0f}")

    checks["total_portfolio_value"] = round(total_value, 2)
    checks["energy_exposure_value"] = round(energy_value, 2)
    checks["energy_exposure_pct"] = round(energy_value / total_value * 100, 2) if total_value > 0 else 0
    checks["energy_tickers"] = energy_tickers  # Shows what's counted

    # Concentration warning
    checks["energy_concentration_status"] = (
        "passing" if checks["energy_exposure_pct"] < 40 else
        "warning" if checks["energy_exposure_pct"] <= 55 else
        "failing"
    )

    # VIX level
    vix = prices.get("^VIX", {}).get("price")
    if vix:
        checks["vix_level"] = vix
        checks["vix_extreme_fear"] = vix > 35
        checks["vix_low_vol"] = vix < 18

    # Oil prices front and centre
    wti = prices.get("CL=F", {})
    brent = prices.get("BZ=F", {})
    checks["oil_summary"] = {
        "wti": wti.get("price"),
        "wti_1d_pct": wti.get("change_1d_pct"),
        "wti_5d_pct": wti.get("change_5d_pct"),
        "brent": brent.get("price"),
        "brent_1d_pct": brent.get("change_1d_pct"),
        "brent_5d_pct": brent.get("change_5d_pct"),
        "brent_wti_spread": round(brent.get("price", 0) - wti.get("price", 0), 2) if brent.get("price") and wti.get("price") else None,
    }

    # Kill switch triggers
    checks["kill_switch_triggers"] = []
    if checks.get("vix_extreme_fear"):
        checks["kill_switch_triggers"].append("VIX >35 — extreme fear — review cash deployment")
    if checks["energy_concentration_status"] == "failing":
        checks["kill_switch_triggers"].append(f"Energy concentration {checks['energy_exposure_pct']:.0f}% >55% — rebalance needed")
    if checks.get("vix_low_vol"):
        checks["kill_switch_triggers"].append("VIX <18 — exit PSQ, hedges unnecessary")

    # Count low-health positions
    low_health = [p["ticker"] for p in positions if p.get("health_score", 10) < 6]
    if len(low_health) >= 3:
        checks["kill_switch_triggers"].append(f"3+ positions at health <6: {', '.join(low_health)} — PORTFOLIO STRESS")

    # Kill switch PROXIMITY — how close to coordinated de-escalation response
    proximity_conditions = []
    # Check ceasefire (from position conditions)
    for p in positions:
        if p["ticker"] == "RTX":
            for c in p.get("conditions", []):
                if c.get("id") == "rtx_ceasefire":
                    if c["status"] == "warning":
                        proximity_conditions.append("ceasefire backchannel talks (RTX amber)")
                    elif c["status"] == "failing":
                        proximity_conditions.append("CEASEFIRE CONFIRMED (RTX red)")
    if checks.get("vix_low_vol"):
        proximity_conditions.append("VIX <18 (low vol)")
    if checks["energy_concentration_status"] == "failing":
        proximity_conditions.append("energy >55%")

    # Escalation gap
    rate_hist = get_rate_history()
    gap_hours = rate_hist.get("hours_since_escalation")
    if gap_hours and gap_hours > 48:
        proximity_conditions.append(f"no escalation in {gap_hours:.0f}h (cooling)")

    checks["kill_switch_proximity"] = {
        "conditions_met": len(proximity_conditions),
        "total_conditions": 4,  # ceasefire, VIX<18, concentration, escalation gap
        "details": proximity_conditions,
        "status": (
            "clear" if len(proximity_conditions) == 0 else
            "monitoring" if len(proximity_conditions) <= 1 else
            "elevated" if len(proximity_conditions) <= 2 else
            "imminent"
        ),
    }

    return checks


def get_position_states() -> list:
    """Load all iran-conflict positions with conditions."""
    store = PositionStore()
    positions = store.load_positions()

    iran_positions = []
    for p in positions:
        if "iran-conflict" not in p.tags:
            continue
        iran_positions.append({
            "id": p.id,
            "ticker": p.ticker,
            "entry_price": p.entry_price,
            "entry_date": p.entry_date,
            "current_price": p.current_price,
            "quantity": p.quantity,
            "direction": p.direction,
            "unrealized_pnl": p.unrealized_pnl,
            "unrealized_pnl_pct": p.unrealized_pnl_pct,
            "health_score": p.health_score,
            "thesis": p.thesis,
            "invalidation_price": p.invalidation_price,
            "target_price": p.target_price,
            "tags": p.tags,
            "notes": p.notes[-3:] if p.notes else [],
            "conditions": [
                {
                    "id": c.id,
                    "label": c.label,
                    "type": c.type,
                    "status": c.status,
                    "current_value": c.current_value,
                    "threshold": c.threshold,
                    "warning_threshold": c.warning_threshold,
                    "source": c.source,
                    "weight": c.weight,
                    "notes": c.notes,
                }
                for c in p.conditions
            ],
        })

    return iran_positions


def get_recent_alerts() -> list:
    """Get recent monitor alerts for iran-tagged tickers."""
    store = PositionStore()
    alerts = store.load_alerts(limit=50)
    iran_tickers = {p["ticker"] for p in get_position_states()}
    return [a for a in alerts if a.get("ticker") in iran_tickers][-10:]


def collect_all() -> dict:
    """Collect everything the agent needs."""
    prices = get_price_data()
    positions = get_position_states()
    rate_history = update_rate_history(prices)

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "prices": prices,
        "derived_metrics": get_derived_metrics(prices),
        "portfolio_checks": get_portfolio_level_checks(positions, prices),
        "rate_history": {
            "vlcc_spot": rate_history.get("vlcc_spot", [])[-6:],  # Last 6 cycles (24h)
            "vlcc_3cycle_trend": rate_history.get("vlcc_3cycle_trend"),
            "vlcc_3cycle_change_pct": rate_history.get("vlcc_3cycle_change_pct"),
            "wti_recent": rate_history.get("wti", [])[-6:],
            "brent_recent": rate_history.get("brent", [])[-6:],
            "last_escalation_event": rate_history.get("last_escalation_event"),
            "hours_since_escalation": rate_history.get("hours_since_escalation"),
            "escalation_gap_status": rate_history.get("escalation_gap_status"),
        },
        "positions": positions,
        "recent_alerts": get_recent_alerts(),
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    args = parser.parse_args()

    data = collect_all()

    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"=== Iran Monitor Data — {data['timestamp']} ===\n")

        print("PRICES:")
        for ticker, info in data["prices"].items():
            if "error" in info:
                print(f"  {ticker:12s} ERROR: {info['error']}")
            else:
                d1 = info["change_1d_pct"]
                d5 = info["change_5d_pct"]
                a1 = "▲" if d1 > 0 else "▼" if d1 < 0 else "─"
                a5 = "▲" if d5 > 0 else "▼" if d5 < 0 else "─"
                ma = "↑MA50" if info.get("above_ma50") else "↓MA50" if info.get("above_ma50") is False else ""
                print(f"  {ticker:12s} ${info['price']:>9.2f}  1d:{a1}{d1:+.1f}%  5d:{a5}{d5:+.1f}%  {ma}")

        print(f"\nDERIVED METRICS:")
        for k, v in data["derived_metrics"].items():
            print(f"  {k}: {v}")

        print(f"\nPORTFOLIO CHECKS:")
        for k, v in data["portfolio_checks"].items():
            print(f"  {k}: {v}")

        print(f"\nPOSITIONS ({len(data['positions'])} iran-tagged):")
        for p in data["positions"]:
            pnl = f"${p['unrealized_pnl']:+.2f} ({p['unrealized_pnl_pct']:+.1f}%)" if p["unrealized_pnl"] else "N/A"
            print(f"\n  {p['ticker']:8s} health={p['health_score']}/10  P&L={pnl}")
            for c in p["conditions"]:
                icon = {"passing": "✓", "warning": "⚠", "failing": "✗", "unknown": "?"}.get(c["status"], "?")
                val = f" = {c['current_value']}" if c["current_value"] is not None else ""
                print(f"    [{icon}] w={c['weight']} {c['label']}{val}")
