"""
Atlas Universe Builder
===========================
Filter and rank tickers to build a tradeable universe based on
liquidity, price, and market cap criteria from the active configuration.
Supports multiple markets (ASX, S&P 500, etc.).

Usage:
    from universe.builder import build_universe
    from utils.config import get_active_config

    config = get_active_config("asx")
    universe = build_universe(config)

    config_us = get_active_config("sp500")
    universe_us = build_universe(config_us)
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

from data.ingest import download_ticker, get_market_tickers
from universe.definitions import UNIVERSES, get_universe, list_universes

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"
PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

# Default market
DEFAULT_MARKET = "asx"


def _get_market_cap(ticker: str, retries: int = 2) -> Optional[float]:
    """Fetch market capitalisation for a ticker via yfinance.

    Uses fast_info first (faster), falls back to .info dict.

    Args:
        ticker: Yahoo Finance ticker symbol (e.g., 'BHP.AX').
        retries: Number of retry attempts on failure.

    Returns:
        Market cap in local currency, or None if unavailable.
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


def _market_processed_dir(market_id: Optional[str] = None) -> Path:
    """Return the processed data directory for a market."""
    market_id = (market_id or DEFAULT_MARKET).lower().strip()
    d = PROCESSED_DIR / market_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def filter_universe_pit(
    tickers: List[str],
    as_of_date,
    config: Dict[str, Any],
) -> List[str]:
    """Filter a universe to point-in-time S&P 500 membership.

    Used during backtesting to eliminate survivorship bias by only including
    tickers that were in the index at the specified date.

    Args:
        tickers: Current universe tickers.
        as_of_date: Date to reconstruct membership for.
        config: Configuration dictionary.

    Returns:
        Filtered list of tickers that were in the S&P 500 at as_of_date.
    """
    uni_cfg = config.get("universe", {})
    if not uni_cfg.get("point_in_time", False):
        return tickers

    try:
        from data.sp500_history import get_members_at_date
        pit_members = get_members_at_date(as_of_date)
        filtered = [t for t in tickers if t in pit_members]
        logger.info(
            f"PIT universe: {len(filtered)}/{len(tickers)} tickers "
            f"at {as_of_date} ({len(tickers) - len(filtered)} excluded)"
        )
        return filtered
    except Exception as e:
        logger.warning(f"PIT filtering failed, using full universe: {e}")
        return tickers


def build_universe(
    config: Dict[str, Any],
    candidate_tickers: Optional[List[str]] = None,
    save: bool = True,
    verbose: bool = True,
    as_of_date=None,
) -> List[str]:
    """Build a filtered and ranked tradeable universe.

    Process:
        1. Start with candidate tickers (default: from market profile)
        2. Download recent price/volume data for each
        3. Filter by minimum price, median daily value, and market cap
        4. Rank by average daily traded value (descending)
        5. Take top N tickers
        6. Apply exclusions from config
        7. Optionally filter to point-in-time membership (if as_of_date set)
        8. Save results to data/processed/{market_id}/universe.json

    Args:
        config: Configuration dictionary (from get_active_config()).
        candidate_tickers: Override list of tickers to evaluate.
                           Defaults to market profile's universe.
        save: Whether to save results to JSON (default True).
        verbose: Whether to print progress (default True).
        as_of_date: If set and point_in_time enabled, filter to PIT membership.

    Returns:
        List of ticker symbols that passed all filters.
    """
    market_id = config.get("market", DEFAULT_MARKET)
    uni_cfg = config.get("universe", {})
    top_n = uni_cfg.get("top_n", 100)
    min_median_dv = uni_cfg.get("min_median_daily_value", 1_000_000)
    min_price = uni_cfg.get("min_price", 1.0)
    min_market_cap = uni_cfg.get("min_market_cap", 300_000_000)
    exclusions = [e.upper() for e in uni_cfg.get("exclusions", [])]

    # Merge auto-exclusions
    try:
        from data.auto_exclusions import get_excluded_tickers
        auto_excl = get_excluded_tickers(market_id)
        if auto_excl:
            exclusions = list(set(exclusions) | auto_excl)
            logger.info("Merged %d auto-exclusions into universe exclusions", len(auto_excl))
    except ImportError:
        pass

    if candidate_tickers is None:
        candidate_tickers = get_market_tickers(market_id)

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

    # Get market profile for suffix stripping
    try:
        from markets import get_market
        _market = get_market(market_id)
    except (ImportError, KeyError):
        _market = None

    for i, ticker in enumerate(candidate_tickers, 1):
        ticker_upper = ticker.upper()
        base = _market.strip_suffix(ticker_upper) if _market else ticker_upper.split(".")[0]

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

        output_path = _market_processed_dir(market_id) / "universe.json"
        with open(output_path, "w") as f:
            json.dump(result, f, indent=2, default=str)

        logger.info(f"Universe saved to {output_path}")
        if verbose:
            print(f"\n  Saved to: {output_path}")

    return universe_tickers


