"""
risk/portfolio_var.py — Portfolio-level VaR/CVaR via correlated Monte Carlo.

Theory:
1. Pull recent log-returns for all held tickers.
2. Compute Ledoit-Wolf shrunk covariance Σ.
3. Cholesky decompose: Σ = L Lᵀ. Generate n_paths × n_tickers ~ N(0,I), apply L.
4. Apply correlated returns to position dollar values; sum per path → portfolio P&L.
5. VaR = empirical percentile; CVaR = mean of tail beyond VaR.

Two methods:
- gaussian_cholesky: classical, assumes normality.
- regime_conditional: hybrid — bootstrap marginals from regime distribution,
  apply historical correlation via Cholesky on standardised draws.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from db.atlas_db import get_db, get_open_positions

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_returns_matrix(tickers: Sequence[str], lookback_days: int) -> pd.DataFrame:
    """Fetch OHLCV close prices for tickers, compute log returns, align dates.

    Returns a DataFrame indexed by date with one column per ticker. Drops any
    ticker with no data, drops dates where any ticker is missing.
    """
    if not tickers:
        return pd.DataFrame()

    # Pull a buffer of days because some dates will be NaN-aligned out
    buffer_days = max(int(lookback_days * 2.5), lookback_days + 30)
    placeholders = ",".join(["?"] * len(tickers))
    query = f"""
        SELECT ticker, date, close
        FROM ohlcv
        WHERE ticker IN ({placeholders})
        ORDER BY date DESC
        LIMIT {len(tickers) * buffer_days}
    """
    with get_db() as db:
        rows = db.execute(query, list(tickers)).fetchall()
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame([dict(r) for r in rows])
    pivot = df.pivot(index="date", columns="ticker", values="close").sort_index()

    # Drop tickers that have no rows
    pivot = pivot.dropna(axis=1, how="all")
    missing = [t for t in tickers if t not in pivot.columns]
    for t in missing:
        logger.warning("No OHLCV history for %s — excluding from VaR calc", t)

    # Forward-fill small gaps then drop any remaining incomplete rows
    pivot = pivot.ffill().dropna()

    # Keep only the most recent lookback_days
    pivot = pivot.tail(lookback_days + 1)

    # Compute log returns
    log_ret = np.log(pivot / pivot.shift(1)).dropna()
    return log_ret


def _ledoit_wolf_shrinkage(returns_df: pd.DataFrame) -> np.ndarray:
    """Shrunk covariance matrix via sklearn's LedoitWolf estimator."""
    from sklearn.covariance import LedoitWolf
    if returns_df.shape[1] == 1:
        # Single asset — just sample variance
        v = float(returns_df.iloc[:, 0].var(ddof=1))
        return np.array([[v]])
    lw = LedoitWolf()
    lw.fit(returns_df.values)
    return lw.covariance_


def _cholesky_safe(cov_matrix: np.ndarray) -> np.ndarray:
    """Cholesky factor with eigenvalue clipping fallback for near-singular Σ."""
    try:
        return np.linalg.cholesky(cov_matrix)
    except np.linalg.LinAlgError:
        # Symmetrize, clip eigenvalues to a small positive floor
        sym = (cov_matrix + cov_matrix.T) / 2
        w, V = np.linalg.eigh(sym)
        w_clipped = np.clip(w, 1e-12, None)
        psd = V @ np.diag(w_clipped) @ V.T
        # Add a tiny ridge for numerical safety
        psd = psd + np.eye(psd.shape[0]) * 1e-10
        return np.linalg.cholesky(psd)


