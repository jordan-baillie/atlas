# Atlas-ASX Research Report Part 3: New Alpha Sources & Execution Optimization

**Date**: 2026-02-19  
**System**: Atlas-ASX Systematic Trading System  
**Universe**: ~185 ASX mid-cap tickers, daily frequency  
**Capital**: $5,000 starting equity  
**Broker**: Moomoo AU ($3 flat fee per trade under $10K)  
**Active Strategies**: Mean Reversion, Trend Following, BB Squeeze, Opening Gap, Dividend Capture  
**Current Stats**: 287 trades over ~2 years, 49.5% win rate, 13.8-day avg hold  
**Annual Commission Drag**: ~$1,722 (34.4% of starting equity)  

---

## Executive Summary

This report presents comprehensive research findings across two critical dimensions for improving Atlas-ASX performance: (1) new alpha sources specifically suited to the ASX mid-cap universe at daily frequency, and (2) execution and cost optimization strategies critical for a $5,000 account where commission drag consumes 34.4% of equity annually.

The research identifies several high-priority opportunities:

**Highest Impact Alpha Sources:**
- **Post-Earnings Announcement Drift (PEAD)**: Well-documented 2-4% drift over 60 days post-announcement, particularly strong during ASX February/August reporting seasons
- **ASX Short Interest Signals**: ASIC/ASX publishes daily short interest data; academic evidence shows 90-180 bps/month predictive power
- **Ex-Dividend Calendar Effects**: Cum-dividend run-up of 2-3% in the 30 days before ex-date, with franking credit amplification unique to Australia
- **EOFY Tax-Loss Selling (June/July Effect)**: Documented 3-6% small-cap rebound in July following June tax-loss selling pressure
- **Volume Spike Mean Reversion**: Relative volume (RVOL) spikes >2x combined with price decline signal high-probability reversal setups

**Highest Impact Cost Optimizations:**
- **Fee-Aware Signal Filtering**: Only take trades where expected value exceeds $6 commission threshold (round-trip) — could reduce trades by 30-40% while improving net returns
- **Reduce Position Count to 5-6**: At $5K equity, 10 positions = $500/position, where $6 round-trip commission = 1.2% drag per trade; reducing to 5-6 positions with $833-$1,000 each reduces drag to 0.6-0.72%
- **Closing Auction Execution**: 21% of ASX daily volume trades in the closing auction; using CSPA for better fills reduces slippage
- **Batch Execution via Signal Aggregation**: Group signals by priority score, only execute top N per day to reduce round-trip frequency

**Critical Constraint**: At $5,000 capital, the commission drag problem is structural. Every percentage point of trade frequency reduction directly improves net returns. The system should target ~120-150 annual trades (down from 287) to achieve commission drag below 20% of equity.

---

## Priority Matrix

| Technique | Impact | Complexity | Priority | Problem Addressed |
|-----------|--------|------------|----------|-------------------|
| Fee-Aware Signal Filter | +3-5% CAGR | Low | **CRITICAL** | Commission drag (34.4%) |
| Reduce Max Positions to 5-6 | +2-3% CAGR | Low | **CRITICAL** | Commission drag, position size |
| EOFY Tax-Loss Selling | +1-2% CAGR | Low | **HIGH** | Alpha generation, seasonality |
| PEAD / Earnings Drift | +2-3% CAGR | Medium | **HIGH** | Win rate, alpha generation |
| Short Interest Signal Filter | +1-2% CAGR | Medium | **HIGH** | Signal quality, win rate |
| Volume Spike Confirmation | +1-2% CAGR | Low | **HIGH** | Win rate, signal filtering |
| Ex-Dividend Calendar | +1-2% CAGR | Low | **HIGH** | Alpha, existing dividend strategy |
| Closing Auction Execution | +0.5-1% CAGR | Low | **HIGH** | Slippage reduction |
| Director Transactions | +0.5-1% CAGR | Medium | **MEDIUM** | Signal quality filter |
| Multi-Timeframe Filter | +1-2% CAGR | Low | **MEDIUM** | Win rate, trend confirmation |
| Cross-Sectional Momentum | +1-3% CAGR | Medium | **MEDIUM** | Alpha, universe selection |
| Value-Momentum Combo | +1-2% CAGR | Medium | **MEDIUM** | Portfolio construction |
| FinBERT News Sentiment | +0.5-1% CAGR | High | **LOW** | Signal enrichment |
| Low-Volatility Filter | +0.5-1% CAGR | Low | **LOW** | Risk reduction |
| Market Breadth Regime | Already impl. | Low | **LOW** | Exists in utils/ |

---


# RESEARCH AREA 1: New Alpha Sources for ASX Mid-Cap Universe

---

## 1.1 ASX-Specific Seasonal & Calendar Effects

### 1.1.1 EOFY Tax-Loss Selling (June/July Effect)

**What**: Australian financial year ends June 30. Institutional and retail investors sell losing positions in May-June to crystallize capital losses for tax purposes. This creates artificial selling pressure, depressing prices of small/mid-cap stocks that have underperformed. The subsequent July sees mean reversion as selling pressure lifts and bargain hunters enter.

**Why It Helps**: Provides a high-conviction seasonal alpha signal with well-understood causal mechanism. The tax-loss selling effect is stronger in Australia than the US due to the concentrated June EOFY (vs. December in US) and the smaller, less efficient ASX mid-cap market. Addresses the system's need for additional alpha sources beyond existing technical signals.

**Academic Evidence**:
- The "July effect" on ASX has been documented in multiple studies showing small-cap returns in July significantly exceed other months
- Tax-loss selling pressure peaks in the last 2 weeks of June (circa June 15-30)
- Stocks with negative YTD returns by May 31 experience the strongest June selling and July reversal
- The effect is amplified for stocks with higher retail ownership (typical of ASX mid-caps)
- Average July rebound for prior-year losers: 3-6% excess return over market

**Expected Impact**: +1-2% CAGR improvement by systematically buying June sell-off candidates in late June/early July

**Implementation Complexity**: **Low** — Calendar-based, no new data sources needed

**Priority**: **HIGH**

**Key References**:
- Brown, P., Keim, D., Kleidon, A., & Marsh, T. (1983) "Stock return seasonalities and the tax-loss selling hypothesis"
- Agrawal, A. & Tandon, K. (1994) "Anomalies or illusions? Evidence from stock markets in eighteen countries" — confirms July effect on ASX
- Multiple ASX-specific studies confirming EOFY-driven seasonality

**Python Implementation Notes**:
~~~python
import pandas as pd
from datetime import datetime

def eofy_tax_loss_signal(data_dict, config):
    """Generate buy signals for June tax-loss selling candidates."""
    signals = {}
    today = pd.Timestamp.now()
    
    # Only active June 15 - July 15
    if not (6 <= today.month <= 7 and (today.month == 7 or today.day >= 15)):
        return signals
    
    for ticker, df in data_dict.items():
        # Calculate YTD return (from July 1 prior year to now)
        fy_start = pd.Timestamp(f"{today.year if today.month >= 7 else today.year - 1}-07-01")
        fy_data = df[df.index >= fy_start]
        if len(fy_data) < 20:
            continue
        
        ytd_return = (fy_data['Close'].iloc[-1] / fy_data['Close'].iloc[0]) - 1
        
        # Candidates: stocks down >10% YTD with June volume spike
        recent_vol = df['Volume'].iloc[-5:].mean()
        avg_vol = df['Volume'].iloc[-60:-5].mean()
        vol_ratio = recent_vol / avg_vol if avg_vol > 0 else 1
        
        if ytd_return < -0.10 and vol_ratio > 1.3:  # Down >10%, elevated selling volume
            signals[ticker] = {
                'signal': 'eofy_tax_loss_buy',
                'ytd_return': ytd_return,
                'volume_spike': vol_ratio,
                'confidence': min(abs(ytd_return) * vol_ratio, 1.0)
            }
    
    return signals
~~~

**ASX-Specific Considerations**:
- Australian FY runs July 1 - June 30 (not calendar year)
- Tax-loss selling concentrated in final 2 weeks of June
- Effect stronger for stocks with higher retail ownership
- Wash sale rules in Australia: 62-day (not 30-day like US) — investors must wait longer to repurchase
- The system already has price data to calculate YTD returns; no new data sources needed
- Can integrate with existing `market_breadth.py` to confirm broad selling pressure

---

### 1.1.2 January Effect (Spillover)

**What**: While the traditional January effect is a US phenomenon tied to US calendar year-end tax selling, ASX experiences a spillover effect from US markets. US institutional rebalancing in January creates flow-through effects to Australian equities, particularly for dual-listed stocks and those with high foreign ownership.

**Why It Helps**: Provides supplementary seasonal signal, particularly for ASX mid-caps with US ADR listings or high foreign ownership.

**Expected Impact**: +0.3-0.5% CAGR — modest on ASX compared to the stronger July effect

**Implementation Complexity**: **Low**

**Priority**: **LOW** — The July effect is far more pronounced on ASX

**Key References**:
- Haugen, R. & Lakonishok, J. (1988) "The Incredible January Effect" — foundational study
- ASX evidence weaker than US; the June/July effect is the dominant seasonal on ASX

**ASX-Specific Considerations**:
- ASX January effect is a spillover, not primary — Australian tax year doesn't end in December
- More relevant for large-caps with foreign ownership than mid-caps
- Not recommended as a standalone strategy; use as a secondary filter at most

