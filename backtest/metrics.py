"""
Atlas Backtest Metrics
===========================
Performance metrics for evaluating trading strategy backtests.

All functions are pure — they take data in and return numbers.
No side effects, no state.

Usage:
    from backtest.metrics import (
        calc_cagr, calc_max_drawdown, calc_sharpe, calc_sortino,
        calc_win_rate, calc_profit_factor, calc_avg_trade,
        calc_exposure, calc_turnover, calc_all_metrics,
    )
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Default risk-free rate (overridden by market profile in practice)
DEFAULT_RF = 0.04


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

    return metrics
