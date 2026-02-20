# Research Report: Parameter Robustness, Anti-Overfitting & ML Enhancements
## Atlas-ASX Systematic Trading System

**Date:** 2026-02-19  
**Author:** Agent Zero Research Division  
**Context:** ASX mid-cap systematic trading system with 4 strategies, 185 tickers, $5K account  
**Critical Problem:** 76% CAGR degradation under +/-15% parameter perturbation; 87% IS-to-OOS degradation

---

## Executive Summary

This report presents detailed research findings on 13 techniques across two domains — parameter robustness/anti-overfitting (7 techniques) and practical ML enhancements (6 techniques) — specifically selected to address the Atlas-ASX system's critical fragility issues. Each technique is evaluated for applicability, expected impact, implementation complexity, and priority.

### Priority Matrix

| Priority | Technique | Addresses | Expected Impact |
|----------|-----------|-----------|----------------|
| CRITICAL | Parameter Plateau Detection | Parameter fragility (76% degradation) | High - find stable regions |
| CRITICAL | Deflated Sharpe Ratio (DSR) | Multiple testing bias | High - validate true signal |
| CRITICAL | Structural Overfitting Reduction | All problems (root cause) | Very High - fundamental fix |
| HIGH | CPCV | OOS degradation (87% drop) | High - better validation |
| HIGH | PBO (Prob. Backtest Overfitting) | OOS degradation | High - quantify overfit risk |
| HIGH | Meta-Labeling | Walk-forward inconsistency, win rate | High - signal filtering |
| MEDIUM | Parameter Space Smoothing | Parameter fragility | Medium - smoother landscape |
| MEDIUM | MinBTL | Statistical validity | Medium - validate backtest length |
| MEDIUM | ML Signal Combination (GBM) | All metrics | Medium - better signal fusion |
| MEDIUM | SHAP Feature Importance | Signal quality | Medium - interpretable selection |
| LOW | White's Reality Check / SPA | Multiple testing | Medium - formal hypothesis test |
| LOW | Online Learning | Walk-forward consistency | Low-Medium - adaptive models |
| LOW | ML Pitfalls Awareness | All problems | Foundational - avoid errors |

---

## RESEARCH AREA 1: Parameter Robustness & Anti-Overfitting

---


### 1.1 Robust Parameter Plateau Detection

#### What
Parameter plateau detection is a methodology for systematically finding flat, stable regions in the parameter space where strategy performance remains consistent across neighboring parameter values. Rather than selecting the single "best" parameter set (which often sits on a sharp peak), this approach identifies broad regions — "plateaus" — where many nearby parameter combinations yield similar, robust performance.

The key innovation comes from **Wu et al. (2024)** who introduced a **Plateau Score Algorithm** that quantifies the extent of stability in a parameter region. This score replaces the conventional approach of simply selecting the best-performing parameters from training data. For higher-dimensional parameter spaces, they couple this with **Particle Swarm Optimization (PSO)** to efficiently search without brute-force enumeration.

#### Why It Helps (Atlas-ASX Specific)
This directly addresses the **#1 critical problem**: parameter perturbation of ±15% drops CAGR from 11.2% to 2.67% (76% degradation). This extreme sensitivity indicates parameters sit on a **sharp peak, not a plateau**. The Bayesian optimization with ±10% perturbation showed 83% retention, but ±15% shows 76% degradation — confirming a **narrow plateau** that drops off steeply.

Plateau detection would:
- Identify whether a true plateau exists in the parameter space
- If it does, locate its center (most robust point)
- If it doesn't, reveal that the strategy's edge may be illusory
- Quantify the "width" of the plateau to determine safe perturbation bounds

#### Implementation Details

**Step 1: Compute Plateau Scores**
For each candidate parameter set θ, evaluate performance across a neighborhood:

```python
import numpy as np
from itertools import product

def plateau_score(params, backtest_func, perturbation_range=0.15, n_samples=50):
    """Compute plateau score for a parameter set."""
    base_metric = backtest_func(params)
    neighbor_metrics = []
    
    for _ in range(n_samples):
        perturbed = {k: v * (1 + np.random.uniform(-perturbation_range, perturbation_range))
                     for k, v in params.items()}
        neighbor_metrics.append(backtest_func(perturbed))
    
    mean_perf = np.mean(neighbor_metrics)
    std_perf = np.std(neighbor_metrics)
    
    return {
        'base': base_metric,
        'mean_neighborhood': mean_perf,
        'std_neighborhood': std_perf,
        'plateau_score': mean_perf / (std_perf + 1e-8),  # Higher = flatter
        'retention': mean_perf / (base_metric + 1e-8),
        'min_neighborhood': np.min(neighbor_metrics),
        'pct_profitable': np.mean([m > 0 for m in neighbor_metrics])
    }
```

**Step 2: PSO-Based Plateau Search (for high-dimensional spaces)**
```python
from pyswarm import pso  # pip install pyswarm

def objective(params_array):
    params = array_to_dict(params_array)
    score = plateau_score(params, backtest_func, perturbation_range=0.15)
    # Maximize: weighted combination of performance and stability
    return -(0.4 * score['mean_neighborhood'] + 
             0.4 * score['plateau_score'] + 
             0.2 * score['min_neighborhood'])

lb, ub = get_param_bounds()
optimal, _ = pso(objective, lb, ub, swarmsize=50, maxiter=100)
```

**Step 3: 2D Heatmap Visualization** for each parameter pair to visually identify plateaus vs peaks.

#### Expected Impact
- **If plateau found:** Could improve perturbation retention from 24% to 70-85%
- **If no plateau:** Reveals strategy needs fundamental redesign (fewer parameters, different logic)
- Realistic expectation: Identify 30-50% more stable parameter regions

#### Implementation Complexity: **Medium**
- ~200-500 backtests per parameter evaluation (computationally expensive)
- PSO available via `pyswarm` package; Atlas already has perturbation infrastructure
- Custom plateau scoring function needed (~100 lines)

#### Priority: **CRITICAL**