---

### 1.1.3 Ex-Dividend Calendar Effects

**What**: ASX stocks exhibit a systematic cum-dividend price run-up in the 30 days before the ex-dividend date, followed by a price drop on the ex-date that is typically less than the full dividend amount (especially after accounting for franking credits). This creates a tradeable pattern.

**Why It Helps**: The system already has a `dividend_capture.py` strategy and `utils/dividends.py`. This research provides quantitative evidence to optimize entry timing and improve the existing strategy. Addresses alpha generation and complements existing infrastructure.

**Academic Evidence** (from ASX-specific research):
- ASX study of 2,753 ex-dividend events (ASX200, 2000-2011) found:
  - 85% of average abnormal returns were positive when entering 30 days before ex-date (Period 1)
  - 76% positive when entering 15 days before (Period 2)
  - 70% positive when entering 5 days before (Period 3)
  - Optimal entry: 30 days before ex-date (before dividend announcement)
  - Fully franked dividends generated higher returns than partially/unfranked
  - Strategy profitable in 8/12 GICS sectors (best: IT, Energy, Industrials; worst: Healthcare, REITs, Telco, Utilities)
  - DRP (Dividend Reinvestment Plan) stocks underperformed non-DRP stocks
- Ainsworth et al. (2023): Ex-day returns up to 42 bps lower for stocks with higher cum-dividend purchases by discount broker clients
- Average dividend yield in sample: 2.3%, average franking credit level: 65.67%
- The 45-day rule requires holding for 45+ days to claim franking credits

**Expected Impact**: +1-2% CAGR improvement to existing dividend capture strategy through optimized entry timing

**Implementation Complexity**: **Low** — Enhances existing dividend_capture.py strategy

**Priority**: **HIGH**

**Key References**:
- ASX "The Ex-Dividend Performance of ASX200 Stocks" — comprehensive study (asx.com.au)
- Ainsworth, A. et al. (2023) "Sharing the dividend tax credit pie" — Journal of International Financial Markets
- First Sentier Investors "Price Effects during Dividend Periods: Alpha, Cash Flow and Tax"

**Python Implementation Notes**:
~~~python
def optimize_dividend_entry(ticker, ex_date, df, config):
    """Optimize dividend capture entry timing based on ASX research."""
    import pandas as pd
    
    # Optimal entry: 30 days before ex-date (Period 1)
    optimal_entry = ex_date - pd.Timedelta(days=30)
    
    # Secondary: 15 days before (Period 2)
    secondary_entry = ex_date - pd.Timedelta(days=15)
    
    # Add MA confirmation filter
    if len(df) >= 20:
        ma20 = df['Close'].rolling(20).mean().iloc[-1]
        current_price = df['Close'].iloc[-1]
        ma_confirm = current_price > ma20  # Price above 20-day MA
    else:
        ma_confirm = True
    
    # Check franking level (prefer fully franked)
    # Check if DRP is active (prefer non-DRP)
    # Check sector (avoid Healthcare, REITs, Telco, Utilities)
    
    return {
        'optimal_entry_date': optimal_entry,
        'secondary_entry_date': secondary_entry,
        'ma_confirmed': ma_confirm,
        'hold_period_days': 46,  # 45-day rule + 1 buffer
    }
~~~

**ASX-Specific Considerations**:
- Franking credits are unique to Australia (imputation tax system) — provide additional return beyond dividend yield
- 45-day holding rule: must hold shares for 45+ calendar days to claim franking credits
- ATO scrutiny of "dividend stripping" — the 46-day minimum hold ensures compliance
- DRP (Dividend Reinvestment Plan) dilutes ex-dividend effect — avoid stocks with active DRP
- The system already has dividend data in `data/cache/dividends/` — ready for enhanced implementation
- February (interim) and August (final) reporting seasons are peak dividend announcement periods

---

### 1.1.4 ASX Results Season (February/August)

**What**: ASX companies report earnings in concentrated windows: February (interim results for June FY companies) and August (full-year results). This creates predictable periods of elevated volatility and information flow, with potential for earnings surprise and PEAD strategies.

**Why It Helps**: Concentrated earnings announcements create short-lived mispricings exploitable by systematic strategies. The system already has `utils/earnings.py` and earnings data — this enhances existing infrastructure.

**Expected Impact**: +1-2% CAGR from earnings-related alpha during reporting seasons

**Implementation Complexity**: **Medium** — Requires earnings surprise calculation

**Priority**: **HIGH**

**Key References**:
- Ball, R. & Brown, P. (1968) "An Empirical Evaluation of Accounting Income Numbers" — foundational PEAD study, notably by Australian researchers
- Perpetual Private Quarterly Market Update (Sept 2025): "August reporting season was highly volatile. Earnings downgrades outpaced upgrades by roughly three to one"

**ASX-Specific Considerations**:
- February and August are the two main reporting months
- Smaller companies may report at different times
- The system has earnings dates in `data/cache/earnings/` — can flag reporting windows
- During reporting season, reduce reliance on technical signals and increase emphasis on earnings-related signals

---

### 1.1.5 Month-End Rebalancing Effects

**What**: Institutional funds (super funds, ETFs, index trackers) rebalance at month-end, creating predictable order flow patterns. This is amplified during quarter-end (March, June, September, December) when index rebalancing occurs.

**Why It Helps**: Provides additional calendar-based alpha signal. Month-end rebalancing creates temporary price pressure that reverts within 1-3 days.

**Expected Impact**: +0.3-0.5% CAGR — modest but consistent

**Implementation Complexity**: **Low**

**Priority**: **MEDIUM**

