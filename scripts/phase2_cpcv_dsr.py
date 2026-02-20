#!/usr/bin/env python3
"""Phase 2: Combinatorial Purged Cross-Validation (CPCV) & Deflated Sharpe Ratio (DSR)
====================================================================================
Validates Phase 1.3 optimized config before promotion to active system.

CPCV: Splits time series into N groups, tests all C(N,k) combinations of test groups.
      Measures performance distribution across folds to assess robustness.

PSR:  Probabilistic Sharpe Ratio - tests if true SR > 0 after adjusting for
      non-normality. The primary statistical test for strategy validity.

DSR:  Deflated Sharpe Ratio - accounts for multiple testing bias from optimization.
      Informational diagnostic (see notes on interpretation).

MinTRL: Minimum Track Record Length - how many observations needed for SR significance.

Reference: Marcos Lopez de Prado, 'Advances in Financial Machine Learning' (2018)
           Bailey & Lopez de Prado, 'The Deflated Sharpe Ratio' (2014)
"""
import json
import os
import sys
import time
import logging
from datetime import datetime
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = 'config/config_phase1_3_og_optimized.json'
RESULTS_PATH = 'backtest/results/phase2_cpcv_dsr.json'

# CPCV parameters
N_GROUPS = 6          # Number of time slices (~4 months each)
K_TEST = 2            # Number of groups used as test per fold
PURGE_DAYS = 5        # Gap days at train/test boundaries to prevent leakage
ANNUAL_TRADING_DAYS = 252

# DSR parameters
N_TRIALS = 70         # Approximate trials across Phase 1.1-1.3 optimization
# Effective trials: most parameter combos are highly correlated (e.g., RSI 25 vs 30).
# Using average inter-trial correlation of ~0.85 (conservative for grid search).
RHO_AVG = 0.85        # Estimated average correlation between trial outcomes
N_EFF = max(2, int(1 + (N_TRIALS - 1) * (1 - RHO_AVG)))  # ~12 effective trials

# Risk-free rate (AUD)
RF_ANNUAL = 0.04

# Success criteria
MIN_FOLDS_PROFITABLE_PCT = 0.60   # >60% folds profitable
MAX_FOLD_LOSS_PCT = -0.15         # No fold worse than -15%
PSR_TARGET = 0.95                 # PSR > 0.95 (95% confidence SR > 0)
PSR_ACCEPTABLE = 0.90             # PSR > 0.90 (90% confidence)

# Ensure logs directory exists
Path('logs').mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/phase2_cpcv_dsr.log'),
    ]
)
logger = logging.getLogger(__name__)


# ===========================================================================
# Data Loading
# ===========================================================================
def load_data():
    """Load all parquet files from cache (same as phase1_3 script)."""
    data_dict = {}
    cache = Path('data/cache')
    for pf in sorted(cache.glob('*.parquet')):
        if pf.stem == 'IOZ_AX':
            continue
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) < 100:
            continue
        ticker = pf.stem.replace('_AX', '.AX')
        data_dict[ticker] = df
    return data_dict


def load_config():
    """Load the Phase 1.3 optimized config."""
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ===========================================================================
# Full Backtest Runner
# ===========================================================================
def run_full_backtest(cfg, data_dict):
    """Run a single full backtest with the given config, return BacktestResult."""
    from backtest.engine import BacktestEngine
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap

    strategies = [MeanReversion(cfg), TrendFollowing(cfg), OpeningGap(cfg)]
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, strategies)
    return result


# ===========================================================================
# CPCV Implementation
# ===========================================================================
def build_cpcv_groups(trading_dates, n_groups):
    """Split sorted trading dates into n_groups of roughly equal size."""
    n = len(trading_dates)
    group_size = n // n_groups
    groups = []
    for i in range(n_groups):
        start = i * group_size
        end = start + group_size if i < n_groups - 1 else n
        groups.append(trading_dates[start:end])
    return groups


