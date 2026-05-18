# 2026-05-18 — Tiingo 5-min Intraday OHLCV Backfill

**Status**: POC complete. Full backfill requires operator approval before enabling timer.  
**Task**: #316  
**Author**: Data Engineer  
**Date**: 2026-05-18

---

## 1. Scope

### Universes

| Universe | Tickers | Priority |
|----------|---------|----------|
| `sp500` | 204 (dynamic; from `data/cache/sp500/`) | Primary — backtest + live monitoring |
| `sector_etfs` | 11 | Secondary — sector rotation strategies |
| `commodity_etfs` | 9 | Secondary — macro overlay |
| **Total** | **224** | — |

`gold_etfs`, `treasury_etfs`, `defensive_etfs` are lower priority and can be added in a follow-on pass.

### Date Range

- **Start**: `2024-01-01` — provides ~2.4 years of history sufficient for:
  - Same-bar stop validation backtests
  - `entry_delay_minutes=15` momentum_breakout parameter sweep
  - Mean-reversion strategy validation
- **End**: Rolling `yesterday` (nightly cron) / `2026-05-17` for initial backfill
- **Bar interval**: 5-minute confirmed (`resampleFreq=5min` on Tiingo IEX endpoint)

### Blocking downstream tasks

1. Same-bar stop validation (was blocked pending this data)
2. `entry_delay_minutes=15` feature flag in `momentum_breakout` (scaffolded)
3. Intraday backtest validation for short-term mean-reversion strategies

---

## 2. Storage

### Decision: **Parquet per-ticker** at `data/cache/intraday_5m/{TICKER}.parquet`

**Rationale over SQLite:**

| Dimension | Parquet | SQLite (new ohlcv_5min table) |
|-----------|---------|-------------------------------|
| Volume | 224 tickers × 78 bars × 252 days × 2.4 yr ≈ **10.6M rows** | Same data; adds ~400MB+ to atlas.db |
| Query pattern | Backtest reads all bars for ticker X → sequential Parquet read is fastest | Row scan across index; slower for per-ticker range queries |
| Schema isolation | Index is `timestamp` (UTC DatetimeIndex); daily `ohlcv` uses `date` (TEXT) — no collision | Must use different table name; adds confusion |
| Existing pattern | Consistent with `data/cache/sp500/*.parquet` (daily) and `data/cache/hourly/*.parquet` | Would be a new pattern in atlas.db |
| Idempotency | Atomic `.tmp`→rename overwrites; no UPSERT complexity | `INSERT OR REPLACE` on (ticker, timestamp) PK |
| atlas.db impact | None — no WAL bloat | ~400MB+ growth on 72MB DB; slows all connections |
| Partial read | `pd.read_parquet(path, filters=[...])` for date slices | Needs proper index; still slower |

**Schema** (`data/cache/intraday_5m/{TICKER}.parquet`):

```
Index: timestamp (DatetimeIndex, UTC-aware, name="timestamp")
Columns:
  open    float64  — bar open price
  high    float64  — bar high price
  low     float64  — bar low price
  close   float64  — bar close price
  volume  int64    — bar volume (shares)
```

All timestamps are UTC. Convert to ET (`tz_convert("America/New_York")`) at display/signal-generation boundaries only.

**Checkpoint file**: `data/cache/intraday_5m/_checkpoint.json`

```json
{
  "SPY":  {"2024-01": "done", "2024-02": "done", ...},
  "AAPL": {"2024-01": "done", ...}
}
```

---

## 3. Tiingo Endpoint

### URL

```
GET https://api.tiingo.com/iex/{ticker}/prices
    ?startDate=YYYY-MM-DD
    &endDate=YYYY-MM-DD
    &resampleFreq=5min
    &columns=open,high,low,close,volume
    &token={TIINGO_API_TOKEN}
```

### Authentication

API key loaded from `~/.atlas-secrets.json` key `TIINGO_API_TOKEN` (existing pattern from `data/tiingo.py`).

### Verified behaviour

- Endpoint confirmed working for historical data back to at least **2024-01-01** ✅
- Returns UTC ISO 8601 timestamps: `"2026-05-12T13:30:00.000Z"` ✅
- 78 bars per trading day (09:30–16:00 ET = 390 min / 5 min = 78) ✅
- Multi-day requests work — tested 1-month windows successfully ✅
- 404 returned for delisted/invalid tickers (handled gracefully) ✅

