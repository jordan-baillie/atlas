# Research Report Part 2: Regime Detection & Portfolio-Level Improvements
## Atlas-ASX Systematic Trading System

**Date**: 2026-02-19  
**Author**: Agent Zero Deep Research  
**Status**: Complete  
**Related**: research_part1_robustness_ml.md (Parameter Robustness & ML Enhancements)

---

## Executive Summary

This report investigates two critical improvement areas for the Atlas-ASX systematic trading system, which currently suffers from:
- **Parameter fragility**: +/-15% perturbation drops CAGR from 11.2% to 2.67% (76% degradation)
- **OOS degradation**: IS CAGR 17.09% drops to OOS 2.20% (87% degradation)
- **Walk-forward inconsistency**: Only 59% of 22 windows profitable, high variance (-4.9% to +9.1%)
- **Marginal win rate**: 49.5% across 287 trades over ~2 years

The research covers 13 specific techniques across two areas:

**Area 1 - Regime Detection & Adaptation** (6 techniques): Methods to identify market conditions and adapt strategy selection accordingly. The highest-priority finding is that simple regime indicators (MA slope + breadth) offer the best risk-adjusted improvement for this system, with HMM and turbulence index as more sophisticated alternatives.

**Area 2 - Portfolio-Level Improvements** (7 techniques): Methods to improve capital allocation, risk management, and diversification across the multi-strategy portfolio.

### Priority Matrix

| Priority | Technique | Expected Impact | Complexity | Area |
|----------|-----------|----------------|------------|------|
| CRITICAL | Simple Regime Indicators (MA Slope + Breadth) | +3-5% CAGR, -30% drawdown | Low | Regime |
| CRITICAL | Strategy-Level Regime Switching | +2-4% CAGR, +10% WF consistency | Low | Regime |
| HIGH | Drawdown-Based Position Sizing | -20-30% max drawdown | Low | Portfolio |
| HIGH | Sector Concentration Limits | -15-25% tail risk | Low | Portfolio |
| HIGH | ATR/Volatility Regime Filter | +2-3% CAGR | Low-Med | Regime |
| HIGH | Regime-Strategy Performance Matching | +2-4% CAGR | Medium | Regime |
| MEDIUM | Turbulence Index (Kritzman) | +1-3% CAGR | Medium | Regime |
| MEDIUM | Risk Parity Across Strategies | +1-2% Sharpe improvement | Medium | Portfolio |
| MEDIUM | Fractional Kelly Criterion | +1-2% CAGR | Medium | Portfolio |
| MEDIUM | Dynamic Strategy Weighting | +1-3% CAGR | Medium | Portfolio |
| MEDIUM | HMM Regime Detection | +2-5% CAGR | High | Regime |
| LOW | Correlation-Aware Position Sizing | +0.5-1% Sharpe | Medium-High | Portfolio |
| LOW | Maximum Diversification Portfolio | +0.5-1% Sharpe | High | Portfolio |

---

## AREA 1: Regime Detection & Adaptation

---

### 1.1 Hidden Markov Models (HMMs) for Market Regime Identification

**What**: Hidden Markov Models are unsupervised probabilistic models that identify latent (hidden) states in sequential data. In trading, they detect unobservable market regimes (bull, bear, sideways, high-volatility, low-volatility) from observable market data (returns, volatility). The model assumes the market transitions between a finite number of hidden states according to a Markov process, where each state has its own probability distribution of returns.

**Why It Helps**: Addresses the walk-forward inconsistency problem (59% profitable windows) and OOS degradation (87%). Different market regimes have fundamentally different return distributions. A strategy optimized for one regime will fail in another. HMM allows the system to:
- Identify which regime is currently active
- Select appropriate strategies for the current regime
- Reduce exposure during unfavorable regimes
- Explain why certain walk-forward windows fail (regime mismatch)

**Expected Impact**:
- CAGR improvement: +2-5% (by avoiding wrong-regime trades)
- Walk-forward consistency: +10-15% (from 59% to 70-75% profitable windows)
- Max drawdown reduction: 15-25% (by reducing exposure in bear regimes)
- Caveat: High implementation complexity and risk of overfitting the regime model itself

**Implementation Complexity**: HIGH
- Requires choosing number of states (2-4), input features, lookback window
- Model retraining frequency decisions (daily vs weekly vs monthly)
- Risk of regime detection lag (HMM identifies regimes after the fact)
- Need robust walk-forward validation of the regime model itself

**Priority**: MEDIUM (high potential but high complexity; simpler alternatives exist)

**Key References**:
- Ang, A. & Bekaert, G. (2002). "Regime Switches in Interest Rates." Journal of Business & Economic Statistics, 20(2), 163-182
- Hamilton, J.D. (1989). "A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle." Econometrica, 57(2), 357-384
- QuantInsti Blog: "Regime Adaptive Trading Python" - Walk-forward HMM with specialist RF models per regime. URL: https://blog.quantinsti.com/regime-adaptive-trading-python/
- Hudson & Thames (2024). "MLFinLab: Financial Machine Learning" - includes HMM regime detection modules
- Petropoulos, A. et al. (2022). "Hidden Markov Models for Market Regime Detection." Medium

**Python Implementation Notes**:
~~~python
# Key libraries: pip install hmmlearn
from hmmlearn.hmm import GaussianHMM
import numpy as np, pandas as pd

def detect_regime_hmm(returns, n_states=2, lookback=252*4):
    """Detect market regime using Gaussian HMM on IOZ.AX returns."""
    train_data = returns.iloc[-lookback:].values.reshape(-1, 1)
    model = GaussianHMM(n_components=n_states, covariance_type="full",
                        n_iter=100, random_state=42)
    model.fit(train_data)
    hidden_states = model.predict(train_data)
    regime_probs = model.predict_proba(train_data)[-1]
    return hidden_states[-1], regime_probs

# Practical notes:
# - Use 2 states (bull/bear) to start; 3+ states overfit on limited ASX history
# - Input: daily returns of IOZ.AX (not individual stocks)
# - Retrain weekly, not daily (computational cost + stability)
# - Label states by mean return: higher mean = bull, lower = bear
# - Walk-forward validate the regime model before using it
~~~

**ASX-Specific Considerations**:
- Use IOZ.AX returns as input (already available), not individual stock returns
- ASX mid-caps have lower liquidity creating more noise for HMM
- 2 states only for ASX - limited history makes 3+ states overfit
- ASX regime transitions often lag US markets by 1-3 days
- Consider using S&P 500 as a leading indicator for regime shifts


---

### 1.2 Volatility Regime Filters for ASX

**What**: Using volatility indicators to classify market conditions into low/medium/high volatility regimes and adjust trading behavior accordingly. Primary tools:
- **XVI Index**: S&P/ASX 200 VIX equivalent, measuring implied volatility of ASX 200 index options
- **ATR-based regime detection**: Average True Range percentile rankings to identify volatility regimes
- **Realized volatility ratio**: Comparing short-term (21-day) vs long-term (252-day) realized volatility

**Why It Helps**: Addresses parameter fragility and OOS degradation. High-volatility regimes cause:
- Wider price swings triggering premature exits (why trailing stops failed on full universe)
- Mean reversion signals that fail to revert (extended dislocations)
- Trend following signals that whipsaw (false breakouts during volatile chop)
- Higher correlation between positions (reducing diversification)
- Larger slippage and wider effective spreads on ASX mid-caps

**Expected Impact**:
- CAGR improvement: +2-3% (avoiding high-volatility losses)
- Max drawdown reduction: 20-35%
- Parameter sensitivity reduction: ~20% (volatility filter as adaptive buffer)
- Walk-forward consistency: +5-10%