def get_purged_test_dates(groups, test_indices, purge_days):
    """Get test dates with purging applied at boundaries."""
    test_dates = []
    for idx in test_indices:
        g = groups[idx]
        if len(g) <= 2 * purge_days:
            mid = len(g) // 2
            start_i = max(0, mid - 1)
            end_i = min(len(g), mid + 1)
            test_dates.extend(g[start_i:end_i])
        else:
            test_dates.extend(g[purge_days:-purge_days])
    return np.array(sorted(test_dates))


def compute_fold_metrics(daily_returns, fold_dates):
    """Compute performance metrics for a specific set of dates."""
    fold_dates_idx = pd.DatetimeIndex(fold_dates)
    common_dates = daily_returns.index.intersection(fold_dates_idx)

    if len(common_dates) < 5:
        return {
            'n_days': int(len(common_dates)),
            'total_return': 0.0,
            'cagr': 0.0,
            'sharpe': 0.0,
            'profitable': False,
            'max_drawdown': 0.0,
            'volatility': 0.0,
        }

    fold_returns = daily_returns.loc[common_dates].sort_index()
    n_days = len(fold_returns)

    # Total return (compounded)
    cumulative = (1 + fold_returns).cumprod()
    total_return = cumulative.iloc[-1] - 1.0

    # Annualized return (CAGR)
    years = n_days / ANNUAL_TRADING_DAYS
    if years > 0 and cumulative.iloc[-1] > 0:
        cagr = (cumulative.iloc[-1]) ** (1 / years) - 1.0
    else:
        cagr = -1.0

    # Sharpe ratio (annualized, zero-rf for fold comparison)
    mean_ret = fold_returns.mean()
    std_ret = fold_returns.std()
    if std_ret > 0:
        sharpe = (mean_ret / std_ret) * np.sqrt(ANNUAL_TRADING_DAYS)
    else:
        sharpe = 0.0

    # Max drawdown from cumulative returns
    running_max = cumulative.cummax()
    drawdowns = (cumulative - running_max) / running_max
    max_dd = drawdowns.min()

    # Volatility (annualized)
    volatility = std_ret * np.sqrt(ANNUAL_TRADING_DAYS)

    return {
        'n_days': int(n_days),
        'total_return': round(float(total_return), 6),
        'cagr': round(float(cagr), 6),
        'sharpe': round(float(sharpe), 4),
        'profitable': bool(total_return > 0),
        'max_drawdown': round(float(max_dd), 6),
        'volatility': round(float(volatility), 6),
    }


