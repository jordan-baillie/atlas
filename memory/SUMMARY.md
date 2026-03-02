# Atlas — Session Memory Summary
*Last updated: 2026-03-02 | Keep under 100 lines*

## System State

| Market | Broker | Mode | Config | Strategies Active |
|--------|--------|------|--------|-------------------|
| **SP500** | Moomoo (OpenD port 11111) | LIVE | v2.2 | TF + MR + OG (max_pos=15) |
| **ASX** | IBKR via IB Gateway (port 4001) | LIVE | v9.3 TF-only | trend_following only (IBKR fees kill MR/OG edge at $3,999 equity) |
| **HK (SEHK)** | IBKR via IB Gateway (port 4001) | PAPER | hk v1.0 | TF + MR + OG (live_enabled=false) |

**Live positions (approx):** SP500: ON, CHTR. ASX: 0 (new account reset 2026-03-02). HK: paper.

## Key Architecture Decisions

1. **Live broker = sole source of truth.** Paper engine removed. Broker state > paper state at all times.
2. **SP500 via Moomoo (US market only).** Moomoo AU API cannot place ASX orders server-side.
3. **ASX via IBKR (IB Gateway Docker), TF-only.** IBKR fees ($6/order, $500 min parcel) kill MR/OG edge at current equity. Revisit when account > $10K.
4. **HK via IBKR, paper mode.** Initial backtest Sharpe 0.82 — promising but unvalidated.
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
- **Moomoo OpenD** must be running for SP500 ops. Port 11111. Restarts via auto_recover.sh.
- **IB Gateway Docker** must be running for ASX/HK ops. Port 4001. Weekly 2FA re-auth.
- **ASX state reset 2026-03-02:** SP500 state corruption from spurious Monday post-close — 7-layer hardening applied to all write paths.
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

- **Wave 1** (Dormant Strategy Activation): 16/24 resolved. **2 promoted** (SMA-200 to SP500 v2.1, ASX reopt to v9.3).
- **Root finding:** Position contention (max_pos=10) blocked all dormant strategies. Allocation pools built (Task #52) as the unlock mechanism.
- **Wave 2 theme:** Volume filter combined test + MTF Momentum bug fixes + position pool tuning.
- **Next experiments in queue:** wave1_vol_filter combined, MTF Momentum post-fix.
