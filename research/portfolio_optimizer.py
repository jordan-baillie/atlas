#!/usr/bin/env python3
"""Atlas Portfolio Optimizer — Strategy Correlation & Optimal Weighting.

Computes the NxN correlation matrix from per-strategy daily P&L streams,
applies Ledoit-Wolf shrinkage, and calculates optimal portfolio weights
using a Sharpe-ratio-tilted inverse-volatility method.

Based on:
  - Bailey & López de Prado (2013): portfolio Sharpe formula
  - Treynor-Black theorem: optimal portfolio squared Sharpe = sum of squared individual Sharpes
  - Barroso & Santa-Clara (2015): volatility scaling
  - Balvers & Wu (2006): momentum-MR correlation of -0.35

Usage:
    from research.portfolio_optimizer import PortfolioOptimizer
    opt = PortfolioOptimizer(market='sp500')
    result = opt.run()
    print(result['correlation_matrix'])
    print(result['optimal_weights'])
    print(result['portfolio_sharpe'])
"""

import copy
import json
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logger = logging.getLogger("portfolio_optimizer")

# Strategy classification for cross-group correlation analysis
STRATEGY_GROUPS = {
    "momentum": [
        "momentum_breakout", "trend_following", "adx_trend_pullback",
        "donchian_breakout", "relative_strength_pullback", "gap_and_go",
        "macd_divergence", "heikin_ashi_reversal",
    ],
    "mean_reversion": [
        "mean_reversion", "short_term_mr", "connors_rsi2", "bb_squeeze",
        "lower_band_reversion", "keltner_reversion", "rsi_divergence",
        "stochastic_oversold", "williams_percent_r", "consecutive_down_days",
        "volume_climax", "demark_sequential",
    ],
    "other": [
        "opening_gap", "sector_rotation", "mtf_momentum",
        "overnight_return", "monthly_rotation", "triple_rsi",
        "inside_bar_nr7", "vwap_reversion", "pead_earnings_drift",
        "put_call_vix_proxy", "dividend_capture",
    ],
}


def _get_group(strategy_name: str) -> str:
    """Return the group a strategy belongs to."""
    for group, members in STRATEGY_GROUPS.items():
        if strategy_name in members:
            return group
    return "other"


def cluster_strategies(
    corr_matrix: pd.DataFrame, threshold: float = 0.7
) -> List[List[str]]:
    """Group strategies by pairwise correlation exceeding threshold.

    Uses union-find agglomerative clustering: if corr(A,B) > threshold and
    corr(B,C) > threshold, then A, B, C are in the same cluster.

    Returns list of clusters, each a sorted list of strategy names, ordered
    by cluster size (largest first). Strategies not correlated above threshold
    with any other appear as singleton clusters.

    Concentration risk: callers should sum portfolio weights within each cluster;
    clusters with combined weight > 50% of the portfolio represent concentration
    risk and may warrant position limits.

    Args:
        corr_matrix: Symmetric correlation DataFrame (strategy names as index/columns).
        threshold: Correlation threshold above which strategies are grouped (default 0.7).

    Returns:
        List of clusters, each a sorted list of strategy names (largest cluster first).
    """
    strategies = list(corr_matrix.columns)
    n = len(strategies)

    if n == 0:
        return []

    # Union-Find with path compression
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # Union strategies whose pairwise correlation exceeds threshold
    for i in range(n):
        for j in range(i + 1, n):
            si, sj = strategies[i], strategies[j]
            corr_val = float(corr_matrix.loc[si, sj])
            if corr_val > threshold:
                union(i, j)

    # Collect clusters by root
    cluster_map: Dict[int, List[str]] = {}
    for i in range(n):
        root = find(i)
        cluster_map.setdefault(root, []).append(strategies[i])

    # Sort strategies within each cluster; sort clusters by size (largest first)
    clusters = [sorted(members) for members in cluster_map.values()]
    clusters.sort(key=len, reverse=True)

    # Log cluster summary
    logger.info(
        f"cluster_strategies: {len(clusters)} clusters from {n} strategies "
        f"(threshold={threshold})"
    )
    for i, cluster in enumerate(clusters):
        if len(cluster) > 1:
            logger.info(f"  Cluster {i + 1} ({len(cluster)} strategies): {cluster}")

    return clusters


