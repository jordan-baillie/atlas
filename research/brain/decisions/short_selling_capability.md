# Short Selling Capability

**Date:** 2026-03-14  
**Status:** PARTIAL (backtest engine + live executor incomplete)

## Decision

Added short selling infrastructure to enable bearish strategies. Currently gated behind config flags — not active in live trading.

## What's Complete

1. **Signal validation** — Signal dataclass accepts `direction="short"` with inverted validation (stop above entry, TP below entry)
2. **MR short signal generation** — `strategies/mean_reversion.py` `_generate_short_signals()` triggers on RSI > 70, z-score > +2.0 (overbought conditions)
3. **MR exit logic** — Direction-aware exits: short take-profit when price drops to target, short stop-loss when price rises above stop
4. **Alpaca verification** — `verify_shorting_enabled()` confirms account has shorting enabled (multiplier=2, margin account)

## What's Incomplete (FIX-1, FIX-2)

1. **Backtest engine P&L** — `_build_trade_record()` hardcodes long-only P&L calculation
2. **Backtest trailing stops** — Track highest price (long-only), shorts need lowest price tracking
3. **Backtest MAE/MFE** — Inverted for shorts (adverse = price rise, favorable = price drop)
4. **Live executor order sides** — Always BUY for entry, SELL for exit. Shorts need SELL to open, BUY to cover
5. **Protective order types** — Stop/limit order types need inversion for short positions
6. **Live preflight check** — No kill switch to block short entries in live trading

## Rollout Plan

1. Complete FIX-1 (backtest engine) and FIX-2 (live executor)
2. Run 3-month backtest with MR `short_enabled=true`
3. Validate short trade P&L accuracy against manual calculations
4. Paper-trade with short signals for 1 month
5. Enable in live config only after validation period

## Config

- `mean_reversion.short_enabled: false` — strategy-level kill switch
- `trading.short_selling_enabled: false` (proposed) — system-level kill switch

## Files

- `strategies/mean_reversion.py` — short signal generation
- `backtest/engine.py` — needs direction-aware P&L (FIX-1)
- `brokers/live_executor.py` — needs order side logic (FIX-2)
- `brokers/alpaca/broker.py` — `verify_shorting_enabled()`
- `tests/test_short_selling.py` — 43 tests (signal + strategy level)

## Risk Assessment

- $3,500 account with 2x margin — max short exposure $3,500
- Unlimited loss potential on shorts — strict stop-losses required
- Short squeeze risk on small-cap stocks — S&P 500 universe mitigates this
- Borrow costs not modeled yet — Alpaca charges hard-to-borrow fees
