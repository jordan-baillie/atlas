"""Regression test: FCX must not appear in both sp500 and commodity_etfs state.

FCX is a copper miner tracked under sp500 (connors_rsi2 strategy).
On 2026-04-28, it was erroneously present in live_commodity_etfs.json
because an earlier reconcile --fix pulled all broker positions without
strict cross-market deduplication.
"""
import json
from pathlib import Path

PROJECT = Path(__file__).parent.parent
STATE_DIR = PROJECT / "brokers" / "state"

MARKET_STATE_FILES = [
    "live_sp500.json",
    "live_commodity_etfs.json",
    "live_sector_etfs.json",
    "live_asx.json",
    "live_defensive_etfs.json",
    "live_gold_etfs.json",
    "live_treasury_etfs.json",
]


def _load_positions(market_file: str) -> list[str]:
    path = STATE_DIR / market_file
    if not path.exists():
        return []
    state = json.loads(path.read_text())
    return [p.get("ticker", "") for p in state.get("positions", [])]


def test_fcx_only_in_sp500():
    """FCX must only appear in sp500, not in commodity_etfs."""
    sp500_tickers = _load_positions("live_sp500.json")
    commodity_tickers = _load_positions("live_commodity_etfs.json")
    assert "FCX" in sp500_tickers, "FCX should be tracked in sp500"
    assert "FCX" not in commodity_tickers, (
        "FCX double-claim: found in commodity_etfs. Remove it — FCX is a sp500/connors_rsi2 position."
    )


def test_no_ticker_in_multiple_markets():
    """No single ticker should appear in more than one market's state file."""
    seen: dict[str, str] = {}  # ticker → first market
    for market_file in MARKET_STATE_FILES:
        tickers = _load_positions(market_file)
        market = market_file.replace("live_", "").replace(".json", "")
        for ticker in tickers:
            if ticker and ticker in seen:
                raise AssertionError(
                    f"Double-claim: {ticker} appears in BOTH {seen[ticker]} AND {market}. "
                    f"Remove from one market's state file."
                )
            if ticker:
                seen[ticker] = market
