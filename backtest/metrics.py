"""
Atlas Backtest Metrics
===========================
Performance metrics for evaluating trading strategy backtests.

All functions are pure — they take data in and return numbers.
No side effects, no state.

Usage:
    from backtest.metrics import (
        calc_cagr, calc_cagr_full_period, calc_max_drawdown, calc_sharpe, calc_sortino,
        calc_win_rate, calc_profit_factor, calc_avg_trade,
        calc_exposure, calc_turnover, calc_all_metrics,
        calc_r_multiples, calc_expectancy_r, calc_edge_ttest,
        calc_var, calc_cvar, calc_calmar,
        calc_strategy_correlation, calc_monte_carlo_drawdown,
    )
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from scipy import stats

logger = logging.getLogger(__name__)

# Default risk-free rate (overridden by market profile in practice)
# Fallback risk-free rate. Callers should pass market-specific rf from
# MarketProfile.risk_free_rate (SP500=0.05, ASX=0.04, HK=0.04).
DEFAULT_RF = 0.045  # Midpoint — nudges callers to pass explicit rf


def calc_cagr(equity_curve: pd.Series) -> float:
    """Calculate Compound Annual Growth Rate from an equity curve.

    Args:
        equity_curve: Series indexed by date with portfolio values.

    Returns:
        CAGR as a decimal (e.g., 0.12 = 12% per year).
        Returns 0.0 if insufficient data.
    """
    if equity_curve is None or len(equity_curve) < 2:
        return 0.0

    start_val = equity_curve.iloc[0]
    end_val = equity_curve.iloc[-1]

    if start_val <= 0:
        return 0.0

    # Calculate years from index
    if isinstance(equity_curve.index, pd.DatetimeIndex):
        days = (equity_curve.index[-1] - equity_curve.index[0]).days
    else:
        days = len(equity_curve)

    if days <= 0:
        return 0.0

    years = days / 365.25
    if years <= 0:
        return 0.0

    total_return = end_val / start_val
    if total_return <= 0:
        return -1.0  # total loss

    cagr = total_return ** (1.0 / years) - 1.0
    return round(cagr, 6)


def calc_cagr_full_period(equity_curve: pd.Series, data_start_date, data_end_date) -> float:
    """Calculate CAGR using the full data window as time denominator.

    The standard calc_cagr() uses equity curve dates, which start after the
    training window in walk-forward. This inflates CAGR because the denominator
    is shorter than the actual data period.

    Example: 3 years of data, 1 year training → equity curve covers 2 years.
    Standard CAGR: (final/start)^(1/2) - 1 = inflated
    Full-period CAGR: (final/start)^(1/3) - 1 = realistic

    Args:
        equity_curve: Series indexed by date with portfolio values.
        data_start_date: Start date of the full data window (including training).
        data_end_date: End date of the full data window.

    Returns:
        CAGR as a decimal (e.g., 0.12 = 12% per year).
    """
    if equity_curve is None or len(equity_curve) < 2:
        return 0.0

    start_val = equity_curve.iloc[0]
    end_val = equity_curve.iloc[-1]

    if start_val <= 0:
        return 0.0

    # Use the full data window as the time denominator
    try:
        days = (pd.Timestamp(data_end_date) - pd.Timestamp(data_start_date)).days
    except Exception:
        return calc_cagr(equity_curve)

    if days <= 0:
        return calc_cagr(equity_curve)

    years = days / 365.25
    if years <= 0:
        return 0.0

    total_return = end_val / start_val
    if total_return <= 0:
        return -1.0  # total loss

    cagr = total_return ** (1.0 / years) - 1.0
    return round(cagr, 6)


def calc_max_drawdown(equity_curve: pd.Series) -> float:
    """Calculate maximum drawdown from an equity curve.

    Args:
        equity_curve: Series indexed by date with portfolio values.

    Returns:
        Maximum drawdown as a positive decimal (e.g., 0.15 = 15% drawdown).
        Returns 0.0 if insufficient data.
    """
    if equity_curve is None or len(equity_curve) < 2:
        return 0.0

    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    max_dd = drawdown.min()

    return round(abs(max_dd), 6)


def calc_drawdown_series(equity_curve: pd.Series) -> pd.Series:
    """Calculate the full drawdown series.

    Args:
        equity_curve: Series indexed by date with portfolio values.

    Returns:
        Series of drawdown values (negative = in drawdown).
    """
    if equity_curve is None or len(equity_curve) < 2:
        return pd.Series(dtype=float)

    running_max = equity_curve.cummax()
    drawdown = (equity_curve - running_max) / running_max
    return drawdown


def calc_sharpe(returns: pd.Series, rf: float = DEFAULT_RF) -> float:
    """Calculate annualized Sharpe ratio.

    Args:
        returns: Series of daily returns (decimal, not percentage).
        rf: Annual risk-free rate (default 0.04 = 4% for AUD).

    Returns:
        Annualized Sharpe ratio. Returns 0.0 if insufficient data.
    """
    if returns is None or len(returns) < 10:
        return 0.0

    # Clean NaN/inf
    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < 10:
        return 0.0

    daily_rf = (1 + rf) ** (1 / 252) - 1
    excess_returns = returns - daily_rf

    mean_excess = excess_returns.mean()
    std_excess = excess_returns.std(ddof=1)

    if std_excess == 0 or np.isnan(std_excess):
        return 0.0

    sharpe = (mean_excess / std_excess) * np.sqrt(252)
    return round(sharpe, 4)


def calc_sortino(returns: pd.Series, rf: float = DEFAULT_RF) -> float:
    """Calculate annualized Sortino ratio.

    Like Sharpe but only penalizes downside volatility.

    Args:
        returns: Series of daily returns (decimal).
        rf: Annual risk-free rate (default 0.04 = 4% for AUD).

    Returns:
        Annualized Sortino ratio. Returns 0.0 if insufficient data.
    """
    if returns is None or len(returns) < 10:
        return 0.0

    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < 10:
        return 0.0

    daily_rf = (1 + rf) ** (1 / 252) - 1
    excess_returns = returns - daily_rf

    mean_excess = excess_returns.mean()

    # Downside deviation: std of negative excess returns only
    downside = excess_returns[excess_returns < 0]
    if len(downside) < 2:
        return 0.0 if mean_excess <= 0 else 10.0  # cap at 10 if no downside

    downside_std = downside.std(ddof=1)
    if downside_std == 0 or np.isnan(downside_std):
        return 0.0

    sortino = (mean_excess / downside_std) * np.sqrt(252)
    return round(sortino, 4)


def calc_win_rate(trades: List[Dict[str, Any]]) -> float:
    """Calculate win rate from a list of trades.

    Args:
        trades: List of trade dicts, each must have 'pnl' key.

    Returns:
        Win rate as a decimal (e.g., 0.55 = 55%). Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    winners = sum(1 for t in trades if t.get("pnl", 0) > 0)
    return round(winners / len(trades), 4)