def load_universe(market_id: Optional[str] = None) -> Dict[str, Any]:
    """Load the most recently built universe from disk.

    Args:
        market_id: Market identifier. Defaults to 'asx'.

    Returns:
        Dict with 'metadata', 'tickers', and 'details' keys.

    Raises:
        FileNotFoundError: If universe.json does not exist.
    """
    market_id = market_id or DEFAULT_MARKET
    path = _market_processed_dir(market_id) / "universe.json"
    if not path.exists():
        # Legacy fallback
        legacy = PROCESSED_DIR / "universe.json"
        if legacy.exists() and market_id == DEFAULT_MARKET:
            path = legacy
        else:
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


def get_universe_tickers(market_id: Optional[str] = None) -> List[str]:
    """Convenience function to get just the ticker list from saved universe.

    Args:
        market_id: Market identifier. Defaults to 'asx'.

    Returns:
        List of ticker strings.
    """
    return load_universe(market_id)["tickers"]


# ── SQLite-backed universe data builders ────────────────────────────────────


def build_from_definition(
    universe_name: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    min_history_days: int = 200,
) -> Dict[str, pd.DataFrame]:
    """Build universe data from SQLite for any of the 6 defined universes.

    For the ``sp500`` universe the data is queried directly from SQLite
    (using the universe column, which is populated by the ingest pipeline).
    For static ETF universes the ticker list comes from
    ``universe.definitions`` so cross-universe tickers are always returned
    for each universe regardless of which universe last wrote the SQLite row.

    DataFrames are returned with the same lowercase column names used
    throughout the backtest engine: ``open``, ``high``, ``low``, ``close``,
    ``volume`` (plus ``adj_close``, ``ticker``, ``universe``, ``source``
    which the engine tolerates as extra columns).

    Args:
        universe_name: One of the 6 universe names defined in
            ``universe.definitions.UNIVERSES``.
        start_date: ISO date string, e.g. ``"2020-01-01"``.
            Defaults to 7 years before today.
        end_date: ISO date string, e.g. ``"2024-12-31"``.
            Defaults to today.
        min_history_days: Minimum number of trading rows required for a
            ticker to be included in the result.  Tickers with fewer rows
            are silently dropped.  Default 200 (enough for a 200-DMA).

    Returns:
        ``dict[str, pd.DataFrame]`` mapping ticker → OHLCV DataFrame with
        a DatetimeIndex named ``date``.

    Raises:
        ValueError: If *universe_name* is not recognised.
    """
    import db.atlas_db as atlas_db  # late import — avoids circular deps

    known = list_universes()
    if universe_name not in known:
        raise ValueError(
            f"Unknown universe {universe_name!r}. "
            f"Known universes: {', '.join(known)}"
        )

    # Default date range: 7 years ago → today
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=7 * 365)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # Fetch raw data from SQLite
    # get_universe_data already handles the static vs dynamic split:
    #   - static: queries by ticker list from definitions
    #   - sp500 / dynamic: queries WHERE universe=?
    raw: Dict[str, pd.DataFrame] = atlas_db.get_universe_data(
        universe_name, start_date=start_date
    )

    # Apply end_date filter and min_history_days filter
    result: Dict[str, pd.DataFrame] = {}
    for ticker, df in raw.items():
        if df.empty:
            continue

        # Filter to end_date
        df = df[df.index <= end_date]

        # Drop tickers below minimum history threshold
        if len(df) < min_history_days:
            logger.debug(
                f"{universe_name}/{ticker}: only {len(df)} rows "
                f"< min_history_days={min_history_days}, dropping"
            )
            continue

        result[ticker] = df

    # Fallback: if SQLite returned no usable data, try loading from parquet cache
    if not result:
        cache_dir = Path(__file__).resolve().parent.parent / "data" / "cache" / universe_name
        if cache_dir.exists():
            logger.warning(
                "build_from_definition(%r): SQLite returned 0 tickers, "
                "falling back to parquet cache at %s",
                universe_name, cache_dir,
            )
            for pf in sorted(cache_dir.glob("*.parquet")):
                try:
                    df = pd.read_parquet(pf)
                    df.columns = [c.lower() for c in df.columns]
                    if "date" in df.columns:
                        df["date"] = pd.to_datetime(df["date"])
                        df = df.set_index("date")
                    df.index = pd.to_datetime(df.index)
                    # Apply date range filters
                    if start_date:
                        df = df[df.index >= start_date]
                    if end_date:
                        df = df[df.index <= end_date]
                    if len(df) >= min_history_days:
                        result[pf.stem] = df
                except Exception as exc:
                    logger.debug("Failed to read %s: %s", pf, exc)
            if result:
                logger.info(
                    "build_from_definition(%r): parquet fallback loaded %d tickers",
                    universe_name, len(result),
                )

    logger.info(
        f"build_from_definition({universe_name!r}): "
        f"{len(result)} tickers returned "
        f"(start={start_date}, end={end_date}, min_history={min_history_days})"
    )
    return result


