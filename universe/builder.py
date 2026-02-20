"""
Atlas-ASX Universe Builder
===========================
Filter and rank ASX tickers to build a tradeable universe based on
liquidity, price, and market cap criteria from the active configuration.

Usage:
    from universe.builder import build_universe

    config = get_active_config()
    universe = build_universe(config)
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yfinance as yf

from data.ingest import download_ticker, get_asx200_tickers

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)


def _get_market_cap(ticker: str, retries: int = 2) -> Optional[float]:
    """Fetch market capitalisation for a ticker via yfinance.

    Uses fast_info first (faster), falls back to .info dict.

    Args:
        ticker: Yahoo Finance ticker symbol (e.g., 'BHP.AX').
        retries: Number of retry attempts on failure.

    Returns:
        Market cap in AUD, or None if unavailable.
    """
    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker)
            # Try fast_info first (much faster)
            try:
                mcap = t.fast_info.get("marketCap", None)
                if mcap and mcap > 0:
                    return float(mcap)
            except Exception:
                pass

            # Fallback to full info
            info = t.info
            mcap = info.get("marketCap", None)
            if mcap and mcap > 0:
                return float(mcap)

            return None
        except Exception as e:
            if attempt < retries:
                time.sleep(0.5)
            else:
                logger.warning(f"{ticker}: failed to get market cap: {e}")
                return None


def _compute_daily_value_stats(
    ticker: str,
    lookback_days: int = 60,
) -> Dict[str, Optional[float]]:
    """Compute median and average daily traded value for a ticker.

    Daily traded value = close_price * volume.

    Args:
        ticker: Yahoo Finance ticker symbol.
        lookback_days: Number of recent trading days to analyse.

    Returns:
        Dict with 'median_daily_value', 'avg_daily_value', 'last_close',
        'avg_volume', 'trading_days'. Values are None if data unavailable.
    """
    end = datetime.now()
    start = end - timedelta(days=int(lookback_days * 1.6))  # buffer for weekends/holidays

    df = download_ticker(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), use_cache=True)

    if df.empty or len(df) < 10:
        return {
            "median_daily_value": None,
            "avg_daily_value": None,
            "last_close": None,
            "avg_volume": None,
            "trading_days": 0,
        }

    # Use last N trading days
    df = df.tail(lookback_days)
    daily_value = df["close"] * df["volume"]

    return {
        "median_daily_value": float(daily_value.median()),
        "avg_daily_value": float(daily_value.mean()),
        "last_close": float(df["close"].iloc[-1]),
        "avg_volume": float(df["volume"].mean()),
        "trading_days": len(df),
    }


def build_universe(
    config: Dict[str, Any],
    candidate_tickers: Optional[List[str]] = None,
    save: bool = True,
    verbose: bool = True,
) -> List[str]:
    """Build a filtered and ranked tradeable universe.

    Process:
        1. Start with candidate tickers (default: ASX 200 list)
        2. Download recent price/volume data for each
        3. Filter by minimum price, median daily value, and market cap
        4. Rank by average daily traded value (descending)
        5. Take top N tickers
        6. Apply exclusions from config
        7. Save results to data/processed/universe.json

    Args:
        config: Configuration dictionary (from get_active_config()).
        candidate_tickers: Override list of tickers to evaluate.
                           Defaults to get_asx200_tickers().
        save: Whether to save results to JSON (default True).
        verbose: Whether to print progress (default True).

    Returns:
        List of ticker symbols that passed all filters.
    """
    uni_cfg = config.get("universe", {})
    top_n = uni_cfg.get("top_n", 100)
    min_median_dv = uni_cfg.get("min_median_daily_value", 1_000_000)
    min_price = uni_cfg.get("min_price", 1.0)
    min_market_cap = uni_cfg.get("min_market_cap", 300_000_000)
    exclusions = [e.upper() for e in uni_cfg.get("exclusions", [])]

    if candidate_tickers is None:
        candidate_tickers = get_asx200_tickers()

    total = len(candidate_tickers)
    if verbose:
        print(f"\nUniverse Builder: evaluating {total} candidates")
        print(f"  Filters: min_price=${min_price}, min_median_dv=${min_median_dv:,.0f}, "
              f"min_mcap=${min_market_cap:,.0f}")
        print(f"  Target: top {top_n} by daily traded value")
        print(f"  Exclusions: {exclusions if exclusions else 'none'}")

    # Phase 1: Collect stats for all candidates
    stats = []
    failed = []

    for i, ticker in enumerate(candidate_tickers, 1):
        ticker_upper = ticker.upper()
        base = ticker_upper.replace(".AX", "")

        # Skip excluded tickers early
        if base in exclusions or ticker_upper in exclusions:
            logger.debug(f"{ticker}: excluded by config")
            continue

        try:
            dv_stats = _compute_daily_value_stats(ticker)

            if dv_stats["last_close"] is None:
                failed.append(ticker)
                continue

            stats.append({
                "ticker": ticker_upper,
                **dv_stats,
            })
        except Exception as e:
            logger.warning(f"{ticker}: error computing stats: {e}")
            failed.append(ticker)

        if verbose and i % 25 == 0:
            print(f"  Progress: {i}/{total} evaluated ({len(stats)} valid, {len(failed)} failed)")

    if verbose:
        print(f"  Evaluation complete: {len(stats)} valid, {len(failed)} failed")

    if not stats:
        logger.error("No valid tickers found after evaluation")
        return []

    # Phase 2: Apply filters
    df = pd.DataFrame(stats)

    # Filter: minimum price
    before = len(df)
    df = df[df["last_close"] >= min_price]
    filtered_price = before - len(df)

    # Filter: minimum median daily value
    before = len(df)
    df = df[df["median_daily_value"].notna() & (df["median_daily_value"] >= min_median_dv)]
    filtered_dv = before - len(df)

    if verbose:
        print(f"\n  After price filter (>=${min_price}): {len(df)} remain ({filtered_price} removed)")
        print(f"  After daily value filter (>=${min_median_dv:,.0f}): {len(df)} remain ({filtered_dv} removed)")

    # Phase 3: Market cap filter (only for remaining candidates to save API calls)
    if min_market_cap > 0 and len(df) > 0:
        if verbose:
            print(f"\n  Fetching market caps for {len(df)} tickers...")

        mcaps = {}
        for idx, row in df.iterrows():
            ticker = row["ticker"]
            mcap = _get_market_cap(ticker)
            mcaps[ticker] = mcap
            time.sleep(0.05)  # gentle rate limiting

        df["market_cap"] = df["ticker"].map(mcaps)

        before = len(df)
        df = df[df["market_cap"].notna() & (df["market_cap"] >= min_market_cap)]
        filtered_mcap = before - len(df)

        if verbose:
            print(f"  After market cap filter (>=${min_market_cap:,.0f}): {len(df)} remain ({filtered_mcap} removed)")
    else:
        df["market_cap"] = np.nan

    # Phase 4: Rank by average daily traded value and take top N
    df = df.sort_values("avg_daily_value", ascending=False)
    df = df.head(top_n)

    universe_tickers = df["ticker"].tolist()

    if verbose:
        print(f"\n  Final universe: {len(universe_tickers)} tickers (top {top_n} by daily value)")
        if len(universe_tickers) > 0:
            print(f"  Top 10: {universe_tickers[:10]}")
            print(f"  Bottom 5: {universe_tickers[-5:]}")

    # Phase 5: Save results
    if save:
        result = {
            "metadata": {
                "built_at": datetime.now().isoformat(),
                "config_version": config.get("version", "unknown"),
                "candidates_evaluated": total,
                "candidates_valid": len(stats),
                "candidates_failed": len(failed),
                "filters": {
                    "min_price": min_price,
                    "min_median_daily_value": min_median_dv,
                    "min_market_cap": min_market_cap,
                    "top_n": top_n,
                    "exclusions": exclusions,
                },
                "filtered_out": {
                    "by_price": filtered_price,
                    "by_daily_value": filtered_dv,
                    "by_market_cap": filtered_mcap if min_market_cap > 0 else 0,
                },
                "final_count": len(universe_tickers),
            },
            "tickers": universe_tickers,
            "details": df[["ticker", "last_close", "median_daily_value",
                           "avg_daily_value", "market_cap"]].to_dict(orient="records"),
        }

        output_path = PROCESSED_DIR / "universe.json"
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        logger.info(f"Universe saved to {output_path}")
        if verbose:
            print(f"\n  Saved to: {output_path}")

    return universe_tickers


def load_universe() -> Dict[str, Any]:
    """Load the most recently built universe from disk.

    Returns:
        Dict with 'metadata', 'tickers', and 'details' keys.

    Raises:
        FileNotFoundError: If universe.json does not exist.
    """
    path = PROCESSED_DIR / "universe.json"
    if not path.exists():
        raise FileNotFoundError(
            f"Universe file not found: {path}. Run build_universe() first."
        )

    with open(path, "r") as f:
        data = json.load(f)

    logger.info(
        f"Loaded universe: {data['metadata']['final_count']} tickers "
        f"(built {data['metadata']['built_at']})"
    )
    return data


def get_universe_tickers() -> List[str]:
    """Convenience function to get just the ticker list from saved universe.

    Returns:
        List of ticker strings.
    """
    return load_universe()["tickers"]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Import config
    sys.path.insert(0, str(PROJECT_ROOT))
    from utils.config import get_active_config

    print("=== Universe Builder Self-Test ===")
    print("NOTE: This downloads data for many tickers and may take a few minutes.")
    print("      Using a small subset for testing...\n")

    config = get_active_config()

    # Test with a small subset to be fast
    test_tickers = [
        "BHP.AX", "CBA.AX", "CSL.AX", "NAB.AX", "WBC.AX",
        "ANZ.AX", "WES.AX", "WOW.AX", "MQG.AX", "FMG.AX",
        "RIO.AX", "TLS.AX", "ALL.AX", "AMC.AX", "GMG.AX",
        "XRO.AX", "REA.AX", "COH.AX", "RMD.AX", "TCL.AX",
    ]

    # Override top_n for test
    test_config = config.copy()
    test_config["universe"] = {**config["universe"], "top_n": 15}

    universe = build_universe(
        test_config,
        candidate_tickers=test_tickers,
        save=True,
        verbose=True,
    )

    print(f"\n--- Result ---")
    print(f"Universe: {universe}")

    # Test load
    print(f"\n--- Load Test ---")
    loaded = load_universe()
    print(f"Loaded {loaded['metadata']['final_count']} tickers")
    print(f"Built at: {loaded['metadata']['built_at']}")
    print(f"Tickers: {loaded['tickers']}")

    print("\n=== Universe Builder OK ===")