def calc_profit_factor(trades: List[Dict[str, Any]]) -> float:
    """Calculate profit factor (gross profits / gross losses).

    Args:
        trades: List of trade dicts, each must have 'pnl' key.

    Returns:
        Profit factor. Returns 0.0 if no trades or no losses.
        Values > 1.0 indicate profitable system.
    """
    if not trades:
        return 0.0

    gross_profit = sum(t["pnl"] for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t["pnl"] for t in trades if t.get("pnl", 0) < 0))

    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0

    return round(gross_profit / gross_loss, 4)


def calc_avg_trade(trades: List[Dict[str, Any]]) -> float:
    """Calculate average P&L per trade.

    Args:
        trades: List of trade dicts, each must have 'pnl' key.

    Returns:
        Average trade P&L in AUD. Returns 0.0 if no trades.
    """
    if not trades:
        return 0.0

    total_pnl = sum(t.get("pnl", 0) for t in trades)
    return round(total_pnl / len(trades), 2)


def calc_exposure(
    equity_curve: pd.Series,
    positions_log: List[Dict[str, Any]],
) -> float:
    """Calculate time-weighted market exposure.

    Exposure = fraction of trading days where at least one position was open.

    Args:
        equity_curve: Series indexed by date with portfolio values.
        positions_log: List of dicts with 'entry_date' and 'exit_date' keys.

    Returns:
        Exposure as a decimal (e.g., 0.40 = 40% of time in market).
    """
    if equity_curve is None or len(equity_curve) < 2 or not positions_log:
        return 0.0

    total_days = len(equity_curve)
    if total_days == 0:
        return 0.0

    # Build a set of dates where positions were open
    invested_dates = set()
    for pos in positions_log:
        entry = pd.Timestamp(pos.get("entry_date"))
        exit_dt = pd.Timestamp(pos.get("exit_date"))
        # Find all equity curve dates between entry and exit
        mask = (equity_curve.index >= entry) & (equity_curve.index <= exit_dt)
        invested_dates.update(equity_curve.index[mask])

    exposure = len(invested_dates) / total_days
    return round(exposure, 4)