def _run_solo_backtest(args: tuple) -> tuple:
    """Run a single strategy backtest in a worker process.

    Returns (strategy_name, equity_curve_dict, metrics_dict) or
    (strategy_name, None, error_str) on failure.
    """
    strategy_name, config_json, market = args

    import json as _json
    import sys as _sys
    from pathlib import Path as _Path
    _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

    from scripts.strategy_evaluator import (
        get_strategy_class, load_market_data, run_backtest,
        make_config_with_strategy,
    )
    from backtest.engine import BacktestEngine

    try:
        config = _json.loads(config_json)
        data = load_market_data(market)

        # Load best-known params if available
        best_params = {}
        best_path = _Path(__file__).resolve().parent.parent / "research" / "best" / f"{strategy_name}.json"
        if best_path.exists():
            with open(best_path) as f:
                best_data = _json.load(f)
            best_params = best_data.get("params", {})

        # Build solo config for this strategy with best-known params
        cfg = make_config_with_strategy(config, strategy_name, best_params, solo=True)

        # Run backtest — we need the BacktestResult for equity_curve
        strategies_list = []
        cls = get_strategy_class(strategy_name)
        strategies_list.append(cls(cfg))

        engine = BacktestEngine(cfg)
        result = engine.run_walkforward(data, strategies_list)

        # Extract daily equity curve as dict for pickling
        eq = result.equity_curve
        if eq is not None and len(eq) > 0:
            eq_dict = {str(d): float(v) for d, v in eq.items()}
        else:
            eq_dict = None

        # Extract key metrics
        m = result.metrics
        metrics = {
            "sharpe": m.get("sharpe", 0),
            "total_trades": m.get("total_trades", 0),
            "cagr": m.get("cagr", 0),
            "max_drawdown": m.get("max_drawdown", 0),
            "sortino": m.get("sortino", 0),
            "win_rate": m.get("win_rate", 0),
            "profit_factor": m.get("profit_factor", 0),
            "total_pnl": m.get("total_pnl", 0),
            "final_equity": m.get("final_equity", 0),
            "calmar": m.get("calmar", 0),
        }

        return (strategy_name, eq_dict, metrics)

    except Exception as e:
        return (strategy_name, None, {"error": str(e)})