def _effective_number_of_bets(weights: np.ndarray, corr_matrix: np.ndarray) -> float:
    """Original-assets Meucci ENB.

    Measures diversification using each asset's contribution to portfolio variance.
    p_i = w_i * (Σw)_i / σ_p²  (marginal contribution to variance per asset)
    ENB = exp(entropy(p))

    For uncorrelated equal weights → ENB ≈ N
    For perfectly correlated → ENB → 1
    For NFLX/MRVL with ρ=0.06 → ENB ≈ 1.86
    """
    w = np.asarray(weights, dtype=np.float64)
    n = len(w)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0

    # Normalize weights so they sum to 1 (callers pass dollar values)
    w_sum = w.sum()
    w_norm = w / w_sum if w_sum != 0 else w

    # Treat correlation matrix as covariance with unit variances
    cov = np.asarray(corr_matrix, dtype=np.float64)

    # Portfolio variance: w' Σ w
    sigma_w = cov @ w_norm
    portfolio_var = float(w_norm @ sigma_w)
    if portfolio_var <= 1e-12:
        return 1.0

    # Marginal contribution to variance per asset
    contrib = w_norm * sigma_w / portfolio_var

    # Filter near-zero (avoid log(0))
    contrib = np.where(contrib > 1e-12, contrib, 1e-12)
    contrib = contrib / contrib.sum()  # renormalize

    # Shannon entropy → ENB
    entropy = -np.sum(contrib * np.log(contrib))
    enb = float(np.exp(entropy))

    return float(min(max(enb, 1.0), float(n)))


def _simulate_gaussian(
    position_values: np.ndarray,
    cov_matrix: np.ndarray,
    n_paths: int,
    n_days: int,
    seed: Optional[int],
) -> np.ndarray:
    """Simulate portfolio P&L over n_days using correlated Gaussian draws.

    Returns array of length n_paths: portfolio P&L in dollars.
    """
    rng = np.random.default_rng(seed)
    L = _cholesky_safe(cov_matrix)
    # Z: shape (n_paths, n_days, n)
    n = len(position_values)
    Z = rng.standard_normal(size=(n_paths, n_days, n))
    # Apply Cholesky: correlated daily returns
    correlated = Z @ L.T  # (n_paths, n_days, n)
    # Sum log returns over horizon -> cumulative log return
    cum_log_ret = correlated.sum(axis=1)  # (n_paths, n)
    # Convert to simple returns and apply to position values
    simple_ret = np.exp(cum_log_ret) - 1.0
    pnl = simple_ret @ position_values  # (n_paths,)
    return pnl


def _simulate_regime_conditional(
    position_values: np.ndarray,
    cov_matrix: np.ndarray,
    regime_distributions,  # RegimeDistributions instance
    regime_state: str,
    n_paths: int,
    n_days: int,
    seed: Optional[int],
) -> np.ndarray:
    """Simulate using regime-conditional marginals + historical correlation.

    Steps per path:
    1. Sample n_days x n_assets returns: for each asset use bootstrapped regime returns.
    2. Standardize them (subtract mean, divide by std of the regime samples).
    3. Apply correlation structure via Cholesky on standardized draws.
    4. Re-scale by per-asset target vol from cov_matrix.
    """
    n = len(position_values)

    # Get target std-devs from cov diagonal
    target_vols = np.sqrt(np.maximum(np.diag(cov_matrix), 1e-12))

    # Bootstrap regime samples per asset, then standardize each column
    regime_samples = regime_distributions.sample_returns(
        regime_state, n_paths * n_days * n, seed=seed
    ).reshape(n_paths, n_days, n)
    # Standardize using per-asset stats from the bootstrap pool
    bootstrap_mean = regime_samples.mean(axis=(0, 1), keepdims=True)
    bootstrap_std = regime_samples.std(axis=(0, 1), keepdims=True) + 1e-12
    standardized = (regime_samples - bootstrap_mean) / bootstrap_std

    # Build correlation matrix from cov_matrix
    inv_vols = 1.0 / target_vols
    corr = cov_matrix * np.outer(inv_vols, inv_vols)
    # Symmetrize and clip
    corr = (corr + corr.T) / 2
    np.fill_diagonal(corr, 1.0)

    L = _cholesky_safe(corr)
    # Apply Cholesky to standardized draws
    correlated = standardized @ L.T  # (n_paths, n_days, n)
    # Re-scale by target vols
    daily_ret = correlated * target_vols  # broadcasting
    # Sum log returns
    cum_log_ret = daily_ret.sum(axis=1)
    simple_ret = np.exp(cum_log_ret) - 1.0
    pnl = simple_ret @ position_values
    return pnl