def calc_turnover(
    trades: List[Dict[str, Any]],
    avg_equity: float,
) -> float:
    """Calculate portfolio turnover.

    Turnover = total value traded / average equity, annualized.

    Args:
        trades: List of trade dicts with 'position_value' key.
        avg_equity: Average portfolio equity over the period.

    Returns:
        Annualized turnover ratio. Returns 0.0 if no trades or zero equity.
    """
    if not trades or avg_equity <= 0:
        return 0.0

    # Sum of all entry + exit values
    total_traded = sum(t.get("position_value", 0) for t in trades) * 2  # round trip
    turnover = total_traded / avg_equity
    return round(turnover, 4)


def calc_r_multiple(trade: Dict[str, Any]) -> Optional[float]:
    """Calculate R-multiple for a single trade.

    R-multiple = actual P&L / initial risk (1R).
    1R = |entry_price - stop_price| * shares.

    A trade that hits its stop exactly returns -1.0R.
    A trade that makes 2× its initial risk returns +2.0R.

    Args:
        trade: Trade dict with entry_price, stop_price, shares, pnl.

    Returns:
        R-multiple as a float, or None if stop_price is missing/invalid.
    """
    entry = trade.get("entry_price", 0)
    stop = trade.get("stop_price", 0)
    pnl = trade.get("pnl", 0)
    shares = trade.get("shares", 0)

    if not entry or not stop or not shares:
        return None

    initial_risk = abs(entry - stop) * shares
    if initial_risk <= 0:
        return None

    return round(pnl / initial_risk, 4)


def calc_r_multiples(trades: List[Dict[str, Any]]) -> List[float]:
    """Calculate R-multiples for all trades that have valid stop prices.

    Args:
        trades: List of trade dicts.

    Returns:
        List of R-multiple values (only for trades with valid stops).
    """
    r_mults = []
    for t in trades:
        r = calc_r_multiple(t)
        if r is not None:
            r_mults.append(r)
    return r_mults


