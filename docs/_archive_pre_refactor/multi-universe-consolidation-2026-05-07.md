# Multi-universe consolidation (2026-05-07)

## Decision
Atlas now trades sp500 only. sector_etfs and commodity_etfs disabled as of 2026-05-07.

## State at consolidation time
- sp500 positions: CAT (1 share, $895.69 MV), SYK (1 share, $294.23 MV) — broker verified
  - State file also lists MCHP, FSLR, EBAY — **⚠ DISCREPANCY: these 3 are in live_sp500.json but NOT at broker**
  - Engineering Lead notified; requires follow-up reconciliation
- sector_etfs positions: 0 (XLE, XLI exited 2026-05-05 cleanly)
- commodity_etfs positions: 0 (GLD exited 2026-05-05 cleanly)
- No sector/commodity ETF tickers held at Alpaca broker (verified via `AlpacaBroker.get_positions()`)

## Changes shipped (this commit)
- `config/active/sector_etfs.json` — `trading.live_enabled: true → false`
- `config/active/commodity_etfs.json` — `trading.live_enabled: true → false`
- `scripts/atlas.crontab` — 13 entries for both markets commented (tag: `CONSOLIDATED-2026-05-07`)
  - Commented: sync_protective_orders (×2), intraday_monitor (×2), execute_approved (×2),
    premarket (×2), postclose (×2), reconcile_positions (×2)
  - Modified: reconcile_ledger loop changed from sp500+commodity_etfs+sector_etfs → sp500 only
- `systemd/atlas-research-window@commodity_etfs.timer` — disabled + removed from timers.target.wants
- `systemd/atlas-research-window@sector_etfs.timer` — disabled + removed from timers.target.wants

## Config backups (pre-consolidation)
- `config/active/sector_etfs.json.bak-pre-consolidation-1778205121`
- `config/active/commodity_etfs.json.bak-pre-consolidation-1778205121`
- `scripts/atlas.crontab.bak-pre-consolidation-1778205173`

## Capital
After consolidation, freed capital from sector/commodity ETF exits (XLE, XLI, GLD exited
2026-05-05) is in the Alpaca account cash balance. sp500 will pick it up via the next equity
refresh and per-market drawdown HWM recalibration.

## Code paths that still reference sector_etfs/commodity_etfs configs (follow-up items)
The following iterate over all markets but are NOT re-enable risks (all respect
`policy.should_skip()` → `live_enabled=False` gate, or are read-only/shadow-only):

| File | Pattern | Risk |
|------|---------|------|
| `scripts/sync_protective_orders.py:65` | `_MARKETS = (..., "commodity_etfs", "sector_etfs")` | ✅ SAFE — `policy.should_skip()` guards; cron now only calls `--market sp500` |
| `scripts/eod_settlement.py:42` | `_TRACKED_MARKETS_FOR_ATTRIBUTION` includes both | ✅ SAFE — order execution gated by `policy.should_skip()`; attribution math runs but produces 0/carry-forward for empty markets |
| `scripts/reconcile_positions.py:60` | `_MARKETS` includes both | ✅ SAFE — invoked per-market from cron; cron entries commented |
| `core/orchestrator.py:33` | `ACTIVE_MARKETS` includes both | ✅ SAFE — shadow mode only (`shadow=True`), no real dispatch |
| `monitor/evaluator.py:333` | hardcoded commodity_etfs in evaluator | ✅ SAFE — read-only position check, 0 positions returned |
| `services/telegram_bot.py:1105` | `_ROLLUP_MARKETS` includes both | ⚠️ LOW RISK — will include empty market summaries in notify-rollup (cosmetic noise) |
| `utils/telegram.py:377` | loops over all markets for summary files | ✅ SAFE — read-only |
| `brokers/live_portfolio.py:43` | `_ALL_TRADED_MARKETS` includes both | ✅ SAFE — used for cross-market ticker lookup, zero-impact with empty state |

**None of these will auto-re-enable trading.** The `BrokerRoutingPolicy.should_skip()`
check on `live_enabled=False` is the definitive gate for all execution paths.

Optional follow-up cleanup (not urgent):
- Update `_ROLLUP_MARKETS` in telegram_bot.py to sp500-only to reduce rollup noise
- Update `core/orchestrator.py:ACTIVE_MARKETS` to sp500-only once shadow mode confirmed stable

## Re-enable criteria
Re-enabling either market is a deliberate operator decision requiring ALL of:

1. sp500 has been live + green ≥30 days from this date (i.e., after 2026-06-06)
2. Freed capital allocation deployed cleanly into sp500 — no idle cash >5% for >7 days
3. Operator explicitly approves re-enable per market — separate decisions per market
4. Universe write-time filter confirmed working with no cross-universe leaks for ≥30 days prior
5. State file / broker discrepancy for MCHP, FSLR, EBAY resolved before any expansion

## Re-enable procedure
1. Flip `trading.live_enabled: false → true` in the market's `config/active/<market>.json`
2. Un-comment cron entries in `scripts/atlas.crontab` (search: `CONSOLIDATED-2026-05-07`)
3. Re-install crontab: `crontab scripts/atlas.crontab`
4. Re-enable systemd timers: `systemctl enable --now atlas-research-window@<market>.timer`
5. Run `python3 scripts/reconcile_positions.py --market <market>` to verify state
6. Send Telegram alert confirming re-enable

## Files touched (this commit)
- config/active/sector_etfs.json
- config/active/commodity_etfs.json
- scripts/atlas.crontab
- docs/multi-universe-consolidation-2026-05-07.md (this file)

## ⚠️ Outstanding discrepancy requiring follow-up
State file `live_sp500.json` lists 5 positions (CAT, SYK, MCHP, FSLR, EBAY) but Alpaca
broker only holds 2 (CAT, SYK). MCHP, FSLR, EBAY are orphaned state file entries.
This is a separate reconciliation task — NOT blocked by this consolidation.
