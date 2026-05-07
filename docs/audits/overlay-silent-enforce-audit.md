# Overlay-Enforce Silent-Impact Audit
*Generated: 2026-05-07T23:39Z*  
*Milestone: overlay-silent-bug-fix | Task #310*

## TL;DR

**11 overlay_decisions rows** carried a non-unit `sizing_override` across 10 distinct calendar days (2026-04-02 → 2026-05-07), but the executor's DB-fallback code was only introduced on 2026-04-28 and sp500 was only flipped into enforce mode on 2026-04-29 — meaning **3 date(s)** saw actual (non-shadow) enforcement: 2026-05-01, 2026-05-05, 2026-05-07.

Cross-referencing plan proposed-sizes, the SQLite trades table, and Alpaca order history reveals **3 silently-reduced entries** and **0 zero-share (skipped) entries**; estimated combined PnL impact is **$-0.40** ($-0.40 realized opportunity-cost + $0.00 heuristic estimate for skipped entries).

The #307 fix (added `overlay.enabled` guard to DB-fallback in `live_executor.py`) is present in the current working tree and correctly blocks enforcement when `overlay.enabled=false`; sector_etfs and commodity_etfs were never at risk (always `shadow_mode=true`).

## Affected Overlay-Decision Rows

| Date | ID | Multiplier | Regime | Tickers Avoided | Reasoning Excerpt |
|------|----|-----------|--------|-----------------|-------------------|
| 2026-04-02 | 2 | 0.35 | transition_uncertain | XLE, XOP, OIH | Strait of Hormuz disruption (Iran toll booth) and Trump's explicit 2-3 week escalation timeline create acute binary even… |
| 2026-04-17 | 18 | 0.55 | recovery_early | XOP, INSW | Active 2026 Strait of Hormuz crisis with 43% ceasefire probability and ongoing Hezbollah multi-front offensive represent… |
| 2026-04-17 | 20 | 0.55 | recovery_early | XLE, XOP, INSW, OXY, CVX | QQQ RSI at 70.9 (overbought) and SPY at resistance 702.78 with low-conviction volume (0.5x ratio) warrants modest size r… |
| 2026-04-20 | 22 | 0.55 | recovery_early | XOP, INSW, UNG, XLE | SPY and QQQ are overbought (RSI 73.2 and 70.9) on below-average volume (0.83x and 0.82x), signaling a fragile rally pron… |
| 2026-04-21 | 25 | 0.55 | recovery_early | XLE, XOP, INSW, UNG | SPY and QQQ RSI both overbought (72.7 / 70.9) on exceptionally low volume (SPY volume ratio 0.08), indicating a low-conv… |
| 2026-04-23 | 32 | 0.55 | recovery_early | XOP, XLE, INSW, UNG | Geopolitical risk is elevated with a coin-flip ceasefire probability (43%), active Hezbollah multi-front offensive, and … |
| 2026-04-27 | 37 | 0.8 | bull_risk_on | XOP, XLE, INSW | SPY and QQQ RSI both overbought (70.1 and 70.9) on below-average volume (SPY 0.66x, QQQ 0.82x), signalling a low-convict… |
| 2026-04-28 | 40 | 0.8 | bull_risk_on | NVDA, AAPL, AMZN, GOOGL, GOOG | SPY and QQQ RSIs are overbought (70.7 and 70.9) on notably low volume (SPY volume ratio 0.51), suggesting the recent ral… |
| 2026-05-01 | 44 | 0.8 | bull_risk_on | NVDA, AAPL, GOOGL, GOOG, AMZN | SPY and QQQ RSIs are overbought (70.5 and 70.9) after a ~10% 20-day momentum surge, placing the market near resistance w… |
| 2026-05-05 | 46 | 0.8 | bull_risk_on | NVDA, AAPL, GOOGL, GOOG | SPY and QQQ are technically overbought (RSI ~71) on anomalously low volume (SPY volume ratio 0.09), suggesting the rally… |
| 2026-05-07 | 49 | 0.8 | bull_risk_on | NVDA, AAPL, GOOGL, GOOG, AMZN | SPY and QQQ RSI both overbought (73.8 and 70.9) on extremely low volume (SPY volume ratio 0.14), suggesting the recent r… |