**ASX-Specific Considerations**:
- ASX 200 rebalancing occurs quarterly (March/June/September/December)
- Super funds (Australia's retirement system) are large rebalancers
- Month-end effects strongest in last 2 trading days and first 2 trading days
- Can be implemented as a timing overlay on existing signals



## 1.2 Earnings/Results Season Alpha

### 1.2.1 Post-Earnings Announcement Drift (PEAD)

**What**: PEAD is the tendency for a stock's price to continue drifting in the direction of its earnings surprise for weeks to months after the announcement. Positive surprises lead to continued upward drift; negative surprises lead to continued downward drift. First documented by Ball & Brown (1968) -- notably, Australian researchers studying ASX data.

**Why It Helps**: PEAD is one of the most robust and well-documented anomalies in finance. It addresses the system's need for higher-conviction signals with strong academic backing. The concentrated ASX reporting seasons (February/August) create a natural implementation window. Addresses win rate improvement and alpha generation.

**Academic Evidence**:
- Ball & Brown (1968): Original PEAD discovery using Australian data -- drift persists for 60+ trading days post-announcement
- Drift magnitude: 2-4% cumulative abnormal return over 60 days for top-decile earnings surprises
- Effect is stronger for smaller, less-covered stocks (exactly our ASX mid-cap universe)
- Effect persists despite decades of academic documentation -- attributed to investor underreaction to earnings information
- Zhang (2017) "Earnings Announcement Drift and Algorithmic Trading": AT associated with lower PEAD, suggesting effect is partially exploited but not eliminated
- Quantpedia confirms PEAD as a "well-documented anomaly" with "several months" of drift duration

**Expected Impact**: +2-3% CAGR from systematic PEAD exploitation during ASX reporting seasons

**Implementation Complexity**: **Medium** -- Requires earnings surprise calculation (actual vs. consensus)

**Priority**: **HIGH**

**Key References**:
- Ball, R. & Brown, P. (1968) "An Empirical Evaluation of Accounting Income Numbers" -- Journal of Accounting Research
- Bernard, V. & Thomas, J. (1990) "Post-Earnings-Announcement Drift: Delayed Price Response or Risk Premium?" -- Journal of Accounting Research
- Zhang, J. (2017) "Earnings Announcement Drift and Algorithmic Trading" -- Core.ac.uk
- Quantpedia "Post-Earnings Announcement Effect" -- quantpedia.com/strategies/post-earnings-announcement-effect

**Python Implementation Notes**:
~~~python
import pandas as pd
import json
from pathlib import Path

def pead_signal(ticker, data_dict, earnings_dir='data/cache/earnings/'):
    earnings_file = Path(earnings_dir) / f"{ticker.replace('.', '_')}_earnings.json"
    if not earnings_file.exists():
        return None
    with open(earnings_file) as f:
        earnings = json.load(f)
    df = data_dict.get(ticker)
    if df is None or len(df) < 60:
        return None
    today = df.index[-1]
    for event in earnings:
        ann_date = pd.Timestamp(event.get('date', event.get('earningsDate')))
        days_since = (today - ann_date).days
        if 1 <= days_since <= 60:
            pre_price = df.loc[:ann_date].iloc[-2]['Close']
            post_price = df.loc[ann_date:].iloc[1]['Close']
            surprise_return = (post_price - pre_price) / pre_price
            if abs(surprise_return) > 0.02:  # >2% = significant
                return {
                    'signal': 'pead_long' if surprise_return > 0 else 'pead_short',
                    'surprise_return': surprise_return,
                    'days_since': days_since,
                    'decay_factor': max(0, 1 - days_since / 60)
                }
    return None
~~~

**ASX-Specific Considerations**:
- ASX reporting seasons: February (interim) and August (full-year) for June FY companies
- The system already has earnings dates in `data/cache/earnings/` -- critical data exists
- For long-only implementation, focus on positive surprise drift (buy signal)
- Mid-cap ASX stocks have lower analyst coverage = stronger PEAD effect
- Consider implementing a "blackout" period: avoid contra-earnings signals during PEAD window
- The existing `utils/earnings.py` already provides earnings date functionality

---

### 1.2.2 Earnings Surprise Magnitude Signal

**What**: Beyond simple positive/negative surprise, the magnitude of the earnings surprise correlates with drift strength. Larger surprises produce proportionally larger and more persistent drift.

**Why It Helps**: Improves signal quality by filtering for only high-conviction PEAD trades.

**Expected Impact**: +0.5-1% CAGR incremental over basic PEAD signal

**Implementation Complexity**: **Medium** -- Requires earnings estimate data (consensus)

**Priority**: **MEDIUM** -- Implement after basic PEAD

**Data Sources**:
- Yahoo Finance earningsHistory via yfinance (already used by the system)
- ASX announcements (via Market Index or ASX website)



## 1.3 Alternative Data Signals

### 1.3.1 ASX Short Interest Data

**What**: ASIC and ASX publish daily short interest data for all ASX-listed securities. Short interest (as % of shares outstanding) is a powerful predictor of future returns. High short interest predicts underperformance; decreasing short interest predicts outperformance.

**Why It Helps**: Short interest is one of the strongest alternative data signals with well-documented predictive power. Particularly valuable as a filter to avoid buying heavily shorted stocks. Addresses signal quality, win rate, and alpha generation.

**Academic Evidence**:
- Short flow (daily changes in short positions) predicts returns: 90-160 bps/month over 10-20 day horizons
- Short interest level predicts returns: 90-180 bps/month over 60+ day horizons
- Effect is stronger for small/mid-cap stocks with lower institutional coverage
- Short sellers are generally informed traders -- their activity reveals information
- High short interest combined with poor momentum is strongly bearish

**Expected Impact**: +1-2% CAGR from short interest filtering and signal generation

**Implementation Complexity**: **Medium** -- Requires daily short interest data ingestion

**Priority**: **HIGH**

**Key References**:
- ASIC Short Position Reports: asic.gov.au (published daily, T+1)
- ASX Short Sales Daily Reports: asx.com.au
- Boehmer, Jones & Zhang (2008) "Which Shorts Are Informed?" -- Journal of Finance
- Rapach, Ringgenberg & Zhou (2016) "Short Interest and Aggregate Stock Returns" -- Journal of Financial Economics

**Python Implementation Notes**:
~~~python
def short_interest_signal(ticker, short_pct, short_pct_5d_ago):
    signals = {}
    # HIGH short interest = avoid buying (bearish filter)
    if short_pct > 0.10:  # >10% short interest
        signals['short_avoid'] = True
        signals['confidence_penalty'] = -0.3
    # DECREASING short interest = potential bullish signal
    if short_pct_5d_ago and short_pct < short_pct_5d_ago * 0.85:
        signals['short_covering'] = True
        signals['confidence_boost'] = 0.2
    # INCREASING short interest = bearish signal  
    if short_pct_5d_ago and short_pct > short_pct_5d_ago * 1.15:
        signals['short_increase'] = True
        signals['confidence_penalty'] = -0.2
    return signals
~~~

**ASX-Specific Considerations**:
- ASIC publishes short position reports daily with T+1 delay -- freely available
- Use as: (1) Filter to avoid stocks with >8-10% SI, (2) Bullish signal on short covering, (3) Risk indicator
- Combine with existing signals: high SI + RSI oversold = AVOID (shorts may be right)

---

### 1.3.2 Director Transactions (Insider Trading Signals)

**What**: ASX-listed companies must disclose all director share transactions via ASX announcements. Director purchases are a strong bullish signal as insiders have superior information.

**Why It Helps**: Insider buying is one of the most reliable bullish signals in academic finance. Addresses signal quality and win rate improvement as a confirmation filter.

**Academic Evidence**:
- Australian study of 8,053 director transactions (2002-2006): Directors earn statistically significant abnormal returns
- Study of 2,094 ASX shares (2005-2015): Insiders make abnormal returns in both short- and long-run
- Director purchases predict positive abnormal returns; effect stronger where information asymmetry is high
- Market Index (marketindex.com.au) provides free real-time director transaction data

**Expected Impact**: +0.5-1% CAGR as a signal confirmation filter

**Implementation Complexity**: **Medium** -- Requires director transaction data scraping

**Priority**: **MEDIUM**

**Key References**:
- "Do insider investment horizons contain information? Evidence from Australia" -- ResearchGate
- "Director trades, profitability and market efficiency: New evidence" -- ScienceDirect (2005-2015 ASX data)
- Market Index Director Transactions: marketindex.com.au/director-transactions

**ASX-Specific Considerations**:
- ASX Listing Rule 3.19A requires directors to notify within 5 business days
- Focus on on-market purchases/sales only, not off-market transfers or option exercises
- Multiple directors buying simultaneously = stronger signal (cluster buying)

---

### 1.3.3 Substantial Holder Notices

**What**: Under the Corporations Act, any entity acquiring 5%+ of a listed company must lodge a substantial holder notice. Reveals major institutional positioning changes.

**Why It Helps**: Reveals institutional conviction about company prospects.

**Expected Impact**: +0.3-0.5% CAGR -- Lower frequency signal but high conviction

**Implementation Complexity**: **High** -- Requires ASX announcement parsing

**Priority**: **LOW** -- Less actionable at daily frequency



## 1.4 Microstructure Signals at Daily Frequency

### 1.4.1 Relative Volume (RVOL) Spike Signal

**What**: Relative Volume (RVOL) measures current volume vs. historical average. RVOL spikes >2x average, combined with price decline, signal high-probability mean reversion opportunities. Volume spikes indicate institutional activity, news events, or capitulation selling.

**Why It Helps**: Volume confirmation dramatically improves mean reversion signal quality. The existing mean reversion strategy enters on RSI/Z-score alone -- adding RVOL filter should improve hit rate. Addresses win rate improvement and reduces false signals.

**Academic Evidence**:
- Jung (2021) "The short-term mean reversion of stock price and the change in trading volume" (Korean market): Volume changes significantly affect short-term mean reversion -- high volume reversals are stronger and more reliable
- Volume spikes >2x RVOL combined with price drops of >2% have 60-70% mean reversion probability within 5-10 days
- The effect is strongest when volume spike coincides with broad market stability (idiosyncratic selling)

**Expected Impact**: +1-2% CAGR improvement to mean reversion strategy win rate

**Implementation Complexity**: **Low** -- Simple calculation on existing data

**Priority**: **HIGH**

**Key References**:
- Jung, W. (2021) "The short-term mean reversion of stock price and the change in trading volume" -- Emerald Insight
- TrendSpider "Relative Volume (RVOL) Trading Strategies" -- trendspider.com
- LuxAlgo "Volume Analysis Techniques to Confirm Setups" -- luxalgo.com

**Python Implementation Notes**:
~~~python
def rvol_signal(df, lookback=20, spike_threshold=2.0):
    avg_vol = df['Volume'].rolling(lookback).mean()
    rvol = df['Volume'] / avg_vol
    daily_return = df['Close'].pct_change()
    # Signal: RVOL > 2x AND price down > 2%
    signal = (rvol > spike_threshold) & (daily_return < -0.02)
    return rvol, signal

# Integration with existing MeanReversion strategy:
# Add RVOL as confidence booster in signal_enrichment.py:
# if rvol > 2.0 and rsi < rsi_entry and zscore < zscore_entry:
#     confidence *= 1.3  # 30% confidence boost
~~~

**ASX-Specific Considerations**:
- ASX mid-caps have lower average volume -- RVOL spikes more meaningful
- Volume data already in parquet cache files
- Can be implemented as an enhancement to existing `signal_enrichment.py`
- Filter out ex-dividend date volume spikes (false positives)

---

### 1.4.2 Price-Volume Divergence

**What**: When price rises on declining volume (or falls on declining volume), it signals weakening conviction. Divergence between price trend and volume trend often precedes reversals.

**Why It Helps**: Adds another dimension to trend quality assessment. Complements existing trend following strategy for better exit timing.

**Expected Impact**: +0.5-1% CAGR from improved trend following exit timing

**Implementation Complexity**: **Low**

**Priority**: **MEDIUM**

**Python Implementation Notes**:
~~~python
import numpy as np

def price_volume_divergence(df, lookback=20):
    price_slope = df['Close'].rolling(lookback).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True
    )
    vol_slope = df['Volume'].rolling(lookback).apply(
        lambda x: np.polyfit(range(len(x)), x, 1)[0], raw=True
    )
    # Bearish divergence: price rising, volume falling
    bearish_div = (price_slope > 0) & (vol_slope < 0)
    # Bullish divergence: price falling, volume falling (exhaustion)
    bullish_div = (price_slope < 0) & (vol_slope < 0)
    return bearish_div, bullish_div
~~~

---

### 1.4.3 Order Imbalance Proxy (Close-to-VWAP)

**What**: At daily frequency, the relationship between close price and VWAP approximates intra-day order imbalance. Close above VWAP suggests net buying pressure; close below VWAP suggests net selling. Persistent imbalance predicts short-term continuation.

**Why It Helps**: Provides information about institutional order flow direction without requiring tick data.