def run_cpcv(equity_curve, n_groups=N_GROUPS, k_test=K_TEST, purge_days=PURGE_DAYS):
    """Run Combinatorial Purged Cross-Validation on the equity curve."""
    trading_dates = equity_curve.index.values
    daily_returns = equity_curve.pct_change().dropna()
    daily_returns = daily_returns.replace([np.inf, -np.inf], 0.0).fillna(0.0)

    logger.info(f"Equity curve: {len(equity_curve)} days, "
                f"{pd.Timestamp(equity_curve.index[0]).date()} to "
                f"{pd.Timestamp(equity_curve.index[-1]).date()}")
    logger.info(f"Daily returns: {len(daily_returns)} days, "
                f"mean={daily_returns.mean():.6f}, std={daily_returns.std():.6f}")

    groups = build_cpcv_groups(trading_dates, n_groups)

    logger.info(f"CPCV: N={n_groups} groups, k={k_test} test groups per fold")
    for i, g in enumerate(groups):
        start_d = pd.Timestamp(g[0]).date()
        end_d = pd.Timestamp(g[-1]).date()
        logger.info(f"  Group {i}: {start_d} to {end_d} ({len(g)} days)")

    combos = list(combinations(range(n_groups), k_test))
    n_combos = len(combos)
    logger.info(f"Total combinations: C({n_groups},{k_test}) = {n_combos}")

    fold_results = []
    for combo_idx, test_indices in enumerate(combos):
        train_indices = tuple(i for i in range(n_groups) if i not in test_indices)
        purged_dates = get_purged_test_dates(groups, test_indices, purge_days)
        metrics = compute_fold_metrics(daily_returns, purged_dates)
        metrics['test_groups'] = list(test_indices)
        metrics['train_groups'] = list(train_indices)
        metrics['fold_id'] = combo_idx
        fold_results.append(metrics)

        status = "PROFITABLE" if metrics['profitable'] else "LOSS"
        logger.info(f"  Fold {combo_idx:2d} test={test_indices}: "
                    f"return={metrics['total_return']*100:+.2f}%, "
                    f"CAGR={metrics['cagr']*100:+.2f}%, "
                    f"Sharpe={metrics['sharpe']:.3f}, "
                    f"MaxDD={metrics['max_drawdown']*100:.2f}% [{status}]")

    # Compute summary statistics
    returns_list = [f['total_return'] for f in fold_results]
    cagr_list = [f['cagr'] for f in fold_results]
    sharpe_list = [f['sharpe'] for f in fold_results]
    dd_list = [f['max_drawdown'] for f in fold_results]
    profitable_count = sum(1 for f in fold_results if f['profitable'])

    summary = {
        'n_groups': n_groups,
        'k_test': k_test,
        'purge_days': purge_days,
        'n_combinations': n_combos,
        'pct_profitable': round(profitable_count / n_combos, 4),
        'n_profitable': profitable_count,
        'mean_return': round(float(np.mean(returns_list)), 6),
        'std_return': round(float(np.std(returns_list)), 6),
        'worst_return': round(float(np.min(returns_list)), 6),
        'best_return': round(float(np.max(returns_list)), 6),
        'mean_cagr': round(float(np.mean(cagr_list)), 6),
        'std_cagr': round(float(np.std(cagr_list)), 6),
        'worst_cagr': round(float(np.min(cagr_list)), 6),
        'best_cagr': round(float(np.max(cagr_list)), 6),
        'mean_sharpe': round(float(np.mean(sharpe_list)), 4),
        'std_sharpe': round(float(np.std(sharpe_list)), 4),
        'worst_sharpe': round(float(np.min(sharpe_list)), 4),
        'best_sharpe': round(float(np.max(sharpe_list)), 4),
        'mean_max_dd': round(float(np.mean(dd_list)), 6),
        'worst_max_dd': round(float(np.min(dd_list)), 6),
        'passes_profitable_threshold': bool(profitable_count / n_combos >= MIN_FOLDS_PROFITABLE_PCT),
        'passes_max_loss_threshold': bool(np.min(returns_list) > MAX_FOLD_LOSS_PCT),
    }

    return {
        'summary': summary,
        'folds': fold_results,
    }


# ===========================================================================
# Statistical Tests: PSR, DSR, MinTRL
# ===========================================================================