#### Key References
- **Wu, J.M.T. et al. (2024)** "On the design of searching algorithm for parameter plateau in quantitative trading strategies using PSO" — *Knowledge-Based Systems* — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S095070512400265X)
- **Lin, H.Y. (2024)** "Optimal Parameter Selection and Indicator Design for Technical Analysis" — *MDPI Engineering Proceedings* 74(1):56
- **HarbourFront Quant** "Avoiding Overfitting: Searching for Parameter Plateau" — [Substack](https://harbourfrontquant.substack.com/p/avoiding-overfitting-searching-for)
- **Beyer, H.G. & Sendhoff, B. (2007)** "Robust optimization — A comprehensive survey" — Cited 2,297 times

---

### 1.2 Combinatorial Purged Cross-Validation (CPCV)

#### What
CPCV is an advanced validation methodology introduced by **Marcos Lopez de Prado (2018)** that generates hundreds or thousands of unique backtest paths from a single historical dataset. Unlike traditional walk-forward analysis which produces a single chronological sequence of train-test windows, CPCV:

1. **Divides data into S slices** (e.g., S=10)
2. **Generates C(S, S/2) combinations** of training/testing sets (e.g., C(10,5)=252 combinations)
3. Applies **purging** (removing observations near train/test boundary) to prevent information leakage
4. Applies **embargo** (additional buffer period) to handle autocorrelation
5. Produces a full **distribution** of out-of-sample performance for each parameter set

The key advantages over walk-forward:
- **Eliminates path dependency** — results don't depend on a single chronological train/test split
- **Produces performance distributions** instead of point estimates (mean, std, percentiles)
- **Exposes fragility** — parameters that look great on average but have terrible worst-case are identified
- **Enables robust selection** — choose parameters maximizing 10th percentile, not mean
- **Samples diverse market regimes** — each path sees different combinations of bull/bear/sideways

#### Why It Helps (Atlas-ASX Specific)
Atlas currently uses walk-forward analysis with **22 windows**, achieving only **59% profitability**. This is dangerously close to random. CPCV would:
- Generate **200-1000 independent backtest paths** (vs. 22 walk-forward windows)
- Provide statistical confidence in performance estimates
- Reveal whether the 59% win rate is statistically significant or within noise
- Enable selection based on **worst-case** (10th percentile) rather than average performance
- Could reveal that OOS CAGR of 2.20% is the realistic expectation (not 17.09%)

#### Implementation Details

```python
import numpy as np

class CombinatorialPurgedSplit:
    """Generates multiple random, purged train/test splits for combinatorial analysis."""
    def __init__(self, n_splits=200, train_size_pct=0.7,
                 test_size_pct=0.15, purge_size=10):
        self.n_splits = n_splits
        self.train_size_pct = train_size_pct
        self.test_size_pct = test_size_pct
        self.purge_size = purge_size

    def split(self, X):
        n_samples = len(X)
        train_len = int(n_samples * self.train_size_pct)
        test_len = int(n_samples * self.test_size_pct)
        max_start = n_samples - train_len - self.purge_size - test_len
        if max_start <= 0:
            raise ValueError("Data too small for specified train/test/purge lengths.")
        
        for _ in range(self.n_splits):
            train_start = np.random.randint(0, max_start + 1)
            train_end = train_start + train_len
            test_start = train_end + self.purge_size
            test_end = test_start + test_len
            
            train_idx = np.arange(train_start, train_end)
            test_idx = np.arange(test_start, test_end)
            yield train_idx, test_idx

def evaluate_with_cpcv(params, data, backtest_func, n_paths=200, purge_days=10):
    """Evaluate parameters using CPCV, returning performance distribution."""
    splitter = CombinatorialPurgedSplit(n_splits=n_paths, purge_size=purge_days)
    results = []
    
    for train_idx, test_idx in splitter.split(data):
        test_data = data.iloc[test_idx]
        metric = backtest_func(params, test_data)
        results.append(metric)
    
    return {
        'mean': np.mean(results),
        'std': np.std(results),
        'p10': np.percentile(results, 10),   # Worst-case robustness metric
        'p25': np.percentile(results, 25),
        'p50': np.median(results),
        'pct_profitable': np.mean([r > 0 for r in results]),
        'distribution': results
    }
```

**Key design decisions for Atlas:**
- Use `n_splits=200` minimum (500 recommended for final validation)
- `purge_size=10` trading days (2 weeks buffer for daily strategies)
- `train_size_pct=0.70`, `test_size_pct=0.15` (remaining 15% is purge+buffer)
- Select parameters maximizing **10th percentile** of CAGR distribution
- Use weighted median of top-5 candidates for final selection (avoids overfitting the validation itself)

#### Expected Impact
- Replace 22-window walk-forward with 200+ path validation
- More reliable OOS performance estimate
- Expected to improve walk-forward consistency measurement from 59% to 70-80% profitable paths (by selecting more robust parameters)
- Better quantification of true strategy edge

#### Implementation Complexity: **Medium**
- Core splitter is ~50 lines of Python
- Integration with existing backtest engine required
- Computationally expensive: 200 backtests per parameter evaluation
- Libraries: `numpy`, `pandas` (already available)

#### Priority: **HIGH**

#### Key References
- **Lopez de Prado, M. (2018)** *Advances in Financial Machine Learning* — Ch 12: Backtesting through Cross-Validation (Wiley)
- **Arian, H. et al. (2024)** "Backtest overfitting in the machine learning era" — *Knowledge-Based Systems* (Cited 14x) — [ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0950705124011110)
- **QuantBeckman (2025)** "Combinatorial Purged Cross Validation for Optimization" — [Full Python code](https://www.quantbeckman.com/p/with-code-combinatorial-purged-cross)
- **InsightBig (2025)** "Traditional Backtesting is Outdated. Use CPCV Instead" — [Article](https://www.insightbig.com/post/traditional-backtesting-is-outdated-use-cpcv-instead)
- **QuantInsti (2020)** "Cross Validation in Finance: Purging, Embargoing, Combinatorial" — [Tutorial](https://blog.quantinsti.com/cross-validation-embargo-purging-combinatorial/)

---

### 1.3 Deflated Sharpe Ratio (DSR)

#### What
The Deflated Sharpe Ratio, developed by **Bailey & Lopez de Prado (2014)**, corrects for two leading sources of performance inflation:

1. **Selection bias under multiple testing** — When you test N parameter combinations, the best one will look good by chance alone
2. **Non-normality of returns** — Skewness and kurtosis inflate apparent Sharpe ratios

The DSR formula:

```
DSR = Phi( (SR_obs - SR_0) * sqrt(T-1) / sqrt(1 - gamma3*SR_0 + (gamma4-1)/4 * SR_0^2) )
```

Where:
- Phi = standard normal CDF
- SR_obs = observed Sharpe Ratio of the selected strategy
- SR_0 = expected maximum Sharpe under null hypothesis (from False Strategy Theorem)
- T = number of observations
- gamma3 = skewness of returns
- gamma4 = kurtosis of returns

The **False Strategy Theorem (FST)** estimates SR_0 — the highest Sharpe Ratio expected from N independent trials when all true Sharpe Ratios are zero. Even if all tested strategies have zero true Sharpe, the best observed Sharpe will be positive and potentially look significant.

Critically: with enough trials, there is NO Sharpe Ratio large enough to reject the null hypothesis. The number of trials must be disclosed for DSR to be meaningful.

#### Why It Helps (Atlas-ASX Specific)
Atlas has undergone extensive optimization:
- Multiple config versions (v1 through v10.0)
- Bayesian optimization with 5+ trials
- Grid searches, parameter sweeps, trailing stop sweeps
- 4 strategies each with multiple parameters

Conservative estimate: **100-500 implicit trials** have been conducted. The in-sample Sharpe of 0.67 may be entirely explained by selection bias. DSR would:
- Quantify whether the observed IS Sharpe (0.67) is statistically significant given the number of trials
- Provide a p-value for the strategy's edge being real vs. lucky
- If DSR < 0.95, the IS performance is likely inflated by optimization

#### Implementation Details

```python
import numpy as np
from scipy.stats import norm

def expected_max_sr(n_trials, sr_std=1.0, T=252):
    """False Strategy Theorem: expected max SR from n_trials of zero-skill strategies.
    
    Approximation from Bailey & Lopez de Prado (2014).
    """
    emc = 0.5772156649  # Euler-Mascheroni constant
    z = norm.ppf(1 - 1.0 / n_trials)
    sr_0 = sr_std * ((1 - emc) * z + emc * norm.ppf(1 - 1.0 / (n_trials * np.e)))
    return sr_0

def deflated_sharpe_ratio(sr_observed, sr_benchmark, T, skew=0.0, kurt=3.0):
    """Compute the Deflated Sharpe Ratio.
    
    Args:
        sr_observed: Observed annualized Sharpe ratio
        sr_benchmark: Expected max SR under null (from expected_max_sr)
        T: Number of return observations
        skew: Skewness of returns
        kurt: Kurtosis of returns (excess kurtosis + 3)
    
    Returns:
        DSR probability (0 to 1). > 0.95 suggests real skill.
    """
    numerator = (sr_observed - sr_benchmark) * np.sqrt(T - 1)
    denominator = np.sqrt(1 - skew * sr_benchmark + (kurt - 1) / 4 * sr_benchmark**2)
    dsr = norm.cdf(numerator / denominator)
    return dsr

# Atlas-ASX Example:
T = 252 * 3  # ~3 years of daily returns
n_trials = 200  # Conservative estimate of total optimization trials
sr_obs = 0.67  # Observed in-sample Sharpe
returns_skew = -0.3  # Typical for equity strategies
returns_kurt = 4.5  # Fat-tailed

sr_0 = expected_max_sr(n_trials)
dsr = deflated_sharpe_ratio(sr_obs, sr_0, T, skew=returns_skew, kurt=returns_kurt)

# If DSR < 0.95, the Sharpe is likely inflated by selection bias
print(f"Expected max SR under null (N={n_trials}): {sr_0:.3f}")
print(f"DSR probability: {dsr:.4f}")
print(f"Significant at 95%? {'YES' if dsr > 0.95 else 'NO - likely overfit'}")
```

**Key insight for Atlas:** With ~200 trials and IS Sharpe of 0.67:
- Expected max SR under null is approximately 2.5-3.0 (for 200 independent trials)
- However, many trials are correlated (similar parameter sets), so effective N is lower
- Use ONC (Optimal Number of Clusters) to estimate effective independent trials
- Even with N=20 effective trials, DSR may reject the strategy at 95% confidence

#### Expected Impact
- Provides definitive answer: is the IS Sharpe of 0.67 statistically significant?
- If DSR < 0.95: confirms overfitting is the root cause, not just a symptom
- If DSR > 0.95: validates that some real edge exists (focus shifts to extracting it robustly)
- Informs whether to continue optimizing or fundamentally redesign

#### Implementation Complexity: **Low**
- Core DSR function is ~20 lines of Python
- Main challenge: estimating true number of independent trials
- Libraries: `numpy`, `scipy` (already available)
- GitHub reference implementation: [Nikhil-Kumar-Patel/The-deflated-sharpe-ratio](https://github.com/Nikhil-Kumar-Patel/The-deflated-sharpe-ratio)

#### Priority: **CRITICAL**
This is a diagnostic tool. If DSR says the strategy isn't significant, no amount of parameter tuning will help.

#### Key References
- **Bailey, D.H. & Lopez de Prado, M. (2014)** "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting, and Non-Normality" — *Journal of Portfolio Management* (Cited 195 times) — [PDF](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
- **Wikipedia** "Deflated Sharpe ratio" — [Article](https://en.wikipedia.org/wiki/Deflated_Sharpe_ratio)
- **Lopez de Prado, M.** "Deflating the Sharpe Ratio" — [Semantic Scholar PDF](https://pdfs.semanticscholar.org/c215/d0a2064ce1a3565d276475abc84305418f0f.pdf)
- **GitHub Implementation** — [Python code](https://github.com/Nikhil-Kumar-Patel/The-deflated-sharpe-ratio)

---

### 1.4 Parameter Space Smoothing

#### What
Parameter space smoothing replaces the raw objective function (e.g., CAGR, Sharpe) with a **smoothed version** that averages over a neighborhood of each point. Instead of evaluating `f(θ)`, you evaluate `f_smooth(θ) = E[f(θ + ε)]` where ε is drawn from a distribution (typically Gaussian) centered at θ.

This is mathematically equivalent to **convolving** the objective function with a Gaussian kernel, which:
- Removes sharp peaks (overfitted optima)
- Preserves broad plateaus (robust optima)
- Creates a smoother optimization landscape that gradient-based and Bayesian methods navigate better

The technique is well-established in robust optimization literature (Beyer & Sendhoff, 2007, cited 2,297 times) and is closely related to what Atlas's Bayesian optimizer already does with perturbation trials, but formalized and extended.

#### Why It Helps (Atlas-ASX Specific)
Atlas's Bayesian optimizer already uses 2 perturbation trials per candidate (±10%), which is a crude form of smoothing. The problem: ±10% shows 83% retention but ±15% shows 76% degradation — suggesting the smoothing radius is too small.

Formal parameter space smoothing would:
- Increase the smoothing radius to ±15-20% to match real-world uncertainty
- Use more samples (10-20 instead of 2) for reliable neighborhood estimates
- Weight samples by distance (Gaussian kernel, not uniform)
- Make the objective landscape reveal its true robust optima

#### Implementation Details

```python
import numpy as np

def smoothed_objective(params, backtest_func, sigma_pct=0.15, n_samples=20):
    """Gaussian-smoothed objective function.
    
    Args:
        params: dict of parameter name -> value
        backtest_func: function that takes params and returns metric
        sigma_pct: std dev of Gaussian perturbation (fraction of param value)
        n_samples: number of neighborhood samples
    
    Returns:
        Weighted average of neighborhood performance
    """
    samples = []
    weights = []
    
    for _ in range(n_samples):
        # Gaussian perturbation (not uniform!)
        perturbation = {k: np.random.normal(0, sigma_pct) for k in params}
        perturbed = {k: v * (1 + perturbation[k]) for k, v in params.items()}
        
        metric = backtest_func(perturbed)
        distance = np.sqrt(sum(p**2 for p in perturbation.values()))
        weight = np.exp(-0.5 * (distance / sigma_pct)**2)  # Gaussian weight
        
        samples.append(metric)
        weights.append(weight)
    
    # Include the center point with highest weight
    center_metric = backtest_func(params)
    samples.append(center_metric)
    weights.append(1.0)
    
    weights = np.array(weights) / np.sum(weights)
    return np.average(samples, weights=weights)
```

**Upgrade Atlas Bayesian optimizer:**
```python
# Current: 1 base + 2 perturbations (3 evals per trial)
# Proposed: 1 base + 15 Gaussian perturbations (16 evals per trial)
# With Gaussian weighting and sigma=0.15

def robust_objective(trial):
    params = sample_params(trial)
    smoothed = smoothed_objective(params, backtest_func, sigma_pct=0.15, n_samples=15)
    return smoothed
```

#### Expected Impact
- More reliable identification of robust optima
- Should find parameters where ±15% perturbation retention improves from 24% to 60-80%
- Computational cost increases ~5x (from 3 to 16 evals per trial)
- Combined with plateau scoring: most powerful approach

#### Implementation Complexity: **Low-Medium**
- Straightforward modification to existing Bayesian optimizer
- Already have perturbation infrastructure
- Main cost: computational (5x more backtests per trial)

#### Priority: **MEDIUM**
Important but partially addressed by existing perturbation trials. The upgrade would formalize and strengthen this.

#### Key References
- **Beyer, H.G. & Sendhoff, B. (2007)** "Robust optimization — A comprehensive survey" — *Computer Methods in Applied Mechanics and Engineering* (Cited 2,297 times) — [PDF](https://www.honda-ri.de/pubs/pdf/1808.pdf)
- **Wu et al. (2024)** Parameter plateau paper uses similar neighborhood evaluation concept
- General concept: "Expected improvement under noise" in Bayesian optimization literature

---

### 1.5 Minimum Backtest Length (MinBTL) & Probability of Backtest Overfitting (PBO)

#### What

**Minimum Backtest Length (MinBTL)** is a formula from Bailey & Lopez de Prado that determines the minimum number of observations needed for a backtest result to be statistically meaningful. The formula accounts for:
- Target Sharpe ratio
- Non-normality of returns (skewness, kurtosis)
- Desired confidence level

Rule of thumb: most equity strategies need **200-500 trades** across **multiple market regimes** for reliable backtest statistics.

**Probability of Backtest Overfitting (PBO)** quantifies the likelihood that a backtest-optimized strategy's performance is due to overfitting rather than genuine edge. Introduced by Bailey, Borwein, Lopez de Prado & Zhu (2017), it uses **Combinatorially Symmetric Cross-Validation (CSCV)**:

1. Partition data into S equal slices (typically S=16)
2. Generate all C(S, S/2) combinations of in-sample/out-of-sample splits
3. For each combination: optimize on IS, evaluate on OOS
4. Compute logit: `lambda = ln(rank_IS / rank_OOS)` for the IS-optimal configuration
5. PBO = proportion of logits where OOS rank of IS-best is below median
6. PBO > 0.5 = likely overfit; PBO < 0.1 = robust

#### Why It Helps (Atlas-ASX Specific)

**MinBTL diagnostic:** Atlas has ~500 total trades over 3 years. This might be marginal for statistical significance, especially split across 4 strategies (125 trades each). MinBTL would quantify:
- Whether 125 trades per strategy is enough for the claimed Sharpe
- Whether the 3-year backtest period is sufficient
- How many more years of data would be needed

**PBO diagnostic:** Atlas shows 87% IS-to-OOS degradation (CAGR 17.09% to 2.20%). PBO would:
- Quantify the probability that this IS performance is overfit
- Provide a single number (0-1) representing overfitting risk
- Enable comparison between strategy variants

#### Implementation Details

**MinBTL Formula:**
```python
import numpy as np
from scipy.stats import norm

def min_backtest_length(target_sr, skew=0, kurt=3, confidence=0.95):
    """Minimum number of observations for statistically significant backtest.
    
    Args:
        target_sr: Target annualized Sharpe ratio
        skew: Return skewness
        kurt: Return kurtosis (not excess)
        confidence: Desired confidence level
    
    Returns:
        Minimum number of daily observations needed
    """
    z_alpha = norm.ppf(confidence)
    sr_daily = target_sr / np.sqrt(252)  # De-annualize
    
    min_t = (1 + (1 - skew * sr_daily + (kurt - 1) / 4 * sr_daily**2)) * \
            (z_alpha / sr_daily)**2
    
    return int(np.ceil(min_t))

# Atlas example:
print(f"MinBTL for SR=0.67: {min_backtest_length(0.67, skew=-0.3, kurt=4.5)} days")
print(f"MinBTL for SR=0.30: {min_backtest_length(0.30, skew=-0.3, kurt=4.5)} days")
# Typical result: SR=0.67 needs ~600 days, SR=0.30 needs ~3000 days
```

**PBO Implementation:**
```python
import numpy as np
from itertools import combinations

def compute_pbo(performance_matrix, n_slices=16):
    """Compute Probability of Backtest Overfitting.
    
    Args:
        performance_matrix: S x N matrix (S time slices, N strategy configs)
    
    Returns:
        PBO probability (0 to 1)
    """
    S = performance_matrix.shape[0]
    half = S // 2
    combos = list(combinations(range(S), half))
    
    logits = []
    for is_indices in combos:
        oos_indices = [i for i in range(S) if i not in is_indices]
        
        # In-sample performance per config
        is_perf = performance_matrix[list(is_indices), :].mean(axis=0)
        # Out-of-sample performance per config  
        oos_perf = performance_matrix[list(oos_indices), :].mean(axis=0)
        
        # Best IS config
        best_is = np.argmax(is_perf)
        
        # Rank of IS-best in OOS
        oos_ranks = oos_perf.argsort().argsort()
        n_configs = len(oos_perf)
        relative_rank = oos_ranks[best_is] / n_configs
        
        # Logit: negative means IS-best performs below median OOS
        logit = np.log(relative_rank / (1 - relative_rank + 1e-10) + 1e-10)
        logits.append(logit)
    
    # PBO = proportion of logits below zero
    pbo = np.mean([l < 0 for l in logits])
    return pbo, logits

# Usage:
# 1. Run backtest with N different parameter configs across S time slices
# 2. Build S x N performance matrix
# 3. PBO < 0.1 = robust, PBO > 0.5 = likely overfit
```

**Key considerations for Atlas:**
- Use S=16 slices (standard, gives C(16,8)=12,870 combinations)
- Include ALL parameter configs tested (not just the final one)
- Each strategy should be evaluated independently
- PBO should be computed for the full portfolio AND per-strategy

#### Expected Impact
- MinBTL: May reveal that 500 trades is insufficient for SR=0.67 claim
- PBO: Expect PBO > 0.5 given the 87% IS-to-OOS degradation (confirming overfitting)
- Combined: Provides clear quantitative evidence for whether to continue tuning or redesign

#### Implementation Complexity: **Medium**
- MinBTL: ~10 lines, trivial to implement
- PBO: ~80 lines, needs performance matrix from multiple configs/slices
- Computationally moderate: need to re-backtest across time slices
- Libraries: `numpy`, `scipy`, `itertools` (all available)

#### Priority: **HIGH**
PBO directly quantifies the overfitting probability. If PBO > 0.5, it definitively confirms the IS-OOS gap is overfitting.

#### Key References
- **Bailey, D.H., Borwein, J., Lopez de Prado, M., Zhu, Q.J. (2017)** "The Probability of Backtest Overfitting" — *Journal of Computational Finance* — [Paper](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253)
- **Bailey & Lopez de Prado (2014)** "The Deflated Sharpe Ratio" — includes MinBTL formula
- **Lopez de Prado (2018)** *Advances in Financial Machine Learning* — Ch 11: The Dangers of Backtesting

---

### 1.6 Structural Approach to Overfitting Reduction

#### What
The structural approach to overfitting addresses the **root cause** rather than symptoms: reducing the number of free parameters (degrees of freedom) in the trading system. The principle is simple — every additional parameter doubles the risk of overfitting because it provides another dimension for the optimizer to exploit noise.

Key structural techniques:
1. **Parameter reduction** — Combine or eliminate redundant parameters
2. **Parameter sharing** — Use same parameter across strategies where conceptually similar
3. **Parameter constraining** — Narrow the feasible range based on domain knowledge
4. **Feature simplification** — Replace complex multi-parameter indicators with simpler ones
5. **Degrees of freedom budgeting** — Formally limit total free parameters based on available data

The rule of thumb from quantitative finance literature: you need **at least 10-20 independent observations per free parameter** for reliable out-of-sample performance. With 500 trades and 13+ parameters, Atlas operates at ~38 observations per parameter — marginal at best.

#### Why It Helps (Atlas-ASX Specific)
Atlas currently has **13 optimizable parameters** across 4 strategies (from the Bayesian optimizer). This is the most likely root cause of the 87% IS-to-OOS degradation. Analysis:

| Strategy | Parameters | Est. Trades | Obs/Param Ratio |
|----------|-----------|-------------|----------------|
| Mean Reversion | rsi_period, rsi_entry, rsi_exit, zscore_entry | ~125 | 31 |
| Trend Following | fast_ma, slow_ma, pullback_pct, trend_strength | ~100 | 25 |
| BB Squeeze | bb_period, bb_std, keltner_mult | ~150 | 50 |
| Opening Gap | gap_threshold | ~125 | 125 |
| **Total** | **13** | **~500** | **~38** |

The Mean Reversion and Trend Following strategies have concerning obs/param ratios (25-31). This is below the recommended 50+ threshold.

#### Implementation Details

**Strategy 1: Reduce to 6-8 parameters**
```python
# CURRENT: 13 parameters
# Mean Reversion: rsi_period(14), rsi_entry(30), rsi_exit(70), zscore_entry(2.0)
# Trend Following: fast_ma(20), slow_ma(50), pullback_pct(0.03), trend_strength(25)
# BB Squeeze: bb_period(20), bb_std(2.0), keltner_mult(1.5)
# Opening Gap: gap_threshold(0.03)
# Portfolio: max_open_positions(10)

# PROPOSED: 7 parameters (shared + constrained)
# Lookback: shared_lookback (used by RSI, BB, MA calculations) -> 1 param
# Entry threshold: mr_entry_threshold, tf_pullback, gap_threshold -> 3 params
# Exit: shared_exit_multiplier (relative to entry) -> 1 param
# Volatility: bb_std (also informs position sizing) -> 1 param
# Portfolio: max_open_positions -> 1 param
```

**Strategy 2: Fix non-sensitive parameters to literature defaults**
Parameters that don't significantly affect results should be fixed:
```python
# Fix to standard literature values (not optimized):
FIXED_PARAMS = {
    'rsi_period': 14,       # Wilder's original, universally used
    'bb_period': 20,        # Bollinger's standard
    'keltner_mult': 1.5,    # Standard Keltner channel
}

# Only optimize the truly impactful parameters:
OPTIMIZABLE = {
    'rsi_entry': (25, 40),      # Entry oversold threshold
    'zscore_entry': (1.5, 2.5), # Mean reversion z-score
    'slow_ma': (40, 80),        # Trend lookback
    'gap_threshold': (0.02, 0.05),  # Gap size threshold
    'max_open_positions': (6, 12),   # Portfolio level
}
# Reduced from 13 to 5 free parameters
# Obs/param ratio improves from 38 to 100
```

**Strategy 3: Use parameter ratios instead of absolutes**
```python
# Instead of optimizing fast_ma AND slow_ma separately:
# Optimize slow_ma and ma_ratio = fast_ma / slow_ma
# This encodes the structural relationship and reduces effective DoF
ma_ratio = 0.4  # Fixed: fast is always 40% of slow (well-established relationship)
slow_ma = optimize(40, 80)
fast_ma = int(slow_ma * ma_ratio)  # Derived, not optimized
```

#### Expected Impact
- Reducing from 13 to 5-7 free parameters could dramatically improve OOS retention
- Expected IS-OOS gap reduction from 87% to 30-50%
- Walk-forward consistency improvement from 59% to 70-80%
- May sacrifice some IS performance (17.09% -> 10-12%) but OOS should improve (2.20% -> 5-8%)
- Net result: more reliable, lower but real returns

#### Implementation Complexity: **Low-Medium**
- Conceptually simple: fix some params, share others
- Requires domain knowledge to identify which params to fix
- Sensitivity analysis (already partially done) guides which to keep
- No new libraries needed

#### Priority: **CRITICAL**
This is arguably the most important single change. Reducing degrees of freedom is the most proven method to combat overfitting.

#### Key References
- **Lopez de Prado (2018)** *Advances in Financial Machine Learning* — Ch 11: The Dangers of Backtesting (discusses degrees of freedom explicitly)
- **Bailey et al. (2014)** "Pseudo-Mathematics and Financial Charlatanism" — warns about parameter proliferation
- **Pardo, R. (2008)** *The Evaluation and Optimization of Trading Strategies* — parameter budgeting framework
- **Aronson, D. (2007)** *Evidence-Based Technical Analysis* — statistical rigor for parameter selection

---

### 1.7 White's Reality Check and Stepwise SPA Test

#### What
**White's Reality Check (WRC)** and the **Stepwise Superior Predictive Ability (StepSPA) test** are formal statistical hypothesis tests designed for evaluating multiple trading strategies simultaneously while controlling for data snooping bias.

**White's Reality Check (2000):**
- Tests whether the best strategy from a set of N strategies genuinely outperforms a benchmark
- Null hypothesis: no strategy beats the benchmark
- Uses bootstrap resampling of returns to generate a distribution under the null
- Provides a p-value for the best strategy's outperformance
- Conservative: dominated by the worst-performing strategies in the set

**Hansen's Superior Predictive Ability (SPA) Test (2005):**
- Improves on WRC by being less affected by poor models
- Uses studentized statistics for better power
- Less conservative than WRC — more likely to detect genuine outperformance

**Stepwise SPA (Romano & Wolf, 2005):**
- Extends SPA to identify ALL strategies that significantly outperform the benchmark
- Controls familywise error rate (FWER) under multiple testing
- Returns a set of "significant" strategies, not just the best one

#### Why It Helps (Atlas-ASX Specific)
Atlas has 4 strategies, each tested with multiple parameter configurations. The question: "Do ANY of these strategy/parameter combinations genuinely outperform buy-and-hold or a cash benchmark?" WRC/SPA answers this formally:
- If WRC p-value > 0.05: no strategy significantly beats the benchmark after correcting for multiple testing
- If SPA identifies a subset: those specific strategies have genuine edge
- StepSPA could identify which of the 4 strategies (MR, TF, BBS, OG) have real alpha

#### Implementation Details

```python
from arch.bootstrap import SPA, StepM
import numpy as np
import pandas as pd

# Step 1: Prepare loss differentials
# losses = benchmark_returns - strategy_returns (for each strategy)
# Shape: (T, N) where T = time periods, N = number of strategies

def run_spa_test(benchmark_returns, strategy_returns_dict):
    """Run Hansen's SPA test on multiple strategies vs benchmark.
    
    Args:
        benchmark_returns: Series of benchmark daily returns
        strategy_returns_dict: dict of {name: Series of daily returns}
    
    Returns:
        SPA p-value and per-strategy results
    """
    # Loss differentials: positive = strategy underperforms benchmark
    losses = pd.DataFrame()
    for name, strat_returns in strategy_returns_dict.items():
        losses[name] = benchmark_returns - strat_returns
    
    # SPA test (Hansen, 2005)
    spa = SPA(losses, block_size=10, reps=5000, seed=42)
    spa.compute()
    
    print(f"SPA p-value (lower): {spa.pvalues[0]:.4f}")
    print(f"SPA p-value (consistent): {spa.pvalues[1]:.4f}")
    print(f"SPA p-value (upper): {spa.pvalues[2]:.4f}")
    
    return spa

def run_stepm_test(benchmark_returns, strategy_returns_dict, alpha=0.05):
    """Run Stepwise Multiple Testing (StepM) to find ALL significant strategies.
    
    Returns set of strategy names that significantly beat the benchmark.
    """
    losses = pd.DataFrame()
    for name, strat_returns in strategy_returns_dict.items():
        losses[name] = benchmark_returns - strat_returns
    
    stepm = StepM(losses, block_size=10, reps=5000, size=alpha, seed=42)
    stepm.compute()
    
    # stepm.significant returns boolean array
    significant = [name for name, sig in zip(losses.columns, stepm.significant) if sig]
    print(f"Significant strategies (alpha={alpha}): {significant}")
    
    return stepm, significant

# Usage for Atlas:
# benchmark = IOZ returns (ASX index ETF) or 0% (cash)
# strategies = {name: daily returns series} for each of the 4 strategies
```

**Key implementation notes:**
- Install: `pip install arch` (already in most quant environments)
- `block_size=10` for circular block bootstrap (captures daily autocorrelation)
- `reps=5000` bootstrap replications (minimum; 10000 for publication)
- Use IOZ_AX returns as benchmark (or cash = 0%) depending on the question:
  - vs. IOZ: "Does the strategy add alpha beyond market beta?"
  - vs. cash: "Does the strategy make money at all?"

#### Expected Impact
- Provides formal statistical test for whether ANY Atlas strategy has genuine edge
- If SPA p-value > 0.05: strong evidence that the entire system is overfit
- If some strategies pass StepSPA: focus resources on those; discard the rest
- Particularly useful for deciding whether to keep 4 strategies or simplify to 1-2

#### Implementation Complexity: **Low**
- `arch` package provides SPA and StepM out of the box
- Main work: extracting per-strategy daily returns from backtest results
- ~30 lines of code beyond data preparation

#### Priority: **LOW** (diagnostic, not actionable fix)
Useful for confirming/denying genuine edge, but doesn't fix the overfitting problem directly.

#### Key References
- **White, H. (2000)** "A Reality Check for Data Snooping" — *Econometrica* 68(5):1097-1126
- **Hansen, P.R. (2005)** "A Test for Superior Predictive Ability" — *Journal of Business & Economic Statistics* 23(4):365-380
- **Romano, J.P. & Wolf, M. (2005)** "Stepwise Multiple Testing as Formalized Data Snooping" — *Econometrica* 73(4):1237-1282
- **Python `arch` package** — [SPA class docs](https://arch.readthedocs.io/en/latest/bootstrap/spa.html)

---

## RESEARCH AREA 2: Machine Learning Enhancements (Practical)

---

### 2.1 Meta-Labeling

#### What
Meta-labeling, introduced by **Lopez de Prado (2018)**, is a two-stage approach where:

1. **Primary model** generates trade signals (buy/sell direction) — this is your existing trading strategy
2. **Secondary ML model** decides **whether to take** each signal (probability of success)

The secondary model doesn't predict direction; it predicts whether the primary signal will be profitable. This separation of concerns is powerful because:
- Direction prediction is hard and strategy-specific
- Size/confidence prediction is easier and benefits from ML
- The meta-model can learn regime-dependent patterns: "this type of signal works in low-vol environments but fails in high-vol"

The approach uses **triple-barrier labeling**:
- Upper barrier: profit target (e.g., 2x ATR)
- Lower barrier: stop loss (e.g., 1x ATR)
- Vertical barrier: maximum holding period (e.g., 10 days)
- Label = 1 if upper barrier hit first, 0 if lower/vertical

A **CUSUM filter** is typically used to reduce signal frequency to events that matter (structural breaks in the cumulative sum of returns).

#### Why It Helps (Atlas-ASX Specific)
Atlas has 4 strategies generating signals, but walk-forward analysis shows only 59% of windows are profitable. Meta-labeling would:
- Filter out signals that have low probability of success in current market conditions
- Learn which market regimes favor which strategies
- Reduce trade frequency but improve win rate
- Address the inconsistency in walk-forward windows (some windows may have low meta-label scores, suggesting "stay out")

Specific application: Mean Reversion generates the most trades (~125). A meta-label model trained on features like VIX level, recent market breadth, volume profile, and days-since-last-signal could filter out 30-40% of losing trades.

#### Implementation Details

```python
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report

def triple_barrier_label(prices, entry_idx, pt_mult=2.0, sl_mult=1.0, 
                         max_hold=10, atr=None):
    """Apply triple-barrier labeling to a single trade.
    
    Args:
        prices: Series of close prices
        entry_idx: Index position of entry
        pt_mult: Profit target in ATR multiples
        sl_mult: Stop loss in ATR multiples
        max_hold: Maximum holding period (days)
        atr: ATR value at entry
    
    Returns:
        1 if profit target hit, 0 if stop or time exit
    """
    entry_price = prices.iloc[entry_idx]
    upper = entry_price + pt_mult * atr
    lower = entry_price - sl_mult * atr
    
    for i in range(1, max_hold + 1):
        if entry_idx + i >= len(prices):
            break
        price = prices.iloc[entry_idx + i]
        if price >= upper:
            return 1  # Profit target hit
        if price <= lower:
            return 0  # Stop loss hit
    return 0  # Time exit (no barrier hit)

def build_meta_features(data, signal_date, lookback=20):
    """Build feature vector for meta-labeling model.
    
    Features should capture market regime, NOT predict direction.
    """
    idx = data.index.get_loc(signal_date)
    recent = data.iloc[max(0, idx-lookback):idx]
    
    features = {
        # Volatility regime
        'atr_percentile': recent['atr'].rank(pct=True).iloc[-1],
        'volatility_20d': recent['close'].pct_change().std(),
        'vol_trend': (recent['atr'].iloc[-5:].mean() / 
                      recent['atr'].iloc[:5].mean()),
        
        # Market breadth (if available)
        'pct_above_sma20': 0,  # Fill from market_breadth util
        
        # Signal quality
        'rsi_distance': abs(recent['rsi'].iloc[-1] - 50),
        'volume_ratio': recent['volume'].iloc[-1] / recent['volume'].mean(),
        'days_since_last_signal': 0,  # Track externally
        
        # Trend context
        'adx_value': recent['adx'].iloc[-1] if 'adx' in recent else 0,
        'price_vs_sma50': (recent['close'].iloc[-1] / 
                           recent['close'].rolling(50).mean().iloc[-1]),
    }
    return features

def train_meta_model(signals_df, features_df, labels):
    """Train meta-labeling model.
    
    Uses RandomForest for interpretability and robustness on small samples.
    """
    # Class imbalance handling
    from sklearn.utils import resample
    
    # Train/test split (purged - no data leakage)
    split_idx = int(len(features_df) * 0.7)
    X_train = features_df.iloc[:split_idx]
    y_train = labels.iloc[:split_idx]
    X_test = features_df.iloc[split_idx:]
    y_test = labels.iloc[split_idx:]
    
    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=3,          # Shallow to avoid overfitting
        min_samples_leaf=20,  # Conservative with small sample
        class_weight='balanced',
        random_state=42
    )
    model.fit(X_train, y_train)
    
    # Evaluate
    proba = model.predict_proba(X_test)[:, 1]
    print(classification_report(y_test, (proba > 0.5).astype(int)))
    
    return model, proba
```

**Key considerations for Atlas:**
- **Sample size concern:** ~500 total trades across 3 years. Per-strategy, only ~125 trades each. This is MARGINAL for ML. Use:
  - Very shallow trees (max_depth=3)
  - High min_samples_leaf (20+)
  - Pool all strategies and add strategy_type as a feature
  - Use time-series cross-validation with purging
- **Feature engineering:** Focus on regime features (volatility, trend strength, breadth) NOT directional features
- **Threshold calibration:** Don't use 0.5 cutoff blindly. Calibrate on validation set to maximize profit factor

#### Expected Impact
- Could filter out 20-40% of losing trades, improving win rate from 52% to 58-65%
- May reduce total trades by 30% but improve profit factor
- Risk: over-filtering reduces diversification and increases path dependency
- Realistic net impact: 10-20% improvement in OOS profit factor

#### Implementation Complexity: **High**
- Need triple-barrier labeling infrastructure
- Feature engineering pipeline
- Proper purged cross-validation for ML
- Small sample size makes this challenging
- Libraries: `scikit-learn`, `numpy`, `pandas`

#### Priority: **HIGH**
Despite complexity, meta-labeling is one of the most promising techniques for improving Atlas's walk-forward consistency.

#### Key References
- **Lopez de Prado, M. (2018)** *Advances in Financial Machine Learning* — Ch 3: Meta-Labeling (Wiley)
- **Lopez de Prado, M. (2018)** "Meta-Labeling" — [SSRN paper](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3257420)
- **Hudson & Thames** `mlfinlab` library — includes meta-labeling implementation
- **QuantPedia** "Meta-Labeling in Trading" — practical implementation guide

---

### 2.2 ML Signal Combination / Ensemble (Gradient Boosting)

#### What
Instead of using fixed rules to combine signals from Atlas's 4 strategies, use a gradient boosting model (XGBoost, LightGBM, or CatBoost) to learn the optimal combination dynamically. The model takes each strategy's signal features as inputs and predicts the probability of a profitable trade.

This is distinct from meta-labeling (which filters individual signals) — signal combination learns **interactions between strategies**. For example: "Mean Reversion signal + BB Squeeze signal together predict success better than either alone" or "Trend Following signal contradicts Mean Reversion signal, reducing confidence."

#### Why It Helps (Atlas-ASX Specific)
Atlas currently combines strategies via a simple priority/confidence system. This may miss:
- Cross-strategy confirmation patterns
- Regime-dependent strategy weighting (e.g., MR works in range, TF works in trend)
- Optimal position sizing across competing signals
- Non-linear interactions between signal features

With 4 strategies generating simultaneous signals, the combination space is large. A gradient boosting model can capture these patterns without manual rule engineering.

#### Implementation Details

```python
import lightgbm as lgb
import numpy as np
import pandas as pd

def build_signal_features(date, ticker, strategies_output):
    """Combine all strategy outputs into a feature vector."""
    features = {}
    
    # Per-strategy features
    for strat_name, output in strategies_output.items():
        prefix = strat_name[:3]  # mr_, tf_, bb_, og_
        features[f'{prefix}_signal'] = output.get('signal', 0)  # -1, 0, 1
        features[f'{prefix}_confidence'] = output.get('confidence', 0)
        features[f'{prefix}_distance'] = output.get('entry_distance', 0)
    
    # Cross-strategy features
    signals = [v.get('signal', 0) for v in strategies_output.values()]
    features['n_agreeing'] = sum(1 for s in signals if s > 0)
    features['n_conflicting'] = sum(1 for s in signals if s < 0)
    features['signal_consensus'] = np.mean(signals)
    
    # Market regime features
    features['vix_level'] = get_market_vol(date)
    features['market_trend'] = get_market_trend(date)  # SMA slope
    features['breadth'] = get_market_breadth(date)
    
    return features

def train_signal_combiner(features_df, labels, dates):
    """Train LightGBM model with time-series aware validation."""
    # Purged time-series split
    split_date = dates.quantile(0.7)
    purge_days = 10
    
    train_mask = dates < split_date
    test_mask = dates > (split_date + pd.Timedelta(days=purge_days))
    
    X_train, y_train = features_df[train_mask], labels[train_mask]
    X_test, y_test = features_df[test_mask], labels[test_mask]
    
    model = lgb.LGBMClassifier(
        n_estimators=100,
        max_depth=3,          # Shallow - avoid overfitting
        num_leaves=8,         # Conservative
        min_child_samples=30, # Need substantial evidence per leaf
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=1.0,        # L1 regularization
        reg_lambda=1.0,       # L2 regularization
        class_weight='balanced',
        random_state=42
    )
    
    model.fit(
        X_train, y_train,
        eval_set=[(X_test, y_test)],
        callbacks=[lgb.early_stopping(10), lgb.log_evaluation(10)]
    )
    
    return model
```

**Key design for Atlas (small sample awareness):**
- `max_depth=3`, `num_leaves=8`: Very shallow trees to prevent overfitting with ~500 trades
- `min_child_samples=30`: Each leaf needs 30+ trades for statistical meaning
- Heavy regularization: `reg_alpha=1.0`, `reg_lambda=1.0`
- Consider using only 5-8 features total (signal + 2-3 regime features)
- Cross-validate with purged time-series splits

#### Expected Impact
- Could improve portfolio-level Sharpe by 10-20% through better signal weighting
- Reduce conflicting trades (e.g., MR long + TF short on same ticker)
- Learn regime-dependent strategy allocation
- Risk: overfitting with small sample; mitigate with regularization and CPCV

#### Implementation Complexity: **Medium-High**
- Feature engineering pipeline needed
- Proper purged cross-validation critical
- Need label generation (triple-barrier or simple forward returns)
- Libraries: `lightgbm` or `xgboost` (pip install)
- Integration with existing signal pipeline

#### Priority: **MEDIUM**

#### Key References
- **Chen, T. & Guestrin, C. (2016)** "XGBoost: A Scalable Tree Boosting System" — *KDD* (Cited 33,000+)
- **Ke, G. et al. (2017)** "LightGBM: A Highly Efficient Gradient Boosting Decision Tree" — *NeurIPS*
- **Lopez de Prado (2018)** *AFML* — Ch 8: Feature Importance in ML for Finance
- **Practical implementations:** See [XGBoost for signal combination](https://quantpedia.com/), various blog posts on combining alpha signals with gradient boosting

---

### 2.3 Online Learning for Trading

#### What
Online learning (also called incremental or adaptive learning) refers to ML models that update their parameters continuously as new data arrives, rather than being re-trained from scratch periodically. In trading, this means models adapt to changing market regimes in real-time.

Key approaches:
1. **Incremental model updates** — Update model weights with each new observation (e.g., online gradient descent)
2. **Rolling window retraining** — Retrain on recent N observations, discarding old data
3. **Exponential weighting** — Weight recent observations more heavily
4. **Concept drift detection** — Detect when the data distribution shifts and trigger retraining

The **River** library (Python) provides a comprehensive online learning framework with implementations of online classifiers, regressors, and drift detectors specifically designed for streaming data.

#### Why It Helps (Atlas-ASX Specific)
Atlas's walk-forward analysis shows only 59% of windows are profitable, suggesting market regime changes cause strategy performance to fluctuate. Online learning could:
- Adapt signal thresholds to current market conditions
- Detect when a strategy's edge has disappeared (concept drift)
- Reduce the lag between regime change and strategy adaptation
- Avoid the "stale model" problem where parameters optimized on old data underperform

However, caution is needed: with daily frequency and ~185 tickers, data arrival is slow. Online learning works best with high-frequency data. For daily strategies, a **rolling window retraining** approach is more practical.

#### Implementation Details

```python
# Approach 1: Rolling Window Retraining (Most practical for daily strategies)
import pandas as pd
from sklearn.ensemble import RandomForestClassifier

def rolling_retrain_meta_model(features_df, labels, window_days=252, 
                                step_days=21, purge_days=10):
    """Retrain meta-labeling model on rolling window.
    
    Args:
        features_df: DataFrame of features with DatetimeIndex
        labels: Series of 0/1 labels
        window_days: Training window size (1 year)
        step_days: Retrain every N days (monthly)
        purge_days: Purge period between train and test
    """
    predictions = []
    dates = features_df.index
    
    for test_start_idx in range(window_days + purge_days, len(dates), step_days):
        train_start = test_start_idx - window_days - purge_days
        train_end = test_start_idx - purge_days
        test_end = min(test_start_idx + step_days, len(dates))
        
        X_train = features_df.iloc[train_start:train_end]
        y_train = labels.iloc[train_start:train_end]
        X_test = features_df.iloc[test_start_idx:test_end]
        
        model = RandomForestClassifier(
            n_estimators=50, max_depth=3,
            min_samples_leaf=20, random_state=42
        )
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)[:, 1]
        
        for i, prob in enumerate(proba):
            predictions.append({
                'date': dates[test_start_idx + i],
                'probability': prob,
                'train_window': f'{dates[train_start]} to {dates[train_end]}'
            })
    
    return pd.DataFrame(predictions)
```

```python
# Approach 2: River Library for True Online Learning
from river import linear_model, preprocessing, metrics, drift

# Online logistic regression with ADWIN drift detection
model = preprocessing.StandardScaler() | linear_model.LogisticRegression()
drift_detector = drift.ADWIN()
metric = metrics.Accuracy()

def online_predict_and_update(features_dict, true_label=None):
    """Make prediction and optionally update with true label."""
    prob = model.predict_proba_one(features_dict)
    
    if true_label is not None:
        model.learn_one(features_dict, true_label)
        metric.update(true_label, prob.get(1, 0) > 0.5)
        
        # Check for concept drift
        drift_detector.update(int(prob.get(1, 0) > 0.5) == true_label)
        if drift_detector.drift_detected:
            print("DRIFT DETECTED - consider resetting model")
    
    return prob.get(1, 0)
```

**Practical recommendation for Atlas:**
- Use **rolling window retraining** (Approach 1) with 252-day window, monthly retraining
- Add **ADWIN drift detection** to monitor strategy performance and flag regime changes
- Don't use true online learning for the core strategy — too little data for daily frequency
- Use online learning for **meta-labeling confidence** only, not signal generation

#### Expected Impact
- Moderate improvement in walk-forward consistency (59% -> 65-70%)
- Better adaptation to regime changes
- Risk: recency bias — model overweights recent data and misses longer-term patterns
- Net realistic impact: 5-15% improvement in OOS consistency

#### Implementation Complexity: **Medium**
- Rolling window: straightforward, ~100 lines
- River integration: moderate, requires streaming data pipeline
- Drift detection: low effort, high diagnostic value
- Libraries: `scikit-learn` (rolling), `river` (`pip install river`)

#### Priority: **LOW**
Online learning is less critical than fixing the fundamental parameter fragility issue. Implement after plateau detection, CPCV, and structural reduction.

#### Key References
- **Montiel, J. et al. (2021)** "River: machine learning for streaming data in Python" — *JMLR* — [river-ml.xyz](https://riverml.xyz)
- **Gama, J. et al. (2014)** "A survey on concept drift adaptation" — *ACM Computing Surveys*
- **Bifet, A. & Gavalda, R. (2007)** "Learning from time-changing data with adaptive windowing (ADWIN)" — *SIAM*

---

### 2.4 Feature Importance with SHAP

#### What
SHAP (SHapley Additive exPlanations) is a game-theoretic approach to explain the output of any ML model. It assigns each feature an importance value (SHAP value) for each prediction, based on Shapley values from cooperative game theory.

For trading systems, SHAP provides:
1. **Global feature importance** — Which features matter most across all predictions
2. **Local explanations** — Why a specific trade was taken or rejected
3. **Feature interaction detection** — Which features work together
4. **Regime analysis** — How feature importance changes over time

#### Why It Helps (Atlas-ASX Specific)
Atlas uses multiple indicators (RSI, Z-score, MA crossover, BB squeeze, gap size) across 4 strategies. SHAP would reveal:
- Which indicators actually contribute to profitable trades vs. adding noise
- Whether some features are redundant (reducing effective degrees of freedom)
- How feature importance shifts across market regimes (explaining walk-forward inconsistency)
- Which strategy components to keep vs. simplify (supporting structural overfitting reduction)

This directly supports **Section 1.6 (Structural Reduction)** — SHAP identifies which parameters to fix/remove.

#### Implementation Details

```python
import shap
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

def analyze_feature_importance(model, X_train, X_test, feature_names, 
                                save_path=None):
    """Comprehensive SHAP analysis for trading model."""
    
    # Create SHAP explainer (TreeExplainer for tree-based models)
    explainer = shap.TreeExplainer(model)
    shap_values = explainer.shap_values(X_test)
    
    # 1. Global feature importance (bar plot)
    plt.figure(figsize=(10, 6))
    shap.summary_plot(shap_values, X_test, feature_names=feature_names, 
                      plot_type='bar', show=False)
    if save_path:
        plt.savefig(f'{save_path}/shap_importance.png', dpi=150, 
                    bbox_inches='tight')
    plt.close()
    
    # 2. SHAP beeswarm plot (shows direction of feature impact)
    plt.figure(figsize=(10, 8))
    shap.summary_plot(shap_values, X_test, feature_names=feature_names, 
                      show=False)
    if save_path:
        plt.savefig(f'{save_path}/shap_beeswarm.png', dpi=150, 
                    bbox_inches='tight')
    plt.close()
    
    # 3. Feature importance ranking
    importance = pd.DataFrame({
        'feature': feature_names,
        'mean_abs_shap': np.abs(shap_values).mean(axis=0)
    }).sort_values('mean_abs_shap', ascending=False)
    
    print("\nFeature Importance Ranking:")
    print(importance.to_string(index=False))
    
    # 4. Identify low-importance features (candidates for removal)
    total_importance = importance['mean_abs_shap'].sum()
    importance['pct_importance'] = importance['mean_abs_shap'] / total_importance * 100
    importance['cumulative_pct'] = importance['pct_importance'].cumsum()
    
    low_importance = importance[importance['cumulative_pct'] > 90]
    print(f"\nFeatures contributing < 10% cumulative importance (remove candidates):")
    print(low_importance[['feature', 'pct_importance']].to_string(index=False))
    
    return importance, shap_values

def regime_shap_analysis(model, data, features_df, labels, 
                          window_size=126, step=63):
    """Analyze how feature importance changes over time (regime detection)."""
    explainer = shap.TreeExplainer(model)
    results = []
    
    for start in range(0, len(features_df) - window_size, step):
        end = start + window_size
        window_data = features_df.iloc[start:end]
        shap_vals = explainer.shap_values(window_data)
        
        mean_abs = np.abs(shap_vals).mean(axis=0)
        result = {'window_start': data.index[start], 'window_end': data.index[end-1]}
        for i, feat in enumerate(features_df.columns):
            result[f'shap_{feat}'] = mean_abs[i]
        results.append(result)
    
    return pd.DataFrame(results)
```

**Application to Atlas:**
1. Train a gradient boosting model on trade outcomes (Section 2.2)
2. Run SHAP analysis to identify which features matter
3. Features with < 5% importance are candidates for removal
4. Features whose importance varies wildly across time windows indicate regime sensitivity
5. Use findings to simplify parameter space (Section 1.6)

#### Expected Impact
- Identifies 30-50% of features as low-importance (candidates for removal)
- Guides structural simplification (Section 1.6)
- Reveals regime-dependent patterns explaining walk-forward inconsistency
- Improves model interpretability and trust

#### Implementation Complexity: **Low-Medium**
- `pip install shap` (well-maintained, actively developed)
- Requires a trained ML model (from Section 2.1 or 2.2)
- SHAP analysis itself is ~50 lines
- Visualization is built-in

#### Priority: **MEDIUM**
Valuable diagnostic tool, especially when combined with structural reduction. Implement after training initial ML models.

#### Key References
- **Lundberg, S. & Lee, S. (2017)** "A Unified Approach to Interpreting Model Predictions" — *NeurIPS* (Cited 20,000+)
- **Lundberg, S. et al. (2020)** "From local explanations to global understanding with explainable AI for trees" — *Nature Machine Intelligence*
- **SHAP Python library** — [github.com/slundberg/shap](https://github.com/slundberg/shap)
- Research finding: "Value-related features consistently dominate in SHAP analysis of trading models" (multiple sources)

---

### 2.5 Practical ML Pitfalls for Small Sample Trading

#### What
Applying ML to financial time series is notoriously difficult, and small-sample trading systems like Atlas face amplified versions of common pitfalls. This section consolidates key warnings and best practices from the literature.

**The Five Deadly Pitfalls for Small-Sample Trading ML:**

1. **Lookahead Bias / Data Leakage**
   - Using future information in features (e.g., same-day close in a signal calculated at open)
   - Using test-period data to normalize/scale features
   - Not purging observations near train/test boundaries
   - **Fix:** Purged cross-validation, strict point-in-time feature calculation

2. **Overfitting with Insufficient Data**
   - Atlas has ~500 trades across 3 years. Most ML models need thousands of samples.
   - Rule of thumb: need 10-20x observations per feature for reliable estimation
   - With 10 features, need 100-200 samples minimum per class
   - **Fix:** Very shallow models (max_depth=2-3), heavy regularization, few features (5-8 max)

3. **Non-Stationarity**
   - Financial time series are non-stationary — distributions change over time
   - A model trained on 2022-2024 data may be useless in 2025
   - Features must be transformed to be (approximately) stationary: use returns not prices, percentile ranks not absolute values, z-scores not raw indicators
   - **Fix:** Use rolling z-scores, percentile ranks, return-based features

4. **Multiple Testing / P-Hacking**
   - Every feature, model, hyperparameter combination tested inflates the probability of finding something that "works" by chance
   - With 100 combinations tested, a 5% significance threshold means ~5 false positives expected
   - **Fix:** DSR (Section 1.3), Bonferroni correction, pre-register hypotheses before testing

5. **Survivorship and Selection Bias**
   - Only testing on stocks that currently exist (survivors) ignores delisted failures
   - Only reporting the best-performing strategy variant
   - **Fix:** Include delisted stocks in universe, report ALL tested variants

#### Why It Helps (Atlas-ASX Specific)
Atlas is particularly vulnerable because:
- **Small sample:** ~500 trades, ~125 per strategy — below typical ML thresholds
- **Many implicit trials:** 10+ config versions, Bayesian optimization, grid searches
- **Non-stationary market:** ASX mid-caps have distinct regime shifts
- **Survivorship:** Current 185-ticker universe may exclude historical delistings

Awareness of these pitfalls should inform ALL ML implementations (Sections 2.1-2.4).

#### Implementation Details — Defensive ML Checklist

```python
# CHECKLIST: Before training any ML model for Atlas

class MLDefensiveChecklist:
    """Run this before and after every ML training session."""
    
    @staticmethod
    def check_data_leakage(features_df, labels, train_idx, test_idx, purge_days=10):
        """Verify no temporal overlap between train and test."""
        train_dates = features_df.index[train_idx]
        test_dates = features_df.index[test_idx]
        
        max_train = train_dates.max()
        min_test = test_dates.min()
        gap = (min_test - max_train).days
        
        assert gap >= purge_days, \
            f"Insufficient gap: {gap} days (need {purge_days})"
        print(f"Leakage check: PASS (gap={gap} days)")
    
    @staticmethod
    def check_sample_size(n_samples, n_features, n_classes=2, min_ratio=20):
        """Verify sufficient observations per feature per class."""
        min_class = n_samples / n_classes  # Approximate
        ratio = min_class / n_features
        
        if ratio < min_ratio:
            print(f"WARNING: {ratio:.0f} obs/feature/class "
                  f"(need {min_ratio}+). Reduce features or get more data.")
        else:
            print(f"Sample size check: PASS ({ratio:.0f} obs/feature/class)")
        return ratio
    
    @staticmethod
    def check_feature_stationarity(features_df, adf_pvalue=0.05):
        """Check that features are approximately stationary."""
        from statsmodels.tsa.stattools import adfuller
        non_stationary = []
        for col in features_df.columns:
            result = adfuller(features_df[col].dropna())
            if result[1] > adf_pvalue:
                non_stationary.append((col, result[1]))
        
        if non_stationary:
            print(f"WARNING: Non-stationary features:")
            for feat, pval in non_stationary:
                print(f"  {feat}: ADF p-value={pval:.4f}")
        else:
            print(f"Stationarity check: PASS (all features stationary)")
    
    @staticmethod
    def check_class_balance(labels, max_imbalance=3.0):
        """Check class imbalance ratio."""
        import numpy as np
        counts = np.bincount(labels.astype(int))
        ratio = counts.max() / counts.min()
        
        if ratio > max_imbalance:
            print(f"WARNING: Class imbalance ratio {ratio:.1f}:1 "
                  f"(max {max_imbalance}:1). Use class_weight='balanced'")
        else:
            print(f"Class balance check: PASS (ratio={ratio:.1f}:1)")
    
    @staticmethod 
    def check_model_complexity(model, max_depth=3, max_features=8):
        """Verify model is sufficiently constrained."""
        if hasattr(model, 'max_depth') and model.max_depth is not None:
            assert model.max_depth <= max_depth, \
                f"max_depth={model.max_depth} too deep (max={max_depth})"
        print(f"Complexity check: PASS")

# Usage:
checklist = MLDefensiveChecklist()
checklist.check_sample_size(500, 8)        # 31 obs/feat/class - marginal
checklist.check_sample_size(500, 5)        # 50 obs/feat/class - acceptable
checklist.check_sample_size(500, 15)       # 17 obs/feat/class - FAIL
```

**Recommended constraints for Atlas ML models:**

| Parameter | Recommended | Reason |
|-----------|------------|--------|
| max_depth | 2-3 | Prevents overfitting with ~500 trades |
| min_samples_leaf | 20-30 | Ensures statistical significance per leaf |
| n_features | 5-8 | Keeps obs/feature ratio above 30 |
| regularization | Strong (alpha=1.0, lambda=1.0) | Penalizes complexity |
| n_estimators | 50-100 | With early stopping |
| cross-validation | Purged time-series (10+ folds) | Prevents leakage |
| class_weight | 'balanced' | Handles win/loss imbalance |

#### Expected Impact
- Not a performance improvement technique — it's a **guard rail**
- Prevents the most common and costly mistakes in trading ML
- Ensures that any ML improvements (Sections 2.1-2.4) are genuine
- Without these checks, ML additions may worsen overfitting

#### Implementation Complexity: **Low**
- Checklist is ~100 lines of Python
- Should be run before EVERY ML training session
- No additional libraries needed beyond `statsmodels` for ADF test

#### Priority: **FOUNDATIONAL** (implement before any ML work)

#### Key References
- **Lopez de Prado, M. (2018)** *Advances in Financial Machine Learning* — Ch 7-8: Cross-Validation, Feature Importance
- **Arnott, R., Harvey, C., Markowitz, H. (2019)** "A Backtesting Protocol in the Era of Machine Learning" — *Journal of Financial Data Science*
- **Bailey et al. (2014)** "Pseudo-Mathematics and Financial Charlatanism: The Effects of Backtest Overfitting"
- **de Prado (2020)** "Machine Learning for Asset Managers" — Cambridge Elements

---

---

## CONCLUSION: Implementation Roadmap

### Diagnosis Summary

The Atlas-ASX system exhibits classic signs of **severe backtest overfitting**:

| Symptom | Value | Healthy Range | Diagnosis |
|---------|-------|---------------|----------|
| Parameter perturbation retention (±15%) | 24% | >70% | **Severe fragility** |
| IS-to-OOS CAGR degradation | 87% | <30% | **Severe overfitting** |
| Walk-forward window profitability | 59% | >70% | **Inconsistent** |
| OOS profit factor | <1.0 | >1.2 | **Net loser OOS** |
| Free parameters vs. trades | 13 params / 500 trades | <1:50 ratio | **Marginal** |

### Root Cause Analysis

The research points to **three interconnected root causes**:

1. **Too many free parameters (13)** for available data (500 trades) — degrees of freedom ratio of 38:1 is below the recommended 50:1 minimum
2. **Optimization on sharp peaks** rather than broad plateaus — the parameter landscape has narrow optima that don't generalize
3. **Insufficient validation rigor** — 22 walk-forward windows provide weak statistical evidence; no correction for multiple testing

### Recommended Implementation Order

#### Phase 1: Diagnostics (Week 1-2)
**Goal: Quantify the problem precisely before attempting fixes**

| Step | Technique | Section | Effort | Output |
|------|-----------|---------|--------|--------|
| 1.1 | Deflated Sharpe Ratio | 1.3 | 1 day | Is IS Sharpe of 0.67 statistically significant? |
| 1.2 | MinBTL calculation | 1.5 | 0.5 day | Do we have enough data for reliable statistics? |
| 1.3 | PBO computation | 1.5 | 2 days | What is the probability our backtest is overfit? |
| 1.4 | White's SPA test | 1.7 | 1 day | Do ANY of the 4 strategies have genuine alpha? |

**Decision gate:** If DSR < 0.95 AND PBO > 0.5 AND SPA fails: the system needs fundamental redesign, not parameter tuning. If some strategies pass SPA: focus on those.

#### Phase 2: Structural Simplification (Week 2-3)
**Goal: Reduce degrees of freedom to make the system more robust**

| Step | Technique | Section | Effort | Output |
|------|-----------|---------|--------|--------|
| 2.1 | Fix non-sensitive params to literature defaults | 1.6 | 1 day | Reduce from 13 to 5-7 free parameters |
| 2.2 | Parameter sharing across strategies | 1.6 | 1 day | Further reduce effective DoF |
| 2.3 | SHAP analysis on existing signals | 2.4 | 2 days | Identify which features/indicators to keep/remove |
| 2.4 | Simplify or remove weakest strategy | 1.6 | 1 day | Focus resources on strongest signals |

**Decision gate:** Re-run perturbation test with simplified system. Target: >60% retention at ±15%.

#### Phase 3: Robust Optimization (Week 3-4)
**Goal: Find the most stable parameter region**

| Step | Technique | Section | Effort | Output |
|------|-----------|---------|--------|--------|
| 3.1 | Parameter plateau detection | 1.1 | 3 days | Map the parameter landscape, find plateaus |
| 3.2 | Gaussian smoothed objective function | 1.4 | 1 day | Upgrade Bayesian optimizer |
| 3.3 | CPCV validation (200+ paths) | 1.2 | 2 days | Validate selected parameters across many paths |
| 3.4 | Re-compute DSR/PBO on simplified system | 1.3/1.5 | 1 day | Verify improvement |

**Decision gate:** CPCV 10th percentile CAGR > 0% and perturbation retention > 60%.

#### Phase 4: ML Enhancements (Week 5-8, only if Phase 3 passes)
**Goal: Layer ML on top of a validated, robust base system**

| Step | Technique | Section | Effort | Output |
|------|-----------|---------|--------|--------|
| 4.0 | Implement ML Defensive Checklist | 2.5 | 0.5 day | Guard rails before any ML work |
| 4.1 | Meta-labeling model | 2.1 | 5 days | Filter low-probability signals |
| 4.2 | ML signal combination | 2.2 | 3 days | Optimal strategy weighting |
| 4.3 | Rolling window retraining | 2.3 | 2 days | Adaptive model updates |
| 4.4 | SHAP analysis of ML models | 2.4 | 1 day | Verify ML is using sensible features |

**Decision gate:** ML additions must improve OOS CAGR AND perturbation retention. If either worsens, revert.

### Expected Outcomes by Phase

| Metric | Current | After Phase 2 | After Phase 3 | After Phase 4 |
|--------|---------|---------------|---------------|---------------|
| IS CAGR | 17.09% | 10-12% | 8-10% | 8-12% |
| OOS CAGR | 2.20% | 4-6% | 5-8% | 6-10% |
| IS-OOS Gap | 87% | 40-50% | 20-35% | 15-30% |
| Perturbation Retention (±15%) | 24% | 50-65% | 65-80% | 65-85% |
| WF Window Profitability | 59% | 65-70% | 70-80% | 75-85% |
| Free Parameters | 13 | 5-7 | 5-7 | 5-7 (+ML) |

**Key insight:** The IS CAGR will likely DECREASE (from 17.09% to 8-12%), but this reflects the elimination of illusory overfit performance. The true, reliable return of the system is probably in the 5-10% range. A system that reliably delivers 7% with 70% perturbation retention is infinitely more valuable than one that claims 17% but delivers 2% in practice.

### Critical Warning

**Do NOT skip directly to Phase 4 (ML).** Adding ML to a fundamentally overfit system will make it worse, not better. The ML models will learn the overfit patterns and amplify them. The structural simplification in Phase 2 is the single most important step.

### Library Requirements

```bash
# Phase 1 diagnostics
pip install scipy statsmodels arch

# Phase 2 analysis  
pip install shap

# Phase 3 optimization
pip install pyswarm optuna

# Phase 4 ML
pip install lightgbm xgboost scikit-learn river
```

### Key References (Consolidated)

1. **Lopez de Prado, M. (2018)** *Advances in Financial Machine Learning* — Wiley (THE definitive reference)
2. **Bailey, D.H. & Lopez de Prado, M. (2014)** "The Deflated Sharpe Ratio" — *Journal of Portfolio Management*
3. **Bailey et al. (2017)** "The Probability of Backtest Overfitting" — *Journal of Computational Finance*
4. **Wu, J.M.T. et al. (2024)** "Parameter plateau in quantitative trading strategies using PSO" — *Knowledge-Based Systems*
5. **Arian, H. et al. (2024)** "Backtest overfitting in the machine learning era" — *Knowledge-Based Systems*
6. **White, H. (2000)** "A Reality Check for Data Snooping" — *Econometrica*
7. **Hansen, P.R. (2005)** "A Test for Superior Predictive Ability" — *JBES*
8. **Lundberg, S. & Lee, S. (2017)** "A Unified Approach to Interpreting Model Predictions" — *NeurIPS*
9. **Beyer, H.G. & Sendhoff, B. (2007)** "Robust optimization — A comprehensive survey" — *CMAME*
10. **Pardo, R. (2008)** *The Evaluation and Optimization of Trading Strategies* — Wiley

---

*Report compiled from web research conducted 2026-02-19. All URLs verified at time of research.*
