"""Portfolio probability of ruin.

MC simulation of portfolio equity path, counting fraction of paths that
breach a drawdown floor (default: 70% of current equity).
"""
import logging
import json
from datetime import datetime, timezone
from typing import Optional
import numpy as np

from db.atlas_db import get_db

logger = logging.getLogger(__name__)


def compute_ruin_probability(
    current_equity: float,
    positions: list,
    floor_pct: float = 0.70,
    horizons: tuple = (30, 60, 90),
    n_paths: int = 10000,
    lookback_days: int = 60,
    seed: Optional[int] = None,
) -> dict:
    """
    Compute probability of portfolio equity breaching a floor over various horizons.

    Uses correlated GBM on position returns. Cash is assumed to stay flat.
    """
    from risk.portfolio_var import _get_returns_matrix, _ledoit_wolf_shrinkage, _cholesky_safe

    floor = current_equity * floor_pct

    if not positions or current_equity <= 0:
        return {
            "current_equity": current_equity,
            "floor": floor,
            "floor_pct": floor_pct,
            "n_paths": n_paths,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "horizons": {
                f"{h}d": {"prob_ruin": 0.0, "worst_case_equity": current_equity,
                          "worst_5pct_equity": current_equity, "median_end_equity": current_equity, "days": h}
                for h in horizons
            },
            "status": "no_positions",
        }

    tickers = [p['ticker'] for p in positions]
    position_values = np.array([float(p['shares']) * float(p['current_price']) for p in positions])
    cash = current_equity - position_values.sum()

    try:
        returns_df = _get_returns_matrix(tickers, lookback_days=lookback_days)
    except Exception as e:
        logger.error(f"Failed to get returns matrix: {e}")
        return {
            "error": str(e),
            "status": "data_error",
            "current_equity": current_equity,
            "floor": floor,
        }

    if returns_df.empty or len(returns_df) < 20:
        return {
            "error": "Insufficient historical data",
            "status": "insufficient_data",
            "current_equity": current_equity,
            "floor": floor,
        }

    # Covariance via Ledoit-Wolf shrinkage
    cov = _ledoit_wolf_shrinkage(returns_df)

    # Daily drift: use historical mean
    daily_drift = returns_df.mean().values

    # Cholesky
    n_assets = len(tickers)
    if n_assets == 1:
        L = np.array([[float(np.sqrt(cov[0, 0]))]])
    else:
        L = _cholesky_safe(cov)

    # Simulate paths
    rng = np.random.default_rng(seed)
    max_days = max(horizons)

    equity_paths = np.zeros((n_paths, max_days + 1))
    equity_paths[:, 0] = current_equity

    pos_value_paths = np.tile(position_values, (n_paths, 1)).astype(float)

    for day in range(1, max_days + 1):
        Z = rng.standard_normal((n_paths, n_assets))
        shocks = Z @ L.T
        daily_returns = daily_drift + shocks

        pos_value_paths = pos_value_paths * (1 + daily_returns)
        pos_value_paths = np.maximum(pos_value_paths, 0)

        equity_paths[:, day] = pos_value_paths.sum(axis=1) + cash

    result = {
        "current_equity": current_equity,
        "floor": floor,
        "floor_pct": floor_pct,
        "n_paths": n_paths,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "positions_value": float(position_values.sum()),
        "cash": cash,
        "tickers": tickers,
        "horizons": {},
        "status": "ok",
    }

    for h in horizons:
        if h > max_days:
            continue
        path_min = equity_paths[:, 1:h+1].min(axis=1)
        prob_ruin = float((path_min <= floor).mean())

        result["horizons"][f"{h}d"] = {
            "days": h,
            "prob_ruin": prob_ruin,
            "worst_case_equity": float(path_min.min()),
            "worst_5pct_equity": float(np.percentile(path_min, 5)),
            "median_end_equity": float(np.median(equity_paths[:, h])),
        }

    return result