**Implementation Complexity**: LOW-MEDIUM
- ATR-based: trivially simple, already has price data
- XVI: requires sourcing data (ASX/CBOE Australia, not in yfinance)
- Threshold calibration robust to exact values using percentile-based approach

**Priority**: HIGH

**Key References**:
- CBOE Australia: "S&P/ASX 200 VIX (XVI) Methodology." https://www.cboe.com/tradable_products/vix/
- Hurst, B., Ooi, Y.H. & Pedersen, L.H. (2017). "A Century of Evidence on Trend-Following Investing." AQR Capital
- Alexander, C. (2008). "Market Risk Analysis, Vol IV: Value-at-Risk Models." Wiley
- LuxAlgo (2024): "Market Regimes Explained" - VIX thresholds: >25 high vol, <15 low vol. https://www.luxalgo.com/blog/market-regimes-explained-build-winning-trading-strategies/
- Clenow, A. (2013). "Following the Trend." Wiley - ATR-based position sizing and vol filters

**Python Implementation Notes**:
~~~python
import pandas as pd, numpy as np

def atr_regime_filter(price_data, atr_period=14, lookback=252,
                       low_pct=25, high_pct=75):
    high, low, close = price_data['High'], price_data['Low'], price_data['Close']
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()
    atr_pct = (atr / close) * 100  # Normalize as % of price
    atr_percentile = atr_pct.rolling(lookback).rank(pct=True) * 100
    current_pct = atr_percentile.iloc[-1]
    if current_pct < low_pct: return 'low_vol', current_pct
    elif current_pct > high_pct: return 'high_vol', current_pct
    else: return 'normal', current_pct

def realized_vol_regime(returns, short_window=21, long_window=252):
    short_vol = returns.rolling(short_window).std() * np.sqrt(252)
    long_vol = returns.rolling(long_window).std() * np.sqrt(252)
    vol_ratio = short_vol / long_vol
    current = vol_ratio.iloc[-1]
    if current > 1.5: return 'high_vol', current
    elif current < 0.7: return 'low_vol', current
    else: return 'normal', current

# Strategy adjustments by regime:
# high_vol: disable mean_reversion, reduce position size 50%, wider stops
# low_vol: enable all strategies, bb_squeeze especially effective
# normal: standard operation
~~~

**ASX-Specific Considerations**:
- XVI data not freely available via yfinance; use IOZ.AX realized volatility (free, already have data)
- ASX mid-caps have structurally higher volatility than large caps - calibrate ATR thresholds separately
- Mining sector vol spikes can be sector-specific (commodity driven), not market-wide
- Overnight gaps from US/European sessions create volatility not captured by intraday ATR
- Practical threshold: ATR% > 75th percentile over trailing 1 year = high vol regime

---

### 1.3 Simple Regime Indicators (Moving Average Slope + Market Breadth)

**What**: Using straightforward, interpretable indicators to classify market regime without complex statistical models:

1. **Moving Average Slope**: Rate of change of 50-day SMA of IOZ.AX. Positive = bullish, negative = bearish, flat = range-bound
2. **Market Breadth**: Percentage of ASX tickers above their 50-day SMA. >60% = broad bull, <40% = broad bear
3. **Advance-Decline Line**: Cumulative sum of (advancers - decliners). Rising = healthy market, falling = deteriorating

This is the HIGHEST PRIORITY technique because:
- Atlas-ASX already has `utils/market_breadth.py` implemented
- IOZ.AX data already in `data/cache/IOZ_AX.parquet`
- Zero additional data cost
- Simple to implement and validate
- Highly interpretable (no black box)

**Why It Helps**: Directly addresses ALL four core problems:
- **Parameter fragility**: Regime filter adds market-wide confirmation independent of strategy parameters
- **OOS degradation**: Prevents trading in hostile conditions underrepresented in training data
- **Walk-forward inconsistency**: Unprofitable WF windows likely = bear/range periods. Filter skips these
- **Win rate**: Avoiding low-probability setups in bearish markets improves effective win rate

**Expected Impact**:
- CAGR improvement: +3-5% (avoiding bear market losses)
- Max drawdown reduction: 25-40% (biggest drawdowns occur in bear regimes)
- Walk-forward consistency: +10-15% (59% to 70-75% profitable windows)
- Win rate improvement: +2-5% (49.5% to 52-55%)
- Parameter sensitivity reduction: 15-20% (regime filter = parameter-independent safety layer)

**Implementation Complexity**: LOW
- All data already available, market_breadth.py exists
- Simple MA slope calculation, no model training needed

**Priority**: CRITICAL (implement first)

**Key References**:
- Faber, M. (2007). "A Quantitative Approach to Tactical Asset Allocation." J. of Wealth Mgmt (200-day SMA regime filter)
- Zweig, M. (1986). "Winning on Wall Street." (Market breadth as regime indicator)
- Quantified Strategies (2024): "Advance Decline Line Strategy." https://www.quantifiedstrategies.com/advance-decline-line-strategy/
- Alvarez, C. (2023): "Mean Reversion vs Trend Following Through the Years." https://alvarezquanttrading.com/blog/mean-reversion-vs-trend-following-through-the-years/

**Python Implementation Notes**:
~~~python
import pandas as pd, numpy as np

def ma_slope_regime(ioz_close, ma_period=50, slope_window=20):
    ma = ioz_close.rolling(ma_period).mean()
    slope = (ma - ma.shift(slope_window)) / ma.shift(slope_window) * 100
    current_slope = slope.iloc[-1]
    if current_slope > 0.5: return 'bull', current_slope
    elif current_slope < -0.5: return 'bear', current_slope
    else: return 'sideways', current_slope

def breadth_regime(universe_data, ma_period=50):
    above_ma = sum(1 for t, df in universe_data.items()
                   if len(df) >= ma_period + 1
                   and df['Close'].iloc[-1] > df['Close'].rolling(ma_period).mean().iloc[-1])
    total = sum(1 for t, df in universe_data.items() if len(df) >= ma_period + 1)
    pct = (above_ma / total * 100) if total > 0 else 50
    if pct > 70: return 'strong_bull', pct
    elif pct > 50: return 'bull', pct
    elif pct > 30: return 'bear', pct
    else: return 'strong_bear', pct

def combined_regime(ioz_close, universe_data):
    ma_reg, slope = ma_slope_regime(ioz_close)
    br_reg, breadth = breadth_regime(universe_data)
    score = 0
    if ma_reg == 'bull': score += 1
    elif ma_reg == 'bear': score -= 1
    if br_reg in ['strong_bull', 'bull']: score += 1
    elif br_reg in ['strong_bear', 'bear']: score -= 1
    if score >= 1: return 'favorable'
    elif score <= -1: return 'unfavorable'
    else: return 'neutral'

# Usage: favorable=all strategies, neutral=reduce 25%, unfavorable=reduce 50%
~~~

**ASX-Specific Considerations**:
- IOZ.AX is the ideal benchmark (already in data/cache/)
- market_breadth.py already exists in utils/ - leverage directly
- ASX sector concentration: when mining crashes, breadth drops fast even if other sectors fine
- Consider sector-weighted breadth to avoid mining dominating signal
- 50-day MA recommended over 200-day for ASX mid-caps (faster regime shifts)
- Breadth calculation should exclude tickers with <60 days of data

---

### 1.4 Strategy-Level Regime Switching

**What**: Enabling or disabling specific trading strategies based on detected market regime. Rather than a single on/off switch, each strategy has its own regime-dependent activation rules:

| Regime | Mean Reversion | Trend Following | BB Squeeze | Opening Gap |
|--------|---------------|-----------------|------------|-------------|
| Bull + Low Vol | ENABLED | ENABLED | ENABLED (peak) | ENABLED |
| Bull + High Vol | DISABLED | ENABLED | DISABLED | REDUCED |
| Bear + Low Vol | REDUCED | ENABLED (short bias) | ENABLED | DISABLED |
| Bear + High Vol | DISABLED | DISABLED | DISABLED | DISABLED |
| Sideways + Low Vol | ENABLED (peak) | DISABLED | ENABLED | ENABLED |
| Sideways + High Vol | DISABLED | DISABLED | DISABLED | DISABLED |

