"""Black-Scholes barrier probability for stop-loss analysis.

Uses the reflection principle to compute P(stop touched before T)
based on current price, stop, and historical volatility.
"""
from __future__ import annotations
import math
import logging
from typing import Optional

from scipy.stats import norm

from db.atlas_db import get_db

logger = logging.getLogger(__name__)


def prob_touch_lower(spot: float, barrier: float, vol_annual: float, days: int) -> float:
    """
    Probability that price touches lower barrier within `days` trading days.

    Uses reflection principle: P(touch) ≈ 2 * N(-d) for downward barrier.
    """
    if spot <= 0 or barrier <= 0 or vol_annual <= 0 or days <= 0:
        return 0.0
    if barrier >= spot:
        return 1.0  # Already touched
    T = days / 252.0
    sigma_sqrt_T = vol_annual * math.sqrt(T)
    if sigma_sqrt_T <= 0:
        return 0.0
    d = math.log(spot / barrier) / sigma_sqrt_T
    return float(min(1.0, 2.0 * norm.cdf(-d)))


def prob_touch_upper(spot: float, barrier: float, vol_annual: float, days: int) -> float:
    """Probability of touching upper barrier (for short positions)."""
    if spot <= 0 or barrier <= 0 or vol_annual <= 0 or days <= 0:
        return 0.0
    if barrier <= spot:
        return 1.0
    T = days / 252.0
    sigma_sqrt_T = vol_annual * math.sqrt(T)
    if sigma_sqrt_T <= 0:
        return 0.0
    d = math.log(barrier / spot) / sigma_sqrt_T
    return float(min(1.0, 2.0 * norm.cdf(-d)))


def expected_loss_at_stop(entry: float, stop: float, shares: int, prob_touch: float) -> dict:
    """Compute expected dollar loss given probability of stop hit."""
    loss_per_share = entry - stop
    max_loss = loss_per_share * shares
    expected_loss = max_loss * prob_touch
    return {
        "loss_per_share": round(loss_per_share, 4),
        "max_loss": round(max_loss, 2),
        "expected_loss": round(expected_loss, 2),
    }


def _fetch_vol_from_cones(ticker: str) -> Optional[float]:
    """Get latest annualized vol from vol_cones table (20-day horizon)."""
    try:
        with get_db() as db:
            row = db.execute(
                "SELECT current_vol FROM vol_cones WHERE ticker = ? AND horizon = 20 "
                "ORDER BY as_of DESC LIMIT 1",
                (ticker,),
            ).fetchone()
            if row:
                return float(row["current_vol"])
    except Exception as e:
        logger.warning("vol_cones fetch failed for %s: %s", ticker, e)
    return None


def _fetch_vols_from_cones_batch(tickers: list[str]) -> dict[str, "Optional[float]"]:
    """Batch version: returns {ticker: vol or None} for all input tickers in ONE query.

    Uses a correlated sub-query to retrieve only the most-recent as_of row
    per ticker instead of doing N separate round-trips.
    """
    if not tickers:
        return {}
    out: dict[str, "Optional[float]"] = {t: None for t in tickers}
    placeholders = ",".join("?" * len(tickers))
    sql = f"""
        SELECT ticker, current_vol
        FROM vol_cones
        WHERE ticker IN ({placeholders}) AND horizon = 20
          AND (ticker, as_of) IN (
              SELECT ticker, MAX(as_of) FROM vol_cones
              WHERE ticker IN ({placeholders}) AND horizon = 20
              GROUP BY ticker
          )
    """
    try:
        with get_db() as db:
            rows = db.execute(sql, list(tickers) + list(tickers)).fetchall()
            for r in rows:
                out[r["ticker"]] = (
                    float(r["current_vol"]) if r["current_vol"] is not None else None
                )
    except Exception as e:
        logger.warning("batch vol_cones fetch failed: %s", e)
    return out