def calc_expectancy_r(trades: List[Dict[str, Any]]) -> Dict[str, float]:
    """Calculate expectancy and related stats in R-multiple terms.

    Expectancy = (win_rate × avg_winner_R) + (loss_rate × avg_loser_R)
    A positive expectancy means the system makes money per unit of risk.

    Args:
        trades: List of trade dicts with entry_price, stop_price, shares, pnl.

    Returns:
        Dict with expectancy_r, avg_r, avg_winner_r, avg_loser_r,
        win_rate_r, total_r, r_count, largest_winner_r, largest_loser_r.
        Returns empty metrics if insufficient R-data.
    """
    r_mults = calc_r_multiples(trades)

    if not r_mults:
        return {
            "expectancy_r": 0.0,
            "avg_r": 0.0,
            "avg_winner_r": 0.0,
            "avg_loser_r": 0.0,
            "win_rate_r": 0.0,
            "total_r": 0.0,
            "r_count": 0,
            "largest_winner_r": 0.0,
            "largest_loser_r": 0.0,
        }

    winners_r = [r for r in r_mults if r > 0]
    losers_r = [r for r in r_mults if r <= 0]

    win_rate = len(winners_r) / len(r_mults) if r_mults else 0
    avg_win_r = np.mean(winners_r) if winners_r else 0.0
    avg_loss_r = np.mean(losers_r) if losers_r else 0.0

    # Expectancy = (win% × avg_win_R) + (loss% × avg_loss_R)
    # Note: avg_loss_R is negative, so this naturally subtracts
    expectancy = (win_rate * avg_win_r) + ((1 - win_rate) * avg_loss_r)

    return {
        "expectancy_r": round(expectancy, 4),
        "avg_r": round(np.mean(r_mults), 4),
        "avg_winner_r": round(avg_win_r, 4),
        "avg_loser_r": round(avg_loss_r, 4),
        "win_rate_r": round(win_rate, 4),
        "total_r": round(sum(r_mults), 4),
        "r_count": len(r_mults),
        "largest_winner_r": round(max(r_mults), 4),
        "largest_loser_r": round(min(r_mults), 4),
    }


def calc_edge_ttest(trades: List[Dict[str, Any]], use_r: bool = True) -> Dict[str, Any]:
    """Test whether a strategy has a statistically significant edge.

    Runs a one-sample t-test on trade P&L (or R-multiples) against zero.
    If p < 0.05, the mean return is statistically different from zero.

    Args:
        trades: List of trade dicts.
        use_r: If True, test R-multiples. If False, test raw P&L.

    Returns:
        Dict with t_statistic, p_value, significant (bool),
        mean, std, n, confidence_95_lower, confidence_95_upper.
    """
    if use_r:
        values = calc_r_multiples(trades)
        label = "r_multiple"
    else:
        values = [t.get("pnl", 0) for t in trades if t.get("pnl") is not None]
        label = "pnl"

    if len(values) < 5:
        return {
            "test_type": label,
            "t_statistic": 0.0,
            "p_value": 1.0,
            "significant": False,
            "mean": 0.0,
            "std": 0.0,
            "n": len(values),
            "confidence_95_lower": 0.0,
            "confidence_95_upper": 0.0,
        }

    arr = np.array(values)
    t_stat, p_val = stats.ttest_1samp(arr, 0)

    # 95% confidence interval for the mean
    se = stats.sem(arr)
    ci = stats.t.interval(0.95, df=len(arr) - 1, loc=arr.mean(), scale=se)

    return {
        "test_type": label,
        "t_statistic": round(float(t_stat), 4),
        "p_value": round(float(p_val), 6),
        "significant": bool(p_val < 0.05),
        "mean": round(float(arr.mean()), 4),
        "std": round(float(arr.std(ddof=1)), 4),
        "n": len(arr),
        "confidence_95_lower": round(float(ci[0]), 4),
        "confidence_95_upper": round(float(ci[1]), 4),
    }


def calc_var(
    returns: pd.Series,
    confidence: float = 0.95,
    method: str = "historical",
) -> float:
    """Calculate Value at Risk (VaR) for a given confidence level.

    VaR answers: "What is the maximum daily loss at the X% confidence level?"

    Args:
        returns: Series of daily returns (decimal, not percentage).
        confidence: Confidence level (0.95 = 95%, 0.99 = 99%).
        method: 'historical' (percentile) or 'parametric' (Gaussian).

    Returns:
        VaR as a positive decimal (e.g., 0.025 = 2.5% daily loss).
        Returns 0.0 if insufficient data.
    """
    if returns is None or len(returns) < 20:
        return 0.0

    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < 20:
        return 0.0

    if method == "parametric":
        # Assume normal distribution
        mu = returns.mean()
        sigma = returns.std(ddof=1)
        z = stats.norm.ppf(1 - confidence)
        var = -(mu + z * sigma)
    else:
        # Historical: empirical percentile
        var = -np.percentile(returns, (1 - confidence) * 100)

    return round(max(var, 0.0), 6)