class PortfolioOptimizer:
    """Compute strategy correlations and optimal portfolio weights.

    Steps:
        1. Run solo backtests for all candidate strategies (parallel)
        2. Extract daily returns from equity curves
        3. Compute correlation matrix (Pearson)
        4. Apply Ledoit-Wolf shrinkage to covariance matrix
        5. Compute optimal weights (Sharpe-tilted inverse-vol)
        6. Estimate portfolio-level Sharpe using the analytic formula
        7. Validate with a weighted combined backtest
    """

    def __init__(
        self,
        market: str = "sp500",
        strategies: Optional[List[str]] = None,
        min_sharpe: float = 0.0,
        min_trades: int = 15,
        max_weight: float = 0.25,
        min_weight: float = 0.03,
        max_workers: int = 6,
        zero_commission: bool = False,
        starting_equity: Optional[float] = None,
    ):
        self.market = market
        self.strategies = strategies  # None = auto-discover
        self.min_sharpe = min_sharpe
        self.min_trades = min_trades
        self.max_weight = max_weight
        self.min_weight = min_weight
        self.max_workers = max_workers

        # Load config
        config_path = PROJECT_ROOT / "config" / "active" / f"{market}.json"
        with open(config_path) as f:
            self.config = json.load(f)

        # Override fees for Alpaca ($0 commission) analysis
        if zero_commission:
            self.config["fees"] = {
                "commission_per_trade": 0.0,
                "commission_pct": 0.0,
                "slippage_pct": 0.0005,
                "flat_fee_threshold": 0,
                "min_position_value": 0,
                "_broker": "alpaca_zero_commission",
            }

        # Override starting equity for more realistic sizing
        if starting_equity:
            self.config["risk"]["starting_equity"] = starting_equity

    def discover_strategies(self) -> List[str]:
        """Find all strategies that can be instantiated."""
        from scripts.strategy_evaluator import get_strategy_class

        candidates = []

        # Core strategies (in strategies/ dir)
        core = [
            "mean_reversion", "trend_following", "opening_gap",
            "momentum_breakout", "bb_squeeze", "connors_rsi2",
            "short_term_mr", "mtf_momentum", "sector_rotation",
        ]

        # Sandbox strategies (in research/strategies/)
        sandbox_dir = PROJECT_ROOT / "research" / "strategies"
        sandbox = []
        if sandbox_dir.exists():
            for f in sandbox_dir.iterdir():
                if f.suffix == ".py" and f.stem not in ("__init__", "_template"):
                    sandbox.append(f.stem)

        for name in core + sandbox:
            try:
                cls = get_strategy_class(name)
                # Try instantiation
                cfg = copy.deepcopy(self.config)
                cfg.setdefault("strategies", {}).setdefault(name, {})["enabled"] = True
                instance = cls(cfg)
                candidates.append(name)
            except Exception as e:
                logger.debug(f"Skipping {name}: {e}")

        return sorted(set(candidates))

    def run_solo_backtests(self, strategies: List[str]) -> Dict[str, dict]:
        """Run solo backtests for all strategies in parallel.

        Returns dict mapping strategy_name -> {equity_curve: pd.Series, metrics: dict}
        """
        config_json = json.dumps(self.config)
        args_list = [(name, config_json, self.market) for name in strategies]

        results = {}
        n_workers = min(self.max_workers, len(strategies))
        logger.info(f"Running {len(strategies)} solo backtests on {n_workers} workers...")

        with ProcessPoolExecutor(max_workers=n_workers) as executor:
            futures = {
                executor.submit(_run_solo_backtest, args): args[0]
                for args in args_list
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    strat_name, eq_dict, metrics = future.result()
                    if eq_dict is not None and "error" not in metrics:
                        # Convert equity dict back to Series
                        eq_series = pd.Series(
                            {pd.Timestamp(k): v for k, v in eq_dict.items()}
                        ).sort_index()
                        results[strat_name] = {
                            "equity_curve": eq_series,
                            "metrics": metrics,
                        }
                        logger.info(
                            f"  {strat_name}: Sharpe={metrics['sharpe']:.4f}, "
                            f"trades={metrics['total_trades']}, "
                            f"CAGR={metrics['cagr']*100:.2f}%"
                        )
                    else:
                        error = metrics.get("error", "no equity curve")
                        logger.warning(f"  {strat_name}: FAILED — {error}")
                except Exception as e:
                    logger.error(f"  {name}: worker exception — {e}")

        return results

    def compute_daily_returns(
        self, backtest_results: Dict[str, dict]
    ) -> pd.DataFrame:
        """Extract aligned daily returns from equity curves.

        Returns DataFrame with strategy names as columns, dates as index,
        and daily returns as values. NaN-filled for dates where a strategy
        had no equity change.
        """
        returns = {}
        for name, data in backtest_results.items():
            eq = data["equity_curve"]
            if len(eq) < 10:
                logger.warning(f"  {name}: equity curve too short ({len(eq)} points), skipping")
                continue
            # Daily returns from equity curve
            daily_ret = eq.pct_change().dropna()
            # Remove zero-variance series (strategy never active)
            if daily_ret.std() < 1e-10:
                logger.warning(f"  {name}: zero variance in returns, skipping")
                continue
            returns[name] = daily_ret

        if not returns:
            return pd.DataFrame()

        # Align all series to common dates
        df = pd.DataFrame(returns)
        # Fill NaN with 0 (strategy not active on that date = 0 return)
        df = df.fillna(0.0)

        return df

    def compute_correlation_matrix(
        self, returns_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute Pearson correlation matrix from daily returns."""
        return returns_df.corr(method="pearson")

    def compute_covariance_ledoit_wolf(
        self, returns_df: pd.DataFrame
    ) -> np.ndarray:
        """Compute Ledoit-Wolf shrinkage covariance matrix.

        Falls back to sample covariance if sklearn not available.
        """
        try:
            from sklearn.covariance import LedoitWolf
            lw = LedoitWolf()
            lw.fit(returns_df.values)
            logger.info(f"  Ledoit-Wolf shrinkage coefficient: {lw.shrinkage_:.4f}")
            return lw.covariance_
        except ImportError:
            logger.warning("sklearn not available, using sample covariance")
            return returns_df.cov().values

    def compute_optimal_weights(
        self,
        returns_df: pd.DataFrame,
        metrics: Dict[str, dict],
        cov_matrix: np.ndarray,
    ) -> Dict[str, float]:
        """Compute optimal portfolio weights using Sharpe-tilted inverse-vol.

        w_i ∝ SR_i / σ_i, subject to:
          - max_weight cap (default 25%)
          - min_weight floor (default 3%)
          - strategies with SR < min_sharpe excluded
          - strategies with trades < min_trades excluded
        """
        strategies = list(returns_df.columns)
        n = len(strategies)

        # Get per-strategy Sharpe and volatility
        sharpes = np.array([metrics[s]["sharpe"] for s in strategies])
        vols = np.array([returns_df[s].std() * np.sqrt(252) for s in strategies])

        # Filter: require positive Sharpe and sufficient trades
        valid = np.ones(n, dtype=bool)
        for i, s in enumerate(strategies):
            if sharpes[i] < self.min_sharpe:
                valid[i] = False
                logger.info(f"  Excluding {s}: Sharpe {sharpes[i]:.4f} < {self.min_sharpe}")
            if metrics[s]["total_trades"] < self.min_trades:
                valid[i] = False
                logger.info(f"  Excluding {s}: {metrics[s]['total_trades']} trades < {self.min_trades}")

        if not valid.any():
            logger.error("No strategies passed filters!")
            return {s: 1.0 / n for s in strategies}  # equal weight fallback

        # Raw weights: w_i ∝ SR_i / σ_i (for valid strategies)
        raw_weights = np.zeros(n)
        for i in range(n):
            if valid[i] and vols[i] > 1e-10:
                raw_weights[i] = max(0, sharpes[i]) / vols[i]

        # Normalize
        total = raw_weights.sum()
        if total > 0:
            weights = raw_weights / total
        else:
            weights = np.ones(n) / n

        # Apply caps and floors
        weights = self._apply_constraints(weights, valid)

        return {strategies[i]: round(float(weights[i]), 4) for i in range(n)}

    def _apply_constraints(
        self, weights: np.ndarray, valid: np.ndarray
    ) -> np.ndarray:
        """Apply max/min weight constraints with iterative redistribution.
        
        Strategies below min_weight are bumped UP to min_weight (not zeroed)
        to preserve diversification. Only truly zero-weight strategies are excluded.
        """
        n = len(weights)
        # Zero out invalid strategies
        weights[~valid] = 0.0

        for _ in range(20):  # iterative capping
            changed = False

            # Cap max
            excess = 0.0
            for i in range(n):
                if weights[i] > self.max_weight:
                    excess += weights[i] - self.max_weight
                    weights[i] = self.max_weight
                    changed = True

            # Redistribute excess proportionally to uncapped strategies
            if excess > 0:
                uncapped = (weights > 0) & (weights < self.max_weight) & valid
                if uncapped.any():
                    weights[uncapped] += excess * weights[uncapped] / weights[uncapped].sum()

            # Bump up strategies below min_weight (don't zero them)
            for i in range(n):
                if 0 < weights[i] < self.min_weight:
                    weights[i] = self.min_weight
                    changed = True

            # Re-normalize
            total = weights.sum()
            if total > 0:
                weights = weights / total

            if not changed:
                break

        return weights

    def compute_optimal_weights_mv(
        self,
        returns_df: pd.DataFrame,
        metrics: Dict[str, dict],
        cov_matrix: np.ndarray,
    ) -> Dict[str, float]:
        """Mean-variance optimization: maximize portfolio Sharpe ratio.

        Solves the following constrained optimization via SLSQP:

            max  (w'μ) / sqrt(w'Σw)
            s.t. sum(w) = 1.0
                 min_weight ≤ w_i ≤ max_weight  ∀i

        where:
            μ = per-strategy mean daily returns (from equity curve history)
            Σ = Ledoit-Wolf shrinkage covariance matrix (daily scale)

        Weight bounds are read from config.portfolio_optimizer (defaults: 0.05 / 0.40).
        Falls back to compute_optimal_weights() if SLSQP fails to converge or if
        scipy is unavailable.

        Args:
            returns_df: Aligned daily returns DataFrame (strategies as columns).
            metrics: Per-strategy metrics dict (must contain 'sharpe', 'total_trades').
            cov_matrix: Ledoit-Wolf covariance matrix (n×n, daily scale).

        Returns:
            Dict mapping strategy_name → weight (values sum ≈ 1.0).
        """
        try:
            from scipy.optimize import minimize
        except ImportError:
            logger.warning("scipy not available — falling back to Sharpe-tilted inverse-vol")
            return self.compute_optimal_weights(returns_df, metrics, cov_matrix)

        strategies = list(returns_df.columns)
        n = len(strategies)

        # μ: per-strategy daily mean returns from equity curves
        mu = returns_df.mean().values  # shape (n,)

        # Weight bounds from config, with instance-level fallback
        opt_cfg = self.config.get("portfolio_optimizer", {})
        min_w = float(opt_cfg.get("min_weight", self.min_weight))
        max_w = float(opt_cfg.get("max_weight", self.max_weight))

        # Objective: negative Sharpe ratio (SLSQP minimises)
        def neg_sharpe(w: np.ndarray) -> float:
            port_ret = float(w @ mu)
            port_var = float(w @ cov_matrix @ w)
            port_vol = np.sqrt(max(port_var, 1e-12))
            return -port_ret / port_vol

        # Analytic gradient for faster convergence
        def neg_sharpe_grad(w: np.ndarray) -> np.ndarray:
            port_ret = float(w @ mu)
            port_var = float(w @ cov_matrix @ w)
            port_vol = np.sqrt(max(port_var, 1e-12))
            # d/dw [ w'μ / sqrt(w'Σw) ] = μ/vol - (w'μ)·(Σw)/vol³
            grad = mu / port_vol - port_ret * (cov_matrix @ w) / (port_vol ** 3)
            return -grad  # negate: we minimise

        constraints = [{"type": "eq", "fun": lambda w: w.sum() - 1.0}]
        bounds = [(min_w, max_w)] * n

        # Feasible equal-weight starting point
        w0 = np.clip(np.ones(n) / n, min_w, max_w)
        w0 = w0 / w0.sum()

        try:
            res = minimize(
                neg_sharpe,
                w0,
                method="SLSQP",
                jac=neg_sharpe_grad,
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 1000, "ftol": 1e-9, "disp": False},
            )

            if res.success:
                w = np.clip(res.x, min_w, max_w)
                w = w / w.sum()
                logger.info(
                    f"  MV optimization converged (nit={res.nit}): "
                    f"annualised Sharpe≈{-res.fun * np.sqrt(252):.4f}"
                )
                return {strategies[i]: round(float(w[i]), 4) for i in range(n)}
            else:
                logger.warning(
                    f"  MV optimization did not converge (status={res.status}): "
                    f"{res.message} — falling back to Sharpe-tilted inverse-vol"
                )
                return self.compute_optimal_weights(returns_df, metrics, cov_matrix)

        except Exception as exc:
            logger.warning(f"  MV optimization raised exception: {exc} — falling back")
            return self.compute_optimal_weights(returns_df, metrics, cov_matrix)

    def estimate_portfolio_sharpe(
        self,
        weights: Dict[str, float],
        returns_df: pd.DataFrame,
        cov_matrix: np.ndarray,
        metrics: Dict[str, dict],
    ) -> dict:
        """Estimate portfolio Sharpe using the analytic formula and simulation.

        Returns dict with:
          - analytic_sharpe: from the Bailey & López de Prado formula
          - simulated_sharpe: from actual weighted return stream
          - n_strategies: number of strategies with non-zero weight
          - avg_correlation: average pairwise correlation
        """
        strategies = list(returns_df.columns)
        w = np.array([weights.get(s, 0.0) for s in strategies])

        # Simulated: weighted daily returns
        portfolio_returns = (returns_df * w).sum(axis=1)
        sim_sharpe = 0.0
        if portfolio_returns.std() > 0:
            sim_sharpe = float(
                portfolio_returns.mean() / portfolio_returns.std() * np.sqrt(252)
            )

        # Analytic: Bailey & López de Prado formula
        # Portfolio SR = SR̄ × √(N / (1 + (N-1) × ρ̄))
        active = w > 0
        n_active = int(active.sum())
        if n_active > 0:
            active_sharpes = np.array([metrics[s]["sharpe"] for s, a in zip(strategies, active) if a])
            avg_sr = float(active_sharpes.mean())

            # Average pairwise correlation among active strategies
            corr = returns_df[
                [s for s, a in zip(strategies, active) if a]
            ].corr().values
            mask = np.triu(np.ones_like(corr, dtype=bool), k=1)
            avg_corr = float(corr[mask].mean()) if mask.sum() > 0 else 0.0

            if 1 + (n_active - 1) * avg_corr > 0:
                analytic_sharpe = avg_sr * np.sqrt(
                    n_active / (1 + (n_active - 1) * avg_corr)
                )
            else:
                analytic_sharpe = avg_sr
        else:
            analytic_sharpe = 0.0
            avg_corr = 0.0

        # Portfolio volatility and return
        port_vol = float(portfolio_returns.std() * np.sqrt(252))
        port_ret = float(portfolio_returns.mean() * 252)
        port_dd = float(
            (portfolio_returns.cumsum() - portfolio_returns.cumsum().cummax()).min()
        )

        return {
            "analytic_sharpe": round(analytic_sharpe, 4),
            "simulated_sharpe": round(sim_sharpe, 4),
            "n_strategies": n_active,
            "avg_correlation": round(avg_corr, 4),
            "portfolio_annual_return": round(port_ret * 100, 2),
            "portfolio_annual_vol": round(port_vol * 100, 2),
            "portfolio_max_drawdown": round(port_dd * 100, 2),
        }

    def analyze_group_correlations(
        self, corr_matrix: pd.DataFrame
    ) -> Dict[str, Any]:
        """Analyze within-group and cross-group correlations.

        Returns summary of momentum vs MR correlation (key hypothesis:
        should be < 0.20, Balvers & Wu predict -0.35).
        """
        strategies = list(corr_matrix.columns)
        groups = {s: _get_group(s) for s in strategies}

        # Within-group correlations
        within = {}
        for group_name in ("momentum", "mean_reversion", "other"):
            members = [s for s in strategies if groups[s] == group_name]
            if len(members) >= 2:
                sub_corr = corr_matrix.loc[members, members].values
                mask = np.triu(np.ones_like(sub_corr, dtype=bool), k=1)
                if mask.sum() > 0:
                    within[group_name] = {
                        "avg_correlation": round(float(sub_corr[mask].mean()), 4),
                        "min_correlation": round(float(sub_corr[mask].min()), 4),
                        "max_correlation": round(float(sub_corr[mask].max()), 4),
                        "n_strategies": len(members),
                    }

        # Cross-group: momentum vs mean_reversion
        mom = [s for s in strategies if groups[s] == "momentum"]
        mr = [s for s in strategies if groups[s] == "mean_reversion"]
        cross_mom_mr = {}
        if mom and mr:
            cross_vals = corr_matrix.loc[mom, mr].values.flatten()
            cross_mom_mr = {
                "avg_correlation": round(float(cross_vals.mean()), 4),
                "min_correlation": round(float(cross_vals.min()), 4),
                "max_correlation": round(float(cross_vals.max()), 4),
                "n_pairs": len(cross_vals),
                "hypothesis_validated": float(cross_vals.mean()) < 0.20,
            }

        return {
            "within_group": within,
            "cross_momentum_mr": cross_mom_mr,
            "strategy_groups": {s: groups[s] for s in strategies},
        }

    def run(self) -> Dict[str, Any]:
        """Execute the full portfolio optimization pipeline.

        Returns comprehensive result dict with:
          - correlation_matrix
          - optimal_weights
          - portfolio_metrics
          - group_analysis
          - per_strategy_metrics
          - all raw data for further analysis
        """
        # Step 0: Discover strategies
        if self.strategies:
            strategies = self.strategies
        else:
            strategies = self.discover_strategies()
        logger.info(f"Analyzing {len(strategies)} strategies: {strategies}")

        # Step 1: Run solo backtests (parallel)
        bt_results = self.run_solo_backtests(strategies)
        logger.info(f"Completed {len(bt_results)}/{len(strategies)} backtests")

        if len(bt_results) < 2:
            return {"error": "Need at least 2 strategies with valid backtests"}

        # Step 2: Extract daily returns
        returns_df = self.compute_daily_returns(bt_results)
        if returns_df.empty:
            return {"error": "No valid return series extracted"}
        logger.info(
            f"Returns matrix: {returns_df.shape[1]} strategies × {returns_df.shape[0]} days"
        )

        # Collect metrics for included strategies
        metrics = {
            name: bt_results[name]["metrics"]
            for name in returns_df.columns
        }

        # Step 3: Correlation matrix
        corr_matrix = self.compute_correlation_matrix(returns_df)
        logger.info("Correlation matrix computed")

        # Step 4: Ledoit-Wolf covariance
        cov_matrix = self.compute_covariance_ledoit_wolf(returns_df)
        logger.info("Covariance matrix (Ledoit-Wolf) computed")

        # Step 5: Optimal weights — method selectable via config
        method = self.config.get("portfolio_optimizer", {}).get(
            "method", "sharpe_inverse_vol"
        )
        if method == "mean_variance":
            logger.info("Using mean-variance optimization (SLSQP Sharpe maximization)")
            weights = self.compute_optimal_weights_mv(returns_df, metrics, cov_matrix)
        else:
            logger.info("Using Sharpe-tilted inverse-vol weighting")
            weights = self.compute_optimal_weights(returns_df, metrics, cov_matrix)
        active_weights = {k: v for k, v in weights.items() if v > 0}
        logger.info(f"Optimal weights: {active_weights}")

        # Step 6: Portfolio Sharpe estimate
        portfolio_metrics = self.estimate_portfolio_sharpe(
            weights, returns_df, cov_matrix, metrics
        )
        logger.info(
            f"Portfolio Sharpe: analytic={portfolio_metrics['analytic_sharpe']:.4f}, "
            f"simulated={portfolio_metrics['simulated_sharpe']:.4f}"
        )

        # Step 7: Group correlation analysis
        group_analysis = self.analyze_group_correlations(corr_matrix)

        # Step 8: Correlation-based cluster analysis
        cluster_threshold = self.config.get("portfolio_optimizer", {}).get(
            "cluster_threshold", 0.7
        )
        clusters = cluster_strategies(corr_matrix, threshold=cluster_threshold)
        logger.info(f"Strategy clusters (threshold={cluster_threshold}): {clusters}")

        # Build per-strategy summary
        per_strategy = {}
        for name in returns_df.columns:
            m = metrics[name]
            per_strategy[name] = {
                "sharpe": m["sharpe"],
                "total_trades": m["total_trades"],
                "cagr_pct": round(m["cagr"] * 100, 2),
                "max_drawdown_pct": round(m["max_drawdown"] * 100, 2),
                "weight": weights.get(name, 0.0),
                "group": _get_group(name),
                "annual_vol_pct": round(
                    float(returns_df[name].std() * np.sqrt(252) * 100), 2
                ),
            }

        return {
            "correlation_matrix": corr_matrix.round(4).to_dict(),
            "optimal_weights": weights,
            "active_weights": active_weights,
            "portfolio_metrics": portfolio_metrics,
            "group_analysis": group_analysis,
            "clusters": clusters,
            "per_strategy": per_strategy,
            "n_strategies_analyzed": len(returns_df.columns),
            "n_strategies_active": len(active_weights),
            "returns_df": returns_df,  # raw data for further analysis
            "cov_matrix": cov_matrix,
        }


def write_vault_notes(result: Dict[str, Any], vault_dir: Path) -> None:
    """Write analysis results to vault markdown files."""
    portfolio_dir = vault_dir / "Portfolio"
    portfolio_dir.mkdir(parents=True, exist_ok=True)

    # 1. Correlation Matrix note
    corr = result.get("correlation_matrix", {})
    strategies = sorted(corr.keys()) if corr else []

    lines = [
        "# Strategy Correlation Matrix",
        "",
        f"> Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
        f"> Strategies analyzed: {result.get('n_strategies_analyzed', 0)}",
        "",
        "## Correlation Matrix (Pearson, daily returns)",
        "",
    ]

    if strategies:
        # Header
        header = "| | " + " | ".join(s[:12] for s in strategies) + " |"
        separator = "|---|" + "|".join("---:" for _ in strategies) + "|"
        lines.extend([header, separator])

        for s1 in strategies:
            row = f"| **{s1[:12]}** |"
            for s2 in strategies:
                val = corr.get(s1, {}).get(s2, 0)
                row += f" {val:.2f} |"
            lines.append(row)

    lines.append("")

    # Group analysis
    ga = result.get("group_analysis", {})
    if ga.get("within_group"):
        lines.append("## Within-Group Correlations")
        lines.append("")
        for group, stats in ga["within_group"].items():
            lines.append(
                f"- **{group}** ({stats['n_strategies']} strategies): "
                f"avg={stats['avg_correlation']:.3f}, "
                f"range=[{stats['min_correlation']:.3f}, {stats['max_correlation']:.3f}]"
            )
        lines.append("")

    if ga.get("cross_momentum_mr"):
        cm = ga["cross_momentum_mr"]
        lines.append("## Cross-Group: Momentum vs Mean Reversion")
        lines.append("")
        lines.append(
            f"- Average correlation: **{cm['avg_correlation']:.3f}** "
            f"(hypothesis: < 0.20, Balvers & Wu predict -0.35)"
        )
        lines.append(
            f"- Range: [{cm['min_correlation']:.3f}, {cm['max_correlation']:.3f}] "
            f"across {cm['n_pairs']} pairs"
        )
        validated = "✅ VALIDATED" if cm.get("hypothesis_validated") else "❌ NOT VALIDATED"
        lines.append(f"- Low cross-group correlation: {validated}")
        lines.append("")

    (portfolio_dir / "Correlation Matrix.md").write_text("\n".join(lines))
    logger.info(f"Wrote {portfolio_dir / 'Correlation Matrix.md'}")

    # 2. Allocation Analysis note
    weights = result.get("optimal_weights", {})
    active = result.get("active_weights", {})
    pm = result.get("portfolio_metrics", {})
    ps = result.get("per_strategy", {})

    lines = [
        "# Portfolio Allocation Analysis",
        "",
        f"> Generated: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Portfolio Metrics",
        "",
        f"- **Analytic Sharpe**: {pm.get('analytic_sharpe', 0):.4f}",
        f"- **Simulated Sharpe**: {pm.get('simulated_sharpe', 0):.4f}",
        f"- **Strategies active**: {pm.get('n_strategies', 0)}",
        f"- **Average correlation**: {pm.get('avg_correlation', 0):.4f}",
        f"- **Annual return**: {pm.get('portfolio_annual_return', 0):.1f}%",
        f"- **Annual volatility**: {pm.get('portfolio_annual_vol', 0):.1f}%",
        f"- **Max drawdown**: {pm.get('portfolio_max_drawdown', 0):.1f}%",
        "",
        "## Optimal Weights (Sharpe-tilted inverse-vol)",
        "",
        "| Strategy | Weight | Sharpe | Trades | CAGR% | Group |",
        "|----------|--------|--------|--------|-------|-------|",
    ]

    # Sort by weight descending
    for name in sorted(active, key=lambda x: active[x], reverse=True):
        info = ps.get(name, {})
        lines.append(
            f"| {name} | {active[name]*100:.1f}% | "
            f"{info.get('sharpe', 0):.3f} | "
            f"{info.get('total_trades', 0)} | "
            f"{info.get('cagr_pct', 0):.1f}% | "
            f"{info.get('group', '?')} |"
        )

    lines.append("")
    lines.append("## Excluded Strategies (weight = 0)")
    lines.append("")
    excluded = {k: v for k, v in weights.items() if v == 0}
    for name in sorted(excluded):
        info = ps.get(name, {})
        reason = "low Sharpe" if info.get("sharpe", 0) < 0.0 else "filtered"
        if info.get("total_trades", 0) < 15:
            reason = f"too few trades ({info.get('total_trades', 0)})"
        lines.append(
            f"- **{name}**: Sharpe={info.get('sharpe', 0):.3f}, "
            f"trades={info.get('total_trades', 0)} — {reason}"
        )

    lines.extend([
        "",
        "## Method",
        "",
        "Weights computed as w_i ∝ SR_i / σ_i (Sharpe-ratio-tilted inverse-volatility),",
        "with Ledoit-Wolf shrinkage on the covariance matrix.",
        f"Constraints: max {result.get('_max_weight', 25)}% per strategy, "
        f"min {result.get('_min_weight', 3)}%.",
        "",
        "References: Bailey & López de Prado (2013), Treynor-Black theorem",
    ])

    (portfolio_dir / "Allocation Analysis.md").write_text("\n".join(lines))
    logger.info(f"Wrote {portfolio_dir / 'Allocation Analysis.md'}")


def main():
    """CLI entry point for portfolio optimization."""
    import argparse

    parser = argparse.ArgumentParser(description="Atlas Portfolio Optimizer")
    parser.add_argument("--market", default="sp500", help="Market ID")
    parser.add_argument(
        "--strategies", nargs="*", default=None,
        help="Strategy names (default: auto-discover all)"
    )
    parser.add_argument(
        "--min-sharpe", type=float, default=0.0,
        help="Minimum Sharpe to include in portfolio (default: 0.0)"
    )
    parser.add_argument(
        "--min-trades", type=int, default=15,
        help="Minimum trades to include (default: 15)"
    )
    parser.add_argument(
        "--max-weight", type=float, default=0.25,
        help="Maximum weight per strategy (default: 0.25)"
    )
    parser.add_argument(
        "--workers", type=int, default=6,
        help="Max parallel workers (default: 6)"
    )
    parser.add_argument(
        "--zero-commission", action="store_true",
        help="Use $0 commission (Alpaca mode)"
    )
    parser.add_argument(
        "--equity", type=float, default=None,
        help="Override starting equity (default: from config)"
    )
    parser.add_argument(
        "--output", default=None,
        help="Output JSON path (default: research/results/portfolio_optimization.json)"
    )
    parser.add_argument(
        "--vault", action="store_true",
        help="Write results to research vault"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress output"
    )
    args = parser.parse_args()

    level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    opt = PortfolioOptimizer(
        market=args.market,
        strategies=args.strategies,
        min_sharpe=args.min_sharpe,
        min_trades=args.min_trades,
        max_weight=args.max_weight,
        max_workers=args.workers,
        zero_commission=args.zero_commission,
        starting_equity=args.equity,
    )

    result = opt.run()

    if "error" in result:
        print(f"ERROR: {result['error']}")
        sys.exit(1)

    # Print summary
    pm = result["portfolio_metrics"]
    print(f"\n{'='*60}")
    print(f"PORTFOLIO OPTIMIZATION RESULTS")
    print(f"{'='*60}")
    print(f"Strategies analyzed:  {result['n_strategies_analyzed']}")
    print(f"Strategies active:    {result['n_strategies_active']}")
    print(f"Analytic Sharpe:      {pm['analytic_sharpe']:.4f}")
    print(f"Simulated Sharpe:     {pm['simulated_sharpe']:.4f}")
    print(f"Avg correlation:      {pm['avg_correlation']:.4f}")
    print(f"Annual return:        {pm['portfolio_annual_return']:.1f}%")
    print(f"Annual volatility:    {pm['portfolio_annual_vol']:.1f}%")
    print(f"Max drawdown:         {pm['portfolio_max_drawdown']:.1f}%")

    print(f"\n{'='*60}")
    print("OPTIMAL WEIGHTS")
    print(f"{'='*60}")
    for name, weight in sorted(
        result["active_weights"].items(), key=lambda x: x[1], reverse=True
    ):
        info = result["per_strategy"].get(name, {})
        print(
            f"  {name:30s} {weight*100:5.1f}%  "
            f"(SR={info.get('sharpe', 0):.3f}, trades={info.get('total_trades', 0)})"
        )

    # Group analysis
    ga = result.get("group_analysis", {})
    if ga.get("cross_momentum_mr"):
        cm = ga["cross_momentum_mr"]
        print(f"\n{'='*60}")
        print("MOMENTUM vs MEAN REVERSION CORRELATION")
        print(f"{'='*60}")
        print(f"  Average: {cm['avg_correlation']:.4f}")
        print(f"  Range:   [{cm['min_correlation']:.4f}, {cm['max_correlation']:.4f}]")
        validated = "✅ YES" if cm.get("hypothesis_validated") else "❌ NO"
        print(f"  Hypothesis (< 0.20): {validated}")

    # Save JSON
    output_path = args.output or str(
        PROJECT_ROOT / "research" / "results" / "portfolio_optimization.json"
    )
    serializable = {
        k: v for k, v in result.items()
        if k not in ("returns_df", "cov_matrix")
    }
    # Convert correlation matrix and weights to serializable format
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(serializable, f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")

    # Write vault notes
    if args.vault:
        vault_dir = PROJECT_ROOT / "research" / "brain"
        write_vault_notes(result, vault_dir)
        print(f"Vault notes written to {vault_dir / 'Portfolio'}")


def update_live_config_weights(
    result: Dict[str, Any],
    market: str = "sp500",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Update live config strategy weights from portfolio optimizer results.

    Only updates weights for strategies that are currently enabled in the config.
    Does NOT enable/disable strategies — that's a manual decision.

    Also updates allocation pool position counts proportionally.

    Args:
        result: Portfolio optimization result dict.
        market: Market ID.
        dry_run: If True, return changes without writing.

    Returns:
        Dict with 'updated', 'changes', 'new_version' keys.
    """
    from utils.config import get_active_config, save_config_version

    config = get_active_config(market)
    optimal_weights = result.get("active_weights", {})
    if not optimal_weights:
        return {"updated": False, "reason": "No active weights in optimizer result"}

    changes = []
    for strat_name, strat_cfg in config.get("strategies", {}).items():
        if not strat_cfg.get("enabled"):
            continue
        old_w = strat_cfg.get("weight", 0)
        new_w = optimal_weights.get(strat_name, 0)
        if new_w > 0 and abs(old_w - new_w) > 0.01:  # >1% change threshold
            changes.append({
                "strategy": strat_name,
                "old_weight": round(old_w, 4),
                "new_weight": round(new_w, 4),
            })
            strat_cfg["weight"] = round(new_w, 4)

    if not changes:
        return {"updated": False, "reason": "No significant weight changes", "changes": []}

    # Update allocation pool position counts proportionally
    alloc = config.get("allocation", {})
    if alloc.get("enabled"):
        max_pos = config.get("risk", {}).get("max_open_positions", 10)
        pools = alloc.get("pools", {})
        for strat_name in list(pools.keys()):
            if strat_name == "_other":
                continue
            w = config.get("strategies", {}).get(strat_name, {}).get("weight", 0)
            if w > 0:
                pools[strat_name]["max_positions"] = max(1, round(w * max_pos))
                pools[strat_name]["weight"] = round(w, 4)

    if dry_run:
        return {"updated": False, "reason": "dry_run", "changes": changes}

    # Version bump
    old_ver = config.get("version", "v3.0")
    # Micro-version: v3.0 -> v3.0.1, v3.0.1 -> v3.0.2
    parts = old_ver.replace("v", "").split(".")
    if len(parts) == 2:
        new_ver = f"v{parts[0]}.{parts[1]}.1"
    elif len(parts) == 3:
        new_ver = f"v{parts[0]}.{parts[1]}.{int(parts[2])+1}"
    else:
        new_ver = old_ver + ".1"

    config["version"] = new_ver
    config["_weight_update"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio_sharpe": result.get("portfolio_metrics", {}).get("simulated_sharpe"),
        "changes": changes,
    }

    save_config_version(config, version=new_ver, market_id=market)
    logger.info("Config weights updated: %s → %s (%d changes)", old_ver, new_ver, len(changes))

    return {"updated": True, "changes": changes, "new_version": new_ver}


if __name__ == "__main__":
    main()