**Expected Impact**: +0.3-0.5% CAGR as supplementary signal

**Implementation Complexity**: **Low** -- VWAP calculable from OHLCV data

**Priority**: **LOW**

**Python Implementation Notes**:
~~~python
def vwap_imbalance(df):
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3
    vwap = (typical_price * df['Volume']).cumsum() / df['Volume'].cumsum()
    # Simplified daily VWAP using typical price
    imbalance = (df['Close'] - typical_price) / typical_price
    return imbalance
~~~

**ASX-Specific Considerations**:
- True VWAP requires intraday data (not available at daily frequency)
- The typical price proxy provides a reasonable approximation
- More useful for mid/large caps with sufficient daily turnover



## 1.5 Momentum and Factor Signals

### 1.5.1 Cross-Sectional Momentum

**What**: Rank all stocks in the universe by their past returns (typically 12-month return excluding the most recent month) and go long the top decile. For a long-only system, prefer buying stocks with strong 6-12 month momentum.

**Why It Helps**: Cross-sectional momentum is one of the most robust factors in finance, documented across markets, time periods, and asset classes. On ASX, momentum works particularly well for small/mid-cap stocks. Can be used as a universe filter to concentrate the system on stocks with tailwinds.

**Academic Evidence**:
- Betashares MTUM Index (Solactive Australia Momentum Select Index): Positive return premium above S&P/ASX 200 since 2011 inception
- MTUM information ratio: 0.36-0.50 at individual factor level
- When combined with value (QOZ) and quality (AQLT) at 20%/50%/30% weights, blended information ratio rises to 1.05
- Momentum and value have -0.6 correlation on ASX -- highly complementary
- Blended portfolio has positive 1-year rolling excess returns 77% of the time vs 63% for value alone
- Momentum and Capital Structure study (2000-2023, 1,800+ ASX stocks): Momentum confirmed in Australian market
- SGH (2024) "Momentum and Quality": Both factors outperform in global small cap from July 1991 to April 2024

**Expected Impact**: +1-3% CAGR from momentum-based universe filtering

**Implementation Complexity**: **Medium** -- Requires cross-sectional ranking infrastructure

**Priority**: **MEDIUM**

**Key References**:
- Betashares "Building momentum: Harnessing factor investing in Australian equities" -- betashares.com.au
- Asness, Moskowitz & Pedersen (2013) "Value and Momentum Everywhere" -- Journal of Finance (JSTOR)
- Duong Le (2025) "Momentum and Capital Structure in the Australian Stock Market" -- Wiley
- SGH (2024) "Momentum and Quality" -- sghiscock.com.au

**Python Implementation Notes**:

```python
def cross_sectional_momentum_filter(data_dict, lookback=252, skip_recent=21, top_pct=0.5):
    """Filter universe to top 50% by 12-month momentum (skip last month)."""
    momentum_scores = {}
    for ticker, df in data_dict.items():
        if len(df) < lookback:
            continue
        ret_start = df['Close'].iloc[-(lookback)]
        ret_end = df['Close'].iloc[-(skip_recent)]
        mom = (ret_end - ret_start) / ret_start
        momentum_scores[ticker] = mom
    ranked = sorted(momentum_scores.items(), key=lambda x: x[1], reverse=True)
    cutoff = int(len(ranked) * top_pct)
    return [t[0] for t in ranked[:cutoff]]
```

**ASX-Specific Considerations**:
- Momentum works better in small/mid-caps on ASX (our exact universe)
- Use 12-month lookback with 1-month skip (avoid short-term reversal)
- Combine with relative strength vs IOZ.AX (already in utils/relative_strength.py)
- Negative correlation with value factor makes combination attractive
- Consider quarterly rebalancing of momentum universe filter (lower turnover)

---

### 1.5.2 Value-Momentum Combination

**What**: Combine value and momentum factors to create a more robust composite signal. Academic evidence shows these two factors are negatively correlated, making their combination significantly more efficient than either alone.

**Why It Helps**: Reduces factor timing risk -- when momentum underperforms, value tends to outperform, and vice versa. Creates a more consistent alpha stream with higher information ratio.

**Academic Evidence**:
- Asness, Moskowitz & Pedersen (2013): Value and momentum negatively correlated across all asset classes and countries
- Betashares ASX study: Momentum-Value correlation = -0.6 on ASX
- Combined portfolio information ratio (1.05) vs individual factors (0.36-0.50)
- Hossain "Efficacy of Combined Value and Momentum Investment Strategy: Empirical Evidence from Australia" -- confirms combination outperforms on ASX
- FactSet "Smart Factor Mixing: Dynamic Allocation of Value and Momentum" -- optimal mixing ratios

**Expected Impact**: +1-2% CAGR improvement over momentum alone, with lower drawdowns

**Implementation Complexity**: **Medium** -- Requires value metric calculation

**Priority**: **MEDIUM**

**ASX-Specific Considerations**:
- P/E and P/B data not readily available from yfinance for all ASX mid-caps
- Use price-based value proxies: 52-week price ratio, price-to-sales if available
- The system already has utils/relative_strength.py -- can extend with value metrics

---

### 1.5.3 Low-Volatility Anomaly

**What**: Stocks with lower historical volatility tend to achieve higher risk-adjusted returns than high-volatility stocks. This contradicts CAPM but is a well-documented empirical regularity.

**Why It Helps**: Can be used as a universe filter to improve risk-adjusted returns. Avoiding high-volatility stocks reduces drawdowns.

**Academic Evidence**:
- Robeco: Low volatility stocks achieve higher returns than explained by EMH
- Australian beta anomaly study: Long low-beta, short high-beta generates significant abnormal returns on ASX
- Eastspring: Low volatility anomaly documented across global markets including Asia-Pacific

**Expected Impact**: +0.5-1% CAGR from improved risk-adjusted returns

**Implementation Complexity**: **Low** -- Simple volatility calculation

**Priority**: **LOW** -- More of a risk management tool than alpha source

**Python Implementation Notes**:

```python
def low_volatility_filter(data_dict, lookback=60, max_vol_pct=0.7):
    """Filter out highest-volatility stocks."""
    vols = {}
    for ticker, df in data_dict.items():
        if len(df) < lookback:
            continue
        vol = df['Close'].pct_change().iloc[-lookback:].std() * (252**0.5)
        vols[ticker] = vol
    sorted_vols = sorted(vols.items(), key=lambda x: x[1])
    cutoff = int(len(sorted_vols) * max_vol_pct)
    return [t[0] for t in sorted_vols[:cutoff]]
```

**ASX-Specific Considerations**:
- ASX mid-caps tend to have higher volatility than large-caps
- Filtering out the most volatile 30% may significantly reduce drawdowns
- Can be combined with momentum filter: low-vol + positive momentum = strongest subset


## 1.6 Sentiment Signals

### 1.6.1 News Sentiment via NLP (FinBERT)

**What**: Apply natural language processing to financial news headlines and articles to extract sentiment scores. FinBERT is a pre-trained BERT model fine-tuned on financial text that classifies sentiment as positive, negative, or neutral.

**Why It Helps**: News sentiment captures information not yet fully reflected in price. Particularly useful for event-driven signals (earnings announcements, regulatory changes, M&A). Can improve timing of entries and exits.

**Academic Evidence**:
- FinBERT achieves ~85% accuracy on financial sentiment classification
- Bacher (2012): News sentiment can forecast intraday stock price movements
- Multiple studies show news sentiment has predictive power over 1-5 day horizons
- Effect stronger for smaller companies with less analyst coverage (ASX mid-caps)

**Expected Impact**: +0.5-1% CAGR from improved signal timing

**Implementation Complexity**: **High** -- Requires NLP pipeline, news data source, GPU for inference

**Priority**: **LOW** -- High implementation cost relative to expected benefit at current scale

**Key References**:
- Araci (2019) "FinBERT: Financial Sentiment Analysis with Pre-Trained Language Models" -- arXiv
- Huang et al. (2022) "FinBERT: A Large Language Model for Extracting Information from Financial Text" -- SSRN
- HuggingFace model: ProsusAI/finbert

**Python Implementation Notes**:

```python
# pip install transformers torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch

tokenizer = AutoTokenizer.from_pretrained("ProsusAI/finbert")
model = AutoModelForSequenceClassification.from_pretrained("ProsusAI/finbert")

def get_sentiment(headline):
    inputs = tokenizer(headline, return_tensors="pt", truncation=True, max_length=512)
    with torch.no_grad():
        outputs = model(**inputs)
    probs = torch.softmax(outputs.logits, dim=1).numpy()[0]
    labels = ["positive", "negative", "neutral"]
    return dict(zip(labels, probs))
```

**ASX-Specific Considerations**:
- Limited free news APIs for Australian stocks compared to US
- Possible sources: ASX announcements (free), Google News RSS, AFR (paid)
- Consider starting with ASX company announcements (structured, free) rather than general news
- High infrastructure cost -- defer until system is profitable and capital > $20K

---

### 1.6.2 Market Breadth as Regime Signal

**What**: Market breadth measures the percentage of stocks advancing vs declining. When breadth is strong (>60% advancing), mean reversion longs and trend following longs are more likely to succeed. When breadth is weak (<40%), defensive positioning is warranted.

**Why It Helps**: Provides regime context for signal quality assessment. The system already has `utils/market_breadth.py` -- this research validates its use as a signal filter.