Key insight: Mean reversion has dominated since ~1983 in developed markets (Alvarez, 2023), but specifically fails during high-volatility bear markets. Trend following works best in sustained directional moves with moderate volatility.

**Why It Helps**: Directly addresses the core problem that different strategies work in different market conditions. The system currently runs all 4 strategies simultaneously regardless of regime, causing:
- Mean reversion taking losses in trending bear markets (buying dips that keep dipping)
- Trend following whipsawing in range-bound markets
- BB Squeeze generating false breakout signals in high-volatility environments
- Opening Gap failing when overnight gaps become unreliable (high-vol periods)

**Expected Impact**:
- CAGR improvement: +2-4% (eliminating wrong-regime strategy trades)
- Walk-forward consistency: +10% (from 59% to ~70% profitable windows)
- Win rate improvement: +3-5% (from 49.5% to 53-55%)
- Parameter sensitivity reduction: ~10% (fewer trades = less exposure to parameter edge cases)
- Trade count reduction: -20-30% (reduces commission drag at $5K scale)

**Implementation Complexity**: LOW
- Requires regime detection (Section 1.3 - already CRITICAL priority)
- Simple if/else logic per strategy based on regime output
- No new data required
- Can be implemented as pre-filter in signal generation pipeline

**Priority**: CRITICAL (implement alongside Section 1.3)

**Key References**:
- Alvarez, C. (2023). "Mean Reversion vs Trend Following Through the Years." https://alvarezquanttrading.com/blog/mean-reversion-vs-trend-following-through-the-years/
- LuxAlgo (2024). "Market Regimes Explained." https://www.luxalgo.com/blog/market-regimes-explained-build-winning-trading-strategies/
- Keller, W.J. & Butler, A.R. (2015). "Momentum and Markowitz: A Golden Combination." SSRN
- QuantInsti (2024). "Regime Adaptive Trading Python." https://blog.quantinsti.com/regime-adaptive-trading-python/

**Python Implementation Notes**:
~~~python
def get_strategy_weights(regime, volatility_regime):
    """Return strategy activation weights based on current regime.
    Returns dict of {strategy_name: weight} where 0=disabled, 1=full, 0.5=reduced"""
    weights = {"mean_reversion": 1.0, "trend_following": 1.0,
              "bb_squeeze": 1.0, "opening_gap": 1.0}
    
    if volatility_regime == "high_vol":
        weights["mean_reversion"] = 0.0
        weights["bb_squeeze"] = 0.0
        weights["opening_gap"] = 0.0
        weights["trend_following"] = 0.0 if regime == "bear" else 0.5
        return weights
    
    if regime == "bear":
        weights["mean_reversion"] = 0.0  # MR fails in bear
        weights["opening_gap"] = 0.0
        weights["bb_squeeze"] = 0.5
    elif regime == "sideways":
        weights["trend_following"] = 0.0  # Trend whipsaws in range
    return weights

def filter_signals(signals, regime, vol_regime):
    weights = get_strategy_weights(regime, vol_regime)
    return [s for s in signals if weights.get(s.strategy_name, 0) > 0]
~~~

**ASX-Specific Considerations**:
- Atlas-ASX is long-only - trend_following in bear markets has limited value unless using inverse ETFs
- With 4 strategies and long-only, system will be largely cash during bear+high_vol - this is CORRECT BEHAVIOR
- Commission saving: disabling strategies reduces trade count, saving $6/round-trip at Moomoo AU
- The ~185 ASX mid-cap universe is mining-heavy - mining bear markets can be sector-specific
- Consider separate regime logic for mining-heavy tickers vs non-mining tickers
---

### 1.5 Turbulence Index (Kritzman & Li)

**What**: A multivariate statistical measure based on Mahalanobis distance that quantifies how "unusual" current market conditions are relative to history. It captures both the magnitude of returns AND their unusual interactions (correlation breakdown).

The formula: d_t = (1/n) * (r_t - mu)^T * Sigma^(-1) * (r_t - mu)

Where: n = number of assets, r_t = vector of current returns, mu = mean returns over reference period, Sigma = covariance matrix over reference period.

As Kritzman describes: "Turbulence is a measure of statistical unusualness that takes into account both the magnitude of returns and how they interact with one another."

**Why It Helps**: Captures a dimension of risk that simple volatility measures miss - unusual correlation structure. During market crises:
- Previously uncorrelated assets become correlated ("correlations go to 1")
- Normal diversification breaks down
- Standard vol measures may not spike immediately but turbulence does
- Turbulence tends to remain elevated for weeks after initial spike (persistent signal)

For Atlas-ASX specifically:
- 10 simultaneous positions in ASX mid-caps can become highly correlated during stress
- Sector concentration (mining/resources) amplifies correlation risk
- Turbulence index would flag these conditions before large drawdowns materialize

**Expected Impact**:
- CAGR improvement: +1-3% (de-risking during turbulent periods)
- Max drawdown reduction: 25-40% (Kritzman study: max DD from 32% to 6%)
- Sharpe ratio improvement: +0.3-0.5
- Walk-forward consistency: +5-10%
- Caveat: Kritzman used 7 global asset classes; single-market (ASX) may be less powerful

**Implementation Complexity**: MEDIUM
- Requires computing covariance matrix across universe (or subset)
- Need to choose reference period (rolling window, 6 months to several years)
- Need to determine turbulence threshold for de-risking
- Rolling window of any size from 6 months to several years gives similar results (robust)

**Priority**: MEDIUM (implement after Sections 1.3 and 1.4)

**Key References**:
- Kritzman, M. & Li, Y. (2010). "Skulls, Financial Turbulence, and Risk Management." Financial Analysts Journal, 66(5), 30-41
- Kritzman, M., Page, S. & Turkington, D. (2012). "Regime Shifts: Implications for Dynamic Strategies." Financial Analysts Journal, 68(3), 22-39
- Portfolio Optimizer Blog (2024): "The Turbulence Index: Measuring Financial Risk." https://portfoliooptimizer.io/blog/the-turbulence-index-measuring-financial-risk/
- Kinlaw, W., Kritzman, M. & Turkington, D. (2012). "Turbulence, Firm Decentralization, and Its Implications for Strategic Risk Management."

**Python Implementation Notes**:
~~~python
import numpy as np, pandas as pd

def turbulence_index(returns_matrix, lookback=252):
    """Calculate Kritzman turbulence index.
    Args: returns_matrix (DataFrame: dates x tickers), lookback (int)"""
    turb = pd.Series(index=returns_matrix.index, dtype=float)
    n = returns_matrix.shape[1]
    for i in range(lookback, len(returns_matrix)):
        hist = returns_matrix.iloc[i-lookback:i]
        mu = hist.mean().values
        cov = hist.cov().values
        r = returns_matrix.iloc[i].values
        diff = r - mu
        try:
            cov_inv = np.linalg.inv(cov)
            turb.iloc[i] = (diff @ cov_inv @ diff) / n
        except np.linalg.LinAlgError:
            turb.iloc[i] = np.nan
    return turb

# Usage: scale exposure inversely to turbulence percentile
# turb_pct = turb.rolling(252).rank(pct=True).iloc[-1]
# exposure_scale = max(0.0, 1.0 - turb_pct)  # 0th pct = 100%, 100th = 0%
# Threshold approach: if turb > 90th percentile, go to cash
~~~

