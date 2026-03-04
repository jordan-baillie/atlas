# FRED / Macro Features Research Report

**Generated:** 2026-03-04 23:22
**Data range:** 2000-01-03 → 2026-03-03
**Observations:** 6,580

---

## 1. Feature–Return Correlations

Top features by absolute correlation with forward SP500 returns.
Correlations > |0.03| are potentially tradeable at daily frequency.

### 1d Forward Return

| Feature | Correlation | |
|---------|------------|--|
| VIX_roc_1d | +0.0478 | ████ |
| VIX_roc_5d | +0.0428 | ████ |
| 10Y_yield_roc_5d | −0.0391 | ███ |
| VIX | +0.0387 | ███ |
| VIX_sma20_ratio | +0.0362 | ███ |
| VIX_above_30 | +0.0299 | ██ |
| 10Y_yield | −0.0287 | ██ |
| gold_copper_ratio | +0.0285 | ██ |
| 5Y_yield | −0.0274 | ██ |
| gold_copper_roc_5d | +0.0272 | ██ |
| 30Y_yield | −0.0257 | ██ |
| VIX_above_25 | +0.0217 | ██ |
| yield_curve_30y_10y | +0.0210 | ██ |
| gold | +0.0189 | █ |
| yield_curve_roc_5d | −0.0189 | █ |

### 3d Forward Return

| Feature | Correlation | |
|---------|------------|--|
| gold_copper_ratio | +0.0513 | █████ |
| 10Y_yield | −0.0495 | ████ |
| 5Y_yield | −0.0471 | ████ |
| VIX_roc_5d | +0.0464 | ████ |
| 10Y_yield_roc_5d | −0.0453 | ████ |
| 30Y_yield | −0.0438 | ████ |
| VIX | +0.0417 | ████ |
| VIX_above_25 | +0.0390 | ███ |
| VIX_above_30 | +0.0379 | ███ |
| yield_curve_30y_10y | +0.0374 | ███ |
| gold | +0.0364 | ███ |
| yield_curve_roc_5d | −0.0364 | ███ |
| gold_copper_roc_5d | +0.0305 | ███ |
| yield_curve_10y_3m | −0.0274 | ██ |
| VIX_sma20_ratio | +0.0225 | ██ |

### 5d Forward Return

| Feature | Correlation | |
|---------|------------|--|
| gold_copper_ratio | +0.0676 | ██████ |
| 10Y_yield | −0.0644 | ██████ |
| 5Y_yield | −0.0618 | ██████ |
| yield_curve_roc_5d | −0.0576 | █████ |
| 30Y_yield | −0.0565 | █████ |
| VIX_roc_5d | +0.0559 | █████ |
| yield_curve_30y_10y | +0.0502 | █████ |
| VIX | +0.0487 | ████ |
| gold | +0.0482 | ████ |
| 10Y_yield_roc_5d | −0.0476 | ████ |
| VIX_above_25 | +0.0473 | ████ |
| gold_copper_roc_5d | +0.0459 | ████ |
| VIX_above_30 | +0.0432 | ████ |
| oil_roc_5d | −0.0371 | ███ |
| yield_curve_10y_3m | −0.0358 | ███ |

### 10d Forward Return

| Feature | Correlation | |
|---------|------------|--|
| gold_copper_ratio | +0.0946 | █████████ |
| 10Y_yield | −0.0931 | █████████ |
| 5Y_yield | −0.0901 | █████████ |
| 30Y_yield | −0.0812 | ████████ |
| yield_curve_30y_10y | +0.0745 | ███████ |
| gold | +0.0723 | ███████ |
| yield_curve_roc_5d | −0.0688 | ██████ |
| VIX_above_25 | +0.0550 | █████ |
| VIX | +0.0541 | █████ |
| VIX_above_30 | +0.0531 | █████ |
| yield_curve_10y_3m | −0.0489 | ████ |
| copper | +0.0382 | ███ |
| crude_oil | −0.0309 | ███ |
| VIX_level_quintile | +0.0301 | ███ |
| 13W_yield | −0.0298 | ██ |