def analyze_position_stop(
    ticker: str,
    spot: float,
    stop: float,
    direction: str = "long",
    horizons: tuple = (1, 5, 10, 20),
    vol_annual: Optional[float] = None,
) -> dict:
    """Analyze a position's stop probability across multiple horizons."""
    if vol_annual is None:
        vol_annual = _fetch_vol_from_cones(ticker)
    if vol_annual is None or vol_annual <= 0:
        # Last-resort default
        vol_annual = 0.30
        logger.info("Using default 30%% vol for %s (no vol_cones data)", ticker)

    result = {
        "ticker": ticker,
        "spot": round(spot, 4),
        "stop": round(stop, 4),
        "direction": direction,
        "vol_annual": round(vol_annual, 4),
        "stop_distance_pct": round(abs(spot - stop) / spot, 4) if spot > 0 else 0.0,
        "horizons": {},
    }
    for days in horizons:
        if direction == "long":
            p = prob_touch_lower(spot, stop, vol_annual, days)
        else:
            p = prob_touch_upper(spot, stop, vol_annual, days)
        result["horizons"][f"{days}d"] = {
            "days": days,
            "prob_touch": round(p, 4),
            "prob_touch_pct": round(p * 100, 2),
        }
    return result


def analyze_all_open_positions(horizons: tuple = (1, 5, 10, 20)) -> list:
    """Analyze stop probability for every open position with a stop."""
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, shares, entry_price, stop_price, strategy "
            "FROM trades WHERE exit_date IS NULL AND stop_price IS NOT NULL"
        ).fetchall()
        positions = [dict(r) for r in rows]

    # Batch-fetch all vols in one query instead of N per-ticker queries
    all_tickers = [pos["ticker"] for pos in positions]
    vol_map = _fetch_vols_from_cones_batch(all_tickers)

    results = []
    for p in positions:
        try:
            # trades table has no current_price column; use entry_price as spot
            spot = float(p["entry_price"] or 0)
            stop = float(p["stop_price"] or 0)
            if spot <= 0 or stop <= 0:
                continue
            analysis = analyze_position_stop(
                ticker=p["ticker"],
                spot=spot,
                stop=stop,
                direction="long",
                horizons=horizons,
                vol_annual=vol_map.get(p["ticker"]),  # pre-fetched, avoids per-ticker query
            )
            analysis["shares"] = int(p["shares"] or 0)
            analysis["strategy"] = p["strategy"]
            analysis["entry"] = round(float(p["entry_price"] or 0), 4)

            analysis["loss"] = expected_loss_at_stop(
                float(p["entry_price"] or 0),
                stop,
                int(p["shares"] or 0),
                analysis["horizons"][f"{horizons[-1]}d"]["prob_touch"],
            )
            results.append(analysis)
        except Exception as e:
            logger.error("Error analyzing %s: %s", p.get("ticker"), e)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = analyze_all_open_positions()
    if not results:
        print("No open positions with stops")
        raise SystemExit(0)
    print(f"\nSTOP PROBABILITY ANALYSIS — {len(results)} positions")
    print("=" * 94)
    print(f"{'Ticker':8} {'Spot':>10} {'Stop':>10} {'Vol':>8} {'1d':>8} {'5d':>8} {'10d':>8} {'20d':>8} {'EL_20d':>12}")
    print("-" * 94)
    for r in results:
        print(
            f"{r['ticker']:8} "
            f"{r['spot']:>10.2f} "
            f"{r['stop']:>10.2f} "
            f"{r['vol_annual']*100:>7.1f}% "
            f"{r['horizons']['1d']['prob_touch_pct']:>7.1f}% "
            f"{r['horizons']['5d']['prob_touch_pct']:>7.1f}% "
            f"{r['horizons']['10d']['prob_touch_pct']:>7.1f}% "
            f"{r['horizons']['20d']['prob_touch_pct']:>7.1f}% "
            f"${r['loss']['expected_loss']:>11.2f}"
        )
    print("=" * 94)