def calc_cvar(
    returns: pd.Series,
    confidence: float = 0.95,
) -> float:
    """Calculate Conditional VaR (Expected Shortfall / CVaR).

    CVaR = average loss in the worst (1 - confidence)% of days.
    More informative than VaR because it captures tail severity.

    Args:
        returns: Series of daily returns (decimal).
        confidence: Confidence level (0.95 = worst 5% of days).

    Returns:
        CVaR as a positive decimal (e.g., 0.038 = 3.8% avg tail loss).
        Returns 0.0 if insufficient data.
    """
    if returns is None or len(returns) < 20:
        return 0.0

    returns = returns.replace([np.inf, -np.inf], np.nan).dropna()
    if len(returns) < 20:
        return 0.0

    var_threshold = np.percentile(returns, (1 - confidence) * 100)
    tail_losses = returns[returns <= var_threshold]

    if len(tail_losses) == 0:
        return 0.0

    cvar = -tail_losses.mean()
    return round(max(cvar, 0.0), 6)


def calc_calmar(
    equity_curve: pd.Series,
) -> float:
    """Calculate Calmar ratio (CAGR / Max Drawdown).

    Higher is better. Measures return per unit of drawdown risk.
    Values > 1.0 mean CAGR exceeds the worst peak-to-trough decline.

    Args:
        equity_curve: Series indexed by date with portfolio values.

    Returns:
        Calmar ratio. Returns 0.0 if max drawdown is zero or insufficient data.
    """
    cagr = calc_cagr(equity_curve)
    max_dd = calc_max_drawdown(equity_curve)

    if max_dd <= 0:
        return 0.0

    return round(cagr / max_dd, 4)


def calc_strategy_correlation(
    trades: List[Dict[str, Any]],
    equity_curve: pd.Series,
) -> Dict[str, Any]:
    """Build a return correlation matrix across strategies.

    Computes daily P&L attribution per strategy, then calculates pairwise
    Pearson correlations. Flags pairs with |correlation| > 0.6 as
    concentrated risk.

    Args:
        trades: List of trade dicts (must have 'strategy', 'entry_date',
                'exit_date', 'pnl', 'hold_days').
        equity_curve: Series indexed by date (used for date range).

    Returns:
        Dict with:
          - 'strategies': list of strategy names
          - 'matrix': 2D list of correlation values
          - 'concentrated_pairs': list of (stratA, stratB, corr) with |r|>0.6
          - 'daily_returns': dict of strategy -> list of daily returns
    """
    if not trades or equity_curve is None or len(equity_curve) < 10:
        return {"strategies": [], "matrix": [], "concentrated_pairs": [], "daily_returns": {}}

    # Gather unique strategies
    strat_names = sorted(set(t.get("strategy", "unknown") for t in trades))
    if len(strat_names) < 2:
        return {"strategies": strat_names, "matrix": [[1.0]], "concentrated_pairs": [], "daily_returns": {}}

    # Build daily P&L series per strategy
    dates = equity_curve.index
    strat_daily = {s: pd.Series(0.0, index=dates) for s in strat_names}

    for t in trades:
        s = t.get("strategy", "unknown")
        pnl = t.get("pnl", 0)
        hold = t.get("hold_days", t.get("holding_days", 1)) or 1
        exit_date = t.get("exit_date")
        if not exit_date or s not in strat_daily:
            continue
        exit_ts = pd.Timestamp(exit_date)
        # Spread P&L evenly across holding days for daily attribution
        daily_pnl = pnl / hold
        entry_date = t.get("entry_date")
        if entry_date:
            entry_ts = pd.Timestamp(entry_date)
            mask = (dates >= entry_ts) & (dates <= exit_ts)
            strat_daily[s].loc[mask] += daily_pnl

    # Build DataFrame and compute correlation
    df = pd.DataFrame(strat_daily)
    # Only keep days where at least one strategy had activity
    active_mask = df.abs().sum(axis=1) > 0
    df_active = df[active_mask]

    if len(df_active) < 10:
        return {"strategies": strat_names, "matrix": [], "concentrated_pairs": [], "daily_returns": {}}

    corr = df_active.corr()
    matrix = corr.values.tolist()

    # Flag concentrated pairs
    concentrated = []
    for i in range(len(strat_names)):
        for j in range(i + 1, len(strat_names)):
            r = corr.iloc[i, j]
            if abs(r) > 0.6:
                concentrated.append((strat_names[i], strat_names[j], round(r, 4)))

    return {
        "strategies": strat_names,
        "matrix": [[round(v, 4) for v in row] for row in matrix],
        "concentrated_pairs": concentrated,
        "n_active_days": int(active_mask.sum()),
    }