# ── Public API ───────────────────────────────────────────────────────────────

def _zero_result(positions, method: str = "gaussian_cholesky", n_paths: int = 0, lookback_days: int = 60) -> dict:
    """Return a sane zero-risk result for empty/degenerate inputs."""
    return {
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
        "equity": 0.0,
        "positions_value": 0.0,
        "positions_count": 0,
        "tickers": [],
        "correlation_avg": 0.0,
        "correlation_max": 0.0,
        "effective_bets": 0.0,
        "horizons": {},
        "method": method,
        "n_paths": n_paths,
        "lookback_days": lookback_days,
        "warnings": ["no_positions"],
    }


def _percentiles_to_metrics(pnl: np.ndarray, position_value_total: float) -> dict:
    """Extract VaR/CVaR metrics from a P&L sample array."""
    var_95 = float(np.percentile(pnl, 5))
    var_99 = float(np.percentile(pnl, 1))
    tail_95 = pnl[pnl <= var_95]
    tail_99 = pnl[pnl <= var_99]
    cvar_95 = float(tail_95.mean()) if len(tail_95) > 0 else var_95
    cvar_99 = float(tail_99.mean()) if len(tail_99) > 0 else var_99
    best_95 = float(np.percentile(pnl, 95))
    pv = position_value_total if position_value_total > 0 else 1.0
    return {
        "var_95": round(var_95, 2),
        "var_99": round(var_99, 2),
        "cvar_95": round(cvar_95, 2),
        "cvar_99": round(cvar_99, 2),
        "var_95_pct": round(var_95 / pv, 6),
        "var_99_pct": round(var_99 / pv, 6),
        "cvar_95_pct": round(cvar_95 / pv, 6),
        "cvar_99_pct": round(cvar_99 / pv, 6),
        "best_case_95": round(best_95, 2),
        "mean_pnl": round(float(pnl.mean()), 2),
        "std_pnl": round(float(pnl.std(ddof=1)), 2),
    }