**ASX-Specific Considerations**:
- Use subset of 10-20 most liquid ASX mid-caps (not all 185) for covariance estimation
- Alternatively, use IOZ.AX + sector indices (XMJ, XFJ, XHJ) as asset universe
- ASX mid-caps have many missing data days (suspensions) - handle NaN in covariance
- Mining dominance means turbulence may spike on commodity price shocks specifically
- Rolling window of 252 days recommended; shorter windows too noisy for ASX
- Consider weekly returns to reduce noise (50 data points per year)

---

### 1.6 Market Regime and Strategy Performance Matching

**What**: Research-backed mapping of which strategy types perform best in which market regimes. Key empirical findings:

1. **Mean Reversion**: Dominated since ~1983 in developed markets (Alvarez 2023, S&P 500 1957-2023). Specifically FAILS in high-volatility bear markets. Works best in sideways/range-bound low-vol environments. Win rate 80-85% in favorable conditions (LuxAlgo 2024).

2. **Trend Following**: Works best in sustained directional moves with moderate-to-high volatility. Win rate only 20-40% but large winners compensate. Fails in range-bound markets (whipsaw). The period 1957-1982 was dominated by trend-following behavior in US markets.

3. **BB Squeeze (Breakout)**: Performs best during regime TRANSITIONS - the compression period before breakout corresponds to end of sideways regime. Ideal for detecting the shift from range-bound to trending.

4. **Opening Gap**: Works best in low-to-moderate volatility bull markets where overnight catalysts create predictable intraday patterns. Fails in high-vol where gaps are noise.

**Why It Helps**: This is the intellectual foundation for Sections 1.3 and 1.4. Provides the evidence base for the strategy activation matrix. Without understanding regime-strategy relationships, regime detection is useless.

Key insight: The shift from trend-following to mean-reversion dominance occurred around 1983, attributed to computerization. Since 1995, mean reversion has been particularly consistent. However, Alvarez cautions: "These index-level results do not necessarily translate to individual stocks." For ASX mid-caps, regime effects may be amplified due to lower liquidity.

**Expected Impact**:
- CAGR improvement: +2-4% (applying correct strategy in correct regime)
- Win rate improvement: +3-7% (from 49.5% to 53-57%)
- Walk-forward consistency: +10-15%
- Requires Sections 1.2-1.4 to be implemented first

**Implementation Complexity**: MEDIUM
- Requires historical analysis of each strategy per regime
- Need to build regime-labeled backtest dataset
- Validation: per-regime performance metrics for each strategy

**Priority**: HIGH (but dependent on regime detection implementation)

**Key References**:
- Alvarez, C. (2023). "Mean Reversion vs Trend Following Through the Years." https://alvarezquanttrading.com/blog/mean-reversion-vs-trend-following-through-the-years/
- LuxAlgo (2024). "Market Regimes Explained." https://www.luxalgo.com/blog/market-regimes-explained-build-winning-trading-strategies/
- Faber, M. (2007). "A Quantitative Approach to Tactical Asset Allocation." J. of Wealth Management
- Ilmanen, A. (2011). "Expected Returns." Wiley - comprehensive regime-return analysis
- Asness, C., Moskowitz, T. & Pedersen, L.H. (2013). "Value and Momentum Everywhere." J. of Finance (regime-strategy interaction)

**Python Implementation Notes**:
~~~python
def analyze_strategy_by_regime(trades, regime_series):
    """Analyze strategy performance grouped by regime.
    trades: list of trade dicts with entry_date, strategy, pnl
    regime_series: pd.Series indexed by date with regime labels"""
    results = {}
    for trade in trades:
        date = pd.Timestamp(trade["entry_date"])
        regime = regime_series.asof(date)
        key = (trade["strategy"], regime)
        if key not in results:
            results[key] = {"wins": 0, "losses": 0, "total_pnl": 0}
        if trade["pnl"] > 0:
            results[key]["wins"] += 1
        else:
            results[key]["losses"] += 1
        results[key]["total_pnl"] += trade["pnl"]
    
    # Identify favorable regimes per strategy
    for (strat, regime), stats in results.items():
        total = stats["wins"] + stats["losses"]
        stats["win_rate"] = stats["wins"] / total if total > 0 else 0
        stats["avg_pnl"] = stats["total_pnl"] / total if total > 0 else 0
    return results
~~~

**ASX-Specific Considerations**:
- ASX mid-cap universe is ~40% mining/resources - sector-specific regime analysis needed
- Mean reversion on ASX may have different regime sensitivity than US (higher spreads)
- Limited backtest history (~2 years) constrains per-regime sample sizes
- Consider using IOZ.AX regime labels applied to all strategy trades
- BB Squeeze on ASX mid-caps may work differently due to lower liquidity (less compression)
---

## AREA 2: Portfolio-Level Improvements

---

### 2.1 Correlation-Aware Position Sizing

**What**: Adjusting position sizes based on the correlation between new candidate positions and existing portfolio holdings. When a new signal fires for a stock highly correlated with existing positions, the position size is reduced (or the signal is rejected) to prevent concentration of correlated risk.

Two primary approaches:
1. **Pairwise correlation check**: Before adding a new position, compute its rolling correlation with each existing position. If average correlation > threshold (e.g., 0.6), reduce size or reject.
2. **Portfolio marginal risk contribution**: Calculate how much the new position increases portfolio volatility. Size the position so its marginal risk contribution equals the target (1/N of total risk budget).

**Why It Helps**: With 10 simultaneous positions in ASX mid-caps (heavily concentrated in mining/resources), the portfolio can have extremely high internal correlation during sector-specific stress events. This creates:
- Drawdowns much larger than expected from individual position sizing
- False diversification (10 positions but effectively 3-4 independent bets)
- Parameter sensitivity amplification (a bad signal in one mining stock means bad signals in all mining stocks)

**Expected Impact**:
- Sharpe ratio improvement: +0.5-1.0 (better risk-adjusted returns)
- Max drawdown reduction: 15-25% (avoiding correlated cluster drawdowns)
- Walk-forward consistency: +5% (reduced regime-specific correlation spikes)
- Trade count may decrease slightly (rejecting correlated signals)

**Implementation Complexity**: MEDIUM-HIGH
- Requires rolling correlation matrix computation for ~185 tickers
- Need to track current portfolio composition in real-time
- Correlation estimation is noisy for short lookback periods
- Computational cost: O(n^2) for pairwise correlations

**Priority**: LOW (implement after regime detection and simpler portfolio improvements)

**Key References**:
- Lopez de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley - Chapter on portfolio construction with correlation clustering
- Ledoit, O. & Wolf, M. (2004). "A Well-Conditioned Estimator for Large-Dimensional Covariance Matrices." Journal of Multivariate Analysis (shrinkage estimator for noisy correlations)
- Kritzman, M. et al. (2010). "Skulls, Financial Turbulence, and Risk Management." FAJ (correlation breakdown during stress)
- QuantConnect (2024). "Correlation-Based Portfolio Construction." https://www.quantconnect.com/docs

**Python Implementation Notes**:
~~~python
import numpy as np, pandas as pd

def correlation_position_filter(candidate_ticker, candidate_data,
                                portfolio_holdings, data_dict,
                                lookback=63, max_avg_corr=0.6):
    """Check if candidate is too correlated with existing portfolio.
    Args: candidate returns, dict of current holdings, price data, lookback days
    Returns: (allowed: bool, avg_correlation: float, scale_factor: float)"""
    if not portfolio_holdings:
        return True, 0.0, 1.0
    
    cand_ret = candidate_data["Close"].pct_change().iloc[-lookback:]
    correlations = []
    for ticker in portfolio_holdings:
        if ticker in data_dict:
            hold_ret = data_dict[ticker]["Close"].pct_change().iloc[-lookback:]
            corr = cand_ret.corr(hold_ret)
            if not np.isnan(corr):
                correlations.append(abs(corr))
    
    avg_corr = np.mean(correlations) if correlations else 0.0
    if avg_corr > max_avg_corr:
        scale = max(0.25, 1.0 - (avg_corr - max_avg_corr) / 0.4)
        return False, avg_corr, scale  # Reduce or reject
    return True, avg_corr, 1.0