## Enforcement Classification per Date × Market

| Date | Market | Overlay ID | Multiplier | Status | Plan Entries | Note |
|------|--------|-----------|-----------|--------|-------------|------|
| 2026-04-02 | sp500 | 2 | 0.35 | ✅ no-code | 4 |  |
| 2026-04-02 | sector_etfs | 2 | 0.35 | ✅ no-code | — | plan unavailable |
| 2026-04-02 | commodity_etfs | 2 | 0.35 | ✅ no-code | — | plan unavailable |
| 2026-04-17 | sp500 | 18 | 0.55 | ✅ no-code | 4 |  |
| 2026-04-17 | sector_etfs | 18 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-17 | commodity_etfs | 18 | 0.55 | ✅ no-code | 1 |  |
| 2026-04-17 | sp500 | 20 | 0.55 | ✅ no-code | 4 |  |
| 2026-04-17 | sector_etfs | 20 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-17 | commodity_etfs | 20 | 0.55 | ✅ no-code | 1 |  |
| 2026-04-20 | sp500 | 22 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-20 | sector_etfs | 22 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-20 | commodity_etfs | 22 | 0.55 | ✅ no-code | 1 |  |
| 2026-04-21 | sp500 | 25 | 0.55 | ✅ no-code | 5 |  |
| 2026-04-21 | sector_etfs | 25 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-21 | commodity_etfs | 25 | 0.55 | ✅ no-code | 1 |  |
| 2026-04-23 | sp500 | 32 | 0.55 | ✅ no-code | 3 |  |
| 2026-04-23 | sector_etfs | 32 | 0.55 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-23 | commodity_etfs | 32 | 0.55 | ✅ no-code | 2 |  |
| 2026-04-27 | sp500 | 37 | 0.8 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-27 | sector_etfs | 37 | 0.8 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-27 | commodity_etfs | 37 | 0.8 | ✅ no-code | 0 | 0 proposed entries |
| 2026-04-28 | sp500 | 40 | 0.8 | 🔵 shadow | 1 |  |
| 2026-04-28 | sector_etfs | 40 | 0.8 | 🔵 shadow | 0 | 0 proposed entries |
| 2026-04-28 | commodity_etfs | 40 | 0.8 | 🔵 shadow | 0 | 0 proposed entries |
| 2026-05-01 | sp500 | 44 | 0.8 | 🔴 enforced | 4 |  |
| 2026-05-01 | sector_etfs | 44 | 0.8 | 🔵 shadow | 1 |  |
| 2026-05-01 | commodity_etfs | 44 | 0.8 | 🔵 shadow | — | plan unavailable |
| 2026-05-05 | sp500 | 46 | 0.8 | 🔴 enforced | 6 |  |
| 2026-05-05 | sector_etfs | 46 | 0.8 | 🔵 shadow | — | plan unavailable |
| 2026-05-05 | commodity_etfs | 46 | 0.8 | 🔵 shadow | 3 |  |
| 2026-05-07 | sp500 | 49 | 0.8 | 🔴 enforced | 3 |  |
| 2026-05-07 | sector_etfs | 49 | 0.8 | 🔵 shadow | — | plan unavailable |
| 2026-05-07 | commodity_etfs | 49 | 0.8 | 🔵 shadow | — | plan unavailable |

## Tickers Silently Resized

| Date | Market | Ticker | Planned Qty | Reduced Qty | Actual Qty | Reduction | Alpaca Submitted | Trade PnL | Opp-Cost $ |
|------|--------|--------|------------|------------|----------|----------|------------------|-----------|------------|
| 2026-05-05 | sp500 | EBAY | 5 | 4 | 4 | −1 | 4 | $-1.61 | $-0.40 |
| 2026-05-05 | sp500 | F | 38 | 30 | 30 | −8 | 30 | — | — |
| 2026-05-05 | sp500 | FCX | 3 | 2 | 2 | −1 | 2 | $-7.95 | — |