def compute_psr(daily_returns, sr_benchmark=0.0):
    """Probabilistic Sharpe Ratio (Bailey & Lopez de Prado, 2012).

    Tests H0: true SR <= sr_benchmark
    Returns probability that the true SR exceeds sr_benchmark,
    adjusted for non-normality (skewness and kurtosis) of returns.

    Formula:
        PSR = Phi( (SR - SR*) * sqrt(T-1) / sqrt(1 - skew*SR + (kurt-1)/4 * SR^2) )

    where:
        SR = observed annualized Sharpe
        SR* = benchmark Sharpe (0 for testing if strategy is positive)
        T = number of observations
        skew = skewness of returns
        kurt = excess kurtosis of returns
        Phi = standard normal CDF

    Args:
        daily_returns: pd.Series of daily portfolio returns
        sr_benchmark: benchmark Sharpe to test against (default 0)

    Returns:
        dict with PSR probability and diagnostics
    """
    returns = daily_returns.dropna()
    returns = returns.replace([np.inf, -np.inf], 0.0)
    T = len(returns)

    if T < 10:
        return {'psr': 0.0, 'error': 'insufficient data'}

    # Compute moments
    mean_r = returns.mean()
    std_r = returns.std(ddof=1)

    if std_r < 1e-12:
        return {'psr': 0.0, 'error': 'zero volatility'}

    # Annualized Sharpe (no risk-free subtraction — raw returns)
    sr_daily = mean_r / std_r
    sr_annual = sr_daily * np.sqrt(ANNUAL_TRADING_DAYS)

    # Skewness and excess kurtosis of returns
    skew = float(returns.skew())
    kurt = float(returns.kurtosis())  # pandas returns EXCESS kurtosis

    # PSR formula (using daily SR for consistency with T)
    # Note: we use daily SR and daily benchmark for the formula,
    # then the result is the same whether daily or annual
    sr_bench_daily = sr_benchmark / np.sqrt(ANNUAL_TRADING_DAYS)

    numerator = (sr_daily - sr_bench_daily) * np.sqrt(T - 1)
    denominator_sq = 1.0 - skew * sr_daily + (kurt / 4.0) * sr_daily**2

    if denominator_sq <= 0:
        # Edge case: adjust to avoid complex numbers
        denominator_sq = abs(denominator_sq) + 1e-10

    denominator = np.sqrt(denominator_sq)
    z_score = numerator / denominator if denominator > 1e-12 else 0.0
    psr_prob = float(norm.cdf(z_score))

    return {
        'psr_probability': round(psr_prob, 6),
        'sr_benchmark': sr_benchmark,
        'sr_observed_annual': round(float(sr_annual), 4),
        'sr_observed_daily': round(float(sr_daily), 6),
        'z_score': round(float(z_score), 4),
        'n_observations': T,
        'skewness': round(skew, 4),
        'excess_kurtosis': round(kurt, 4),
        'passes_95pct': bool(psr_prob >= PSR_TARGET),
        'passes_90pct': bool(psr_prob >= PSR_ACCEPTABLE),
    }


def compute_min_trl(daily_returns, sr_benchmark=0.0, confidence=0.95):
    """Minimum Track Record Length (Lopez de Prado, 2014).

    Minimum number of observations needed for the observed SR to be
    statistically significant at the given confidence level.

    MinTRL = 1 + (1 - skew*SR + (kurt/4)*SR^2) * (z_alpha / (SR - SR*))^2

    Args:
        daily_returns: pd.Series of daily returns
        sr_benchmark: benchmark daily SR
        confidence: confidence level (default 0.95)

    Returns:
        dict with MinTRL and comparison to actual track record
    """
    returns = daily_returns.dropna().replace([np.inf, -np.inf], 0.0)
    T = len(returns)
    std_r = returns.std(ddof=1)

    if std_r < 1e-12 or T < 10:
        return {'min_trl': float('inf'), 'sufficient': False}

    sr_daily = returns.mean() / std_r
    sr_bench_daily = sr_benchmark / np.sqrt(ANNUAL_TRADING_DAYS)
    skew = float(returns.skew())
    kurt = float(returns.kurtosis())

    z_alpha = norm.ppf(confidence)
    sr_diff = sr_daily - sr_bench_daily

    if abs(sr_diff) < 1e-10:
        return {'min_trl': float('inf'), 'sufficient': False}

    variance_factor = 1.0 - skew * sr_daily + (kurt / 4.0) * sr_daily**2
    if variance_factor <= 0:
        variance_factor = abs(variance_factor) + 1e-10

    min_trl = 1 + variance_factor * (z_alpha / sr_diff) ** 2

    return {
        'min_trl_days': round(float(min_trl), 0),
        'min_trl_years': round(float(min_trl / ANNUAL_TRADING_DAYS), 2),
        'actual_days': T,
        'actual_years': round(T / ANNUAL_TRADING_DAYS, 2),
        'sufficient': bool(T >= min_trl),
        'confidence': confidence,
    }