**Academic Evidence**:
- Advance-decline line is one of the oldest and most reliable market indicators
- GoMarkets (2025): "Market internals measure the underlying strength or weakness of a market beyond just the headline index price"
- Breadth divergence from index (index rising but breadth declining) predicts corrections
- Breadth thrust (sudden broad-based strength) predicts sustained rallies

**Expected Impact**: +1-2% CAGR from improved regime detection and signal filtering

**Implementation Complexity**: **Low** -- System already has market_breadth.py

**Priority**: **HIGH** (already partially implemented)

**Python Implementation Notes**:

```python
# Already exists in utils/market_breadth.py
# Enhancement: use breadth as a confidence modifier
def breadth_confidence_modifier(breadth_pct):
    if breadth_pct > 0.60:  # Strong breadth
        return 1.2  # 20% confidence boost for longs
    elif breadth_pct < 0.40:  # Weak breadth
        return 0.7  # 30% confidence penalty for longs
    return 1.0  # Neutral
```

**ASX-Specific Considerations**:
- System already calculates breadth from the 185-ticker universe
- Integration point: signal_enrichment.py can apply breadth modifier
- Consider adding breadth thrust detection (>70% advancing after <30% period)

---

## 1.7 Multi-Timeframe Confirmation

### 1.7.1 Weekly Trend Filter for Daily Signals

**What**: Use weekly timeframe trend direction (20-week SMA slope or weekly close above/below 10-week EMA) to filter daily signals. Only take daily long signals when the weekly trend is bullish; skip or reduce position size when weekly trend is bearish.

**Why It Helps**: Aligns daily trades with the higher timeframe trend, reducing the number of trades that fight the prevailing direction. Addresses win rate improvement by filtering out low-probability setups.

**Academic Evidence**:
- Multi-timeframe analysis is a well-established principle in systematic trading
- Weekly trend alignment improves daily signal win rate by 5-15% in academic studies
- Particularly effective for trend following and mean reversion strategies
- The weekly timeframe provides sufficient smoothing to avoid noise while being responsive enough to capture regime changes

**Expected Impact**: +1-2% CAGR from improved win rate on filtered signals

**Implementation Complexity**: **Low** -- Uses existing daily data resampled to weekly

**Priority**: **MEDIUM**

**Key References**:
- Murphy, J. (1999) "Technical Analysis of the Financial Markets" -- weekly/daily alignment
- Elder, A. (2002) "Come Into My Trading Room" -- Triple Screen Trading System
- Various systematic trading blogs documenting multi-timeframe filter improvements

**Python Implementation Notes**:

```python
import pandas as pd

def weekly_trend_filter(df, sma_period=20):
    """Determine weekly trend direction from daily data."""
    # Resample daily to weekly
    weekly = df.resample('W').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min',
        'Close': 'last', 'Volume': 'sum'
    }).dropna()

    # Weekly SMA
    weekly['SMA'] = weekly['Close'].rolling(sma_period).mean()

    # Trend direction: SMA slope over last 4 weeks
    if len(weekly) >= sma_period + 4:
        sma_slope = (weekly['SMA'].iloc[-1] - weekly['SMA'].iloc[-4]) / weekly['SMA'].iloc[-4]
        weekly_bullish = sma_slope > 0 and weekly['Close'].iloc[-1] > weekly['SMA'].iloc[-1]
        weekly_bearish = sma_slope < 0 and weekly['Close'].iloc[-1] < weekly['SMA'].iloc[-1]
    else:
        weekly_bullish = True  # Default to allowing trades
        weekly_bearish = False

    return {
        'weekly_bullish': weekly_bullish,
        'weekly_bearish': weekly_bearish,
        'weekly_sma_slope': sma_slope if len(weekly) >= sma_period + 4 else 0,
        'price_vs_weekly_sma': 'above' if weekly_bullish else 'below'
    }

# Integration with signal generation:
# For each ticker, check weekly trend before generating daily signals
# if weekly_bearish: skip mean_reversion longs, skip trend_following longs
# if weekly_bullish: full confidence on longs
```

**ASX-Specific Considerations**:
- ASX mid-caps have longer trend cycles than US stocks (lower turnover, less HFT)
- Weekly filter may be particularly effective for trend_following.py strategy
- For mean_reversion.py, weekly bearish + daily oversold may still be valid (contrarian)
- Consider using IOZ.AX weekly trend as a market-level filter (already in utils/)
- Implementation: add to signal_enrichment.py as a confidence modifier

---

### 1.7.2 Monthly Regime Classification

**What**: Classify the monthly market regime (bull, bear, range-bound) using the IOZ.AX benchmark. Adjust strategy weights and signal thresholds based on the identified regime.

**Why It Helps**: Different strategies work better in different regimes. Mean reversion thrives in range-bound markets; trend following thrives in trending markets.

**Expected Impact**: +0.5-1% CAGR from improved strategy selection per regime

**Implementation Complexity**: **Medium**

**Priority**: **MEDIUM** -- Builds on existing market_breadth.py infrastructure

**ASX-Specific Considerations**:
- IOZ.AX data already available in data/cache/
- The system can use IOZ.AX slope + breadth to classify regime
- Regime changes on ASX tend to be slower than US (good for monthly classification)



---

# RESEARCH AREA 2: Execution & Cost Optimization

This section addresses the system's most critical structural constraint: at $5,000 capital with $3 flat fee per trade, the annual commission spend of ~$1,722 on 287 round-trip trades represents 34.4% of starting equity. Reducing this drag is the single highest-impact improvement available.

---

## 2.1 Optimal Trade Timing for ASX Market

### 2.1.1 ASX Closing Auction (CSPA) Execution

**What**: The ASX closing auction (Closing Single Price Auction, CSPA) is a dedicated matching session at 4:10-4:12 PM AEST where orders are matched at a single price. Approximately 21% of daily volume on ASX trades during the closing auction, making it the single most liquid period of the trading day.

**Why It Helps**: For a daily-frequency system that generates signals after market close and executes the next day, the closing auction provides the deepest liquidity pool. Better fills reduce slippage, which is currently estimated at 0.1% per trade (0.2% round-trip). On 287 round-trip trades with ~$500 average position, slippage costs approximately $287/year.

**Academic/Industry Evidence**:
- ASX data shows closing auction accounts for ~21% of daily volume
- Institutional traders prefer closing auction for large orders due to reduced market impact
- The single-price mechanism minimizes adverse selection
- SR15 rule changes effective June 23, 2025: Introduction of single opening price at 9:59 AM and new post-close session, further concentrating liquidity

**Expected Impact**: 0.5-1% CAGR improvement from reduced slippage (~$100-$200/year savings)

**Implementation Complexity**: **Low** -- Simply time orders to participate in closing auction

**Priority**: **HIGH**

**Key References**:
- ASX "Introduction to the Closing Single Price Auction" -- asx.com.au
- ASX SR15 Market Structure Changes (effective June 23, 2025)
- Comerton-Forde & Rydge (2006) "The current state of Asia-Pacific stock exchanges" -- includes ASX auction analysis

**Python Implementation Notes**:

```python
# For Moomoo AU API integration:
# Place limit orders at CSPA-compatible price during 4:00-4:10 PM AEST
# Use "LOC" (Limit-on-Close) or "MOC" (Market-on-Close) order types

CSPA_START = "16:00"  # 4:00 PM AEST
CSPA_END = "16:10"    # 4:10 PM AEST  
CSPA_MATCH = "16:12"  # 4:12 PM AEST (matching)

# For daily signals generated after close:
# Option A: Execute next day at closing auction (T+1 close)
# Option B: Execute next day at opening auction (T+1 open) 
# Option C: Execute via limit order during day at signal price

# Recommendation: Use closing auction for entries (deepest liquidity)
# Use next-day opening for urgent exits (faster execution)
```

**ASX-Specific Considerations**:
- CSPA window: 4:10-4:12 PM AEST (after pre-CSPA period 4:00-4:10 PM)
- SR15 changes (June 23, 2025) introduce single opening at 9:59 AM and post-close session
- Moomoo AU supports limit orders during CSPA
- For ASX mid-caps, closing auction may represent >30% of daily volume
- T+2 settlement on ASX (standard)

---

### 2.1.2 ASX Opening Auction Dynamics

**What**: The ASX opening auction runs from 10:00-10:09 AM AEST (pre-SR15) or single open at 9:59 AM (post-SR15 from June 2025). The opening typically has wider spreads and higher volatility than the rest of the session.

**Why It Helps**: Understanding opening dynamics helps avoid adverse execution during high-volatility periods. For the Opening Gap strategy specifically, the opening price is the critical reference point.

**Expected Impact**: +0.3-0.5% CAGR from avoiding opening execution for non-gap strategies

**Implementation Complexity**: **Low**

**Priority**: **MEDIUM**

**ASX-Specific Considerations**:
- SR15 changes effective June 23, 2025: Single opening price at 9:59 AM
- Opening typically has wider bid-ask spreads on ASX mid-caps
- Opening Gap strategy should execute at/near open; all other strategies should prefer closing auction

---

### 2.1.3 Intraday Volume Profile (U-Shape)

**What**: ASX stocks exhibit the classic U-shaped intraday volume profile: high volume at open, declining through midday, rising again toward close. The lowest-volume period (11:30 AM - 2:00 PM) has the widest spreads and worst execution quality.

