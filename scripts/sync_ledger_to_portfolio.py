#!/usr/bin/env python3
"""Sync trade_ledger.json exits into LivePortfolio closed_trades.

Fixes the dashboard closed trades bug by syncing the TradeLedger
(which has all exits) into the LivePortfolio state file (which the dashboard reads).

Also fixes "unknown" strategy names for ECL and NFLX by parsing client_order_ids.
"""
import json
import logging
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Strategy abbreviation mapping (from client_order_id format)
STRATEGY_EXPANSIONS = {
    "conn": "connors_rsi2",
    "tren": "trend_following",
    "mome": "momentum_breakout",
    "mean": "mean_reversion",
    "open": "opening_gap",
    "sect": "sector_rotation",
    "shor": "short_term_mr",
}

def expand_strategy_name(abbrev: str) -> str:
    """Expand abbreviated strategy name to full name."""
    return STRATEGY_EXPANSIONS.get(abbrev, abbrev)


def main():
    ledger_path = PROJECT / "journal" / "trade_ledger.json"
    state_path = PROJECT / "brokers" / "state" / "live_sp500.json"

    # Load trade ledger
    try:
        with open(ledger_path) as f:
            ledger = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load trade ledger: {e}")
        return 1

    # Load live state
    try:
        with open(state_path) as f:
            state = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load live state: {e}")
        return 1

    # Extract all exits from ledger
    ledger_exits = [e for e in ledger if e.get("type") == "exit"]
    logger.info(f"Found {len(ledger_exits)} exits in trade ledger")

    # Get existing closed trades from state (to avoid duplicates)
    existing_closed = state.get("closed_trades", [])
    existing_order_ids = {t.get("order_id") for t in existing_closed if t.get("order_id")}
    logger.info(f"Existing closed trades in state: {len(existing_closed)}")

    # Sync exits from ledger to state
    new_trades = []
    fixed_strategies = 0

    for exit_rec in ledger_exits:
        order_id = exit_rec.get("order_id", "")
        
        # Skip if already in closed_trades (don't duplicate)
        if order_id and order_id in existing_order_ids:
            continue

        # Build closed trade record for LivePortfolio format
        closed_trade = {
            "ticker": exit_rec.get("ticker", ""),
            "strategy": exit_rec.get("strategy", "unknown"),
            "entry_price": exit_rec.get("entry_price", 0),
            "exit_price": exit_rec.get("fill_price", 0),
            "shares": exit_rec.get("shares", 0),
            "pnl": exit_rec.get("pnl", 0),
            "pnl_pct": exit_rec.get("pnl_pct", 0),
            "holding_days": exit_rec.get("holding_days", 0),
            "exit_reason": exit_rec.get("exit_reason", "unknown"),
            "exit_date": exit_rec.get("timestamp", "")[:10],
            "order_id": order_id,
        }
        new_trades.append(closed_trade)

    # Fix "unknown" strategies in ledger by parsing client_order_ids from Alpaca
    # For ECL and NFLX specifically
    try:
        from brokers.secrets import get_secret
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        api_key = get_secret("ALPACA_API_KEY")
        api_secret = get_secret("ALPACA_SECRET_KEY")
        paper = (get_secret("ALPACA_PAPER") or "false").lower() in ("true", "1")

        client = TradingClient(api_key, api_secret, paper=paper)
        req = GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500, direction="desc")
        orders = client.get_orders(filter=req)

        # Build lookup: ticker → strategy (from client_order_id)
        ticker_strategies = {}
        for order in orders:
            sym = str(getattr(order, "symbol", ""))
            coid = str(getattr(order, "client_order_id", ""))
            side_val = getattr(order, "side", None)
            side = str(side_val.value if hasattr(side_val, "value") else side_val).lower()
            
            if side != "buy" or not coid.startswith("atlas_"):
                continue
            
            # Parse strategy from client_order_id: atlas_atlas_{abbrev}_{uuid}
            parts = coid.split("_")
            if len(parts) >= 3:
                abbrev = parts[2]
                if abbrev != "atlas":  # skip double "atlas_atlas_atlas_..."
                    strategy = expand_strategy_name(abbrev)
                    ticker_strategies[sym] = strategy

        # Fix "unknown" strategies in both ledger entries and exits
        for entry in ledger:
            ticker = entry.get("ticker", "")
            if entry.get("strategy") == "unknown" and ticker in ticker_strategies:
                old_strat = entry["strategy"]
                entry["strategy"] = ticker_strategies[ticker]
                logger.info(f"Fixed {ticker} strategy: {old_strat} → {entry['strategy']}")
                fixed_strategies += 1

    except Exception as e:
        logger.warning(f"Could not fetch Alpaca orders to fix strategies: {e}")

    # Write updated ledger with fixed strategies
    if fixed_strategies > 0:
        try:
            with open(ledger_path, "w") as f:
                json.dump(ledger, f, indent=2)
            logger.info(f"Updated trade ledger with {fixed_strategies} fixed strategies")
        except Exception as e:
            logger.error(f"Failed to write updated ledger: {e}")

    # Fix "unknown" strategies in open positions from ledger entry records
    ledger_entries = {e["ticker"]: e["strategy"] for e in ledger
                      if e.get("type") == "entry" and e.get("strategy")
                      and e["strategy"] != "unknown"}
    fixed_positions = 0
    for pos in state.get("positions", []):
        ticker = pos.get("ticker", "")
        if pos.get("strategy", "unknown") == "unknown" and ticker in ledger_entries:
            old = pos["strategy"]
            pos["strategy"] = ledger_entries[ticker]
            logger.info(f"Fixed open position {ticker} strategy: {old} → {pos['strategy']}")
            fixed_positions += 1

    # Add new trades to state
    if new_trades or fixed_positions > 0:
        state["closed_trades"] = existing_closed + new_trades
        try:
            with open(state_path, "w") as f:
                json.dump(state, f, indent=2, default=str)
            logger.info(f"Synced {len(new_trades)} new closed trades to live_sp500.json")
        except Exception as e:
            logger.error(f"Failed to write state: {e}")
            return 1
    else:
        logger.info("No new closed trades to sync")

    logger.info(f"✅ Done! Total closed trades: {len(existing_closed) + len(new_trades)}")
    logger.info(f"✅ Fixed {fixed_strategies} 'unknown' strategy names")
    return 0


if __name__ == "__main__":
    sys.exit(main())
