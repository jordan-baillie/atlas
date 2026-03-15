# Survivorship Bias: Point-in-Time Universe Implementation

**Date:** 2026-03-14  
**Status:** IMPLEMENTED (comparison pending)

## Decision

Implemented point-in-time (PIT) universe filtering to eliminate survivorship bias from backtests.

## Problem

Standard backtests use today's S&P 500 membership list for all historical periods. This creates survivorship bias — stocks that were later removed (often due to poor performance, mergers, or bankruptcy) are excluded, inflating backtest returns.

## Implementation

### Data Source

- `data/sp500_changes.csv`: 152 membership changes from 2019–2025
- Each entry records: date, ticker added, ticker removed, reason
- Sources: Wikipedia S&P 500 change history, cross-referenced with press releases

### Algorithm

- `data/sp500_history.py`: `get_members_at_date(target_date)` walks backward from current membership
- Starts with today's S&P 500 list, then reverses each change before the target date
- For each change after target_date: if ticker was added, remove it; if removed, add it back
- Result: reconstructed S&P 500 membership as of any historical date

### Integration

- `universe/builder.py`: `filter_universe_pit()` applies PIT filtering during universe construction
- `backtest/engine.py`: PIT filtering in walk-forward loop when `universe.point_in_time=true`
- Config: `universe.point_in_time` (default: `false` for backward compatibility)

## Files

- `data/sp500_changes.csv` — change history data
- `data/sp500_history.py` — membership reconstruction
- `universe/builder.py` — PIT universe filtering
- `backtest/engine.py` — engine integration
- `tests/test_sp500_history.py` — 28 tests

## TODO

- [ ] Run comparison backtest: `point_in_time: true` vs `false`
- [ ] Quantify the bias magnitude (expected: 1-3% CAGR inflation from survivorship)
- [ ] Determine if PIT should become the default for all backtests
