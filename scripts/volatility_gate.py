"""Pre-market Volatility Gate for Atlas Trading System.

Checks overnight macro market moves before allowing order submission.
When triggered, reduces or blocks new entries while always allowing
protective orders (stops, take-profits) to proceed.

Indicators checked:
    - Oil (CL=F):        overnight gap > 5%   → flag
    - Gold (GC=F):       overnight gap > 2%   → flag
    - VIX (^VIX):        level > 25 OR daily spike > 20% → flag
    - ASX futures (^AXJO): pre-market gap > 1.5% → flag

Behavior:
    - 0 flags:  no action (size_multiplier = 1.0)
    - 1 flag:   reduce position sizes by 50% (size_multiplier = 0.5), log warning
    - 2+ flags: block ALL new entries (size_multiplier = 0.0), send Telegram alert

Protective orders (stops, take-profits) are ALWAYS allowed.

Gate can be disabled per-market via config:
    "volatility_gate": {"enabled": false}

Usage:
    from scripts.volatility_gate import check_volatility_gate

    result = check_volatility_gate(config)
    # result["action"] in ("none", "reduce", "block")
    # result["size_multiplier"] in (1.0, 0.5, 0.0)

CLI:
    python3 scripts/volatility_gate.py --check [--market sp500|asx]
    python3 scripts/volatility_gate.py --check --config config/active/sp500.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("atlas.volatility_gate")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Default thresholds ─────────────────────────────────────────────────────

DEFAULT_THRESHOLDS = {
    "oil_gap_pct": 5.0,            # CL=F overnight gap > 5%
    "gold_gap_pct": 2.0,           # GC=F overnight gap > 2%
    "vix_level": 25.0,             # ^VIX absolute level > 25
    "vix_spike_pct": 20.0,         # ^VIX daily spike (open/prev_close) > 20%
    "asx_futures_gap_pct": 1.5,    # ^AXJO pre-market gap > 1.5%
}


# ── Core gate logic ────────────────────────────────────────────────────────

def _fetch_overnight_data(ticker: str, lookback_days: int = 5) -> Optional[dict]:
    """Fetch the last N days of OHLCV data for a ticker via yfinance.

    Returns a dict with keys: prev_close, open, high, low, close, volume
    for the most recent session, or None if data unavailable.
    """
    try:
        import yfinance as yf
        end = datetime.utcnow()
        start = end - timedelta(days=lookback_days)
        df = yf.download(
            ticker,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if df is None or df.empty or len(df) < 2:
            logger.warning("Insufficient data for %s (rows=%d)", ticker, len(df) if df is not None else 0)
            return None

        # Handle MultiIndex columns from yfinance
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.droplevel(1)

        prev_row = df.iloc[-2]
        curr_row = df.iloc[-1]

        return {
            "ticker": ticker,
            "prev_date": str(df.index[-2].date()),
            "curr_date": str(df.index[-1].date()),
            "prev_close": float(prev_row["Close"]),
            "open": float(curr_row["Open"]),
            "high": float(curr_row["High"]),
            "low": float(curr_row["Low"]),
            "close": float(curr_row["Close"]),
            "volume": float(curr_row["Volume"]) if "Volume" in curr_row else 0,
        }
    except Exception as e:
        logger.error("Failed to fetch data for %s: %s", ticker, e)
        return None


def _check_gap_indicator(
    name: str,
    ticker: str,
    threshold_pct: float,
    data: Optional[dict],
) -> dict:
    """Check if an overnight gap exceeds the threshold.

    Gap = abs(open - prev_close) / prev_close * 100

    Returns indicator result dict.
    """
    result = {
        "name": name,
        "ticker": ticker,
        "threshold_pct": threshold_pct,
        "flagged": False,
        "gap_pct": None,
        "prev_close": None,
        "open": None,
        "error": None,
    }

    if data is None:
        result["error"] = f"No data available for {ticker}"
        logger.warning("Volatility gate: %s — no data, skipping", name)
        return result

    prev_close = data["prev_close"]
    open_price = data["open"]

    if prev_close <= 0:
        result["error"] = f"Invalid prev_close ({prev_close}) for {ticker}"
        return result

    gap_pct = abs(open_price - prev_close) / prev_close * 100.0
    result["gap_pct"] = round(gap_pct, 3)
    result["prev_close"] = prev_close
    result["open"] = open_price
    result["flagged"] = gap_pct > threshold_pct

    if result["flagged"]:
        logger.warning(
            "Volatility gate FLAG: %s (%s) gap=%.2f%% > threshold=%.2f%%",
            name, ticker, gap_pct, threshold_pct,
        )

    return result


def _check_vix_indicator(
    vix_level_threshold: float,
    vix_spike_threshold_pct: float,
    data: Optional[dict],
) -> dict:
    """Check VIX level AND daily spike.

    Flags if:
        - Current VIX level > vix_level_threshold, OR
        - Daily spike (open/prev_close - 1) * 100 > vix_spike_threshold_pct

    Returns indicator result dict.
    """
    result = {
        "name": "vix",
        "ticker": "^VIX",
        "vix_level_threshold": vix_level_threshold,
        "vix_spike_threshold_pct": vix_spike_threshold_pct,
        "flagged": False,
        "vix_level": None,
        "spike_pct": None,
        "prev_close": None,
        "flag_reason": None,
        "error": None,
    }

    if data is None:
        result["error"] = "No VIX data available"
        logger.warning("Volatility gate: VIX — no data, skipping")
        return result

    vix_level = data["close"]
    prev_close = data["prev_close"]
    open_price = data["open"]

    result["vix_level"] = round(vix_level, 2)
    result["prev_close"] = prev_close

    reasons = []

    # Check absolute level
    if vix_level > vix_level_threshold:
        reasons.append(f"level={vix_level:.1f} > {vix_level_threshold}")

    # Check daily spike
    if prev_close > 0:
        spike_pct = (open_price - prev_close) / prev_close * 100.0
        result["spike_pct"] = round(spike_pct, 3)
        if spike_pct > vix_spike_threshold_pct:
            reasons.append(f"spike={spike_pct:.1f}% > {vix_spike_threshold_pct}%")

    if reasons:
        result["flagged"] = True
        result["flag_reason"] = "; ".join(reasons)
        logger.warning("Volatility gate FLAG: VIX — %s", result["flag_reason"])

    return result


def check_volatility_gate(config: dict) -> dict:
    """Run the pre-market volatility gate check.

    Fetches current market data and evaluates all macro indicators.
    Returns a structured result that callers can use to gate entries.

    Args:
        config: Market config dict (from config/active/*.json).
                Reads config["volatility_gate"] for settings/thresholds.

    Returns:
        {
            "gate_enabled": bool,
            "triggered_count": int,        # Number of flagged indicators
            "flags": list[str],            # Names of triggered indicators
            "action": str,                 # "none" | "reduce" | "block"
            "size_multiplier": float,      # 1.0 | 0.5 | 0.0
            "details": dict,               # Per-indicator results
            "message": str,                # Human-readable summary
            "checked_at": str,             # ISO timestamp
        }
    """
    checked_at = datetime.utcnow().isoformat() + "Z"

    gate_cfg = config.get("volatility_gate", {})
    gate_enabled = gate_cfg.get("enabled", True)

    base_result = {
        "gate_enabled": gate_enabled,
        "triggered_count": 0,
        "flags": [],
        "action": "none",
        "size_multiplier": 1.0,
        "details": {},
        "message": "Volatility gate OK — no flags triggered",
        "checked_at": checked_at,
    }

    if not gate_enabled:
        base_result["message"] = "Volatility gate disabled via config"
        logger.info("Volatility gate disabled — skipping checks")
        return base_result

    # Load thresholds (config overrides defaults)
    thresholds = dict(DEFAULT_THRESHOLDS)
    thresholds.update({
        k: v for k, v in gate_cfg.get("thresholds", {}).items()
        if k in DEFAULT_THRESHOLDS
    })

    # Determine which indicators to check
    indicators_cfg = gate_cfg.get("indicators", {})
    check_oil = indicators_cfg.get("oil", True)
    check_gold = indicators_cfg.get("gold", True)
    check_vix = indicators_cfg.get("vix", True)
    check_asx = indicators_cfg.get("asx_futures", True)

    flags = []
    details = {}

    # ── Oil (CL=F) ─────────────────────────────────────────────────────────
    if check_oil:
        oil_data = _fetch_overnight_data("CL=F")
        oil_result = _check_gap_indicator(
            "oil", "CL=F", thresholds["oil_gap_pct"], oil_data,
        )
        details["oil"] = oil_result
        if oil_result["flagged"]:
            flags.append("oil")

    # ── Gold (GC=F) ────────────────────────────────────────────────────────
    if check_gold:
        gold_data = _fetch_overnight_data("GC=F")
        gold_result = _check_gap_indicator(
            "gold", "GC=F", thresholds["gold_gap_pct"], gold_data,
        )
        details["gold"] = gold_result
        if gold_result["flagged"]:
            flags.append("gold")

    # ── VIX (^VIX) ────────────────────────────────────────────────────────
    if check_vix:
        vix_data = _fetch_overnight_data("^VIX")
        vix_result = _check_vix_indicator(
            thresholds["vix_level"],
            thresholds["vix_spike_pct"],
            vix_data,
        )
        details["vix"] = vix_result
        if vix_result["flagged"]:
            flags.append("vix")

    # ── ASX futures (^AXJO) ────────────────────────────────────────────────
    if check_asx:
        asx_data = _fetch_overnight_data("^AXJO")
        asx_result = _check_gap_indicator(
            "asx_futures", "^AXJO", thresholds["asx_futures_gap_pct"], asx_data,
        )
        details["asx_futures"] = asx_result
        if asx_result["flagged"]:
            flags.append("asx_futures")

    triggered_count = len(flags)

    # Determine action
    if triggered_count == 0:
        action = "none"
        size_multiplier = 1.0
        message = "Volatility gate OK — no flags triggered"
    elif triggered_count == 1:
        action = "reduce"
        size_multiplier = 0.5
        message = f"Volatility gate WARNING — 1 flag ({flags[0]}): reducing position sizes by 50%"
        logger.warning("Volatility gate REDUCE: %s", message)
    else:
        action = "block"
        size_multiplier = 0.0
        message = (
            f"⚠️ Volatility gate TRIGGERED — {triggered_count} flags "
            f"({', '.join(flags)}): ALL new entries paused"
        )
        logger.warning("Volatility gate BLOCK: %s", message)

    return {
        "gate_enabled": gate_enabled,
        "triggered_count": triggered_count,
        "flags": flags,
        "action": action,
        "size_multiplier": size_multiplier,
        "details": details,
        "message": message,
        "checked_at": checked_at,
    }


def send_volatility_alert(gate_result: dict) -> bool:
    """Send a Telegram alert when the volatility gate blocks entries.

    Only called when action == "block". Returns True if sent.
    """
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from utils.telegram import send_message

        flags_str = ", ".join(gate_result.get("flags", []))
        details = gate_result.get("details", {})

        lines = [
            "⚠️ <b>Volatility Gate Triggered — Entries Paused</b>",
            "",
            f"🚨 <b>Flags triggered:</b> {flags_str}",
            f"⏰ <b>Checked at:</b> {gate_result.get('checked_at', 'N/A')}",
            "",
            "<b>Indicator details:</b>",
        ]

        for name, det in details.items():
            if det.get("flagged"):
                if name == "vix":
                    lines.append(
                        f"  • VIX: level={det.get('vix_level', 'N/A')} "
                        f"| {det.get('flag_reason', '')}"
                    )
                else:
                    lines.append(
                        f"  • {name.upper()} ({det.get('ticker', '')}): "
                        f"gap={det.get('gap_pct', 'N/A'):.2f}% "
                        f"> threshold={det.get('threshold_pct', 'N/A'):.1f}%"
                    )

        lines += [
            "",
            "✅ Protective stops and exits will proceed normally.",
            "📋 Review macro conditions before manually re-enabling entries.",
        ]

        text = "\n".join(lines)
        return send_message(text)
    except Exception as e:
        logger.error("Failed to send volatility alert: %s", e)
        return False


# ── CLI entry point ────────────────────────────────────────────────────────

def _load_config(config_path: Optional[str] = None, market: str = "sp500") -> dict:
    """Load config from file or discover from market name."""
    if config_path:
        path = Path(config_path)
    else:
        path = PROJECT_ROOT / "config" / "active" / f"{market}.json"

    if not path.exists():
        logger.error("Config file not found: %s", path)
        return {}

    with open(path) as f:
        return json.load(f)


def main():
    """CLI entry point for standalone gate checks."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Atlas pre-market volatility gate check",
    )
    parser.add_argument("--check", action="store_true", default=True,
                        help="Run gate check (default)")
    parser.add_argument("--market", default="sp500",
                        help="Market ID (sp500, asx, hk)")
    parser.add_argument("--config", default=None,
                        help="Path to config JSON file")
    parser.add_argument("--alert", action="store_true",
                        help="Send Telegram alert if gate fires (block)")
    parser.add_argument("--json", action="store_true",
                        help="Output result as JSON")
    args = parser.parse_args()

    config = _load_config(args.config, args.market)
    result = check_volatility_gate(config)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"\nVolatility Gate Check — {args.market.upper()}")
        print(f"  Status:     {result['action'].upper()}")
        print(f"  Flags:      {result['flags'] or 'none'}")
        print(f"  Size mult:  {result['size_multiplier']:.1f}x")
        print(f"  Message:    {result['message']}")
        print(f"  Checked at: {result['checked_at']}")

        if result.get("details"):
            print("\nIndicator Details:")
            for name, det in result["details"].items():
                flagged = "🚨" if det.get("flagged") else "✅"
                if name == "vix":
                    print(f"  {flagged} VIX: level={det.get('vix_level', 'N/A')} "
                          f"spike={det.get('spike_pct', 'N/A')}%")
                else:
                    print(f"  {flagged} {name} ({det.get('ticker', '')}): "
                          f"gap={det.get('gap_pct', 'N/A')}%")

    if args.alert and result["action"] == "block":
        print("\nSending Telegram alert...")
        ok = send_volatility_alert(result)
        print("Alert sent." if ok else "Alert failed (check credentials).")

    # Exit code signals to bash callers:
    #   0 = no gate action (proceed normally)
    #   1 = reduce (50% size reduction)
    #   2 = block (skip all new entries)
    action_codes = {"none": 0, "reduce": 1, "block": 2}
    sys.exit(action_codes.get(result["action"], 0))


if __name__ == "__main__":
    main()
