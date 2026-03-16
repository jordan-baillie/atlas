# Decision: Cap Daily Entries to 3 (PDT Protection)

**Date:** 2026-03-17
**Status:** ACTIVE

## Problem
Account under $25K. Alpaca PDT allows 3 day trades per 5 rolling business days.
Each protective stop on a same-day position consumes a day trade slot.
Entering 5+ positions leaves positions 4+ without stop protection until
the next pre-market sync (~22 hours later, 6.5 market hours exposed).

## Tested and Failed
- `pdt_check: exit` — still blocked at 3/3 count
- `dtbp_check: exit` — still blocked
- `ptp_no_exception_entry: True` — still blocked
- OTO bracket orders — PDT rejects the entire order including entry
- All sell order types (stop, trailing_stop, limit) — blocked on same-day positions at limit
- Every combination of account config settings — hard server-side block at 3/3

## Solution
`max_daily_orders: 10 → 3`. Every entry gets a same-day protective stop.
Position slots fill over 3-4 days instead of 1. This is fine for swing trading
where hold periods are 5-15 days.

## What stays the same
- `max_open_positions: 10` — positions accumulate over multiple days
- Sync schedule: 19:15 + 23:45 AEST — unchanged
- Stop placement: `sync_protective_orders.py` places trailing stops after fill

## Resolves When
Account equity exceeds $25K (PDT restriction removed). At that point,
increase `max_daily_orders` back to match `max_open_positions`.