def persist_ruin_probability(result: dict) -> None:
    """Store ruin probability results."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS ruin_probability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                current_equity REAL,
                floor REAL,
                floor_pct REAL,
                n_paths INTEGER,
                horizon_days INTEGER NOT NULL,
                prob_ruin REAL,
                worst_case_equity REAL,
                worst_5pct_equity REAL,
                median_end_equity REAL,
                tickers TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, horizon_days, floor_pct)
            )
        """)

        if result.get('status') != 'ok':
            return

        tickers_json = json.dumps(result['tickers'])
        for h_key, h in result['horizons'].items():
            db.execute("""
                INSERT OR REPLACE INTO ruin_probability
                (as_of, current_equity, floor, floor_pct, n_paths, horizon_days,
                 prob_ruin, worst_case_equity, worst_5pct_equity, median_end_equity, tickers)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result['as_of'], result['current_equity'], result['floor'], result['floor_pct'],
                result['n_paths'], h['days'], h['prob_ruin'], h['worst_case_equity'],
                h['worst_5pct_equity'], h['median_end_equity'], tickers_json
            ))
        db.commit()


def compute_for_current_portfolio(floor_pct: float = 0.70) -> dict:
    """Helper: compute ruin probability for currently open positions."""
    with get_db() as db:
        # NOTE: trades table has no current_price column; alias entry_price as a fallback.
        rows = db.execute("""
            SELECT ticker, shares, entry_price AS current_price, strategy, entry_price
            FROM trades WHERE exit_date IS NULL
        """).fetchall()

    positions = [dict(r) for r in rows]

    if not positions:
        return compute_ruin_probability(
            current_equity=0.0, positions=[], floor_pct=floor_pct, seed=42
        )

    # Get current equity - get_latest_equity returns a dict with 'equity' key
    equity = None
    try:
        from db.atlas_db import get_latest_equity
        equity_row = get_latest_equity()
        if equity_row and isinstance(equity_row, dict):
            equity = equity_row.get('equity')
    except Exception as e:
        logger.warning(f"get_latest_equity failed: {e}")

    if not equity or equity <= 0:
        # Fallback: sum of position values
        equity = sum(float(p['shares']) * float(p['current_price']) for p in positions)

    return compute_ruin_probability(
        current_equity=equity,
        positions=positions,
        floor_pct=floor_pct,
        seed=42,
    )


def get_latest_ruin_probability() -> dict:
    """Load latest ruin probability from DB."""
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM ruin_probability
            WHERE as_of = (SELECT MAX(as_of) FROM ruin_probability)
            ORDER BY horizon_days
        """).fetchall()

    if not rows:
        return {}

    first = rows[0]
    result = {
        "as_of": first['as_of'],
        "current_equity": first['current_equity'],
        "floor": first['floor'],
        "floor_pct": first['floor_pct'],
        "n_paths": first['n_paths'],
        "tickers": json.loads(first['tickers']) if first['tickers'] else [],
        "horizons": {}
    }
    for r in rows:
        result["horizons"][f"{r['horizon_days']}d"] = {
            "days": r['horizon_days'],
            "prob_ruin": r['prob_ruin'],
            "worst_case_equity": r['worst_case_equity'],
            "worst_5pct_equity": r['worst_5pct_equity'],
            "median_end_equity": r['median_end_equity'],
        }
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = compute_for_current_portfolio(floor_pct=0.70)
    persist_ruin_probability(result)

    print(f"\nPORTFOLIO PROBABILITY OF RUIN")
    print("=" * 75)
    print(f"Status:         {result.get('status', 'unknown')}")
    print(f"Current equity: ${result.get('current_equity', 0):,.2f}")
    print(f"Floor (70%):    ${result.get('floor', 0):,.2f}")
    print(f"Positions:      ${result.get('positions_value', 0):,.2f} in {len(result.get('tickers', []))} tickers")
    print(f"Cash:           ${result.get('cash', 0):,.2f}")
    print(f"N paths:        {result.get('n_paths', 0):,}")
    print()
    print(f"{'Horizon':>10} {'P(ruin)':>12} {'':>4} {'Worst 5%':>15} {'Median End':>15}")
    print("-" * 75)
    for h_key, h in result.get('horizons', {}).items():
        p = h['prob_ruin']
        marker = 'SAFE' if p < 0.05 else 'WATCH' if p < 0.15 else 'HIGH'
        print(f"{h_key:>10} {p*100:>10.2f}% {marker:>6} "
              f"${h['worst_5pct_equity']:>12,.0f} "
              f"${h['median_end_equity']:>13,.0f}")
    print("=" * 75)