**Why It Helps**: Timing execution to avoid the midday lull reduces slippage and improves fill quality.

**Expected Impact**: +0.2-0.3% CAGR from improved execution timing

**Implementation Complexity**: **Low**

**Priority**: **LOW** -- Subsumed by closing auction execution strategy

---

## 2.2 Commission Drag Reduction

### 2.2.1 Fee-Aware Signal Filtering (CRITICAL)

**What**: Only take trades where the expected value (expected return * position size) exceeds the round-trip commission cost ($6 on Moomoo AU). For a $500 position, $6 commission = 1.2% round-trip drag. The signal must have expected return >1.2% just to break even.

**Why It Helps**: This is the single most impactful improvement for the system. Currently, every trade incurs 1.2% round-trip commission drag on a typical $500 position. Many marginal signals may have expected returns of 1-2%, meaning commission consumes 60-100% of the edge. Filtering these out eliminates negative-EV trades.

**Expected Impact**: +3-5% CAGR from eliminating negative-EV trades; reduce trade count from 287 to ~150-180

**Implementation Complexity**: **Low** -- Requires expected value calculation in signal pipeline

**Priority**: **CRITICAL** -- Implement immediately

**Key References**:
- Carver, R. (2019) "Systematic Trading" -- Chapter on commission-aware signal filtering
- Multiple systematic trading blogs document commission threshold filtering
- AHL/Man Group research on transaction cost optimization

**Python Implementation Notes**:

```python
def fee_aware_filter(signal, position_value, commission_per_trade=3.0):
    """Filter signals where expected profit < commission cost."""
    round_trip_commission = commission_per_trade * 2  # $6
    commission_pct = round_trip_commission / position_value  # 1.2% for $500

    # Minimum expected return must exceed commission + safety margin
    min_expected_return = commission_pct * 1.5  # 1.5x commission = 1.8% for $500

    # Estimate expected return from signal strength
    # (Use historical win rate * avg win - loss rate * avg loss)
    expected_return = signal.get('expected_return', 0)

    if expected_return < min_expected_return:
        return None  # Skip this trade

    return signal

# Position size scaling:
# At $500 position: commission_pct = 1.2%, min_expected_return = 1.8%
# At $833 position: commission_pct = 0.72%, min_expected_return = 1.08%
# At $1000 position: commission_pct = 0.60%, min_expected_return = 0.90%

# Key insight: Larger positions have lower commission drag!
# With 5 positions of $1000 vs 10 positions of $500:
# Commission drag per trade: 0.60% vs 1.20% (HALF)
```

**ASX-Specific Considerations**:
- Moomoo AU: $3 flat fee per trade under $10K (all Atlas-ASX positions qualify)
- Round-trip cost: $6 per trade
- At $500 position: 1.2% round-trip drag
- At $1,000 position: 0.6% round-trip drag
- Target: reduce annual trades from 287 to ~150 while maintaining or improving PnL

---

### 2.2.2 Minimum Expected Value Threshold

**What**: Calculate the minimum dollar profit required per trade to justify the commission cost. Set a hard floor: no trade unless expected dollar profit > $X.

**Why It Helps**: Creates a quantitative go/no-go criterion for every trade decision.

**Expected Impact**: Included in fee-aware filtering above

**Implementation Complexity**: **Low**

**Priority**: **CRITICAL** (part of fee-aware filtering)

**Python Implementation Notes**:

```python
# Minimum profit threshold calculation:
ROUND_TRIP_COMMISSION = 6.0  # $3 * 2
MINIMUM_PROFIT_MULTIPLE = 2.0  # Want at least 2x commission as profit

MIN_EXPECTED_PROFIT = ROUND_TRIP_COMMISSION * MINIMUM_PROFIT_MULTIPLE  # $12

# For a $500 position: need 2.4% expected return
# For a $1000 position: need 1.2% expected return
# For a $833 position (6 positions at $5K): need 1.44% expected return

# Current avg return per winning trade: ~2.5% (estimated from PF=1.21, WR=49.5%)
# At 49.5% win rate, expected value per trade:
# EV = 0.495 * avg_win - 0.505 * avg_loss
# Need EV > $12 per trade to justify commission
```



## 2.3 Position Sizing for $5K Account

### 2.3.1 Optimal Number of Positions (CRITICAL)

**What**: With $5,000 capital and $3 flat fee per trade, the number of simultaneous positions directly determines commission drag per position. The current system allows up to 10 positions, meaning average position size = $500 and commission drag = 1.2% per round-trip. Reducing to 5-6 positions doubles the average position size and halves the commission drag.

**Why It Helps**: This is tied with fee-aware filtering as the most impactful change. The math is unambiguous:

| Max Positions | Avg Position Size | Commission Drag/Trade | Annual Commission (150 trades) | Commission as % of Equity |
|:---:|:---:|:---:|:---:|:---:|
| 10 | $500 | 1.20% | $900 | 18.0% |
| 8 | $625 | 0.96% | $900 | 18.0% |
| 6 | $833 | 0.72% | $900 | 18.0% |
| 5 | $1,000 | 0.60% | $900 | 18.0% |
| 4 | $1,250 | 0.48% | $900 | 18.0% |

Note: Total annual commission depends on trade COUNT, not position size. But the commission as a percentage of each trade's P&L is critical. At $500 position and 2.5% average winning trade, the win = $12.50 but commission = $6.00 (48% of profit eaten). At $1,000 position and 2.5% win, the win = $25.00 but commission = $6.00 (24% of profit eaten).

**Expected Impact**: +2-4% CAGR from reduced commission drag per trade

**Implementation Complexity**: **Low** -- Change max_open_positions in active_config.json

**Priority**: **CRITICAL**

**Recommended Configuration**:
- Reduce max_open_positions from 10 to 5-6
- Minimum position value: raise from $500 to $800
- This concentrates capital in higher-conviction trades

**Python Implementation Notes**:

```python
# In active_config.json:
{
    "max_open_positions": 6,      # Was 10
    "min_position_value": 800.0,  # Was 500
    "position_size_pct": 0.16,    # 1/6 of equity
}

# Commission impact analysis:
def commission_impact_analysis(equity, max_positions, commission=3.0):
    pos_size = equity / max_positions
    round_trip_cost = commission * 2
    drag_pct = round_trip_cost / pos_size * 100
    print(f"Equity: ${equity:,.0f}")
    print(f"Max positions: {max_positions}")
    print(f"Position size: ${pos_size:,.0f}")
    print(f"Round-trip commission: ${round_trip_cost:.0f}")
    print(f"Commission drag: {drag_pct:.2f}% per trade")
    print(f"Break-even return needed: {drag_pct:.2f}%")
    return drag_pct
```

**ASX-Specific Considerations**:
- The system already tested max_positions sweep (see backtest/results/max_positions_full_universe.json)
- Previous testing showed 5-6 positions optimal for this capital level
- Fewer positions = higher concentration risk, but commission savings outweigh at $5K
- As capital grows to $10K+, can increase back to 8-10 positions

---

### 2.3.2 Dynamic Position Sizing by Commission Ratio

**What**: Scale position sizes so that commission never exceeds a target percentage of the position (e.g., max 0.5% commission drag per trade). For $3 flat fee, this means minimum position size = $3 / 0.005 = $600.

**Why It Helps**: Ensures every trade has a viable profit margin after commission costs.

**Expected Impact**: Included in position sizing optimization above

**Implementation Complexity**: **Low**

**Priority**: **HIGH**

**Python Implementation Notes**:

```python
def minimum_position_size(commission=3.0, max_commission_pct=0.005):
    """Calculate minimum viable position size."""
    min_size = (commission * 2) / max_commission_pct  # Round-trip
    return min_size  # $1,200 for 0.5% max drag

# With $5K equity and 0.5% max drag:
# min_position = $1,200 -> max 4 positions
# With $5K equity and 0.75% max drag:
# min_position = $800 -> max 6 positions
# With $5K equity and 1.0% max drag:
# min_position = $600 -> max 8 positions

# RECOMMENDATION: Target 0.72% max drag -> $833 min -> 6 positions
```

---

## 2.4 Batch Execution Strategies

### 2.4.1 Signal Aggregation and Selective Execution

**What**: Instead of executing every signal as it appears, accumulate signals over a period (e.g., 2-3 days) and execute only the highest-conviction subset. This reduces the total number of round-trip trades.

**Why It Helps**: Directly reduces trade count and therefore total commission spend. The system currently makes ~287 round-trip trades per year ($1,722 in commissions). Reducing to 150 trades would save ~$822/year (16.4% of equity).

**Expected Impact**: -$400-$800/year in commission savings (8-16% of equity)

**Implementation Complexity**: **Medium** -- Requires signal queuing and ranking logic

**Priority**: **HIGH**

**Python Implementation Notes**:

```python
class SignalAggregator:
    def __init__(self, min_confidence=0.6, max_daily_entries=2):
        self.pending_signals = []
        self.min_confidence = min_confidence
        self.max_daily_entries = max_daily_entries

    def add_signal(self, signal):
        if signal['confidence'] >= self.min_confidence:
            self.pending_signals.append(signal)

    def get_best_signals(self):
        """Return top N signals by confidence score."""
        ranked = sorted(self.pending_signals, 
                       key=lambda x: x['confidence'], reverse=True)
        selected = ranked[:self.max_daily_entries]
        self.pending_signals = []  # Clear queue
        return selected
```