def calc_monte_carlo_drawdown(
    trades: List[Dict[str, Any]],
    starting_equity: float,
    n_simulations: int = 1000,
    seed: int = 42,
) -> Dict[str, Any]:
    """Monte Carlo drawdown stress test via trade sequence bootstrapping.

    Reshuffles the order of closed trades N times and computes the max
    drawdown for each permutation. Reports the 95th and 99th percentile
    drawdowns. Catches strategies that look good historically but have
    fragile trade sequencing.

    Args:
        trades: List of trade dicts (must have 'pnl').
        starting_equity: Initial portfolio value.
        n_simulations: Number of bootstrap iterations (default 1000).
        seed: Random seed for reproducibility.

    Returns:
        Dict with:
          - 'p50_drawdown': median max drawdown across simulations
          - 'p75_drawdown': 75th percentile
          - 'p95_drawdown': 95th percentile max drawdown
          - 'p99_drawdown': 99th percentile max drawdown
          - 'worst_drawdown': worst max drawdown seen
          - 'actual_drawdown': max drawdown from original trade order
          - 'n_simulations': number of runs
          - 'n_trades': number of trades
          - 'fragile': True if p95 DD > 2× actual DD (trade sequence matters)
    """
    pnls = [t.get("pnl", 0) for t in trades if t.get("pnl") is not None]
    n = len(pnls)

    if n < 5 or starting_equity <= 0:
        return {
            "p50_drawdown": 0.0, "p75_drawdown": 0.0,
            "p95_drawdown": 0.0, "p99_drawdown": 0.0,
            "worst_drawdown": 0.0, "actual_drawdown": 0.0,
            "n_simulations": 0, "n_trades": n, "fragile": False,
        }

    rng = np.random.RandomState(seed)
    pnl_arr = np.array(pnls)

    def _max_dd_from_pnls(pnl_seq: np.ndarray) -> float:
        """Compute max drawdown from a P&L sequence."""
        equity = starting_equity + np.cumsum(pnl_seq)
        equity = np.insert(equity, 0, starting_equity)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / peak
        return abs(dd.min())

    # Actual drawdown from original order
    actual_dd = _max_dd_from_pnls(pnl_arr)

    # Monte Carlo: reshuffle and compute
    mc_drawdowns = np.empty(n_simulations)
    for i in range(n_simulations):
        shuffled = rng.permutation(pnl_arr)
        mc_drawdowns[i] = _max_dd_from_pnls(shuffled)

    return {
        "p50_drawdown": round(float(np.percentile(mc_drawdowns, 50)), 6),
        "p75_drawdown": round(float(np.percentile(mc_drawdowns, 75)), 6),
        "p95_drawdown": round(float(np.percentile(mc_drawdowns, 95)), 6),
        "p99_drawdown": round(float(np.percentile(mc_drawdowns, 99)), 6),
        "worst_drawdown": round(float(mc_drawdowns.max()), 6),
        "actual_drawdown": round(actual_dd, 6),
        "n_simulations": n_simulations,
        "n_trades": n,
        "fragile": bool(np.percentile(mc_drawdowns, 95) > 2 * actual_dd) if actual_dd > 0 else False,
    }