### 20d Forward Return

| Feature | Correlation | |
|---------|------------|--|
| gold_copper_ratio | +0.1321 | █████████████ |
| 10Y_yield | −0.1275 | ████████████ |
| 5Y_yield | −0.1227 | ████████████ |
| 30Y_yield | −0.1121 | ███████████ |
| gold | +0.1062 | ██████████ |
| VIX_above_30 | +0.1049 | ██████████ |
| yield_curve_30y_10y | +0.0991 | █████████ |
| VIX_above_25 | +0.0973 | █████████ |
| VIX | +0.0837 | ████████ |
| yield_curve_10y_3m | −0.0634 | ██████ |
| copper | +0.0565 | █████ |
| yield_curve_roc_5d | −0.0495 | ████ |
| VIX_level_quintile | +0.0455 | ████ |
| 13W_yield | −0.0432 | ████ |
| 10Y_yield_roc_5d | −0.0432 | ████ |

## 2. Regime Analysis

SP500 forward returns conditioned on macro regimes.

### VIX quintile → 5d fwd return

| Regime | Mean Ret % | Median % | Win Rate | Sharpe | N |
|--------|-----------|----------|----------|--------|---|
| Q1 (VIX low) | +0.083 | +0.183 | 57.2% | 0.5 | 1318 |
| Q2 (VIX low) | +0.108 | +0.339 | 59.2% | 0.47 | 1316 |
| Q3 (VIX mid) | +0.147 | +0.404 | 58.7% | 0.57 | 1315 |
| Q4 (VIX high) | +0.071 | +0.359 | 55.6% | 0.2 | 1310 |
| Q5 (VIX high) | +0.344 | +0.563 | 55.9% | 0.62 | 1316 |

### Yield curve regime → 10d fwd return

| Regime | Mean Ret % | Median % | Win Rate | Sharpe | N |
|--------|-----------|----------|----------|--------|---|
| Normal (positive slope) | +0.255 | +0.556 | 58.7% | 0.38 | 5645 |
| Inverted (negative slope) | +0.539 | +0.995 | 67.2% | 0.99 | 925 |

### VIX spike regime → 5d fwd return

| Regime | Mean Ret % | Median % | Win Rate | Sharpe | N |
|--------|-----------|----------|----------|--------|---|
| VIX calm (<0.9× SMA) | +0.111 | +0.280 | 58.7% | 0.4 | 1262 |
| VIX normal (0.9-1.1× SMA) | +0.134 | +0.286 | 56.6% | 0.42 | 4160 |
| VIX elevated (1.1-1.3× SMA) | +0.265 | +0.534 | 58.0% | 0.63 | 899 |
| VIX spike (>1.3× SMA) | +0.248 | +0.764 | 60.4% | 0.38 | 235 |

### Gold/copper regime → 10d fwd return

| Regime | Mean Ret % | Median % | Win Rate | Sharpe | N |
|--------|-----------|----------|----------|--------|---|
| Risk-on (low gold/copper) | -0.065 | +0.420 | 55.8% | — | 2116 |
| Neutral | +0.178 | +0.490 | 58.6% | — | 2181 |
| Risk-off (high gold/copper) | +0.776 | +1.019 | 66.3% | — | 2106 |

## 3. Directional Signal Tests

Testing if extreme macro moves predict SP500 direction.

### VIX overnight spike → next-day SP500

| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |
|-----------|---|------------|------------|--------|---------|---------|
| +0.05 | 1241 | +0.083 | +0.031 | +0.052 | 55.0% | 53.7% |
| +0.10 | 486 | +0.176 | +0.031 | +0.145 | 57.6% | 53.7% |
| +0.15 | 220 | +0.295 | +0.031 | +0.264 | 57.3% | 53.7% |
| +0.20 | 114 | +0.441 | +0.031 | +0.410 | 62.3% | 53.7% |

### VIX 5-day spike → 5d fwd SP500

| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |
|-----------|---|------------|------------|--------|---------|---------|
| +0.10 | 1276 | +0.320 | +0.151 | +0.170 | 59.6% | 57.3% |
| +0.20 | 535 | +0.409 | +0.151 | +0.258 | 62.2% | 57.3% |
| +0.30 | 250 | +0.897 | +0.151 | +0.746 | 67.2% | 57.3% |
| +0.50 | 72 | +1.156 | +0.151 | +1.005 | 69.4% | 57.3% |

### Yield curve flattening → 10d fwd SP500

| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |
|-----------|---|------------|------------|--------|---------|---------|
| -0.20 | 340 | +1.326 | +0.295 | +1.031 | 68.8% | 59.9% |
| -0.15 | 676 | +0.895 | +0.295 | +0.600 | 65.8% | 59.9% |
| -0.10 | 1286 | +0.695 | +0.295 | +0.400 | 63.2% | 59.9% |
| -0.05 | 2227 | +0.565 | +0.295 | +0.270 | 62.6% | 59.9% |

### Dollar strength → 5d fwd SP500

| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |
|-----------|---|------------|------------|--------|---------|---------|
| +0.01 | 1029 | +0.080 | +0.151 | -0.070 | 56.9% | 57.3% |
| +0.02 | 235 | +0.187 | +0.151 | +0.036 | 54.5% | 57.3% |
| +0.03 | 52 | +0.168 | +0.151 | +0.017 | 50.0% | 57.3% |

### Oil spike → 5d fwd SP500

| Threshold | N | Trig Mean % | Base Mean % | Edge % | Trig WR | Base WR |
|-----------|---|------------|------------|--------|---------|---------|
| +0.05 | 920 | +0.007 | +0.151 | -0.143 | 53.5% | 57.3% |
| +0.10 | 154 | +0.049 | +0.151 | -0.101 | 56.5% | 57.3% |
| +0.15 | 48 | +0.898 | +0.151 | +0.748 | 62.5% | 57.3% |

## 4. Top Candidate Features for Atlas Integration

Based on correlation, regime, and signal analysis:

### Ranked by peak correlation

| Rank | Feature | Peak |r| | Assessment |
|------|---------|---------|------------|
| 1 | gold_copper_ratio | 0.1321 | 🟢 Strong |
| 2 | 10Y_yield | 0.1275 | 🟢 Strong |
| 3 | 5Y_yield | 0.1227 | 🟢 Strong |
| 4 | 30Y_yield | 0.1121 | 🟢 Strong |
| 5 | gold | 0.1062 | 🟢 Strong |
| 6 | VIX_above_30 | 0.1049 | 🟢 Strong |
| 7 | yield_curve_30y_10y | 0.0991 | 🟢 Strong |
| 8 | VIX_above_25 | 0.0973 | 🟢 Strong |
| 9 | VIX | 0.0837 | 🟢 Strong |
| 10 | yield_curve_roc_5d | 0.0688 | 🟢 Strong |

## 5. Implementation Notes

### Data Sources (no FRED API key needed)
All features above are available via yfinance:
- Treasury yields: `^TNX`, `^FVX`, `^IRX`, `^TYX`
- VIX: `^VIX`
- Dollar index: `DX-Y.NYB`
- Commodities: `GC=F` (gold), `CL=F` (oil), `HG=F` (copper)

### FRED API Extension (requires free API key)
Additional series worth testing with FRED API:
- `FEDFUNDS` — Fed Funds effective rate (daily)
- `T10Y2Y` — 10Y-2Y spread (pre-computed)
- `BAMLH0A0HYM2` — High-yield OAS (credit stress)
- `UMCSENT` — Consumer sentiment (monthly)
- `ICSA` — Initial jobless claims (weekly)
- `DTWEXBGS` — Trade-weighted dollar (daily)

### Integration Path
1. Add daily macro data download to `data/ingest.py`
2. Store in `data/macro/` as parquet files
3. Expose as strategy features via config
4. Backtest with macro features as entry/exit filters
5. Most promising: regime-based position sizing