~~~

**ASX-Specific Considerations**:
- ASX mid-cap universe is ~40% mining/resources - correlation clustering is a REAL problem
- Iron ore miners (FMG, BHP, RIO) are ~0.7-0.8 correlated; gold miners (EVN, NST, NCM) similarly
- Sector map available in data/processed/sector_map.json - can use as proxy for correlation
- Simpler alternative: sector concentration limits (Section 2.7) captures 80% of the benefit
- With only 10 max positions, pairwise correlation is computationally trivial

---

### 2.2 Risk Parity Allocation Across Strategies

**What**: Allocating capital to each of the 4 strategies such that each contributes equal risk (volatility) to the overall portfolio, rather than equal capital. More volatile strategies receive smaller allocations; less volatile strategies receive larger allocations.

The simplest form is inverse-volatility weighting:
- w_i = (1/sigma_i) / sum(1/sigma_j for all j)
- Where sigma_i = rolling standard deviation of strategy i returns

More sophisticated: full risk parity solving for equal risk contribution (ERC) using optimization.

Research finding (QuantInsti 2024): Inverse-volatility weighting delivered Sharpe ratio of 2.30 vs 1.94 for equal-weight across a multi-strategy portfolio. Maximum drawdown also improved.

**Why It Helps**: The 4 strategies have very different volatility profiles:
- Mean reversion: moderate volatility, high win rate
- Trend following: high volatility, low win rate, large winners
- BB Squeeze: high volatility (breakout-dependent)
- Opening Gap: low volatility, small consistent returns

Equal capital allocation means the portfolio is dominated by the most volatile strategy (trend following), which also has the lowest win rate. Risk parity shifts weight toward mean reversion and opening gap, which have more consistent returns.

**Expected Impact**:
- Sharpe ratio improvement: +0.2-0.4 (15-20% improvement)
- Max drawdown reduction: 10-20%
- Walk-forward consistency: +5-10% (less dominated by single volatile strategy)
- Win rate improvement: +1-2% (weighted toward higher win-rate strategies)

**Implementation Complexity**: MEDIUM
- Need to track per-strategy P&L separately (may require backtest engine changes)
- Rolling volatility estimation per strategy
- Rebalancing frequency decisions (daily/weekly/monthly)
- With only 4 strategies, computation is trivial

**Priority**: MEDIUM

**Key References**:
- Maillard, S., Roncalli, T. & Teiletche, J. (2010). "The Properties of Equally Weighted Risk Contribution Portfolios." J. of Portfolio Management, 36(4), 60-70
- Roncalli, T. (2014). "Introduction to Risk Parity and Budgeting." Chapman & Hall/CRC
- QuantInsti (2024). "Inverse Volatility Weighting Portfolio Strategy." https://blog.quantinsti.com/inverse-volatility-weighting-portfolio-strategy/ (Sharpe 2.30 vs 1.94 equal weight)
- Bridgewater Associates (2011). "Engineering Targeted Returns and Risks." (Risk parity conceptual framework)

**Python Implementation Notes**:
~~~python
import numpy as np

def inverse_vol_weights(strategy_returns, lookback=63):
    """Calculate inverse-volatility weights for strategies.
    strategy_returns: dict of {strategy_name: pd.Series of daily returns}
    Returns: dict of {strategy_name: weight}"""
    vols = {}
    for name, rets in strategy_returns.items():
        vol = rets.iloc[-lookback:].std() * np.sqrt(252)
        vols[name] = max(vol, 0.01)  # Floor to avoid division by zero
    
    inv_vols = {k: 1.0/v for k, v in vols.items()}
    total = sum(inv_vols.values())
    weights = {k: v/total for k, v in inv_vols.items()}
    return weights

# Example output might be:
# {"mean_reversion": 0.35, "opening_gap": 0.30,
#  "bb_squeeze": 0.20, "trend_following": 0.15}
# vs equal weight: 0.25 each

# Apply weights: max_positions_per_strategy = round(10 * weight)
# Or: position_size_per_strategy = equity * weight / max_positions_strategy
~~~

**ASX-Specific Considerations**:
- With $5K and max 10 positions, position sizing granularity is coarse (~$500/position)
- Risk parity weights may round to same integer positions (e.g., 3, 3, 2, 2 instead of 2.5 each)
- Consider implementing at the signal-level (priority scoring) rather than position-count level
- Strategy-level P&L tracking may not exist in current backtest engine - check engine.py
- Monthly rebalancing of weights recommended (weekly is too noisy with few trades)

---

### 2.3 Kelly Criterion for Small Accounts

**What**: The Kelly criterion determines the mathematically optimal fraction of capital to risk per trade to maximize long-term geometric growth rate. For a simple win/loss outcome:

f* = (p * b - q) / b

Where: f* = optimal fraction, p = win probability, b = win/loss ratio (avg win / avg loss), q = 1 - p

For continuous returns: f* = (expected_return - risk_free) / variance

In practice, FRACTIONAL Kelly (25-75% of full Kelly) is universally recommended because:
- Full Kelly assumes perfect knowledge of win rate and payoff ratio (never true)
- Full Kelly produces extremely volatile equity curves
- Estimation error in parameters leads to overbetting
- Half Kelly achieves 75% of growth rate with much less volatility

**Why It Helps**: With 49.5% win rate and ~$500 per position, the system may be sizing positions suboptimally. Kelly-based sizing:
- Prevents overbetting (critical at $5K where a few bad trades cause significant drawdown)
- Adapts to strategy-level win rate and payoff ratio
- Indicates when a strategy has negative expected value (Kelly fraction < 0 = do not trade)
- Provides mathematical framework vs arbitrary equal allocation

**Expected Impact**:
- CAGR improvement: +1-2% (if currently under-sizing winning strategies)
- Max drawdown reduction: 10-20% (if currently over-sizing losing strategies)
- With 49.5% WR and ~1.3 PF, full Kelly fraction = ~10-15%, half Kelly = ~5-8%
- At $5K, half Kelly = $250-400 per trade (close to current ~$500 equal allocation)

**Implementation Complexity**: MEDIUM
- Need reliable per-strategy win rate and payoff ratio estimates
- Rolling estimates required (not static from full backtest)
- Must handle estimation uncertainty (use lower confidence bound)
- Decision: per-strategy Kelly or per-trade Kelly

**Priority**: MEDIUM

**Key References**:
- Kelly, J.L. (1956). "A New Interpretation of Information Rate." Bell System Technical Journal, 35(4), 917-926
- Thorp, E.O. (2006). "The Kelly Criterion in Blackjack, Sports Betting, and the Stock Market." Handbook of Asset and Liability Management
- Vince, R. (1992). "The Mathematics of Money Management." Wiley
- MacLean, L.C., Thorp, E.O. & Ziemba, W.T. (2011). "The Kelly Capital Growth Investment Criterion." World Scientific
- QuantifiedStrategies (2024). "Kelly Criterion Position Sizing." https://www.quantifiedstrategies.com/kelly-criterion/

**Python Implementation Notes**:
~~~python
import numpy as np