def calc_all_metrics(
    equity_curve: pd.Series,
    trades: List[Dict[str, Any]],
    positions_log: Optional[List[Dict[str, Any]]] = None,
    rf: float = DEFAULT_RF,
) -> Dict[str, Any]:
    """Calculate all performance metrics in one call.

    Args:
        equity_curve: Series indexed by date with portfolio values.
        trades: List of completed trade dicts.
        positions_log: Optional list of position dicts for exposure calc.
        rf: Annual risk-free rate.

    Returns:
        Dict with all metric values.
    """
    if positions_log is None:
        positions_log = trades  # trades can serve as positions_log

    # Daily returns from equity curve
    if equity_curve is not None and len(equity_curve) > 1:
        returns = equity_curve.pct_change().dropna()
    else:
        returns = pd.Series(dtype=float)

    avg_equity = equity_curve.mean() if equity_curve is not None and len(equity_curve) > 0 else 0

    metrics = {
        "total_return": round(
            (equity_curve.iloc[-1] / equity_curve.iloc[0] - 1) if equity_curve is not None and len(equity_curve) > 1 else 0.0,
            4,
        ),
        "cagr": calc_cagr(equity_curve),
        "max_drawdown": calc_max_drawdown(equity_curve),
        "sharpe": calc_sharpe(returns, rf=rf),
        "sortino": calc_sortino(returns, rf=rf),
        "win_rate": calc_win_rate(trades),
        "profit_factor": calc_profit_factor(trades),
        "avg_trade": calc_avg_trade(trades),
        "total_trades": len(trades),
        "total_pnl": round(sum(t.get("pnl", 0) for t in trades), 2),
        "exposure": calc_exposure(equity_curve, positions_log),
        "turnover": calc_turnover(trades, avg_equity),
        "avg_equity": round(avg_equity, 2),
        "final_equity": round(equity_curve.iloc[-1], 2) if equity_curve is not None and len(equity_curve) > 0 else 0.0,
    }

    # Additional trade stats
    if trades:
        pnls = [t.get("pnl", 0) for t in trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]
        metrics["avg_winner"] = round(np.mean(winners), 2) if winners else 0.0
        metrics["avg_loser"] = round(np.mean(losers), 2) if losers else 0.0
        metrics["largest_winner"] = round(max(pnls), 2) if pnls else 0.0
        metrics["largest_loser"] = round(min(pnls), 2) if pnls else 0.0
        metrics["avg_hold_days"] = round(
            np.mean([t.get("hold_days", 0) for t in trades]), 1
        )
    else:
        metrics["avg_winner"] = 0.0
        metrics["avg_loser"] = 0.0
        metrics["largest_winner"] = 0.0
        metrics["largest_loser"] = 0.0
        metrics["avg_hold_days"] = 0.0

    # Calmar ratio (CAGR / Max DD)
    metrics["calmar"] = calc_calmar(equity_curve)

    # Value at Risk & Conditional VaR (Expected Shortfall)
    metrics["var_95"] = calc_var(returns, confidence=0.95)
    metrics["var_99"] = calc_var(returns, confidence=0.99)
    metrics["cvar_95"] = calc_cvar(returns, confidence=0.95)
    metrics["var_95_parametric"] = calc_var(returns, confidence=0.95, method="parametric")

    # R-multiple analysis
    r_metrics = calc_expectancy_r(trades)
    metrics.update(r_metrics)

    # Statistical edge test (t-test on R-multiples)
    edge = calc_edge_ttest(trades, use_r=True)
    metrics["edge_t_statistic"] = edge["t_statistic"]
    metrics["edge_p_value"] = edge["p_value"]
    metrics["edge_significant"] = edge["significant"]

    # Strategy correlation matrix (only if multiple strategies present)
    corr = calc_strategy_correlation(trades, equity_curve)
    metrics["strategy_correlation"] = corr

    # Monte Carlo drawdown stress test
    starting_eq = equity_curve.iloc[0] if equity_curve is not None and len(equity_curve) > 0 else 5000
    mc = calc_monte_carlo_drawdown(trades, starting_eq)
    metrics["mc_p95_drawdown"] = mc["p95_drawdown"]
    metrics["mc_p99_drawdown"] = mc["p99_drawdown"]
    metrics["mc_fragile"] = mc["fragile"]
    metrics["monte_carlo"] = mc

    return metrics