def build_multi_universe(
    universe_names: List[str],
    start_date: Optional[str] = None,
    **kwargs,
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """Build data for multiple universes at once.

    Calls :func:`build_from_definition` for each requested universe and
    returns the results as a nested mapping.  Useful for the plan generator
    and regime-aware backtest which need to scan multiple active universes
    simultaneously.

    Args:
        universe_names: List of universe names to build.
        start_date: ISO date string passed to each
            :func:`build_from_definition` call.
        **kwargs: Additional keyword arguments forwarded to
            :func:`build_from_definition` (e.g. ``end_date``,
            ``min_history_days``).

    Returns:
        ``dict[str, dict[str, pd.DataFrame]]`` —
        ``{universe_name: {ticker: DataFrame}}``.

    Raises:
        ValueError: If any name in *universe_names* is not recognised.
    """
    return {
        name: build_from_definition(name, start_date=start_date, **kwargs)
        for name in universe_names
    }



def ensure_universe_current(config: Dict[str, Any]) -> bool:
    """Check if the saved universe reflects current exclusions; rebuild if stale.

    Compares the exclusions baked into the saved universe.json with the
    current config exclusions + auto-exclusions. If they differ, triggers
    a lightweight rebuild (using cached data, no API calls).

    Args:
        config: Active config dict.

    Returns:
        True if universe was rebuilt, False if already current.
    """
    market_id = config.get("market", DEFAULT_MARKET)
    uni_cfg = config.get("universe", {})
    config_exclusions = set(e.upper() for e in uni_cfg.get("exclusions", []))

    # Add auto-exclusions
    try:
        from data.auto_exclusions import get_excluded_tickers
        auto_excl = get_excluded_tickers(market_id)
        current_exclusions = config_exclusions | auto_excl
    except ImportError:
        current_exclusions = config_exclusions

    # Load saved universe and compare
    try:
        saved = load_universe(market_id)
        saved_exclusions = set(
            e.upper() for e in saved.get("metadata", {}).get("filters", {}).get("exclusions", [])
        )
    except FileNotFoundError:
        logger.info("No saved universe for %s — will build fresh", market_id)
        saved_exclusions = None

    if saved_exclusions is not None and saved_exclusions == current_exclusions:
        logger.debug("Universe exclusions are current for %s", market_id)
        return False

    # Exclusions have changed — need to rebuild
    logger.info(
        "Universe exclusions changed for %s: saved=%s, current=%s. Rebuilding.",
        market_id, saved_exclusions, current_exclusions,
    )

    # Rebuild — filter saved tickers if possible (avoids full API crawl)
    try:
        if saved_exclusions is not None:
            saved_tickers = saved.get("tickers", [])
            new_exclusions = current_exclusions - (saved_exclusions or set())
            if new_exclusions and not (saved_exclusions - current_exclusions):
                # Only additions to exclusions — can filter without full rebuild
                filtered = [t for t in saved_tickers
                           if t.upper() not in new_exclusions
                           and t.split(".")[0].upper() not in new_exclusions]
                if filtered:
                    result = {
                        "metadata": {
                            **saved.get("metadata", {}),
                            "built_at": datetime.now().isoformat(),
                            "filters": {
                                **saved.get("metadata", {}).get("filters", {}),
                                "exclusions": sorted(current_exclusions),
                            },
                            "final_count": len(filtered),
                            "rebuild_reason": f"exclusion_change: added {new_exclusions}",
                        },
                        "tickers": filtered,
                        "details": [d for d in saved.get("details", [])
                                   if d.get("ticker", "").upper() not in new_exclusions],
                    }
                    output_path = _market_processed_dir(market_id) / "universe.json"
                    with open(output_path, "w") as f:
                        json.dump(result, f, indent=2, default=str)
                    logger.info(
                        "Universe rebuilt (lightweight): %d -> %d tickers, "
                        "excluded %s", len(saved_tickers), len(filtered), new_exclusions,
                    )
                    return True
    except Exception as e:
        logger.warning("Lightweight rebuild failed, will do full rebuild: %s", e)

    # Full rebuild needed
    build_universe(config, save=True, verbose=False)
    return True

if __name__ == "__main__":
    import sys
    from utils.logging_config import setup_logging
    setup_logging("universe_builder", telegram_errors=False)

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