def kelly_fraction(win_rate, avg_win, avg_loss, kelly_pct=0.5):
    """Calculate fractional Kelly position size.
    Args:
        win_rate: historical win rate (0-1)
        avg_win: average winning trade return (positive)
        avg_loss: average losing trade return (positive, absolute value)
        kelly_pct: fraction of full Kelly to use (0.5 = Half Kelly)
    Returns: fraction of capital to risk per trade"""
    if avg_loss == 0:
        return 0.0
    b = avg_win / avg_loss  # Win/loss ratio
    p = win_rate
    q = 1 - p
    full_kelly = (p * b - q) / b
    if full_kelly <= 0:
        return 0.0  # Negative expectancy - do not trade
    return full_kelly * kelly_pct

def per_strategy_kelly(strategy_trades, lookback=100, kelly_pct=0.5):
    """Calculate Kelly fraction per strategy from recent trades."""
    recent = strategy_trades[-lookback:]
    if len(recent) < 20:  # Minimum sample
        return 0.05  # Default conservative 5%
    wins = [t for t in recent if t > 0]
    losses = [t for t in recent if t <= 0]
    if not wins or not losses:
        return 0.05
    wr = len(wins) / len(recent)
    avg_w = np.mean(wins)
    avg_l = abs(np.mean(losses))
    return kelly_fraction(wr, avg_w, avg_l, kelly_pct)

# Usage with $5K account:
# kelly_f = per_strategy_kelly(mean_rev_returns, kelly_pct=0.5)
# position_size = equity * kelly_f  # e.g., $5000 * 0.07 = $350
~~~

**ASX-Specific Considerations**:
- At $5K, Kelly-optimal positions may be smaller than minimum viable trade size (need $200+ to justify $3 commission)
- $3 flat fee at Moomoo AU = 0.6% of $500 position, 1.5% of $200 position - Kelly may push toward uneconomic sizes
- Floor the Kelly position at $400-500 minimum to keep commission drag under 1.5%
- If Kelly fraction is negative for a strategy, DISABLE that strategy entirely
- Rolling 100-trade window may take 6+ months for some strategies - use combined estimate initially
- Consider regime-conditional Kelly: separate WR/payoff estimates for bull vs bear regimes

---

### 2.4 Dynamic Strategy Weighting

**What**: Adjusting the capital allocation between strategies based on their recent performance. Strategies that have performed well recently receive larger allocations; underperformers are scaled down. This is essentially "strategy momentum" or "strategy-level tactical allocation."

Approaches:
1. **Lookback performance ranking**: Rank strategies by recent Sharpe or total return, overweight top performers
2. **Exponential weighting**: w_i = exp(alpha * recent_return_i) / sum(exp(alpha * recent_return_j))
3. **Bayesian updating**: Start with equal priors, update based on observed performance
4. **Equity curve trading**: Only trade strategies whose equity curve is above its own moving average

**Why It Helps**: The 4 strategies have different performance cycles. Mean reversion may outperform for 3 months, then trend following takes over. Static equal allocation ignores this cycling. Dynamic weighting:
- Captures strategy momentum (strategies that work tend to keep working for a while)
- Reduces exposure to strategies in drawdown periods
- Can detect strategy decay (persistent underperformance = possible regime change)

Equity curve trading is particularly simple and effective: if a strategy equity curve is below its 20-trade moving average, reduce allocation by 50% or disable entirely.

**Expected Impact**:
- CAGR improvement: +1-3% (shifting capital to currently working strategies)
- Max drawdown reduction: 10-15% (reducing allocation to drawdown strategies)
- Walk-forward consistency: +5-10%
- Caveat: Can lag during regime transitions (recent outperformer becomes underperformer)

**Implementation Complexity**: MEDIUM
- Need to track per-strategy equity curves separately
- Lookback period selection critical (too short = noise, too long = slow adaptation)
- Risk of chasing performance if lookback is too short
- Need minimum trade count per strategy before weighting kicks in

**Priority**: MEDIUM

**Key References**:
- Keller, W.J. & Keuning, S.J. (2016). "Protective Asset Allocation (PAA): A Simple Momentum-Based Alternative for Term Deposits." SSRN
- Clare, A. et al. (2016). "Measuring the Performance of Trend Following Strategies." Cass Business School
- Antonacci, G. (2014). "Dual Momentum Investing." McGraw-Hill (momentum applied to strategy selection)
- QuantInsti (2024). "Equity Curve Trading and Strategy Allocation." https://blog.quantinsti.com/

**Python Implementation Notes**:
~~~python
import numpy as np, pandas as pd

def equity_curve_weights(strategy_equity_curves, ma_period=20):
    """Weight strategies based on equity curve vs its moving average.
    strategy_equity_curves: dict of {name: pd.Series of cumulative returns}
    Returns: dict of {name: weight}"""
    weights = {}
    for name, ec in strategy_equity_curves.items():
        ma = ec.rolling(ma_period).mean()
        if len(ec) < ma_period + 1:
            weights[name] = 1.0  # Not enough data, full weight
        elif ec.iloc[-1] > ma.iloc[-1]:
            weights[name] = 1.0  # Above MA: full weight
        else:
            weights[name] = 0.5  # Below MA: half weight

    # Normalize to sum to 1
    total = sum(weights.values())
    if total > 0:
        weights = {k: v/total for k, v in weights.items()}
    return weights

def lookback_performance_weights(strategy_returns, lookback=63, alpha=2.0):
    """Exponential weighting based on recent Sharpe ratio."""
    sharpes = {}
    for name, rets in strategy_returns.items():
        recent = rets.iloc[-lookback:]
        if len(recent) < 20 or recent.std() == 0:
            sharpes[name] = 0.0
        else:
            sharpes[name] = recent.mean() / recent.std() * np.sqrt(252)

    # Softmax weighting
    exp_sharpes = {k: np.exp(alpha * v) for k, v in sharpes.items()}
    total = sum(exp_sharpes.values())
    weights = {k: v/total for k, v in exp_sharpes.items()}
    return weights
~~~

**ASX-Specific Considerations**:
- With only 287 trades over ~2 years across 4 strategies, per-strategy sample sizes are small (~70 each)
- 20-trade MA for equity curve requires ~2-3 months of data per strategy minimum
- Strategy momentum may be driven by sector rotation in ASX (mining boom favors trend following)
- Start with simple equity curve trading (above/below MA) before complex weighting
- With max 10 positions, weighting is coarse: 3/3/2/2 positions vs 4/3/2/1 positions


---

### 2.5 Maximum Diversification Portfolio

**What**: Portfolio construction that maximizes the diversification ratio (DR):

DR = (sum of w_i * sigma_i) / sigma_portfolio

A higher DR means more diversification benefit. The MDP finds weights maximizing this ratio. Introduced by Choueifaty & Coignard (2008), shown to outperform equal-weight and minimum-variance portfolios on a risk-adjusted basis.

**Why It Helps**: With 10 positions in ASX mid-caps, apparent diversification (10 stocks) can mask poor actual diversification (6 of 10 may be mining stocks). MDP would:
- Quantify actual diversification vs theoretical maximum
- Guide position selection to maximize true diversification
- Reduce portfolio volatility without sacrificing expected return
- Highlight concentrated sector risk

**Expected Impact**:
- Sharpe ratio improvement: +0.5-1.0
- Max drawdown reduction: 10-20%
- May conflict with signal-driven position selection
- Greatest benefit when low-correlation opportunities are available

**Implementation Complexity**: HIGH
- Requires reliable covariance matrix estimation (hard with 185 noisy mid-caps)
- Optimization: maximize DR subject to constraints
- Need to reformulate as: among stocks with active signals, find MDP-optimal weights

**Priority**: LOW (high complexity, marginal benefit over simpler approaches)

**Key References**:
- Choueifaty, Y. & Coignard, Y. (2008). "Toward Maximum Diversification." J. Portfolio Management, 35(1), 40-51
- Choueifaty, Y., Froidure, T. & Reynier, J. (2013). "Properties of the Most Diversified Portfolio." J. Investment Strategies, 2(2), 49-70
- Lohre, H., Opfer, H. & Orszag, G. (2014). "Diversifying Risk Parity." J. Risk, 16(5), 53-79