def compute_portfolio_var(
    positions: list,
    lookback_days: int = 60,
    n_paths: int = 10000,
    horizons: tuple = (1, 5),
    seed: Optional[int] = None,
    equity: Optional[float] = None,
) -> dict:
    """Compute Gaussian-Cholesky VaR/CVaR over multiple horizons.

    positions: list of dicts with keys: ticker, shares, current_price, strategy.
    """
    warnings: list = []
    if not positions:
        return _zero_result(positions, method="gaussian_cholesky",
                            n_paths=n_paths, lookback_days=lookback_days)

    tickers = [p["ticker"] for p in positions]
    shares = np.array([float(p.get("shares") or 0) for p in positions])
    prices = np.array([float(p.get("current_price") or p.get("entry_price") or 0) for p in positions])
    pos_values = shares * prices
    pos_value_total = float(pos_values.sum())

    if pos_value_total <= 0:
        warnings.append("zero_position_value")
        return {**_zero_result(positions, "gaussian_cholesky", n_paths, lookback_days),
                "warnings": warnings}

    # Pull returns
    returns = _get_returns_matrix(tickers, lookback_days)
    if returns.empty:
        warnings.append("no_return_data")
        return {**_zero_result(positions, "gaussian_cholesky", n_paths, lookback_days),
                "warnings": warnings}

    # Filter positions to those that have return data
    available_tickers = list(returns.columns)
    keep_idx = [i for i, t in enumerate(tickers) if t in available_tickers]
    if len(keep_idx) < len(tickers):
        excluded = [tickers[i] for i in range(len(tickers)) if i not in keep_idx]
        warnings.append(f"excluded_tickers:{','.join(excluded)}")
        tickers = [tickers[i] for i in keep_idx]
        pos_values = pos_values[keep_idx]
        pos_value_total = float(pos_values.sum())

    # Reorder returns columns to match positions order
    returns = returns[tickers]

    actual_lookback = len(returns)
    if actual_lookback < 10:
        warnings.append(f"sparse_history:{actual_lookback}_days")

    # Covariance + correlation
    cov = _ledoit_wolf_shrinkage(returns)
    vols = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    corr = cov / np.outer(vols, vols)
    np.fill_diagonal(corr, 1.0)

    # Off-diagonal correlation stats
    n_assets = len(tickers)
    if n_assets > 1:
        triu_mask = np.triu_indices(n_assets, k=1)
        off_diag = corr[triu_mask]
        corr_avg = float(np.mean(off_diag))
        corr_max = float(np.max(off_diag))
    else:
        corr_avg = 0.0
        corr_max = 0.0

    # ENB
    enb = _effective_number_of_bets(pos_values, corr) if n_assets > 0 else 0.0

    # Single-asset shortcut: skip Cholesky, use closed form for n_days=1 sanity
    horizons_out: dict = {}
    for h in horizons:
        if n_assets == 1:
            # Just simulate using single-asset variance
            rng = np.random.default_rng(seed)
            std_1d = float(np.sqrt(cov[0, 0]))
            std_h = std_1d * np.sqrt(h)
            log_ret = rng.standard_normal(n_paths) * std_h
            simple_ret = np.exp(log_ret) - 1.0
            pnl = simple_ret * pos_values[0]
        else:
            pnl = _simulate_gaussian(pos_values, cov, n_paths, h, seed)
        horizons_out[f"{h}d"] = _percentiles_to_metrics(pnl, pos_value_total)

    return {
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
        "equity": round(float(equity or pos_value_total), 2),
        "positions_value": round(pos_value_total, 2),
        "positions_count": len(tickers),
        "tickers": tickers,
        "correlation_avg": round(corr_avg, 4),
        "correlation_max": round(corr_max, 4),
        "effective_bets": round(enb, 3),
        "horizons": horizons_out,
        "method": "gaussian_cholesky",
        "n_paths": n_paths,
        "lookback_days": actual_lookback,
        "warnings": warnings,
    }


