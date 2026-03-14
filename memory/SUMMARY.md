# Atlas — Session Memory Summary
*Last updated: 2026-03-13 | Keep under 100 lines*

## System State

| Market | Broker | Mode | Config | Strategies Active |
|--------|--------|------|--------|-------------------|
| **SP500** | Alpaca ($0 commission) | LIVE | v3.0 | TF+MR+OG+MB+SR+STMR+CR2 (max_pos=10, weighted alloc) |
| **ASX** | Moomoo (monitoring only — 2 open positions) | MONITOR | v1.0-passive | No new trades; Moomoo = source of truth |
| **HK (SEHK)** | INACTIVE (config moved to config/inactive/) | — | — | — |

**Live positions:** SP500: 2 (DHR, OXY via Alpaca, ~$3,520 USD equity). ASX: 2 (WHC.AX, WDS.AX via Moomoo, ~A$12,417 account equity).

## Key Architecture Decisions

1. **Live broker = sole source of truth.** Paper engine removed. Broker state > paper state at all times.
2. **SP500 via Alpaca ($0 commission).** Switched from Moomoo 2026-03-13. Commission-free eliminates fee drag.
3. **ASX monitoring mode.** config/active/asx.json uses Moomoo (monitoring_enabled=true, live_enabled=false). No new trades; reads real positions from Moomoo REAL account. `atlas status -m asx` shows live positions. HK still inactive.
5. **No paper trading layer.** `LivePortfolio` reads broker. Paper engine used only for backtest and plan generation.
6. **Allocation pools implemented but disabled** (`allocation.enabled=false`). Enable when momentum_breakout is re-activated.
7. **Position count:** SP500 max_open_positions=15 (up from 10 — confirmed +13% Sharpe improvement).
8. **SMA-200 filter on all SP500 strategies** (v2.1 onwards). Sharpe +47%, DD -1.2pp.
9. **Research cron runs Mon-Fri 09:00 AEST** — both markets closed in this window.
10. **All crons are Tue-Sat** for overnight US sessions (Friday US = Saturday AEST).

## Known Issues & Gotchas

- **C3/C4 (CRITICAL — FIXED):** `LivePortfolio.update_positions()` and `get_today_deals()` missing on IBKR — fixed in 2026-03-02 audit swarm.
- **C1 (FIXED):** Look-ahead bias in trailing stop exits — fixed to use T-1 close.
- **C5 (FIXED):** IBKR account ID hardcoded in config — moved to secrets.
- **Alpaca live.** API key/secret in ~/.atlas-secrets.json. DNS fix in /etc/hosts for api.alpaca.markets.
- **SP500 state reset 2026-03-13:** Old Moomoo state caused false drawdown halt. Clean state for Alpaca start.
- **Sector rotation generates 0 trades** when tested solo (sector_map missing SP500 entries). Fixed via sector_map_sp500.json.
- **MTF Momentum has Series comparison bugs** — deferred, needs full code audit.
- **BB Squeeze solo** near-breakeven after optimization (Sharpe -0.38) — deferred.

## Critical Operational Procedures

```bash
# Daily health check
python3 scripts/cli.py status               # portfolio state
python3 scripts/health_check.py             # degradation check

# Pre-market
python3 scripts/cli.py -m sp500 ingest && python3 scripts/cli.py -m sp500 plan

# Emergency halt
python3 scripts/cli.py halt                 # cancels all open orders

# Re-optimization (when health check flags degradation)
python3 scripts/reoptimize_parallel.py --market sp500  # ~2h

# Recovery
scripts/pi-cron.sh recover postclose sp500
```

## Research State

- **Wave 1** (Dormant Strategy Activation): 23/24 resolved. **2 promoted** (SMA-200 to SP500 v2.1, ASX reopt to v9.3).
- **Root finding:** Position contention (max_pos=10) blocked all dormant strategies. Allocation pools built (Task #52) as the unlock mechanism.
- **Wave 2** (Enhanced MR Alpha): 6/10 run. **0 promotions.** ConnorsRSI2 and all solo strategies unprofitable at $4K equity.
- **Wave 3** (MR Deep Dive): 5/5 run. **0 promotions.** IBS, volume, hold-period sweeps — marginal improvements only.
- **Wave 4** (LBR + MR Tweaks): 7/10 run, 3 deferred. **0 promotions.** LBR comprehensively unprofitable on individual stocks (Sharpe -2.08 to -1.44). Published ETF strategies do NOT translate to component stocks. MR strength exit no improvement.
  - **Pattern confirmed:** Strategies published for SPY/index ETFs (ConnorsRSI2, LBR) fail on individual SP500 stocks. Don't adapt more of these.
  - **Queue empty** — need Wave 5 theme. Options: re-optimization of TF/MR/OG (>30 days), portfolio-level improvements, or new strategy class designed for individual stocks.
