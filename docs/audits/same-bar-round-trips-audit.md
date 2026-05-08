# Same-Bar Round-Trip Audit

**Generated**: 2026-05-07 23:51:31 UTC  
**Lookback**: 30 days  
**Same-bar threshold**: <300s  

## Summary Stats

| Metric | Count |
|--------|-------|
| Total round-trips (30d) | 27 |
| Same-bar (<5min) (30d) | 3 |
| Today (2026-05-07) round-trips | 1 |
| Today same-bar | 1 |
| INVISIBLE in ledger (30d) | 14 |
| PARTIAL in ledger (30d) | 11 |
| RECORDED in ledger (30d) | 2 |

## Today's Deep-Dive (2026-05-07)

**Total same-bar realized PnL today**: $-2.67

| Ticker | Strategy | Buy Time | Buy Price | Sell Time | Sell Price | Elapsed (s) | Exit Reason | PnL | Plan Stop | Ledger |
|--------|----------|----------|-----------|-----------|------------|-------------|-------------|-----|-----------|--------|
| MCHP | momentum_breakout | 13:30:00 UTC | $102.28 | 13:30:36 UTC | $100.94 | 36 | unknown_non_atlas | $-2.67 | $100.8875 | INVISIBLE |

## Root-Cause Analysis

Of 3 same-bar round-trips:
- Stop-loss fills (opening volatility): **0** (0%)
- Take-profit fills: 0
- Trailing-stop fills: 0
- Unknown: 3


## All Round-Trips (30 days)

| Date | Ticker | Buy Price | Sell Price | Elapsed (s) | Same-Bar | Exit Reason | PnL | Ledger |
|------|--------|-----------|------------|-------------|----------|-------------|-----|--------|
| 2026-05-07 | MCHP | $102.28 | $100.94 | 36 | ✓ | unknown_non_atlas | $-2.67 | INVISIBLE |
| 2026-05-06 | FSLR | $218.16 | $213.82 | 96375 |  | unknown_non_atlas | $-8.67 | INVISIBLE |
| 2026-05-05 | EBAY | $107.50 | $107.10 | 37 | ✓ | unknown_non_atlas | $-1.61 | PARTIAL_ENTRY_ONLY |
| 2026-05-01 | XLE | $59.06 | $59.35 | 347420 |  | unknown_atlas | $+2.28 | PARTIAL_ENTRY_ONLY |
| 2026-04-29 | FCX | $57.59 | $56.00 | 432157 |  | trailing_stop_fill | $-7.95 | PARTIAL_ENTRY_ONLY |
| 2026-04-29 | MU | $524.56 | $508.66 | 88825 |  | unknown_non_atlas | $-31.81 | PARTIAL_ENTRY_ONLY |
| 2026-04-28 | MU | $517.70 | $508.68 | 707 |  | unknown_non_atlas | $-18.04 | RECORDED |
| 2026-04-24 | FCX | $61.48 | $58.34 | 350256 |  | trailing_stop_fill | $-15.71 | PARTIAL_ENTRY_ONLY |
| 2026-04-24 | ADI | $403.88 | $383.83 | 350371 |  | trailing_stop_fill | $-40.10 | PARTIAL_ENTRY_ONLY |
| 2026-04-24 | UNG | $10.34 | $10.58 | 274072 |  | trailing_stop_fill | $+14.57 | PARTIAL_ENTRY_ONLY |
| 2026-04-24 | SLV | $68.27 | $65.73 | 345601 |  | trailing_stop_fill | $-15.26 | PARTIAL_ENTRY_ONLY |
| 2026-04-24 | XLI | $173.97 | $172.17 | 952224 |  | unknown_atlas | $-16.20 | PARTIAL_ENTRY_ONLY |
| 2026-04-23 | CCJ | $126.47 | $119.73 | 420985 |  | trailing_stop_fill | $-26.96 | INVISIBLE |
| 2026-04-23 | AVGO | $422.57 | $399.07 | 432000 |  | trailing_stop_fill | $-23.50 | PARTIAL_ENTRY_ONLY |
| 2026-04-23 | XLK | $156.77 | $157.27 | 432000 |  | trailing_stop_fill | $+3.99 | PARTIAL_ENTRY_ONLY |
| 2026-04-21 | CHTR | $243.93 | $241.84 | 187097 |  | trailing_stop_fill | $-2.09 | RECORDED |
| 2026-04-21 | ON | $85.51 | $96.01 | 260693 |  | trailing_stop_fill | $+42.01 | INVISIBLE |
| 2026-04-17 | AMD | $278.25 | $337.53 | 864244 |  | trailing_stop_fill | $+118.57 | INVISIBLE |
| 2026-04-17 | ALB | $207.75 | $201.25 | 157 | ✓ | unknown_non_atlas | $-13.00 | INVISIBLE |
| 2026-04-15 | XLY | $116.44 | $116.71 | 1215158 |  | trailing_stop_fill | $+2.73 | INVISIBLE |
| 2026-04-15 | GLD | $442.80 | $420.71 | 1729816 |  | unknown_atlas | $-44.18 | INVISIBLE |
| 2026-04-15 | SLV | $71.92 | $70.99 | 518400 |  | trailing_stop_fill | $-5.60 | INVISIBLE |
| 2026-04-14 | UNG | $10.68 | $10.63 | 780834 |  | trailing_stop_fill | $-2.70 | INVISIBLE |
| 2026-04-14 | FCX | $68.03 | $61.64 | 777685 |  | trailing_stop_fill | $-31.95 | INVISIBLE |
| 2026-04-13 | MRVL | $130.08 | $129.84 | 256765 |  | trailing_stop_fill | $-0.95 | INVISIBLE |
| 2026-04-13 | CARR | $63.31 | $61.95 | 171213 |  | trailing_stop_fill | $-14.91 | INVISIBLE |
| 2026-04-13 | STZ | $163.95 | $159.52 | 623307 |  | unknown_non_atlas | $-22.13 | INVISIBLE |

## Systemic Bug Assessment

### Confirmed: `reconcile_entry_fills` silently drops same-bar round-trips

**Location**: `brokers/live_executor.py` lines ~2417-2431

The guard introduced on 2026-05-06 to prevent EBAY zombie rows correctly
prevents `OPEN` trade rows from being created for already-closed positions.
However, it also **silently drops** the recording of the completed round-trip.

**Result**: Any BUY+SELL pair where SELL fills within the 7-day order scan
window will be invisible in the trade ledger unless `reconcile_exit_fills`
separately picks it up from an existing entry record.

- **INVISIBLE today**: 14 of 27 total 30d round-trips
- `reconcile_exit_fills` cannot fix this alone — it requires a pre-existing
  entry record to compute PnL, so a round-trip with NO entry record produces
  a SELL entry with entry_price=0, PnL=None.

### Fix required

When the guard fires (`sell_filled_at >= buy_filled_at`), instead of
silently skipping, the reconciler should record BOTH an entry stub AND
an exit record marked `same_bar_round_trip=True`.