def compute_portfolio_var_regime_aware(
    positions: list,
    current_regime: str,
    lookback_days: int = 60,
    n_paths: int = 10000,
    horizons: tuple = (1, 5),
    seed: Optional[int] = None,
    equity: Optional[float] = None,
) -> dict:
    """Regime-conditional version: marginals from regime distribution, correlation from history."""
    from regime.distributions import RegimeDistributions

    warnings: list = []
    if not positions:
        return _zero_result(positions, method="regime_conditional",
                            n_paths=n_paths, lookback_days=lookback_days)

    tickers = [p["ticker"] for p in positions]
    shares = np.array([float(p.get("shares") or 0) for p in positions])
    prices = np.array([float(p.get("current_price") or p.get("entry_price") or 0) for p in positions])
    pos_values = shares * prices
    pos_value_total = float(pos_values.sum())
    if pos_value_total <= 0:
        return {**_zero_result(positions, "regime_conditional", n_paths, lookback_days),
                "warnings": ["zero_position_value"]}

    returns = _get_returns_matrix(tickers, lookback_days)
    if returns.empty:
        return {**_zero_result(positions, "regime_conditional", n_paths, lookback_days),
                "warnings": ["no_return_data"]}

    available_tickers = list(returns.columns)
    keep_idx = [i for i, t in enumerate(tickers) if t in available_tickers]
    if len(keep_idx) < len(tickers):
        excluded = [tickers[i] for i in range(len(tickers)) if i not in keep_idx]
        warnings.append(f"excluded_tickers:{','.join(excluded)}")
        tickers = [tickers[i] for i in keep_idx]
        pos_values = pos_values[keep_idx]
        pos_value_total = float(pos_values.sum())

    returns = returns[tickers]

    cov = _ledoit_wolf_shrinkage(returns)
    vols = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    corr = cov / np.outer(vols, vols)
    np.fill_diagonal(corr, 1.0)

    n_assets = len(tickers)
    if n_assets > 1:
        triu_mask = np.triu_indices(n_assets, k=1)
        off_diag = corr[triu_mask]
        corr_avg = float(np.mean(off_diag))
        corr_max = float(np.max(off_diag))
    else:
        corr_avg = 0.0
        corr_max = 0.0

    enb = _effective_number_of_bets(pos_values, corr) if n_assets > 0 else 0.0

    # Load regime distributions
    rd = RegimeDistributions()
    try:
        rd.fit(lookback_years=10)
    except Exception as e:
        logger.warning("Regime distribution fit failed: %s — falling back to gaussian", e)
        return compute_portfolio_var(positions, lookback_days, n_paths, horizons, seed, equity)

    # Check regime sample density
    stats = rd.regime_stats(current_regime)
    method_label = "regime_conditional"
    if stats.get("fallback"):
        warnings.append(f"sparse_regime:{current_regime}_using_unconditional")
        method_label = "regime_conditional_fallback"

    horizons_out: dict = {}
    for h in horizons:
        pnl = _simulate_regime_conditional(
            pos_values, cov, rd, current_regime, n_paths, h, seed,
        )
        horizons_out[f"{h}d"] = _percentiles_to_metrics(pnl, pos_value_total)

    return {
        "as_of": datetime.utcnow().strftime("%Y-%m-%d"),
        "equity": round(float(equity or pos_value_total), 2),
        "positions_value": round(pos_value_total, 2),
        "positions_count": len(tickers),
        "tickers": tickers,
        "correlation_avg": round(corr_avg, 4),
        "correlation_max": round(corr_max, 4),
        "effective_bets": round(enb, 3),
        "horizons": horizons_out,
        "method": method_label,
        "regime_state": current_regime,
        "n_paths": n_paths,
        "lookback_days": len(returns),
        "warnings": warnings,
    }


# ── Persistence ──────────────────────────────────────────────────────────────

def _ensure_table():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_risk (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                equity REAL NOT NULL,
                positions_value REAL NOT NULL,
                positions_count INTEGER NOT NULL,
                tickers TEXT NOT NULL,
                correlation_avg REAL,
                correlation_max REAL,
                effective_bets REAL,
                var_1d_95 REAL,
                var_1d_99 REAL,
                cvar_1d_95 REAL,
                cvar_1d_99 REAL,
                var_5d_95 REAL,
                var_5d_99 REAL,
                cvar_5d_95 REAL,
                cvar_5d_99 REAL,
                var_1d_95_pct REAL,
                cvar_1d_95_pct REAL,
                method TEXT,
                n_paths INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, method)
            )
        """)


def persist_portfolio_var(result: dict) -> Optional[int]:
    """Save a VaR result to the portfolio_risk table. Returns row id."""
    if not result or result.get("positions_count", 0) == 0:
        return None
    _ensure_table()
    h1 = result.get("horizons", {}).get("1d", {})
    h5 = result.get("horizons", {}).get("5d", {})
    with get_db() as db:
        cursor = db.execute("""
            INSERT OR REPLACE INTO portfolio_risk
                (as_of, equity, positions_value, positions_count, tickers,
                 correlation_avg, correlation_max, effective_bets,
                 var_1d_95, var_1d_99, cvar_1d_95, cvar_1d_99,
                 var_5d_95, var_5d_99, cvar_5d_95, cvar_5d_99,
                 var_1d_95_pct, cvar_1d_95_pct, method, n_paths)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result["as_of"],
            result["equity"],
            result["positions_value"],
            result["positions_count"],
            json.dumps(result["tickers"]),
            result.get("correlation_avg"),
            result.get("correlation_max"),
            result.get("effective_bets"),
            h1.get("var_95"), h1.get("var_99"),
            h1.get("cvar_95"), h1.get("cvar_99"),
            h5.get("var_95"), h5.get("var_99"),
            h5.get("cvar_95"), h5.get("cvar_99"),
            h1.get("var_95_pct"), h1.get("cvar_95_pct"),
            result.get("method"),
            result.get("n_paths"),
        ))
        return cursor.lastrowid