def compute_dsr(daily_returns, n_trials, n_eff=None):
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    Tests whether the observed Sharpe could have been achieved by chance
    given the number of trials (parameter combinations) tested.

    Key insight: uses ZERO risk-free rate for consistency with the null
    hypothesis (E[SR] = 0 under the null of no skill).

    Args:
        daily_returns: pd.Series of daily portfolio returns
        n_trials: total number of parameter combinations tested
        n_eff: effective number of independent trials (if None, uses n_trials)

    Returns:
        dict with DSR results for both raw and effective trial counts
    """
    returns = daily_returns.dropna().replace([np.inf, -np.inf], 0.0)
    T = len(returns)
    std_r = returns.std(ddof=1)

    if std_r < 1e-12 or T < 10:
        return {'error': 'insufficient data or zero volatility'}

    # Observed SR (zero risk-free rate for DSR consistency)
    sr_daily = returns.mean() / std_r
    sr_annual = sr_daily * np.sqrt(ANNUAL_TRADING_DAYS)

    skew = float(returns.skew())
    kurt = float(returns.kurtosis())

    if n_eff is None:
        n_eff = n_trials

    results = {}
    for label, N in [('raw_trials', n_trials), ('effective_trials', n_eff)]:
        # Expected maximum SR under null (Lopez de Prado)
        # E[max(SR)] ~ (1-gamma)*Phi^{-1}(1 - 1/N) + gamma*Phi^{-1}(1 - 1/(N*e))
        gamma = 0.5772156649  # Euler-Mascheroni constant

        if N <= 1:
            e_max_sr = 0.0
        else:
            # Avoid numerical issues with very small probabilities
            p1 = max(1e-15, 1.0 - 1.0 / N)
            p2 = max(1e-15, 1.0 - 1.0 / (N * np.e))
            e_max_sr = (1 - gamma) * norm.ppf(p1) + gamma * norm.ppf(p2)

        # DSR: probability that observed SR exceeds E[max(SR)]
        # Using the non-normal SR distribution
        sr_diff = sr_annual - e_max_sr
        variance_factor = 1.0 - skew * sr_daily + (kurt / 4.0) * sr_daily**2
        if variance_factor <= 0:
            variance_factor = abs(variance_factor) + 1e-10

        if T > 1:
            z_dsr = sr_diff * np.sqrt(T - 1) / (np.sqrt(variance_factor) * np.sqrt(ANNUAL_TRADING_DAYS))
        else:
            z_dsr = 0.0

        dsr_pvalue = float(norm.cdf(z_dsr))

        results[label] = {
            'n_trials': int(N),
            'e_max_sr': round(float(e_max_sr), 4),
            'sr_observed': round(float(sr_annual), 4),
            'sr_gap': round(float(sr_diff), 4),
            'z_score': round(float(z_dsr), 4),
            'dsr_pvalue': round(float(dsr_pvalue), 6),
            'passes_5pct': bool(dsr_pvalue >= 0.95),
            'passes_10pct': bool(dsr_pvalue >= 0.90),
        }

    # Overall summary
    results['sr_observed_annual'] = round(float(sr_annual), 4)
    results['skewness'] = round(skew, 4)
    results['excess_kurtosis'] = round(kurt, 4)
    results['n_observations'] = T
    results['note'] = (
        f"DSR uses zero-rf Sharpe ({sr_annual:.3f}) for consistency with E[max(SR)] null hypothesis. "
        f"Effective trials ({n_eff}) accounts for correlation between parameter combinations "
        f"(rho_avg={RHO_AVG:.2f}). Raw trials ({n_trials}) assumes full independence."
    )

    return results


# ===========================================================================
# Report
# ===========================================================================
def print_report(cpcv_res, psr_res, dsr_res, mtrl_res, full_met, elapsed):
    """Print Phase 2 validation report."""
    sm = cpcv_res["summary"]
    folds = cpcv_res["folds"]
    SEP = "=" * 78
    DSEP = "-" * 78

    print(f"\n{SEP}")
    print("  PHASE 2  |  CPCV & STATISTICAL VALIDATION")
    print(SEP)
    print(f"  Config:     {CONFIG_PATH}")
    print(f"  Timestamp:  {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"  Runtime:    {elapsed:.1f}s")

    print(f"\n{DSEP}")
    print("  FULL-PERIOD BACKTEST (Phase 1.3 Config)")
    print(DSEP)
    fm = full_met
    for k, lb in [("cagr","CAGR"),("sharpe","Sharpe (rf=4%)"),("profit_factor","Profit Factor"),("max_drawdown","Max Drawdown"),("win_rate","Win Rate"),("total_trades","Total Trades")]:
        print(f"  {lb+':':22s} {fm.get(k, 0)}")

    ng = sm["n_groups"]; kt = sm["k_test"]; pd_ = sm["purge_days"]; nc = sm["n_combinations"]
    print(f"\n{DSEP}")
    print(f"  CPCV: N={ng}, k={kt}, purge={pd_}d  =>  {nc} folds")
    print(DSEP)
    print(f"  {'#':>4}  {'Test Grps':>12}  {'Return':>9}  {'CAGR':>8}  {'Sharpe':>8}  {'MaxDD':>7}  {'Status':>8}")
    print("  " + "-" * 68)
    for fd in folds:
        grp = str(tuple(fd["test_groups"]))
        r = fd["total_return"] * 100
        c = fd["cagr"] * 100
        d = fd["max_drawdown"] * 100
        st = "PROFIT" if fd["profitable"] else "LOSS"
        flag = " !!" if fd["total_return"] < MAX_FOLD_LOSS_PCT else ""
        sr = fd["sharpe"]
        print(f"  {fd['fold_id']:4d}  {grp:>12}  {r:+8.2f}%  {c:+7.2f}%  {sr:+7.3f}  {d:6.2f}%  {st:>8}{flag}")

    print(f"\n  CPCV Summary:")
    s = sm
    print(f"    {'Mean':>10}  ret={s['mean_return']*100:+8.2f}%  cagr={s['mean_cagr']*100:+7.2f}%  sr={s['mean_sharpe']:+7.3f}")
    print(f"    {'Std Dev':>10}  ret={s['std_return']*100:+8.2f}%  cagr={s['std_cagr']*100:+7.2f}%  sr={s['std_sharpe']:+7.3f}")
    print(f"    {'Worst':>10}  ret={s['worst_return']*100:+8.2f}%  cagr={s['worst_cagr']*100:+7.2f}%  sr={s['worst_sharpe']:+7.3f}")
    print(f"    {'Best':>10}  ret={s['best_return']*100:+8.2f}%  cagr={s['best_cagr']*100:+7.2f}%  sr={s['best_sharpe']:+7.3f}")
    print(f"    {'Worst DD':>10}  {s['worst_max_dd']*100:8.2f}%")

    pct_p = s["pct_profitable"]
    worst_r = s["worst_return"]
    pass_folds = s["passes_profitable_threshold"]
    pass_cat = s["passes_max_loss_threshold"]
    v1 = "PASS" if pass_folds else "FAIL"
    v2 = "PASS" if pass_cat else "FAIL"
    print(f"\n  CPCV Criteria:")
    print(f"    Profitable folds: {s['n_profitable']}/{nc} ({pct_p*100:.1f}%) target>{MIN_FOLDS_PROFITABLE_PCT*100:.0f}% [{v1}]")
    print(f"    No catastrophic:  worst={worst_r*100:+.2f}% target>{MAX_FOLD_LOSS_PCT*100:.0f}% [{v2}]")

    print(f"\n{DSEP}")
    print("  PROBABILISTIC SHARPE RATIO (PSR)")
    print(DSEP)
    psr = psr_res
    for k, lb in [("sr_observed_annual","Observed SR (zero-rf)"),("sr_benchmark","Benchmark SR"),("n_observations","Sample size (days)"),("skewness","Skewness"),("excess_kurtosis","Excess kurtosis"),("psr_probability","PSR (prob SR > 0)")]:
        if k == "n_observations":
            print(f"  {lb+':':28s} {psr[k]}")
        else:
            print(f"  {lb+':':28s} {psr[k]:.4f}")
    psr_val = psr["psr_probability"]
    psr_v = "PASS" if psr_val >= 0.95 else "MARGINAL" if psr_val >= 0.85 else "FAIL"
    print(f"  Assessment:                {psr_v} (target >= 0.95)")

    print(f"\n{DSEP}")
    print("  MINIMUM TRACK RECORD LENGTH (MinTRL)")
    print(DSEP)
    mt = mtrl_res
    print(f"  {'MinTRL (95% conf):':28s} {mt['min_trl_days']:.0f} days")
    print(f"  {'Actual track record:':28s} {mt['actual_days']} days")
    print(f"  {'Observed SR:':28s} {psr_res['sr_observed_annual']:.4f}")
    print(f"  {'Skewness:':28s} {psr_res['skewness']:.4f}")
    print(f"  {'Excess kurtosis:':28s} {psr_res['excess_kurtosis']:.4f}")
    mt_v = "PASS" if mt["sufficient"] else "FAIL"
    print(f"  Sufficient data:           {mt_v}")

    print(f"\n{DSEP}")
    print("  DEFLATED SHARPE RATIO (DSR)")
    print(DSEP)
    ds = dsr_res
    print(f"  {'Total independent trials:':30s} {ds['n_trials']}")
    print(f"  {'Effective trials (rho adj):':30s} {ds['n_eff']:.1f}")
    print(f"  {'Correlation haircut rho:':30s} {ds['rho']:.2f}")
    print(f"  {'Observed SR (zero-rf):':30s} {ds['observed_sr']:.4f}")
    print(f"  {'E[max(SR)] under null:':30s} {ds['e_max_sr']:.4f}")
    print(f"  {'DSR p-value:':30s} {ds['dsr_pvalue']:.4f}")
    ds_v = "PASS" if ds["dsr_pvalue"] < 0.05 else "MARGINAL" if ds["dsr_pvalue"] < 0.10 else "FAIL"
    print(f"  Assessment:                  {ds_v} (target p < 0.05)")
    print("\n  Note: DSR tests if observed Sharpe exceeds what random trials produce.")
    print("  Low p-value = genuine skill, not luck from multiple testing.")

    print(f"\n{SEP}")
    print("  OVERALL VERDICT")
    print(SEP)
    verdicts = {}
    verdicts["CPCV Profitable Folds"] = pass_folds
    verdicts["CPCV No Catastrophic"] = pass_cat
    verdicts["PSR > 0.95"] = psr_val >= 0.95
    verdicts["MinTRL Sufficient"] = mt["sufficient"]
    verdicts["DSR p < 0.05"] = ds["dsr_pvalue"] < 0.05
    for name, passed in verdicts.items():
        tag = "PASS" if passed else "FAIL"
        print(f"    {name:30s} [{tag}]")
    n_pass = sum(verdicts.values())
    n_total = len(verdicts)
    overall = "VALIDATED" if n_pass >= 4 else "MARGINAL" if n_pass >= 3 else "REJECTED"
    print(f"\n  Result: {n_pass}/{n_total} checks passed => {overall}")
    print(SEP)

    return {
        "cpcv": cpcv_res,
        "psr": psr_res,
        "dsr": dsr_res,
        "min_trl": mtrl_res,
        "full_metrics": full_met,
        "verdict": overall,
        "n_checks_passed": n_pass,
        "n_checks_total": n_total,
    }


# ===========================================================================
# Main
# ===========================================================================
def main():
    """Run Phase 2 CPCV and statistical validation."""
    t0 = time.time()

    # Load config
    print(f"Loading config: {CONFIG_PATH}")
    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Load data
    print("Loading market data...")
    data_dict = load_data()
    print(f"  {len(data_dict)} tickers loaded")

    # Step 1: Run full-period backtest as reference
    print("\n[Step 1] Running full-period backtest (reference)...")
    bt_result = run_full_backtest(config, data_dict)
    full_metrics = bt_result.metrics
    equity_curve = bt_result.equity_curve
    print(f"  CAGR={full_metrics.get('cagr', 0)}, Sharpe={full_metrics.get('sharpe', 0)}, PF={full_metrics.get('profit_factor', 0)}")
    print(f"  Equity curve: {len(equity_curve)} days")

    if len(equity_curve) < 20:
        print("ERROR: Equity curve too short for CPCV analysis")
        return None

    # Step 2: CPCV on equity curve
    print(f"\n[Step 2] Running CPCV (N={N_GROUPS}, k={K_TEST}, purge={PURGE_DAYS}d)...")
    cpcv_res = run_cpcv(equity_curve, n_groups=N_GROUPS, k_test=K_TEST, purge_days=PURGE_DAYS)
    sm = cpcv_res["summary"]
    print(f"  {sm['n_profitable']}/{sm['n_combinations']} folds profitable ({sm['pct_profitable']*100:.1f}%)")
    print(f"  Mean CAGR: {sm['mean_cagr']*100:+.2f}%, Mean Sharpe: {sm['mean_sharpe']:+.3f}")

    # Step 3: Get portfolio daily returns for statistical tests
    print("\n[Step 3] Computing portfolio daily returns...")
    daily_returns = equity_curve.pct_change().dropna()
    daily_returns = daily_returns.replace([np.inf, -np.inf], 0.0).fillna(0.0)
    print(f"  {len(daily_returns)} daily return observations")
    print(f"  Mean daily return: {daily_returns.mean()*100:.4f}%")
    print(f"  Daily return std:  {daily_returns.std()*100:.4f}%")

    # Step 4: PSR
    print("\n[Step 4] Computing Probabilistic Sharpe Ratio...")
    psr_res = compute_psr(daily_returns, sr_benchmark=0.0)
    print(f"  PSR = {psr_res['psr_probability']:.4f} (prob SR > 0)")

    # Step 5: MinTRL
    print("\n[Step 5] Computing Minimum Track Record Length...")
    mtrl_res = compute_min_trl(daily_returns, sr_benchmark=0.0)
    print(f"  MinTRL = {mtrl_res['min_trl_days']:.0f} days, Actual = {mtrl_res['actual_days']} days")
    print(f"  Sufficient: {mtrl_res['sufficient']}")

    # Step 6: DSR
    print("\n[Step 6] Computing Deflated Sharpe Ratio...")
    n_eff = 1 + (N_TRIALS - 1) * (1 - RHO_AVG)
    dsr_raw = compute_dsr(daily_returns, n_trials=N_TRIALS, n_eff=n_eff)

    # Reshape DSR output for print_report (expects flat dict)
    eff = dsr_raw.get('effective_trials', {})
    dsr_res = {
        'n_trials': N_TRIALS,
        'n_eff': round(n_eff, 1),
        'rho': RHO_AVG,
        'observed_sr': dsr_raw.get('sr_observed_annual', 0.0),
        'e_max_sr': eff.get('e_max_sr', 0.0),
        'dsr_pvalue': eff.get('dsr_pvalue', 1.0),
        'z_score': eff.get('z_score', 0.0),
        'skewness': dsr_raw.get('skewness', 0.0),
        'excess_kurtosis': dsr_raw.get('excess_kurtosis', 0.0),
        'raw_trials': dsr_raw.get('raw_trials', {}),
        'effective_trials': eff,
    }
    print(f"  DSR p-value = {dsr_res['dsr_pvalue']:.4f} (target < 0.05)")
    print(f"  E[max(SR)] = {dsr_res['e_max_sr']:.4f}, Observed SR = {dsr_res['observed_sr']:.4f}")
    print(f"  N_eff = {n_eff:.1f} (from N={N_TRIALS}, rho={RHO_AVG})")

    elapsed = time.time() - t0

    # Print report
    results = print_report(cpcv_res, psr_res, dsr_res, mtrl_res, full_metrics, elapsed)

    # Save results
    output_path = Path(RESULTS_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Make JSON serializable
    def make_serializable(obj):
        if isinstance(obj, (np.floating, np.integer)):
            return float(obj) if isinstance(obj, np.floating) else int(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        return obj

    def clean_dict(d):
        if isinstance(d, dict):
            return {k: clean_dict(v) for k, v in d.items()}
        if isinstance(d, list):
            return [clean_dict(v) for v in d]
        return make_serializable(d)

    with open(output_path, 'w') as f:
        json.dump(clean_dict(results), f, indent=2, default=str)
    print(f"\nResults saved to {output_path}")

    return results


if __name__ == "__main__":
    main()
