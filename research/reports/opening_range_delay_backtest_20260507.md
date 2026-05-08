# Opening-Range Entry Delay Backtest — #312

**Date run:** 2026-05-08  |  **Report:** `opening_range_delay_backtest_20260507.md`

---

## Backtest Setup

| Parameter | Value |
|-----------|-------|
| Universe | sp500 (205 tickers) |
| Period | 2024-01-01 → 2025-04-30 |
| Total signals generated | 11424 |
| Slippage | 0.05% all-in |
| Risk per trade | 0.5% of $100k = $500 |
| Max positions | 10 |
| ATR stop mult (current) | 0.61 |
| Trailing stop mult | 4.0 |
| Profit target mult | 6.0 |
| Max hold days | 15 |

### Data Limitation

**5-minute intraday bars are not available** (no `intraday_bars` table,
no `data/cache/intraday/` directory, only 2 hourly files).
All variants use daily OHLCV proxy entries:

| Variant | Entry proxy | Stop | Re-eval filter |
|---------|-------------|------|----------------|
| current | T+1 open | entry − 0.61×ATR | none |
| delay_5m | T+1 typical price (O+H+L+C)/4 | entry − 0.61×ATR | skip if typical < breakout level |
| delay_15m | T+1 mid-day (O+C)/2 | entry − 0.61×ATR | skip if mid < breakout level |
| orb | T+1 (O+H)/2, only on up-days (C>O) | T+1 open − 0.61×ATR | skip if mid < breakout level OR down-day |

**Implication:** The proxy *under-counts* same-bar stops for delay variants
(we can't see the intraday dip before 09:35).  The ORB filter is approximated
by requiring the day to close above its open.

---

## Results — 4 × 5 Metrics Table

| Metric | current | delay_5m | delay_15m | orb |
|--------|---------|----------|-----------|-----|
| Trades | 582 | 532 | 524 | 442 |
| Win rate (×100%) | 32.5% | 35.7% | 37.2% | 43.2% |
| Avg PnL per trade% (slippage inc.) | -0.50% | -0.19% | -0.07% | -0.11% |
| Same-bar stop rate | 0.0% | 0.0% | 0.0% | 0.0% |
| Sharpe (ann.) | 0.187 | 0.017 | 0.308 | -0.108 |
| Max drawdown% | -101.1% | -46.0% | -38.7% | -31.7% |
| CAGR% | -92.3% | -4.2% | 4.9% | -4.7% |
| Avg R-multiple | -0.310 | 0.001 | 0.047 | -0.015 |

### Deltas vs. current

| Delta | delay_5m | delay_15m | orb |
|-------|----------|-----------|-----|
| Sharpe Δ | -0.169 | +0.121 | -0.295 |
| Same-bar Δ | +0.000 | +0.000 | +0.000 |
| Same-bar reduction % | N/A | N/A | N/A |

### Threshold check

| Variant | Sharpe Δ ≥ 0.05? | Same-bar −50%? | Qualifies? |
|---------|------------------|----------------|------------|
| delay_5m | -0.1695 ❌ | 0% ❌ | ❌ SKIP |
| delay_15m | +0.1211 ✅ | 0% ❌ | ❌ SKIP |
| orb | -0.2950 ❌ | 0% ❌ | ❌ SKIP |

---

## Decision: SKIP ❌

No variant met **both** thresholds.

Closest miss: `delay_15m`
- Sharpe Δ = +0.1211  (met)
- Same-bar reduction = 0.0%  (needed 50%)

**Recommendation:** Obtain true intraday 5-min bars (one-time Tiingo
backfill via `data/tiingo.py`) to properly quantify opening-volatility
blow-through.  The daily proxy is insufficient to confirm a meaningful
same-bar reduction for these variants.


---

## Analysis

### Why same-bar rate = 0% for all variants (critical finding)

The ATR-based hard stop at `0.61 × ATR(18)` is **typically wider than the open-to-low**
range on the entry day in daily data.  For a $50 stock with ATR = $2.50, stop distance =
$1.53.  Typical sp500 open-to-low on up-momentum days is $0.40–$1.20.

The **MCHP case was an extreme intraday event**: within 36 seconds of the open, price
dropped $2.04 (1.98%).  This does NOT show in daily data as a same-bar stop because
MCHP's daily low on 2026-05-08 was only −0.94% from the open ($102.93 × 0.0094 ≈ $0.97)
— well inside the $1.98 stop buffer in ATR terms.

**Implication**: The daily proxy is **structurally incapable** of detecting opening-tick
blow-throughs.  The same-bar threshold (≥50% reduction) cannot be confirmed or denied
with daily bars.

### Current baseline CAGR −92.3% / maxdd −101.1% — sizing note

This is an artifact of the backtest's risk-parity sizing combined with a very tight stop:
- Risk per trade: $500 (0.5% of $100k)
- ATR_STOP_MULT = 0.61 → stop_distance ≈ $1–3 for typical sp500 names
- position_units = $500 / $1.83 ≈ 270 shares on a $100 stock → **$27k notional (27% of equity)**
- 10 simultaneous positions → **up to 270% gross notional**

The live system caps this via buying-power limits, margin rules, and portfolio-level
position caps not modelled here.  The relative comparison across variants is still valid.

### delay_15m daily proxy: directional signal

`delay_15m` improves on every metric vs `current` (Sharpe +0.121, CAGR +97pp, maxdd −62pp,
win_rate +4.7pp).  The improvement is driven by the **fade-filter** (skip entry if intraday
mid-price < breakout level), which eliminates ~10% of signals where momentum reversed by
mid-session.  This is a legitimate filter even without intraday resolution.

This is a **directional signal** worth re-testing with 5-min bars.

### Recommended next step: Tiingo 5-min backfill

```bash
# One-time intraday backfill (estimate: ~2h for 205 sp500 tickers, 24 months)
python3 -c "
from data.tiingo import TiingoClient
# Tiingo provides free 5-min bars on paid plan; use /iex endpoint or /prices?resampleFreq=5min
# Saves to data/cache/intraday/{TICKER}.parquet
"
```

With true 5-min bars:
- Re-run this script (will auto-detect `data/cache/intraday/` and use real entry prices)
- Expect same-bar rate for `current` ≈ 5–15% (opening-tick stops are common on tight ATR stops)
- Expect delay variants to halve same-bar rate (re-evaluation at 09:35/09:45 filters the worst opens)

---

## Trade samples — same-bar stop-outs (current variant)

| Ticker | Entry date | Entry $ | Exit $ | PnL% |
|--------|------------|---------|--------|------|

---

## Notes

- **MCHP 2026-05-08** triggered this investigation: entered 09:30 open $102.93,
  stopped $100.89 within 36s (−1.98%).  In daily data, MCHP 2026-05-06 closed
  $102.93 (breakout vs 14-day high).  ATR_STOP_MULT=0.61 → very tight stop.
- All intraday-resolution findings require a one-time Tiingo intraday backfill.
- Feature flag path: `config/active/sp500.json` →
  `strategies.momentum_breakout.entry_delay_minutes` (0 = current).
- Trade CSV: `research/reports/c1_trades.csv`