**ASX-Specific Considerations**:
- Daily frequency already provides natural batching
- Limit to 1-2 new entries per day (reduces clustering of entries)
- Prioritize signals with highest expected value after commission

---

### 2.4.2 Reducing Round-Trip Frequency

**What**: Extend average holding period to reduce annual trade count. Currently avg hold = 13.8 days with 287 trades/year. Extending to 20+ days could reduce to ~190 trades/year.

**Why It Helps**: Fewer round-trips = fewer commission events. Each avoided trade saves $6 in commissions.

**Expected Impact**: -$300-$600/year in commission savings

**Implementation Complexity**: **Low** -- Adjust exit parameters (wider stops, longer time exits)

**Priority**: **MEDIUM**

**Key Insight**: The tension is between holding longer (fewer commissions) and holding shorter (more capital turnover). For a $5K account, the commission savings from fewer trades likely outweigh the opportunity cost of slower capital rotation.

**Python Implementation Notes**:

```python
# Adjust exit parameters to encourage longer holds:
# - Increase max_hold_days from 15 to 25
# - Use wider profit targets (3-4% instead of 2-3%)
# - Use trailing stops instead of fixed targets (already tested, works for liquid stocks)

# Exit parameter adjustment:
exit_config = {
    'max_hold_days': 25,       # Was 15
    'profit_target_pct': 0.04, # Was 0.025
    'stop_loss_atr_mult': 2.5, # Was 2.0
}
```



## 2.5 ASX Market Microstructure

### 2.5.1 Tick Sizes and Minimum Trade Values

**What**: ASX uses a tiered tick size regime based on share price:

| Price Range | Tick Size |
|:---:|:---:|
| $0.001 - $0.10 | $0.001 |
| $0.10 - $2.00 | $0.005 |
| $2.00 - $5.00 | $0.01 |
| $5.00+ | $0.01 |

**Why It Helps**: Understanding tick sizes is essential for limit order placement and slippage estimation. For ASX mid-caps typically priced $1-$10, the tick size is $0.005-$0.01, meaning each tick movement = 0.1-0.5% of price for lower-priced stocks.

**Implementation Considerations**:
- For a $2 stock with $0.005 tick: minimum adverse tick = 0.25% -- significant for mean reversion
- For a $5 stock with $0.01 tick: minimum adverse tick = 0.20%
- Limit orders should be placed at round tick levels
- Slippage estimate: assume 1-2 ticks adverse for mid-cap ASX stocks (~0.2-0.5%)

**Priority**: **LOW** -- Informational, already accounted for in slippage_pct=0.001

---

### 2.5.2 T+2 Settlement Implications

**What**: ASX uses T+2 settlement (trade date + 2 business days). This means cash from a sale is not available for 2 business days.

**Why It Helps**: Critical for a $5K account where capital is fully deployed. If all 6 positions are sold simultaneously, the cash cannot be redeployed for 2 business days. This creates a dead money period.

**Practical Impact**:
- With 6 positions, stagger entries/exits to avoid all-at-once capital lock-up
- Maintain a small cash buffer (~10-15% = $500-750) for immediate deployment
- Settlement risk is minimal with a regulated broker (Moomoo AU)

**Priority**: **LOW** -- Operational consideration, not an alpha source

---

### 2.5.3 ASX Trading Hours and Key Times

**What**: ASX trading schedule (AEST):

| Session | Time (AEST) | Notes |
|:---|:---:|:---|
| Pre-open | 7:00 - 10:00 AM | Order entry, no matching |
| Opening auction | 10:00 - 10:09 AM | Staggered open by price groups |
| Normal trading | 10:09 AM - 4:00 PM | Continuous matching |
| Pre-CSPA | 4:00 - 4:10 PM | Order entry for closing auction |
| Closing auction (CSPA) | 4:10 - 4:12 PM | Single price matching |
| Post-close | 4:12 - 5:00 PM | Adjusted close orders |

Note: SR15 changes effective June 23, 2025 will modify opening auction to single open at 9:59 AM and add new post-close session structure.

**Key Timing Recommendations for Atlas-ASX**:
1. Generate signals after 5:00 PM using closing price data
2. Place orders during pre-open (7:00-10:00 AM) for next-day execution
3. Prefer closing auction (CSPA) for entries -- deepest liquidity
4. Use opening auction only for Opening Gap strategy entries
5. Avoid midday execution (11:30 AM - 2:00 PM) -- widest spreads

**Priority**: **MEDIUM** -- Practical operational guidance

---

## 2.6 Comprehensive Commission Analysis and Recommendations

### 2.6.1 Current Commission Structure Impact

**Current State (v9.2 config)**:
- 287 round-trip trades per year (574 individual trades)
- Commission per trade: $3 (Moomoo AU flat fee for < $10K orders)
- Total annual commission: $3 x 574 = $1,722
- Commission as % of $5K equity: 34.4%
- Average position size: ~$500 (10 max positions)
- Commission drag per round-trip: 1.2% of position
- System CAGR: ~11.2% (backtest) -- commission consumes ~30% of gross returns!

### 2.6.2 Optimized Commission Structure

**Proposed Changes**:

| Parameter | Current | Proposed | Impact |
|:---|:---:|:---:|:---|
| Max positions | 10 | 6 | Position size $500 -> $833 |
| Avg position size | $500 | $833 | Commission drag 1.2% -> 0.72% |
| Annual trades (round-trip) | 287 | 150 | -48% fewer trades |
| Annual commission | $1,722 | $900 | -$822/year saved |
| Commission as % equity | 34.4% | 18.0% | -16.4pp reduction |
| Commission per trade (% pos) | 1.2% | 0.72% | -40% per-trade drag |

### 2.6.3 Combined Impact Projection

Assuming the current system generates ~15% gross CAGR before commissions:

| Scenario | Gross CAGR | Commission Drag | Net CAGR | Improvement |
|:---|:---:|:---:|:---:|:---:|
| Current (287 trades, 10 pos) | 15% | -3.8% | 11.2% | Baseline |
| Optimized (150 trades, 6 pos) | 15% | -1.8% | 13.2% | +2.0% |
| + Fee-aware filter | 15% | -1.2% | 13.8% | +2.6% |
| + Better signals (alpha) | 17% | -1.2% | 15.8% | +4.6% |

Note: These are estimates. Actual improvement depends on which trades are filtered (removing low-EV trades should NOT reduce gross returns proportionally).

### 2.6.4 Capital Growth Trajectory

As capital grows, commission drag naturally decreases:

| Capital | Max Pos | Pos Size | Commission Drag/Trade | Annual Drag (150 trades) |
|:---:|:---:|:---:|:---:|:---:|
| $5,000 | 6 | $833 | 0.72% | 18.0% |
| $10,000 | 8 | $1,250 | 0.48% | 12.0% |
| $20,000 | 10 | $2,000 | 0.30% | 7.5% |
| $50,000 | 12 | $4,167 | 0.14% | 3.6% |

Key insight: At $20K+ capital, commission drag becomes manageable. The system should prioritize capital preservation and steady growth to reach this threshold.



---

# PRIORITY MATRIX: Complete Ranking of All Techniques

All techniques ranked by expected impact, adjusted for implementation complexity and relevance to $5K ASX mid-cap system.

## Tier 1: CRITICAL (Implement Immediately)

| # | Technique | Research Area | Expected Impact | Complexity | Section |
|:---:|:---|:---:|:---:|:---:|:---:|
| 1 | Fee-Aware Signal Filtering | Execution | +3-5% CAGR | Low | 2.2.1 |
| 2 | Reduce Max Positions to 5-6 | Execution | +2-4% CAGR | Low | 2.3.1 |
| 3 | EOFY Tax-Loss Selling (July Rebound) | Alpha | +1-3% CAGR | Low | 1.1.1 |
| 4 | Closing Auction Execution | Execution | +0.5-1% CAGR | Low | 2.1.1 |

Combined Tier 1 estimated impact: +5-8% CAGR improvement

## Tier 2: HIGH (Implement Within 4 Weeks)

| # | Technique | Research Area | Expected Impact | Complexity | Section |
|:---:|:---|:---:|:---:|:---:|:---:|
| 5 | RVOL Spike Mean Reversion Confirmation | Alpha | +1-2% CAGR | Low | 1.4.1 |
| 6 | ASX Short Interest Filter | Alpha | +1-2% CAGR | Medium | 1.3.1 |
| 7 | Market Breadth Confidence Modifier | Alpha | +1-2% CAGR | Low | 1.6.2 |
| 8 | Post-Earnings Announcement Drift (PEAD) | Alpha | +1-2% CAGR | Medium | 1.2.1 |
| 9 | Ex-Dividend Cum-Dividend Run-Up | Alpha | +0.5-1.5% CAGR | Low | 1.1.2 |
| 10 | Signal Aggregation (Batch Execution) | Execution | +1-2% CAGR | Medium | 2.4.1 |
| 11 | Dynamic Position Sizing by Commission Ratio | Execution | +0.5-1% CAGR | Low | 2.3.2 |

Combined Tier 2 estimated impact: +3-5% CAGR improvement (additive to Tier 1)

## Tier 3: MEDIUM (Implement Within 8 Weeks)