## Zero-Share Cases (Most Severe — Order Never Placed)

*No zero-share entries found.*

## Estimated PnL Impact

> **Realized opportunity-cost**: `(planned_qty − actual_qty) × trade_pct_return × entry_price` for completed trades (negative = overlay *saved* losses; positive = overlay cost upside).  
> **Unfilled**: order submitted at reduced qty but expired/cancelled — no PnL impact from reduction.  
> **Heuristic**: `planned_qty × entry_price × 0.55 × 0.015` for zero-share entries (win_rate=55%, avg_return=1.5%).

| Date | Market | Ticker | Category | PnL Impact | Type |
|------|--------|--------|----------|-----------|------|
| 2026-05-05 | sp500 | EBAY | reduced | $-0.40 | realized |
| 2026-05-05 | sp500 | F | reduced | — | unfilled |
| 2026-05-05 | sp500 | FCX | reduced | — | unfilled |

**Realized opportunity-cost total**: $-0.40  
**Heuristic estimate total**: $0.00  
**Grand total**: $-0.40  

## Recommendations

1. **#307 fix is sufficient for the DB-fallback path**: The `overlay.enabled` guard added to `live_executor.py` correctly blocks the silent DB-fallback enforcement. No further code change is needed for this specific bug. Verify via `python3 -m pytest tests/test_overlay_gating.py`.

2. **Affected plans may warrant re-evaluation**: For any date classified 🔴 enforced where entries were reduced, consider whether the overlay decision was valid at the time. If the overlay analysis was sound (e.g. overbought RSI on low volume), the tightening was arguably appropriate even if applied unintentionally. Re-running those plans' signals at original sizing would only be warranted if the overlay fundamentals were demonstrably wrong.

3. **Add `overlay_enforce_validated=true` gate to sector_etfs and commodity_etfs configs before flipping their `shadow_mode` to false**: Both markets are currently `shadow_mode=true` (safe). When their overlay is validated and shadow mode is removed, ensure `enabled=true` is set simultaneously — the guard introduced by #307 requires `enabled=true` to run the DB-fallback.

## Appendix: Per-Date Detail

### 2026-04-02 (overlay_decisions id=2)
- **Action**: tighten | **Multiplier**: 0.35
- **Regime**: transition_uncertain
- **Confidence**: 0.62
- **Tickers avoided**: XLE, XOP, OIH
- **Reasoning**: Strait of Hormuz disruption (Iran toll booth) and Trump's explicit 2-3 week escalation timeline create acute binary event risk the regime model cannot price through VIX alone (25.2 reads as only 'mode…

**sp500** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| MRVL | 4 | 1 | 4 | 4 | not_enforced | — |
| DVN | 5 | 1 | 5 | 5 | not_enforced | — |
| NKE | 18 | 6 | — | — | not_enforced | — |
| BSX | 10 | 3 | — | — | not_enforced | — |

**sector_etfs** — ✅ no-code
 — plan unavailable for this date

**commodity_etfs** — ✅ no-code
 — plan unavailable for this date

---

### 2026-04-17 (overlay_decisions id=18)
- **Action**: tighten | **Multiplier**: 0.55
- **Regime**: recovery_early
- **Confidence**: 0.62
- **Tickers avoided**: XOP, INSW
- **Reasoning**: Active 2026 Strait of Hormuz crisis with 43% ceasefire probability and ongoing Hezbollah multi-front offensive represents acute oil-supply tail risk not priced into VIX at 18.1; quant model indicators…

**sp500** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| ALB | 2 | 1 | — | 2 | not_enforced | — |
| AMD | 2 | 1 | — | 2 | not_enforced | — |
| XLK | 8 | 4 | — | 8 | not_enforced | — |
| CCJ | 4 | 2 | — | — | not_enforced | — |

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CCJ | 3 | 1 | — | — | not_enforced | — |

---