# ── CLI ──────────────────────────────────────────────────────────────────────

def _format_result(result: dict) -> str:
    lines = []
    lines.append("=" * 70)
    lines.append(f"PORTFOLIO VaR/CVaR — as of {result['as_of']}")
    lines.append("=" * 70)
    lines.append(f"Method:           {result['method']}")
    lines.append(f"Equity:           ${result['equity']:,.2f}")
    lines.append(f"Positions value:  ${result['positions_value']:,.2f}")
    lines.append(f"Positions count:  {result['positions_count']}")
    lines.append(f"Tickers:          {', '.join(result['tickers'])}")
    lines.append(f"Lookback days:    {result['lookback_days']}")
    lines.append(f"MC paths:         {result['n_paths']:,}")
    lines.append(f"Correlation avg:  {result['correlation_avg']:.3f}")
    lines.append(f"Correlation max:  {result['correlation_max']:.3f}")
    lines.append(f"Effective bets:   {result['effective_bets']:.2f}")
    if result.get("warnings"):
        lines.append(f"Warnings:         {result['warnings']}")
    lines.append("")
    lines.append(f"{'Horizon':<10} {'VaR95':>12} {'VaR99':>12} {'CVaR95':>12} {'CVaR99':>12} {'VaR95%':>10}")
    lines.append("-" * 70)
    for hkey, h in result.get("horizons", {}).items():
        lines.append(
            f"{hkey:<10} ${h['var_95']:>10.2f} ${h['var_99']:>10.2f} "
            f"${h['cvar_95']:>10.2f} ${h['cvar_99']:>10.2f} "
            f"{h['var_95_pct']*100:>9.3f}%"
        )
    lines.append("=" * 70)
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Load open positions
    open_trades = get_open_positions()
    if not open_trades:
        print("No open positions — nothing to compute.")
        return 1

    # Build position list with current prices (use latest close from ohlcv)
    positions = []
    for t in open_trades:
        ticker = t["ticker"]
        with get_db() as db:
            row = db.execute(
                "SELECT close FROM ohlcv WHERE ticker=? ORDER BY date DESC LIMIT 1",
                (ticker,),
            ).fetchone()
        current_price = float(row["close"]) if row else float(t["entry_price"])
        positions.append({
            "ticker": ticker,
            "shares": int(t["shares"]),
            "current_price": current_price,
            "entry_price": float(t["entry_price"]),
            "strategy": t.get("strategy", "unknown"),
        })

    # Try to get equity from latest equity_curve
    equity = None
    try:
        from db.atlas_db import get_latest_equity
        eq_row = get_latest_equity()
        if eq_row:
            equity = float(eq_row.get("equity") or 0)
    except Exception:
        pass

    # Run gaussian computation
    result = compute_portfolio_var(positions, n_paths=10000, seed=42, equity=equity)
    print(_format_result(result))

    # Persist
    try:
        row_id = persist_portfolio_var(result)
        print(f"\nPersisted to portfolio_risk row id={row_id}")
    except Exception as e:
        print(f"Persistence failed: {e}")
        return 1

    # Try regime-aware version too
    try:
        from db.atlas_db import get_current_regime
        cur_reg = get_current_regime()
        if cur_reg:
            regime_state = cur_reg["regime_state"]
            print(f"\nRegime-aware computation for: {regime_state}")
            result_reg = compute_portfolio_var_regime_aware(
                positions, current_regime=regime_state,
                n_paths=10000, seed=42, equity=equity,
            )
            print(_format_result(result_reg))
            persist_portfolio_var(result_reg)
    except Exception as e:
        logger.warning("Regime-aware computation failed: %s", e)

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