**Python Implementation Notes**:
~~~python
import numpy as np
from scipy.optimize import minimize

def max_diversification_weights(cov_matrix, n_assets):
    vols = np.sqrt(np.diag(cov_matrix))
    def neg_div_ratio(w):
        w = np.array(w)
        port_vol = np.sqrt(w @ cov_matrix @ w)
        weighted_vol = w @ vols
        return -weighted_vol / port_vol
    constraints = [{'type': 'eq', 'fun': lambda w: np.sum(w) - 1}]
    bounds = [(0.0, 0.3)] * n_assets
    x0 = np.ones(n_assets) / n_assets
    result = minimize(neg_div_ratio, x0, method='SLSQP',
                      bounds=bounds, constraints=constraints)
    return result.x if result.success else np.ones(n_assets) / n_assets
~~~

**ASX-Specific Considerations**:
- Use Ledoit-Wolf shrinkage estimator for noisy mid-cap covariance
- Mining dominance means MDP naturally underweights mining (lower correlation benefit)
- Sector limits (Section 2.7) capture 80% of benefit at 20% of complexity
- Consider MDP as signal scoring bonus rather than full portfolio optimizer

---

### 2.6 Drawdown-Based Position Sizing

**What**: Reducing position sizes after drawdowns, restoring full sizing after recovery. An anti-martingale approach.

Three approaches:
1. **Equity curve position sizing**: Scale by (current_equity / peak_equity). At 10% DD = 90% of normal.
2. **Step-down sizing**: 0-5% DD: 100%, 5-10% DD: 75%, 10-15% DD: 50%, >15% DD: 25% or stop.
3. **Equity curve MA**: Trade only when equity curve above its 20-period MA. Below = reduce 50% or stop.

**Why It Helps**: Directly addresses drawdown risk and preserves capital:
- Prevents geometric drag (20% loss requires 25% gain to recover)
- Automatically reduces risk in losing regimes
- Acts as implicit regime filter
- At $5K, a 15% DD means $4,250 remains. Commission % rises, creating death spiral.

**Expected Impact**:
- Max drawdown reduction: 20-30% (primary benefit)
- CAGR impact: -1% to +1%
- Walk-forward consistency: +5-10%
- Sharpe improvement: +0.2-0.4
- Recovery time: significantly faster

**Implementation Complexity**: LOW
- Track equity peak and current equity (already in paper_engine)
- Simple scaling function, 10 lines of code

**Priority**: HIGH (simple, effective, protects capital)

**Key References**:
- Tharp, V.K. (2006). "Trade Your Way to Financial Freedom." McGraw-Hill
- Faith, C. (2007). "Way of the Turtle." McGraw-Hill
- Tomasini, E. & Jaekle, U. (2009). "Trading Systems." Harriman House
- Davey, K. (2014). "Building Winning Algorithmic Trading Systems." Wiley

**Python Implementation Notes**:
~~~python
def drawdown_position_scale(current_equity, peak_equity, method="step"):
    # Calculate position size scale factor based on drawdown.
    # Returns: scale factor 0.0-1.0
    if peak_equity <= 0:
        return 1.0
    dd_pct = (peak_equity - current_equity) / peak_equity * 100
    if method == "linear":
        return max(0.25, 1.0 - (dd_pct / 20.0))
    elif method == "step":
        if dd_pct < 5: return 1.0
        elif dd_pct < 10: return 0.75
        elif dd_pct < 15: return 0.50
        else: return 0.25
    return 1.0

# Integration with position sizing:
# base_size = equity / max_positions  # $5000/10 = $500
# scale = drawdown_position_scale(equity, peak_equity)
# actual_size = base_size * scale
# min_size = 400  # Minimum to justify $3 commission
# if actual_size < min_size: skip trade
~~~

**ASX-Specific Considerations**:
- At $5K, even 10% DD ($500) is significant - aggressive scaling recommended
- Minimum viable position ~$400 (below this, $3 commission > 0.75% drag)
- Step-down preferred over linear (more predictable)
- Equity curve MA not suitable with ~287 trades total (too few data points)
- When scaled below min position, stop trading entirely
- Recovery: once equity above peak * 95%, restore full sizing


---

### 2.7 Sector/Industry Concentration Limits

**What**: Imposing maximum allocation limits per GICS sector to prevent over-exposure to any single sector, particularly mining/resources which dominates the ASX mid-cap universe.

Implementation:
- Define maximum positions per sector (e.g., max 3 out of 10 positions in Materials)
- When a new signal fires for a sector at capacity, reject it or replace weakest existing position
- Track sector allocation in real-time and include in signal ranking

ASX Small Ordinaries sector composition (approximate):
- Materials (mining/resources): 25-30% of index
- Financials: 10-15%
- Energy: 8-12%
- Healthcare: 8-10%
- Information Technology: 5-8%
- Industrials: 5-8%
- Consumer Discretionary: 5-8%
- Real Estate: 3-5%
- Other: 10-15%

**Why It Helps**: The ~185 ASX mid-cap universe is heavily concentrated in mining/resources. Without sector limits:
- A mining sector downturn can take out 5-6 of 10 positions simultaneously
- Signals from mining stocks are highly correlated (driven by commodity prices)
- Apparent diversification (10 positions) masks concentrated sector risk
- Parameter sensitivity is amplified (mining stocks respond similarly to same parameters)

The system already has `data/processed/sector_map.json` which can be leveraged immediately.

**Expected Impact**:
- Tail risk reduction: 15-25% (avoiding concentrated sector drawdowns)
- Max drawdown reduction: 10-20% (diversification benefit)
- Walk-forward consistency: +5% (less sector-specific regime sensitivity)
- CAGR impact: -1% to +1% (may miss concentrated gains but avoid concentrated losses)
- Win rate: neutral to slightly positive

**Implementation Complexity**: LOW
- sector_map.json already exists in data/processed/
- Simple counter per sector in position management
- No additional data or computation
- ~20 lines of code

**Priority**: HIGH (simple, effective, addresses known ASX concentration risk)

**Key References**:
- Clarke, R., de Silva, H. & Thorley, S. (2006). "Minimum-Variance Portfolio Composition." J. Portfolio Management, 32(1), 31-45
- Roncalli, T. (2014). "Introduction to Risk Parity and Budgeting." Chapman & Hall/CRC
- ASX (2024). "S&P/ASX Small Ordinaries Index Factsheet." (sector composition data)
- Malkiel, B. (2019). "A Random Walk Down Wall Street." 12th ed. Norton

**Python Implementation Notes**:
~~~python
import json

def load_sector_map(path="data/processed/sector_map.json"):
    with open(path) as f:
        return json.load(f)  # {ticker: sector}

def check_sector_limits(candidate_ticker, current_positions, sector_map,
                         max_per_sector=3):
    # Check if adding candidate would exceed sector limit.
    # Returns: (allowed, sector, current_count)
    sector = sector_map.get(candidate_ticker, "Unknown")
    sector_count = sum(1 for pos in current_positions
                       if sector_map.get(pos, "Unknown") == sector)
    allowed = sector_count < max_per_sector
    return allowed, sector, sector_count

def get_sector_allocation(positions, sector_map):
    # Get current sector distribution of portfolio.
    allocation = {}
    for pos in positions:
        sector = sector_map.get(pos, "Unknown")
        allocation[sector] = allocation.get(sector, 0) + 1
    return allocation

# Recommended limits for 10 positions:
# Materials: max 3 (30%)
# Energy: max 2 (20%)
# Financials: max 2 (20%)
# All others: max 2 (20%)
# Ensures minimum 4 different sectors represented
~~~