### 2026-04-17 (overlay_decisions id=20)
- **Action**: tighten | **Multiplier**: 0.55
- **Regime**: recovery_early
- **Confidence**: 0.62
- **Tickers avoided**: XLE, XOP, INSW, OXY, CVX
- **Reasoning**: QQQ RSI at 70.9 (overbought) and SPY at resistance 702.78 with low-conviction volume (0.5x ratio) warrants modest size reduction. Geopolitical risk is acute — Hezbollah multi-front offensive with only…

**sp500** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| ALB | 2 | 1 | — | — | not_enforced | — |
| AMD | 2 | 1 | — | — | not_enforced | — |
| XLK | 8 | 4 | — | — | not_enforced | — |
| CCJ | 4 | 2 | — | — | not_enforced | — |

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CCJ | 3 | 1 | — | — | not_enforced | — |

---

### 2026-04-20 (overlay_decisions id=22)
- **Action**: tighten | **Multiplier**: 0.55
- **Regime**: recovery_early
- **Confidence**: 0.62
- **Tickers avoided**: XOP, INSW, UNG, XLE
- **Reasoning**: SPY and QQQ are overbought (RSI 73.2 and 70.9) on below-average volume (0.83x and 0.82x), signaling a fragile rally prone to reversal; energy sector is already weak (XLE -7.3% 20d momentum, RSI 37, be…

**sp500** — ✅ no-code
 — no proposed entries in plan

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| XOP | 4 | 2 | — | — | not_enforced | — |

---

### 2026-04-21 (overlay_decisions id=25)
- **Action**: tighten | **Multiplier**: 0.55
- **Regime**: recovery_early
- **Confidence**: 0.62
- **Tickers avoided**: XLE, XOP, INSW, UNG
- **Reasoning**: SPY and QQQ RSI both overbought (72.7 / 70.9) on exceptionally low volume (SPY volume ratio 0.08), indicating a low-conviction rally vulnerable to reversal. Geopolitical risk remains elevated with a 4…

**sp500** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| MRVL | 4 | 2 | — | 2 | not_enforced | — |
| ON | 8 | 4 | — | 4 | not_enforced | — |
| CHTR | 3 | 1 | 1 | 1 | not_enforced | — |
| CCJ | 4 | 2 | — | — | not_enforced | — |
| XLK | 8 | 4 | — | 4 | not_enforced | — |

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CCJ | 2 | 1 | — | — | not_enforced | — |

---

### 2026-04-23 (overlay_decisions id=32)
- **Action**: tighten | **Multiplier**: 0.55
- **Regime**: recovery_early
- **Confidence**: 0.62
- **Tickers avoided**: XOP, XLE, INSW, UNG
- **Reasoning**: Geopolitical risk is elevated with a coin-flip ceasefire probability (43%), active Hezbollah multi-front offensive, and potential Kurdish/PKK second front — these are acute idiosyncratic risks not yet…

**sp500** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| AVGO | 1 | 1 | 1 | 1 | not_enforced | — |
| CCJ | 4 | 2 | — | 4 | not_enforced | — |
| XLK | 8 | 4 | 8 | 8 | not_enforced | — |

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CCJ | 4 | 2 | — | 4 | not_enforced | — |
| DBB | 69 | 37 | — | — | not_enforced | — |

---

### 2026-04-27 (overlay_decisions id=37)
- **Action**: tighten | **Multiplier**: 0.8
- **Regime**: bull_risk_on
- **Confidence**: 0.62
- **Tickers avoided**: XOP, XLE, INSW
- **Reasoning**: SPY and QQQ RSI both overbought (70.1 and 70.9) on below-average volume (SPY 0.66x, QQQ 0.82x), signalling a low-conviction rally that is vulnerable to a pullback; XLF is below its 200 DMA, underminin…

**sp500** — ✅ no-code
 — no proposed entries in plan

**sector_etfs** — ✅ no-code
 — no proposed entries in plan

**commodity_etfs** — ✅ no-code
 — no proposed entries in plan

---

### 2026-04-28 (overlay_decisions id=40)
- **Action**: tighten | **Multiplier**: 0.8
- **Regime**: bull_risk_on
- **Confidence**: 0.62
- **Tickers avoided**: NVDA, AAPL, AMZN, GOOGL, GOOG
- **Reasoning**: SPY and QQQ RSIs are overbought (70.7 and 70.9) on notably low volume (SPY volume ratio 0.51), suggesting the recent rally lacks institutional conviction and is at elevated pullback risk. Broad inside…

**sp500** — 🔵 shadow

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| MU | 2 | 1 | 2 | 2 | not_enforced | — |

**sector_etfs** — 🔵 shadow
 — no proposed entries in plan

**commodity_etfs** — 🔵 shadow
 — no proposed entries in plan

---

### 2026-05-01 (overlay_decisions id=44)
- **Action**: tighten | **Multiplier**: 0.8
- **Regime**: bull_risk_on
- **Confidence**: 0.55
- **Tickers avoided**: NVDA, AAPL, GOOGL, GOOG, AMZN
- **Reasoning**: SPY and QQQ RSIs are overbought (70.5 and 70.9) after a ~10% 20-day momentum surge, placing the market near resistance with elevated mean-reversion risk. Broad insider selling across mega-cap tech (AA…

**sp500** — 🔴 enforced

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CARR | 8 | 6 | — | — | no_trade_found | — |
| GOOG | 2 | 1 | — | — | no_trade_found | — |
| ORCL | 1 | 1 | — | — | no_trade_found | — |
| XOP | 4 | 3 | — | — | no_trade_found | — |

**sector_etfs** — 🔵 shadow

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| XLE | 8 | 6 | 8 | 8 | not_enforced | — |

**commodity_etfs** — 🔵 shadow
 — plan unavailable for this date

---

### 2026-05-05 (overlay_decisions id=46)
- **Action**: tighten | **Multiplier**: 0.8
- **Regime**: bull_risk_on
- **Confidence**: 0.55
- **Tickers avoided**: NVDA, AAPL, GOOGL, GOOG
- **Reasoning**: SPY and QQQ are technically overbought (RSI ~71) on anomalously low volume (SPY volume ratio 0.09), suggesting the rally lacks institutional conviction and is vulnerable to a pullback. Broad insider s…

**sp500** — 🔴 enforced

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| CDNS | 1 | 1 | — | — | no_trade_found | — |
| EBAY | 5 | 4 | 4 | 4 | silently_reduced | $-0.40 |
| F | 38 | 30 | 30 | 30 | silently_reduced | — |
| FCX | 3 | 2 | 2 | 2 | silently_reduced | — |
| GLD | 1 | 1 | — | — | no_trade_found | — |
| SLV | 3 | 2 | — | — | no_trade_found | — |

**sector_etfs** — 🔵 shadow
 — plan unavailable for this date

**commodity_etfs** — 🔵 shadow

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| DBB | 18 | 14 | — | — | not_enforced | — |
| XOP | 1 | 1 | — | — | not_enforced | — |
| DBA | 33 | 26 | — | — | not_enforced | — |

---

### 2026-05-07 (overlay_decisions id=49)
- **Action**: tighten | **Multiplier**: 0.8
- **Regime**: bull_risk_on
- **Confidence**: 0.55
- **Tickers avoided**: NVDA, AAPL, GOOGL, GOOG, AMZN
- **Reasoning**: SPY and QQQ RSI both overbought (73.8 and 70.9) on extremely low volume (SPY volume ratio 0.14), suggesting the recent rally lacks conviction and is vulnerable to a pullback. Broad-based insider selli…

**sp500** — 🔴 enforced

| Ticker | Planned | Reduced | Actual | Alpaca | Category | PnL Impact |
|--------|---------|---------|--------|--------|----------|-----------|
| LRCX | 1 | 1 | — | — | no_trade_found | — |
| MCHP | 6 | 4 | — | 2 | no_trade_found | — |
| XOP | 2 | 1 | — | — | no_trade_found | — |

**sector_etfs** — 🔵 shadow
 — plan unavailable for this date

**commodity_etfs** — 🔵 shadow
 — plan unavailable for this date

---