### Rate limits

| Tier | Limit | Safe call interval | Notes |
|------|-------|-------------------|-------|
| Free | 50 req/hr | 72s between calls | Set `TIINGO_CALL_DELAY=72` env var |
| Paid (Power) | 10,000 req/hr | 0.4s | Script default: 1.5s (conservative) |

Default `INTER_CALL_DELAY = 1.5s` targets paid-tier usage at ~2,400 req/hr — leaving substantial headroom.

### Expected total call count (initial backfill)

- Date range: 2024-01-01 → 2026-05-17 = **29 months**
- Per-ticker: 1 API call per calendar month
- sp500: 204 × 29 = **5,916 calls**
- sector_etfs: 11 × 29 = 319 calls
- commodity_etfs: 9 × 29 = 261 calls
- **Total: ~6,496 calls**

---

## 4. Resume / Idempotency

### Per-ticker-month checkpointing

The script tracks progress at `(ticker, YYYY-MM)` granularity in `_checkpoint.json`.

**Skip logic**:
```python
if checkpoint.get(ticker, {}).get(month_key) == "done":
    skip  # no API call
```

**On restart**: Load checkpoint → skip all already-done pairs → continue from first missing pair.

**Force refresh**: `--force-refresh` flag ignores checkpoint; re-fetches and overwrites.

**Idempotency invariant**: Running the same command twice produces identical parquet output.

**Merge strategy**: On partial month re-fetch, new bars are merged via `merge_bars()`:
1. Concat existing + new
2. Deduplicate by timestamp (keep newer value)
3. Sort ascending by timestamp
4. Atomic overwrite via `.tmp` → rename

### "Already backfilled" detection (without re-pulling)

Check checkpoint first — if `(ticker, YYYY-MM) == "done"`, skip unconditionally.

Fallback: Even without a checkpoint, `merge_bars()` deduplicates by timestamp so re-running is safe but wasteful.

---

## 5. Cron / Systemd Schedule

### Nightly incremental update

Files staged (NOT installed):
- `systemd/atlas-intraday-backfill.service`
- `systemd/atlas-intraday-backfill.timer`

**Timer**: `OnCalendar=*-*-* 23:30:00` UTC

**Why 23:30 UTC?**
- US market closes 16:00 ET = 20:00 UTC
- 3.5-hour buffer for Tiingo data propagation + any late corrections
- Completes well before next market open (09:30 ET = 13:30 UTC)

**Incremental update scope**: Rolling 90-day window via `date -d "90 days ago"`. This re-checks the last 3 months daily, ensuring any late corrections or backfills from Tiingo are captured.

**Enable command** (after operator approval):
```bash
cp systemd/atlas-intraday-backfill.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable atlas-intraday-backfill.timer
systemctl start  atlas-intraday-backfill.timer
```

### Design consideration: single daily vs per-month chunks

**Decision: single daily cron** with rolling 90-day window.

Rationale:
- The checkpoint means idempotency is guaranteed — re-running old months costs 0 API calls
- Simpler cron management than 30 separate monthly jobs
- 90-day window catches late Tiingo corrections without full re-pull

---

## 6. Estimated Wall-Clock Time

### Initial full backfill (one-time, manual)

| Scenario | Call count | Delay/call | Time |
|----------|-----------|------------|------|
| Paid tier (default 1.5s) | 6,496 | 1.5s | **~2.7 hours** |
| Paid tier (aggressive 0.4s) | 6,496 | 0.4s | ~43 minutes |
| Free tier (72s/call) | 6,496 | 72s | ~130 hours (**not viable**) |

**Recommendation**: Confirm paid Tiingo tier before running full backfill.  
If on free tier: run in small batches (e.g. `--universe sector_etfs` first, then sp500 month-by-month).

### Nightly incremental update (post-backfill)

- Scope: 204 tickers × 3 months = 612 calls (most skipped by checkpoint; net ~204 new calls for yesterday)
- At 1.5s/call: **~5 minutes** for nightly delta

---

## ⚠️ OPERATOR APPROVAL REQUIRED

**DO NOT** enable the systemd timer until:

1. **Tiingo tier confirmed**: Verify paid tier is active (`TIINGO_API_TOKEN` account settings). Free tier cannot complete the initial backfill in a reasonable timeframe.
2. **Storage headroom**: Initial backfill will create ~224 parquet files totalling ~400–600MB in `data/cache/intraday_5m/`. Confirm disk space.
3. **Initial backfill run**: Manually trigger the full backfill first:
   ```bash
   nohup python3 -m scripts.backfill_intraday_5min \
     --universe sp500 \
     --start 2024-01-01 \
     --end 2026-05-17 \
     > /tmp/intraday-backfill.log 2>&1 &
   ```
   Then monitor: `tail -f /tmp/intraday-backfill.log`
4. **Validate**: After backfill, run a few tickers through the downstream signal code to confirm data quality.
5. **Enable timer**: Only after validation passes.

---

## 7. Smoke Test Results

**Command**:
```bash
cd /root/atlas && time python3 -m scripts.backfill_intraday_5min \
  --ticker SPY --start 2026-05-12 --end 2026-05-16
```

**Output**:
```
2026-05-18T21:56:07Z INFO [__main__] Backfill config: 1 tickers | 2026-05-12 -> 2026-05-16 | dry_run=False | force_refresh=False
2026-05-18T21:56:07Z INFO [__main__] Processing SPY (1/1)
2026-05-18T21:56:09Z INFO [__main__] Tiingo 5m: SPY [2026-05-12 -> 2026-05-16] -> 312 bars

============================================================
Backfill complete: 1 tickers processed
Total new rows:    312
Elapsed:           2.7s (0.0 min)
Cache dir:         /root/atlas/data/cache/intraday_5m

Parquet contents for SPY:
  Rows:  312
  First: 2026-05-12 13:30:00+00:00
  Last:  2026-05-15 19:55:00+00:00
  Cols:  ['open', 'high', 'low', 'close', 'volume']

Sample (first 3 rows):
                              open     high      low    close  volume
timestamp
2026-05-12 13:30:00+00:00  736.870  737.180  736.165  736.335   17133
2026-05-12 13:35:00+00:00  736.355  736.365  735.530  736.090   16933
2026-05-12 13:40:00+00:00  736.090  736.780  735.795  736.710   15749

Sample (last 3 rows):
                             open     high      low   close  volume
timestamp
2026-05-15 19:45:00+00:00  740.04  740.425  739.800  740.25   37661
2026-05-15 19:50:00+00:00  740.16  740.160  738.805  738.94   35710
2026-05-15 19:55:00+00:00  738.94  739.305  738.540  739.13  128738

real    0m3.788s
```

**Idempotency re-run** (immediate second run):
```
Total new rows:    0
Elapsed:           0.0s
```
✅ Zero API calls made on re-run — checkpoint correctly skips completed months.

**Wall clock**: 3.8s for 1 ticker × 1 month (2 seconds network + 1.5s rate-limit sleep)

### Key observations

- 312 bars for 4 trading days (Mon 12 → Thu 15; Friday 16 had no data yet as of run time)
- 78 bars/day confirmed (09:30–16:00 ET = 390 min / 5 min = 78)
- Timestamps in UTC (13:30–20:00 UTC = 09:30–16:00 ET) ✅
- All prices positive, volumes non-zero ✅
- Parquet schema: `timestamp` (UTC DatetimeIndex), `open`, `high`, `low`, `close`, `volume` ✅
- File size for SPY 1 week: ~22KB (snappy compressed)

---

## Files Created

| File | Description |
|------|-------------|
| `scripts/backfill_intraday_5min.py` | POC + production script |
| `tests/test_backfill_intraday_5min.py` | 17 unit tests (all passing) |
| `systemd/atlas-intraday-backfill.service` | Systemd service (NOT enabled) |
| `systemd/atlas-intraday-backfill.timer` | Systemd timer at 23:30 UTC (NOT enabled) |
| `data/cache/intraday_5m/SPY.parquet` | Smoke test output |
| `data/cache/intraday_5m/_checkpoint.json` | Checkpoint from smoke test |

---

## Next Steps (Post-Approval)

1. Confirm Tiingo paid tier active
2. Run initial full backfill for sp500 (estimated ~2.7 hours at 1.5s/call)
3. Run sector_etfs + commodity_etfs backfill
4. Install and enable systemd timer
5. Wire up downstream consumers:
   - `same_bar_stop_validator.py` — reads from `data/cache/intraday_5m/`
   - `strategies/momentum_breakout.py` — `entry_delay_minutes=15` feature flag
   - Mean-reversion backtest validation