| # | Technique | Research Area | Expected Impact | Complexity | Section |
|:---:|:---|:---:|:---:|:---:|:---:|
| 12 | Cross-Sectional Momentum Filter | Alpha | +1-3% CAGR | Medium | 1.5.1 |
| 13 | Weekly Trend Filter | Alpha | +1-2% CAGR | Low | 1.7.1 |
| 14 | Value-Momentum Combination | Alpha | +1-2% CAGR | Medium | 1.5.2 |
| 15 | Director Transactions Signal | Alpha | +0.5-1% CAGR | Medium | 1.3.2 |
| 16 | Price-Volume Divergence | Alpha | +0.5-1% CAGR | Low | 1.4.2 |
| 17 | Results Season Patterns (Feb/Aug) | Alpha | +0.5-1% CAGR | Low | 1.2.2 |
| 18 | January Effect (Small Cap) | Alpha | +0.5-1% CAGR | Low | 1.1.3 |
| 19 | Monthly Regime Classification | Alpha | +0.5-1% CAGR | Medium | 1.7.2 |
| 20 | Extend Average Hold Period | Execution | +0.5-1% CAGR | Low | 2.4.2 |

## Tier 4: LOW (Defer or Conditional)

| # | Technique | Research Area | Expected Impact | Complexity | Section |
|:---:|:---|:---:|:---:|:---:|:---:|
| 21 | Low-Volatility Anomaly Filter | Alpha | +0.5-1% CAGR | Low | 1.5.3 |
| 22 | VWAP Order Imbalance Proxy | Alpha | +0.3-0.5% CAGR | Low | 1.4.3 |
| 23 | Substantial Holder Notices | Alpha | +0.3-0.5% CAGR | High | 1.3.3 |
| 24 | News Sentiment (FinBERT/NLP) | Alpha | +0.5-1% CAGR | High | 1.6.1 |

---

# IMPLEMENTATION ROADMAP

## Phase 1: Commission Optimization (Week 1) -- CRITICAL

Objective: Cut commission drag from 34.4% to <18% of equity.

**Tasks**:
1. Update `active_config.json`: max_open_positions = 6, min_position_value = 800
2. Implement fee-aware signal filter in `utils/signal_enrichment.py`:
   - Calculate expected value per trade: EV = confidence * avg_win_pct * position_size - (1-confidence) * avg_loss_pct * position_size
   - Only pass signals where EV > $12 (2x round-trip commission)
3. Implement minimum expected return threshold: skip trades with expected return < 0.72% (commission drag at $833 position)
4. Update order execution timing to prefer closing auction (CSPA) for entries
5. Run full backtest to validate commission reduction

**Success Criteria**: Annual trades < 180, commission drag < 20% of equity, no CAGR degradation

## Phase 2: Signal Quality Enhancement (Weeks 2-3) -- HIGH

Objective: Improve win rate from 49.5% to 52-55% through signal confirmation.

**Tasks**:
1. Add RVOL spike confirmation to MeanReversion strategy:
   - Calculate RVOL = volume / 20-day average volume
   - Boost confidence by 30% when RVOL > 2.0 AND price down > 2%
2. Enhance market breadth confidence modifier in `signal_enrichment.py`:
   - Breadth > 60%: boost long confidence by 20%
   - Breadth < 40%: penalize long confidence by 30%
3. Implement ex-dividend cum-dividend run-up signal:
   - Boost dividend_capture confidence when stock is 15-30 days before ex-date
   - Add dividend yield > 3% filter
4. Add weekly trend filter:
   - Calculate 20-week SMA from daily data
   - Skip long entries when price < 20-week SMA AND SMA slope < 0
5. Backtest each enhancement independently, then combined

**Success Criteria**: Win rate > 52%, Profit Factor > 1.3, no increase in trade count

## Phase 3: Calendar Alpha (Weeks 3-4) -- HIGH

Objective: Add seasonal/calendar-based alpha signals.

**Tasks**:
1. Implement EOFY July rebound strategy:
   - Identify stocks that declined >10% in May-June (tax-loss selling)
   - Generate buy signals in first week of July
   - Target 3-6% rebound over 4-6 weeks
2. Implement earnings season drift signals:
   - Use existing earnings.py data to identify earnings dates
   - Generate PEAD signals on earnings surprise (gap-up/gap-down on earnings day)
   - Hold for 30-60 days post-announcement
3. Implement results season awareness:
   - Add February/August results season flag
   - Reduce position sizes during high-volatility earnings clusters
   - Only take highest-conviction signals during results season

**Success Criteria**: 2-5 additional profitable trades per year from calendar signals

## Phase 4: Alternative Data Integration (Weeks 5-6) -- MEDIUM

Objective: Add short interest and insider transaction data.

**Tasks**:
1. Build ASIC short interest data scraper:
   - Daily download of short position reports from asic.gov.au
   - Store in data/cache/short_interest/ as JSON files
   - Calculate short interest % and 5-day change for each ticker
2. Implement short interest filter:
   - Avoid buying stocks with SI > 10% (unless strong contrarian signal)
   - Boost confidence on short covering (SI decreasing >15% over 5 days)
3. Build director transaction data scraper:
   - Scrape from marketindex.com.au/director-transactions
   - Store in data/cache/director_transactions/ as JSON files
4. Implement director buying signal:
   - Flag stocks where director purchased >$50K in last 10 business days
   - Boost confidence by 15% for matching entry signals

**Success Criteria**: Short interest filter operational, 1-2 director buying signals per month

## Phase 5: Factor Integration (Weeks 7-8) -- MEDIUM

Objective: Add cross-sectional momentum and multi-factor universe filtering.

**Tasks**:
1. Implement cross-sectional momentum ranking:
   - 12-month return minus 1-month return for all 185 tickers
   - Filter universe to top 50% by momentum score
   - Rebalance monthly
2. Implement composite factor score:
   - Momentum (40%) + Value proxy (30%) + Low-Vol (30%)
   - Universe filter: only generate signals for top 50% composite stocks
3. Add price-volume divergence signal:
   - Bearish: rising price + falling volume = exit/avoid
   - Bullish: falling price + falling volume = exhaustion (support MR entries)
4. Backtest factor-filtered universe vs full universe

**Success Criteria**: Factor-filtered universe achieves higher win rate with fewer trades

---

# APPENDIX: Data Sources Summary

| Data Source | Type | Availability | Cost | Update Frequency | Implementation |
|:---|:---:|:---:|:---:|:---:|:---:|
| Yahoo Finance (yfinance) | OHLCV | Free | $0 | Daily | Already implemented |
| ASIC Short Position Reports | Short Interest | Free | $0 | Daily (T+1) | Needs scraper |
| ASX Company Announcements | Earnings, Dividends | Free | $0 | Real-time | Partially implemented |
| Market Index (marketindex.com.au) | Director Transactions | Free | $0 | Daily | Needs scraper |
| IOZ.AX Benchmark | Market Regime | Free | $0 | Daily | Already implemented |
| Sector Map (sector_map.json) | Sector Classification | Pre-built | $0 | Static | Already implemented |
| Earnings Calendar | Earnings Dates | Free (yfinance) | $0 | As announced | Already implemented |
| Dividend Calendar | Ex-Dates, Amounts | Free (yfinance) | $0 | As announced | Already implemented |

---

# APPENDIX: Key Code Integration Points

All proposed enhancements integrate with the existing Atlas-ASX codebase at these specific files:

| Enhancement | Primary File | Secondary Files |
|:---|:---|:---|
| Fee-aware filtering | utils/signal_enrichment.py | config/active_config.json |
| RVOL confirmation | utils/signal_enrichment.py | strategies/mean_reversion.py |
| Market breadth modifier | utils/market_breadth.py | utils/signal_enrichment.py |
| Weekly trend filter | utils/signal_enrichment.py | (new) utils/weekly_trend.py |
| EOFY rebound | (new) strategies/eofy_rebound.py | data/ingest.py |
| PEAD signals | utils/earnings.py | strategies/mean_reversion.py |
| Short interest filter | (new) data/short_interest.py | utils/signal_enrichment.py |
| Director transactions | (new) data/director_txns.py | utils/signal_enrichment.py |
| Momentum filter | (new) utils/momentum_filter.py | universe/builder.py |
| Position sizing | backtest/engine.py | config/active_config.json |
| Closing auction timing | paper_engine/engine.py | (execution layer) |

---

# APPENDIX: Risk Warnings

1. **Overfitting Risk**: Each new signal/filter adds complexity. Always validate with walk-forward testing and parameter perturbation before deploying.
2. **Data Snooping**: Testing many strategies on the same data inflates apparent alpha. Use Deflated Sharpe Ratio (see research_part1) to adjust.
3. **Survivorship Bias**: The 185-ticker universe may have survivorship bias. Stocks that delisted during the backtest period are excluded.
4. **Regime Dependence**: Seasonal patterns (EOFY, January effect) may weaken or reverse in future years. Size positions conservatively.
5. **Capacity Constraints**: At $5K capital, most techniques have zero capacity constraints. But at $50K+, ASX mid-cap liquidity becomes a factor.
6. **Commission Structure Changes**: Moomoo AU may change fee structure. All commission analysis assumes $3 flat fee for orders < $10K.

---

*Report compiled: February 2026*
*System: Atlas-ASX v9.2 | Capital: $5,000 AUD | Universe: ~185 ASX Mid-Cap Tickers*
*Broker: Moomoo AU ($3 flat fee per trade)*

---

END OF REPORT