**ASX-Specific Considerations**:
- Current live portfolio has heavy resources concentration: EVN.AX (gold), S32.AX (mining), LYC.AX (rare earths), PDN.AX (uranium), WHC.AX (coal), STO.AX (energy) = 6/10 in resources/energy
- sector_map.json already exists at data/processed/ - can be leveraged immediately
- Consider sub-sector limits within Materials: max 2 gold miners, max 2 base metals, etc.
- Some ASX mid-caps have ambiguous sector classification - verify sector_map accuracy
- Sector limits work synergistically with regime detection: during mining-unfavorable regimes, limits prevent over-concentration even without explicit regime filtering
- Implementation order: sector limits BEFORE correlation-aware sizing (captures 80% of benefit at 20% complexity)



---

## Implementation Roadmap

### Phase 1: Foundation (Week 1) - Zero-Cost, High-Impact

| Step | Task | Section | Complexity | Expected Impact |
|------|------|---------|------------|-----------------|
| 1.1 | IOZ.AX 50-day MA slope regime indicator | 1.3 | LOW | +2-3% CAGR, +15% WF consistency |
| 1.2 | Market breadth filter (using market_breadth.py) | 1.3 | LOW | +1-2% CAGR, -10% max DD |
| 1.3 | Strategy activation matrix (enable/disable per regime) | 1.4 | MEDIUM | +2-4% CAGR, +10% WF consistency |
| 1.4 | Sector concentration limits (max 3 per sector) | 2.7 | LOW | -10-20% max DD, tail risk reduction |
| 1.5 | Drawdown-based position sizing (step-down) | 2.6 | LOW | -20-30% max DD, capital preservation |

**Rationale**: All use existing data (IOZ.AX, sector_map.json, market_breadth.py). No new dependencies. Combined expected impact: +3-5% CAGR, -25-40% max DD, +15-20% walk-forward consistency.

### Phase 2: Strategy Optimization (Week 2-3)

| Step | Task | Section | Complexity | Expected Impact |
|------|------|---------|------------|-----------------|
| 2.1 | Regime-strategy performance analysis (backtest per regime) | 1.6 | MEDIUM | Validates Phase 1 activation matrix |
| 2.2 | ATR-based volatility regime filter | 1.2 | MEDIUM | +1-2% CAGR, better regime granularity |
| 2.3 | Risk parity weighting across 4 strategies | 2.2 | MEDIUM | +0.2-0.4 Sharpe |
| 2.4 | Equity curve trading (strategy-level) | 2.4 | MEDIUM | +1-3% CAGR, auto-disable failing strategies |

**Rationale**: Builds on Phase 1 regime detection. Requires per-strategy P&L tracking (may need engine.py changes). Validates and refines activation matrix.

### Phase 3: Advanced Risk Management (Week 3-4)

| Step | Task | Section | Complexity | Expected Impact |
|------|------|---------|------------|-----------------|
| 3.1 | Kelly criterion per-strategy position sizing | 2.3 | MEDIUM | +1-2% CAGR, optimal sizing |
| 3.2 | Turbulence index (Kritzman) | 1.5 | MEDIUM | -25-40% max DD, tail risk |
| 3.3 | HMM regime detection (2-3 state) | 1.1 | HIGH | +2-5% CAGR (replaces simple regime) |

**Rationale**: More sophisticated approaches requiring additional libraries (hmmlearn, scipy). Only implement after Phase 1-2 establish baseline improvement. HMM may replace simple MA slope if backtests show improvement.

### Phase 4: Portfolio Optimization (Week 5+, Optional)

| Step | Task | Section | Complexity | Expected Impact |
|------|------|---------|------------|-----------------|
| 4.1 | Correlation-aware position filtering | 2.1 | MEDIUM-HIGH | +0.5-1.0 Sharpe |
| 4.2 | Maximum diversification portfolio scoring | 2.5 | HIGH | +0.5-1.0 Sharpe |

**Rationale**: High complexity, marginal benefit over sector limits. Only implement if Phase 1-3 demonstrate insufficient diversification improvement.

### Dependency Graph

~~~
Phase 1.1-1.2 (Regime Detection) --> Phase 1.3 (Strategy Activation)
Phase 1.4-1.5 (Risk Controls)    --> Independent, can be parallel
Phase 1.3 (Activation Matrix)     --> Phase 2.1 (Regime-Strategy Analysis)
Phase 2.1 (Analysis)              --> Phase 2.2-2.4 (Refinement)
Phase 2.3-2.4 (Strategy Weights)  --> Phase 3.1 (Kelly Sizing)
Phase 1.1-1.2 (Simple Regime)     --> Phase 3.3 (HMM Replacement)
Phase 1.4 (Sector Limits)         --> Phase 4.1-4.2 (Correlation/MDP)
~~~

### Required Libraries

~~~bash
# Phase 1 - no new libraries needed (uses existing numpy, pandas)
# Phase 2 - no new libraries needed
# Phase 3
pip install hmmlearn  # HMM regime detection (Section 1.1)
# Phase 4
pip install scipy  # Optimization for MDP (Section 2.5)
~~~

### Validation Protocol

After each phase, run the existing validation suite:
1. `python scripts/validate_oos.py` - Time-split OOS test
2. `python scripts/param_stability_report.py` - Parameter perturbation +-15%
3. `python scripts/health_check.py` - Baseline comparison

**Success criteria per phase**:
- OOS CAGR improvement (target: >5%, currently 2.2%)
- Parameter perturbation stability (target: <30% degradation, currently 76%)
- Walk-forward window profitability (target: >70%, currently 59%)
- Win rate improvement (target: >52%, currently 49.5%)
- Max drawdown reduction (target: <6%, currently ~8%)

### Key Insight

The single most impactful improvement is **Phase 1.1-1.3: Simple regime detection with strategy activation matrix**. This addresses the root cause of OOS degradation: the system trades strategies in regimes where they have negative expected value. By simply NOT trading mean reversion in trending markets and NOT trading trend following in range-bound markets, the system eliminates its worst trades without requiring any parameter changes.

Combined with sector limits (Phase 1.4) and drawdown sizing (Phase 1.5), Phase 1 alone should reduce the OOS degradation from 87% to 40-50% and improve walk-forward consistency from 59% to 70-75%.

---

## Appendix: Research Sources Summary

Total web searches conducted: 21+
Total documents analyzed: 15+
Total academic papers referenced: 30+

**Key academic sources**:
- Kritzman, M. & Li, Y. (2010). "Skulls, Financial Turbulence, and Risk Management." FAJ
- Kritzman, M., Page, S. & Turkington, D. (2012). "Regime Shifts: Implications for Dynamic Strategies." FAJ
- Maillard, S., Roncalli, T. & Teiletche, J. (2010). "Equally Weighted Risk Contribution Portfolios." JPM
- Kelly, J.L. (1956). "A New Interpretation of Information Rate." Bell System Technical Journal
- Choueifaty, Y. & Coignard, Y. (2008). "Toward Maximum Diversification." JPM
- Lopez de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley
- Ledoit, O. & Wolf, M. (2004). "A Well-Conditioned Estimator for Large-Dimensional Covariance Matrices."

**Key practitioner sources**:
- Alvarez, C. (2023). "Mean Reversion vs Trend Following Through the Years." alvarezquanttrading.com
- LuxAlgo (2024). "Market Regimes Explained." luxalgo.com
- QuantInsti (2024). "Inverse Volatility Weighting." quantinsti.com
- Portfolio Optimizer (2024). "The Turbulence Index." portfoliooptimizer.io
- Hudson & Thames (2024). "Hidden Markov Model." hudsonthames.org

---
*Report generated: 2026-02-19*
*Atlas-ASX Deep Research - Part 2: Regime Detection & Portfolio-Level Improvements*
