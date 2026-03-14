# Atlas — Knowledge Index
*THE go-to file when you need to find anything in the project.*
*Last updated: 2026-03-02*

---

## Table of Contents

1. [Quick Reference](#1-quick-reference)
2. [Architecture](#2-architecture)
3. [Configuration](#3-configuration)
4. [Research & Findings](#4-research--findings)
5. [Operational Runbooks](#5-operational-runbooks)
6. [Decisions & Lessons](#6-decisions--lessons)
7. [Scripts & Tools](#7-scripts--tools)
8. [Data & State Files](#8-data--state-files)
9. [Pi Skills & Extensions](#9-pi-skills--extensions)
10. [Glossary](#10-glossary)
11. [Navigation Guide — "How do I…"](#11-navigation-guide--how-do-i)

---

## 1. Quick Reference

> One-line answers to "where is the thing?"

| What you need | File |
|---|---|
| System state summary (current config, known issues) | [`memory/SUMMARY.md`](../memory/SUMMARY.md) |
| Why we made a key decision | [`docs/DECISIONS.md`](DECISIONS.md) |
| Patterns and rules from past mistakes | [`tasks/lessons.md`](../tasks/lessons.md) |
| Active SP500 config | [`config/active/sp500.json`](../config/active/sp500.json) |
| Active ASX config | [`config/active/asx.json`](../config/active/asx.json) |
| Active HK config | [`config/active/hk.json`](../config/active/hk.json) |
| Current experiment queue | [`research/queue.json`](../research/queue.json) |
| All experiment results | [`research/journal.json`](../research/journal.json) |
| All executed trades | [`journal/trade_ledger.json`](../journal/trade_ledger.json) |
| How to re-optimize | [`docs/OPTIMIZATION_GUIDE.md`](OPTIMIZATION_GUIDE.md) |
| Daily workflow (CLI commands) | [`README.md`](../README.md) |
| Critical bugs and fix status | [`audit/FULL_AUDIT.md`](../audit/FULL_AUDIT.md) |
| Research system design | [`research/README.md`](../research/README.md) |

---

## 2. Architecture

### Core Documentation

| File | Description |
|---|---|
| [`README.md`](../README.md) | Project overview, how it works, CLI reference, adding new markets |
| [`AGENTS.md`](../AGENTS.md) | Swarm coordination rules: role hierarchy, builder scope, anti-patterns |
| [`memory/SUMMARY.md`](../memory/SUMMARY.md) | Session memory: current market states, live positions, known issues, critical procedures |
| [`docs/DECISIONS.md`](DECISIONS.md) | Chronological decision log — every architectural/operational decision with rationale |

### System Design

```
Data (yfinance)
    ↓
Universe Filter (liquid, tradeable tickers per market)
    ↓
Strategy Signals (TF, MR, OG per ticker, with confidence scores)
    ↓
Trade Plan Generator (respects risk limits, max positions, allocation pools)
    ↓
[APPROVAL GATE — human review]
    ↓
Live Executor (Moomoo for SP500, IBKR for ASX/HK)
    ↓
EOD Settlement (stop checks, PnL updates, equity curve)
    ↓
Research Pipeline (A/B tests, re-optimization, promotion)
```

**Key invariants:**
- Live broker state is the sole source of truth — no parallel paper portfolio
- SP500 via Moomoo (OpenD port 11111), ASX/HK via IBKR (IB Gateway port 4001)
- All crons are Tue–Sat (US Friday session = Saturday AEST)
- Research cron runs Mon–Fri 09:00 AEST (both markets closed)

### Module Map

| Module | Path | Purpose |
|---|---|---|
| Markets | `markets/` | `MarketProfile` per market (tickers, fees, hours, timezone) |
| Strategies | `strategies/` | Signal generation ABC + 10 implementations |
| Backtest Engine | `backtest/` | Walk-forward engine with sector/position checks, metrics |
| Brokers | `brokers/` | `BrokerAdapter` ABC, Moomoo + IBKR implementations, live executor |
| Data Ingestion | `data/` | yfinance download, per-market parquet cache |
| Universe | `universe/` | Liquidity/quality filtering for each market |
| Utils | `utils/` | Indicators, allocation pools, config loader, Telegram, sizing |
| Research | `research/` | Experiment queue, journal, models, file-locked I/O |
| Journal | `journal/` | Trade ledger, decision journal, allocation research |
| Monitor | `monitor/` | Performance evaluator, degradation detection |
| Dashboard | `dashboard/` | HTML dashboard + data generation |
| Scripts | `scripts/` | All CLI tools, cron wrapper, automation |
| Pi Package | `pi-package/` | Pi agent skills + extensions for autonomous ops |

### Source Files by Module

<details>
<summary>Expand full source file listing</summary>

**Strategies (`strategies/`)**

| File | Description |
|---|---|
| `strategies/base.py` | `BaseStrategy` ABC: `generate_signals()`, `check_exits()`, `calc_atr()` |
| `strategies/mean_reversion.py` | MR: RSI(14)/z-score oversold entries, SMA-200 filter, IBS confirmation |
| `strategies/trend_following.py` | TF: MA crossover breakouts with ATR trailing stop |
| `strategies/opening_gap.py` | OG: overnight gap fade/follow with 1–3 day hold |
| `strategies/momentum_breakout.py` | MB: N-day high breakout with trend MA alignment (DISABLED, awaiting allocation pools) |
| `strategies/bb_squeeze.py` | BB Squeeze: Bollinger inside Keltner volatility compression (DISABLED, near breakeven) |
| `strategies/mtf_momentum.py` | MTF: daily pullback within weekly uptrend (DISABLED, code bugs pending fix) |
| `strategies/sector_rotation.py` | SR: top sector momentum → top stocks within sectors (DISABLED, rebalance-aware backtest needed) |
| `strategies/short_term_mr.py` | STMR: RSI(2)/IBS rapid 1–5 day reversals (DISABLED, position contention) |
| `strategies/dividend_capture.py` | DC: dividend capture (NOT IMPLEMENTED) |

**Brokers (`brokers/`)**

| File | Description |
|---|---|
| `brokers/base.py` | `BrokerAdapter` ABC: all required broker methods |
| `brokers/moomoo/broker.py` | Moomoo live broker (SP500, US market only) |
| `brokers/moomoo/mapper.py` | Moomoo ticker/order format conversion |
| `brokers/ibkr/broker.py` | IBKR live broker via `ib_insync` (ASX + HK) |
| `brokers/ibkr/mapper.py` | IBKR contract/order format conversion |
| `brokers/live_executor.py` | Order execution, stop placement, stop reconciliation |
| `brokers/live_portfolio.py` | Live portfolio state from broker (SoT) |
| `brokers/plan.py` | `TradePlanGenerator`: entry/exit plan from signals + risk |
| `brokers/position.py` | `Position` dataclass: excursions, stop tracking, P&L |
| `brokers/registry.py` | Broker factory: returns broker instance from config |
| `brokers/secrets.py` | Secure credential loading from `~/.atlas-secrets.json` |

**Backtest (`backtest/`)**

| File | Description |
|---|---|
| `backtest/engine.py` | Walk-forward backtest engine: position simulation, sector limits, allocation pools |
| `backtest/index.py` | Backtest result indexing and retrieval |
| `backtest/metrics.py` | Sharpe, CAGR, MaxDD, PF, WR, walk-forward, perturbation, strategy correlation |

**Markets (`markets/`)**

| File | Description |
|---|---|
| `markets/base.py` | `MarketProfile` ABC: tickers, fees, hours, timezone, universe path |
| `markets/sp500.py` | S&P 500 profile (292 tickers, USD, NYSE/NASDAQ, ET timezone) |
| `markets/asx.py` | ASX 200 profile (248 tickers, AUD, ASX, AEST timezone) |
| `markets/hk.py` | SEHK profile (130 tickers, HKD, HKEX, HKT timezone) |
| `markets/registry.py` | Market factory: returns `MarketProfile` from market_id string |

**Utils (`utils/`)**

| File | Description |
|---|---|
| `utils/allocation.py` | `StrategyAllocationPool`: per-strategy position caps (hard/soft pool modes) |
| `utils/config.py` | Config loading, active config paths, config update helpers |
| `utils/dividends.py` | Dividend date lookup and earnings calendar |
| `utils/dynamic_sizing.py` | Drawdown-scaled position sizing |
| `utils/earnings.py` | Earnings date cache loading and proximity checks |
| `utils/helpers.py` | `calc_atr()`, `calc_ibs()`, `calc_wvf()`, general indicator helpers |
| `utils/logging_config.py` | Centralized logging setup |
| `utils/market_breadth.py` | Market breadth indicators (% above MA, advance/decline) |
| `utils/relative_strength.py` | Ticker relative strength vs index |
| `utils/signal_enrichment.py` | Post-signal enrichment: sector tags, breadth context |
| `utils/telegram.py` | Telegram bot notifications (async, retry-safe) |

**Data (`data/`)**

| File | Description |
|---|---|
| `data/ingest.py` | yfinance download, atomic parquet cache writes, freshness checks |
| `data/fred.py` | FRED economic data API (VIX, macro indicators) |

**Universe (`universe/`)**

| File | Description |
|---|---|
| `universe/builder.py` | Applies price/volume/market-cap filters, builds per-market universe.json |

**Research (`research/`)**

| File | Description |
|---|---|
| `research/models.py` | `QueueEntry`, `ExperimentEnvelope` models; file-locked queue/journal I/O |
| `research/__init__.py` | Public API: `claim_experiment()`, `update_status()`, `append_journal()` |

**Monitor (`monitor/`)**

| File | Description |
|---|---|
| `monitor/evaluator.py` | Performance degradation evaluator (compare recent vs historical metrics) |
| `monitor/models.py` | Performance snapshot models |
| `monitor/seed.py` | Seeds monitor with baseline metrics from last backtest |

**Services (`services/`)**

| File | Description |
|---|---|
| `services/dashboard_server.py` | HTTP server for dashboard with Basic Auth |
| `services/telegram_bot.py` | Telegram bot webhook receiver for manual commands |

**Tests (`tests/`)**

| File | Description |
|---|---|
| `tests/test_allocation.py` | 19 unit tests for allocation pool (hard pool, soft pool, disabled, overflow) |
| `tests/test_hk_market.py` | HK market profile unit tests |

</details>

---

## 3. Configuration

### Active Configs (live trading parameters)

| File | Market | Version | Mode | Key Strategies |
|---|---|---|---|---|
| [`config/active/sp500.json`](../config/active/sp500.json) | SP500 | v2.2 | LIVE | TF + MR + OG, max_pos=15, SMA-200 filter ON |
| [`config/active/asx.json`](../config/active/asx.json) | ASX | v9.3 TF-only | LIVE | trend_following only (IBKR fee drag kills MR/OG at $3,999 equity) |
| [`config/active/hk.json`](../config/active/hk.json) | HK (SEHK) | v1.0 | PAPER | TF + MR + OG, live_enabled=false, max_pos=15 |

**Config top-level sections** (every active config):
- `market` — market_id, broker, account settings, IBKR client_id
- `universe` — price/volume/market-cap filters
- `risk` — starting_equity, max_risk_per_trade, max_positions, sector concentration
- `strategies` — per-strategy params + `enabled` flag
- `fees` — commission model (IBKR: $6/order flat; Moomoo: percentage; ASX: $500 min parcel)
- `backtest` — walk-forward windows (train=252, test=63, step=21 days by default)
- `allocation` — strategy pool caps (currently `enabled=false` on all markets)
- `data` — cache settings, data range

### Candidate Configs (awaiting promotion or deferred)

| File | Market | Status |
|---|---|---|
| [`config/candidates/sp500_wave1_sma200.json`](../config/candidates/sp500_wave1_sma200.json) | SP500 | Already promoted → v2.1 |
| [`config/candidates/sp500_wave1_moment_opt.json`](../config/candidates/sp500_wave1_moment_opt.json) | SP500 | Solo pass, combined failed — deferred |
| [`config/candidates/sp500_wave1_sector_opt.json`](../config/candidates/sp500_wave1_sector_opt.json) | SP500 | Combined failed (Sharpe -0.32) — deferred |
| [`config/candidates/sp500_wave1_short__opt.json`](../config/candidates/sp500_wave1_short__opt.json) | SP500 | Combined failed — deferred |
| [`config/candidates/sp500_wave1_bb_squ_opt.json`](../config/candidates/sp500_wave1_bb_squ_opt.json) | SP500 | Optimization partial (near breakeven) — deferred |
| [`config/candidates/asx_ibkr_reopt.json`](../config/candidates/asx_ibkr_reopt.json) | ASX | IBKR fee-constraint reopt — deferred |
| [`config/candidates/asx_ibkr_tf_only.json`](../config/candidates/asx_ibkr_tf_only.json) | ASX | TF-only for IBKR — now active as v9.3 |
| [`config/candidates/asx_wave1_asx_reopt.json`](../config/candidates/asx_wave1_asx_reopt.json) | ASX | Wave 1 reopt — promoted to v9.3 |

### Config Version History (rollback snapshots)

| File | Snapshot of | Date |
|---|---|---|
| [`config/versions/sp500_v2.2.json`](../config/versions/sp500_v2.2.json) | SP500 v2.2 (current active) | 2026-03-02 |
| [`config/versions/sp500_v2.1.json`](../config/versions/sp500_v2.1.json) | SP500 v2.1 with SMA-200 filter | 2026-03-01 |
| [`config/versions/sp500_v2.1_pre_maxpos15.json`](../config/versions/sp500_v2.1_pre_maxpos15.json) | SP500 before max_pos=15 bump | 2026-03-02 |
| [`config/versions/sp500_v2.0_optimized.json`](../config/versions/sp500_v2.0_optimized.json) | SP500 first US optimization | 2026-02-27 |
| [`config/versions/sp500_pre_reopt_20260227.json`](../config/versions/sp500_pre_reopt_20260227.json) | SP500 pre Wave 1 reopt | 2026-02-27 |
| [`config/versions/sp500_candidate_pre_v2.json`](../config/versions/sp500_candidate_pre_v2.json) | SP500 before v2 promotion | 2026-02-27 |
| [`config/versions/asx_v9.3.json`](../config/versions/asx_v9.3.json) | ASX v9.3 (current active) | 2026-02-28 |
| [`config/versions/asx_ibkr_tf_only_v1.0.json`](../config/versions/asx_ibkr_tf_only_v1.0.json) | ASX IBKR TF-only v1.0 | 2026-03-02 |
| [`config/versions/asx_pre_ibkr_tf_only_20260302.json`](../config/versions/asx_pre_ibkr_tf_only_20260302.json) | ASX before TF-only pivot | 2026-03-02 |
| [`config/versions/asx_pre_promotion_20260226.json`](../config/versions/asx_pre_promotion_20260226.json) | ASX before v9.3 promotion | 2026-02-26 |
| [`config/versions/asx_pre_reopt_20260225.json`](../config/versions/asx_pre_reopt_20260225.json) | ASX before v9.4 reopt | 2026-02-25 |
| [`config/versions/asx_v9.3_ibkr_live.json`](../config/versions/asx_v9.3_ibkr_live.json) | ASX v9.3 IBKR live variant | 2026-03-02 |
| [`config/versions/asx_v9.3_ibkr_3999.json`](../config/versions/asx_v9.3_ibkr_3999.json) | ASX v9.3 at $3,999 equity | 2026-03-02 |
| [`config/versions/asx_candidate_reoptimized_20260225.json`](../config/versions/asx_candidate_reoptimized_20260225.json) | ASX reopt candidate | 2026-02-25 |
| [`config/versions/config_v9.1_pre_reoptimization.json`](../config/versions/config_v9.1_pre_reoptimization.json) | Pre v9.2 degraded baseline | 2026-02-18 |
| [`config/versions/config_v9.3_robust.json`](../config/versions/config_v9.3_robust.json) | v9.3 blend (rejected, same stability as v9.2) | 2026-02-19 |
| `config/versions/active_config_pre_reopt_*.json` | Auto-snapshot before each reopt run (SP500) | 2026-03-02 |
| `config/versions/active_config_pre_reopt_asx_*.json` | Auto-snapshot before each reopt run (ASX) | 2026-03-02 |

### SP500 Config Version Metrics Summary

| Version | CAGR | Sharpe | MaxDD | Key Change |
|---|---|---|---|---|
| v9.1 (pre-reopt) | -0.35% | -0.30 | 12.84% | Degraded baseline (post data-refresh) |
| v9.2 | +11.21% | +0.67 | 7.76% | Full coordinate descent reoptimization |
| v9.3 blend | +6.65% | +0.29 | 12.32% | Rejected — same stability as v9.2, -4.5% CAGR |
| v9.4 | +8.81% | +0.42 | 10.11% | Parallel reopt, robust scoring (338 trades) |
| v2.0 | +15.69% | +1.040 | 5.39% | US-specific optimization, RSI(14) over RSI(2) |
| v2.1 | +11.7% | +0.87 | 5.3% | SMA-200 filter (+47% Sharpe, -18% trades) |
| **v2.2 (active)** | **+13.3%** | **+0.983** | **5.2%** | **max_positions 10→15 (+13% Sharpe)** |

### Allocation Pool Config

Implemented but disabled by default in all active configs. Enable when `momentum_breakout` is re-added:

```json
"allocation": {
  "enabled": false,
  "mode": "hard_pool",
  "pools": {
    "trend_following":   {"max_positions": 5},
    "mean_reversion":    {"max_positions": 5},
    "opening_gap":       {"max_positions": 5},
    "momentum_breakout": {"max_positions": 5},
    "_other":            {"max_positions": 2}
  }
}
```

Full implementation notes: [`journal/allocation_research.md`](../journal/allocation_research.md)

---

## 4. Research & Findings

### Research System Files

| File | Description |
|---|---|
| [`research/README.md`](../research/README.md) | Research system design: lifecycle, queue schema, file roles, multi-agent patterns |
| [`research/queue.json`](../research/queue.json) | Prioritized experiment queue — current status of every Wave 1 experiment |
| [`research/journal.json`](../research/journal.json) | Append-only log of all completed experiment results (never edit, only append) |
| [`research/models.py`](../research/models.py) | Data models, file-locked I/O, queue/journal operations |
| [`research/waves/wave_1_brief.json`](../research/waves/wave_1_brief.json) | Wave 1 theme brief: dormant strategy activation |

### Research Experiment Queue Summary (Wave 1)

| Experiment ID | Strategy | Status | Outcome |
|---|---|---|---|
| `wave1_moment_solo` | momentum_breakout | **passed** | Solo viable |
| `wave1_moment_opt` | momentum_breakout | **passed** | Optimized params ready |
| `wave1_moment_comb` | momentum_breakout | **failed** | Position contention at max_pos=10 |
| `wave1_moment_oos` | momentum_breakout | deferred | Blocked by comb fail |
| `wave1_short__solo` | short_term_mr | **passed** | Solo viable |
| `wave1_short__opt` | short_term_mr | **passed** | Optimized params ready |
| `wave1_short__comb` | short_term_mr | **failed** | Position contention |
| `wave1_short__oos` | short_term_mr | deferred | Blocked |
| `wave1_sector_solo` | sector_rotation | **passed** | Solo viable (251 trades, WR 44%, PF 1.24) |
| `wave1_sector_opt` | sector_rotation | **passed** | Optimized |
| `wave1_sector_comb` | sector_rotation | **failed** | Degrades portfolio Sharpe by -0.32 |
| `wave1_sector_oos` | sector_rotation | deferred | Blocked |
| `wave1_mtf_mo_solo` | mtf_momentum | **queued** | Code bugs fixed, ready to run |
| `wave1_mtf_mo_opt` | mtf_momentum | **queued** | Awaiting solo pass |
| `wave1_mtf_mo_comb` | mtf_momentum | **queued** | Awaiting opt pass |
| `wave1_mtf_mo_oos` | mtf_momentum | **queued** | Awaiting comb pass |
| `wave1_bb_squ_solo` | bb_squeeze | **passed** | Solo viable |
| `wave1_bb_squ_opt` | bb_squeeze | **partial** | PF 1.04 < 1.1 threshold, Sharpe -0.38 |
| `wave1_bb_squ_comb` | bb_squeeze | deferred | Optimization only partial |
| `wave1_bb_squ_oos` | bb_squeeze | deferred | Blocked |
| `wave1_asx_reopt` | ASX all strategies | **promoted** | Sharpe +0.17, DD -2.6pp → v9.3 active |
| `wave1_vix_filter` | SP500 VIX filter | **failed** | All 4 thresholds degraded Sharpe |
| `wave1_vol_filter` | SP500 volume filter | **passed** | 1.5x threshold optimal for MR |
| `wave1_cross_mkt` | SMA-200 filter | **promoted** | Sharpe +47%, CAGR +1.6pp → v2.1 active |

**Wave 1 Root Finding:** Position contention at max_pos=10 blocked all dormant strategies. Allocation pools (Task #52) are the unlock mechanism.
**Wave 2 theme:** volume filter combined test + MTF Momentum after bug fix.

### Individual Experiment Envelopes

`research/experiments/` — self-contained JSON files with full inputs + outputs + verdicts:

| File | Description |
|---|---|
| `exp-wave1_{id}.json` | Full experiment record: inputs, config snapshot, outputs, verdict, learnings |
| `eval-wave1_{id}.json` | Analyst evaluation overlay (verdict + rationale) |
| `position_allocation_research.json` | Allocation pool comparison backtest (no-pools vs hard vs soft) |
| `sp500_v2.2_oos_validation.json` | v2.2 OOS validation result (all 3 tests passed) |

### Backtest Results Archive

`backtest/results/` — raw JSON from backtest engine runs:

| File | Description |
|---|---|
| `backtest/results/index.json` | Index of all saved backtest runs |
| `backtest/results/reopt_sp500.json` | SP500 full reoptimization output |
| `backtest/results/sp500_v2_oos_validation.json` | SP500 v2 time-split + perturbation + WF validation |
| `backtest/results/sp500_v2_optimized.json` | SP500 v2 optimized params |
| `backtest/results/sp500_v2.2_oos_validation.json` | SP500 v2.2 OOS (Sharpe ratio OOS/IS = 1.80) |
| `backtest/results/oos_wave1_sma200.json` | SMA-200 filter OOS validation |
| `backtest/results/oos_wave1_asx_reopt.json` | ASX Wave 1 reopt OOS validation |
| `backtest/results/reopt_ibkr_constraints.json` | ASX backtest with IBKR fee constraints (combo: Sharpe -1.046; TF-only: 0.455) |
| `backtest/results/phase5_report.json` | Phase 5 optimization plan report |
| `backtest/results/fee_impact_analysis_20260226.json` | IBKR vs Moomoo fee drag comparison |
| `backtest/results/v92_oos_validation*.json` | v9.2 OOS validation results |
| `backtest/results/backtest_equity_curve.json` | Equity curve for most recent backtest |
| `backtest/results/reoptimization_full_universe.json` | Full universe coord descent output |
| `backtest/results/reopt_wave1_asx_reopt.json` | ASX Wave 1 reopt backtest output |

### Journal & Research Notes

| File | Description |
|---|---|
| [`journal/allocation_research.md`](../journal/allocation_research.md) | Allocation pool implementation: architecture, comparison results, activation guide |
| [`journal/hk_initial_backtest.md`](../journal/hk_initial_backtest.md) | HK first backtest: Sharpe 0.82, 58 trades, PF 2.36, MaxDD 2.7% (unoptimized) |
| [`journal/allocation_research.json`](../journal/allocation_research.json) | Raw comparison backtest data (no-pool vs hard vs soft, all identical when not binding) |
| [`journal/decision_journal.json`](../journal/decision_journal.json) | Human approval decisions (plan approvals, config promotions) |
| [`journal/trade_ledger.json`](../journal/trade_ledger.json) | All trades (live + paper) with fills, fees, P&L |
| [`docs/sp500_backtest_plan.md`](sp500_backtest_plan.md) | Original 7-phase SP500 optimization plan with code change specs and acceptance criteria |
| [`docs/OPTIMIZATION_GUIDE.md`](OPTIMIZATION_GUIDE.md) | Version history table, key learnings per version, optimization procedure, validation checklist |

---

## 5. Operational Runbooks

### Daily Workflow

```bash
# 1. Morning — Pre-market
python3 scripts/cli.py -m sp500 ingest        # Refresh market data
python3 scripts/cli.py -m sp500 universe       # Rebuild universe (if needed)
python3 scripts/cli.py -m sp500 plan           # Generate today's trade plan
python3 scripts/cli.py -m sp500 approve        # Review + approve plan

# 2. Execute approved plan (live)
python3 scripts/cli.py -m sp500 live-run       # Execute via Moomoo broker

# 3. Evening — Post-close
python3 scripts/eod_settlement.py --market sp500  # EOD settlement + equity curve
scripts/refresh_dashboard.sh                       # Refresh dashboard

# 4. Monitoring
python3 scripts/health_check.py               # Degradation check (exit 0=healthy, 1=degraded)
python3 scripts/cli.py status                 # Portfolio state
python3 scripts/cli.py -m sp500 orders        # Open orders
python3 scripts/cli.py -m sp500 broker        # Broker connection + account info
```

### Emergency Procedures

```bash
# Emergency halt — cancel all open orders
python3 scripts/cli.py halt

# Reconcile local state with broker
python3 scripts/cli.py sync

# Recovery after crash
scripts/pi-cron.sh recover postclose sp500

# Watchdog restart
scripts/auto_recover.sh
```

### Re-Optimization (when health check flags degradation)

```bash
# Full re-optimization (~2 hours, 8 cores)
python3 scripts/reoptimize_parallel.py --market sp500

# OOS validation (3 tests required before promotion)
python3 scripts/validate_oos_parallel.py --market sp500

# Automated full pipeline: health → reoptimize → validate → update
python3 scripts/auto_reoptimize.py
```

See [`docs/OPTIMIZATION_GUIDE.md`](OPTIMIZATION_GUIDE.md) for the full 3-test validation checklist.

### Broker Setup & Connectivity

| Broker | Startup | Port | Auth |
|---|---|---|---|
| Moomoo (SP500) | `python3 scripts/start_opend.py` | 11111 | Trade password via `~/.atlas-secrets.json` |
| IBKR (ASX/HK) | `docker start ib-gateway` | 4001 | 2FA once per week via IBC |

```bash
# Connectivity tests
python3 scripts/test_moomoo.py          # Moomoo API test
python3 scripts/test_ibkr.py            # IBKR IB Gateway test
python3 scripts/test_live_plumbing.py   # Full live plumbing safety test

# Credential setup
python3 scripts/cli.py setup-secrets    # Interactive credential setup
python3 scripts/setup_moomoo_login.py   # Moomoo-specific credential setup
```

### Cron Schedule

Managed by `scripts/pi-cron.sh` (Pi agent wrapper):

```
# SP500 (US market — overnight AEST time)
Pre-market:   Mon-Fri 07:30 AEST  → ingest, plan, Telegram summary
Post-close:   Tue-Sat 06:00 AEST  → EOD settlement, dashboard, Telegram report

# ASX (AU market — daytime AEST)
Pre-market:   Mon-Fri 08:30 AEST
Post-close:   Mon-Fri 17:30 AEST

# Research (both markets closed)
Research:     Mon-Fri 09:00 AEST  → research_runner.py

# Maintenance
Health:       Weekly              → healthz_cron.sh
Maintenance:  Sunday 06:00 AEST   → weekly_maintenance.sh
```

> **Note:** US Friday session = Saturday AEST. Post-close crons use `2-6` (Tue-Sat), not `1-5`.

### Dashboard

```bash
python3 scripts/serve_dashboard.py         # Start dashboard HTTP server
python3 dashboard/generate_data.py         # Regenerate dashboard-data.json
scripts/refresh_dashboard.sh               # Full refresh (generate + serve)
```

Dashboard data: [`dashboard/data/dashboard-data.json`](../dashboard/data/dashboard-data.json)

### Logs & Monitoring

| Path | Description |
|---|---|
| `logs/eod_summary_{date}.json` | EOD settlement results by date |
| `logs/equity_curve_sp500.json` | SP500 equity curve (all sessions) |
| `logs/equity_curve_asx.json` | ASX equity curve |
| `logs/health_check_{date}.json` | Health check degradation snapshot |
| `logs/intraday/` | Intraday position snapshots per market per date |
| `logs/pi-cron-premarket-*.md` | Pi agent pre-market run log |
| `logs/pi-cron-postclose-*.md` | Pi agent post-close run log |

### Security

```bash
scripts/security_audit.sh    # Scan for hardcoded secrets, check file permissions
```

Credentials live in `~/.atlas-secrets.json` (never committed to git). Required keys:
`telegram_bot_token`, `telegram_chat_id`, `moomoo_trade_pwd`, `ibkr_account_id`.

---

## 6. Decisions & Lessons

### Decision & Memory Files

| File | Description |
|---|---|
| [`docs/DECISIONS.md`](DECISIONS.md) | 20+ major decisions with rationale, config impact, and lessons captured |
| [`memory/SUMMARY.md`](../memory/SUMMARY.md) | Live session state: market configs, positions, known issues, procedures, research state |
| [`tasks/lessons.md`](../tasks/lessons.md) | 29 accumulated lessons: backtesting, broker, code, ops, research process |
| [`tasks/todo.md`](../tasks/todo.md) | Current task tracker |

### Key Decisions Chronology

| Date | Decision | Config / File Impact |
|---|---|---|
| 2026-02-18 | Coord descent reopt + fix degenerate scorer (min_trades, PF cap) | v9.2 |
| 2026-02-18 | Reject v9.3 blend — blending ≠ robustness | v9.3 archived |
| 2026-02-27 | Remove paper trading layer; live broker is SoT | Architecture |
| 2026-02-27 | Abandon ASX Moomoo → SP500 primary live market | Broker pivot |
| 2026-02-27 | SP500 v2.0 US optimization (RSI(14) > RSI(2)) | v2.0 |
| 2026-02-27 | Continuous research pipeline (queue + journal + Pi) | `research/` |
| 2026-02-28 | IBKR via IB Gateway + ib_insync (IBeam abandoned) | `brokers/ibkr/` |
| 2026-02-28 | ASX Wave 1 reopt promoted | v9.3 |
| 2026-03-01 | SMA-200 filter: +47% Sharpe on SP500 | v2.1 |
| 2026-03-01 | VIX filter permanently rejected (destroys MR alpha) | — |
| 2026-03-02 | max_open_positions 10→15: +13% Sharpe | v2.2 |
| 2026-03-02 | ASX: TF-only at $3,999 (IBKR $12 round-trip = 2.4% drag) | v9.3 TF-only |
| 2026-03-02 | HK market added (SEHK, paper mode, Sharpe 0.82 baseline) | hk v1.0 |
| 2026-03-02 | Allocation pools (disabled) — unlock for strategy re-addition | `utils/allocation.py` |
| 2026-03-02 | Full codebase audit + all 5 CRITICAL fixes | All modules |

### Top 10 Lessons

1. **Position contention** is the bottleneck — always run combined test, not just solo
2. **Scoring: min_trades=15, PF cap=4.0** — prevents degenerate optimizer convergence
3. **Blending ≠ robustness** — the param landscape has one ridge; choose the peak
4. **A/B toggle reveals what coord descent hides** — test filters as toggles, not params
5. **VIX filter destroys MR alpha** — never apply to any portfolio containing mean_reversion
6. **Fee drag = minimum account size** — verify round_trip_fee / avg_position < 1%
7. **Broker offline → never write state** — guard all 7 write paths with `broker_data_valid`
8. **US Friday = Saturday AEST** — crons use `2-6` (Tue-Sat)
9. **OOS validation (3 tests) before ANY promotion** — time-split + perturbation + WF
10. **Control test often outperforms adding strategies** — max_pos 10→15 beat all dormant strategies

Full list: [`tasks/lessons.md`](../tasks/lessons.md)

### Audit Findings

| File | Description |
|---|---|
| [`audit/FULL_AUDIT.md`](../audit/FULL_AUDIT.md) | 90-file audit: 5 CRITICAL + 12 HIGH + 15 MEDIUM + 10 LOW issues |

**CRITICAL fixes applied (2026-03-02 swarm):**
- ✅ C1: Look-ahead bias — trailing stop/max_loss_cap now use T-1 close
- ✅ C3: `LivePortfolio.update_positions()` added
- ✅ C4: `get_today_deals()` added to BrokerAdapter + IBKRBroker
- ✅ C5: IBKR account ID moved from config to `~/.atlas-secrets.json`

**HIGH fixes applied:**
- ✅ H1: Sector concentration enforced in backtest `_simulate_day()`
- ✅ H3: MTF trailing stop tracks `highest_high` since entry
- ✅ H4: PaperPortfolio commission model matches backtest (`flat_fee_threshold`)
- ✅ H9: Stop-loss exits use MARKET orders
- ✅ H10: Moomoo trade unlock failure is now fatal
- ✅ H8/M15: Atomic writes for parquet cache and paper state files

**Open (deferred):** H2 (WF date indexing), H5/H6 (IBKR deal history), H11 (SP500 timezone fallback), M1 (strategy registry), M3 (config validation), M8 (yfinance timeout).

---

## 7. Scripts & Tools

### Daily Operations

| Script | Purpose | Runtime |
|---|---|---|
| `scripts/cli.py` | Main CLI: status, ingest, universe, plan, approve, live-run, orders, halt, sync, broker | — |
| `scripts/eod_settlement.py` | EOD: stop checks, PnL, equity curve, paper state update | ~30s |
| `scripts/intraday_monitor.py` | Real-time P&L + 🛡 stop indicators | continuous |
| `scripts/health_check.py` | 6-month performance degradation check (exit 0=healthy, 1=degraded) | ~90s |
| `scripts/telegram_notify.py` | Manual Telegram message sender | ~2s |
| `scripts/serve_dashboard.py` | Local dashboard HTTP server (port 8080) | continuous |

### Optimization & Validation

| Script | Purpose | Runtime |
|---|---|---|
| `scripts/reoptimize_parallel.py` | Coordinate descent reoptimization, 8-core parallel | ~2h |
| `scripts/reoptimize_full_universe.py` | Sequential coord descent (single process fallback) | ~45min |
| `scripts/validate_oos_parallel.py` | OOS 3-test suite: time-split + perturbation + WF (parallel) | ~30min |
| `scripts/validate_oos.py` | OOS validation, sequential | ~55min |
| `scripts/auto_reoptimize.py` | Full pipeline: health → reoptimize → validate → update config | ~2.5h |
| `scripts/anneal.py` | Self-annealing: compare realized vs expected, flag divergence | ~10min |
| `scripts/profile_backtest.py` | Backtest profiler: identifies hot paths in engine | ~5min |

### Research Pipeline

| Script | Purpose |
|---|---|
| `scripts/research_runner.py` | Experiment engine: reads queue → claims → dispatches → updates status + journal |
| `scripts/research_promote.py` | Promotion pipeline: validate candidate → write to `config/candidates/` → request approval |
| `scripts/seed_research_queue.py` | Seed `research/queue.json` with Wave 1 experiments |
| `scripts/wave_planner.py` | Plan next wave: analyze journal → propose experiments → write wave brief |
| `scripts/strategy_evaluator.py` | Single-strategy backtest on any market (used by `research_runner`) |
| `scripts/allocation_comparison.py` | Compare no-pools vs hard-pool vs soft-pool (Task #52 verification) |
| `scripts/position_allocation_research.py` | Allocation pool impact research (sequential) |
| `scripts/position_allocation_research_parallel.py` | Allocation pool research (parallel, 8-core) |

### Broker & Connectivity

| Script | Purpose |
|---|---|
| `scripts/test_moomoo.py` | Moomoo API connectivity + order placement test |
| `scripts/test_ibkr.py` | IBKR IB Gateway connectivity test |
| `scripts/test_live_plumbing.py` | Full live trading plumbing verification (all safety gates) |
| `scripts/start_opend.py` | Start/restart Moomoo OpenD daemon |
| `scripts/setup_moomoo_login.py` | Add Moomoo credentials to `~/.atlas-secrets.json` |

### Automation & Maintenance

| Script | Purpose |
|---|---|
| `scripts/pi-cron.sh` | Pi agent cron wrapper (pre-market + post-close, all markets) |
| `scripts/auto_recover.sh` | Watchdog: restart crashed processes, send Telegram alert |
| `scripts/healthz_cron.sh` | Weekly health check cron wrapper |
| `scripts/refresh_dashboard.sh` | Full dashboard refresh: generate data + restart server |
| `scripts/security_audit.sh` | Check for hardcoded secrets and bad file permissions |
| `scripts/weekly_maintenance.sh` | Log rotation, pycache purge, old log cleanup (Sunday 06:00) |

---

## 8. Data & State Files

### Broker State

| File | Market | Description |
|---|---|---|
| `brokers/state/live_sp500.json` | SP500 | Live broker-synced state: positions, equity curve |
| `brokers/state/live_asx.json` | ASX | Live broker-synced state |
| `brokers/state/sp500.json` | SP500 | Paper state (used for plan generation sizing) |
| `brokers/state/asx.json` | ASX | Paper state |
| `brokers/state/hk.json` | HK | Paper state |

### Trade Plans

| File | Description |
|---|---|
| `plans/plan_sp500_{date}.json` | SP500 trade plan (entries + exits + sizing) |
| `plans/plan_asx_{date}.json` | ASX trade plan |
| `plans/plan_{date}.json` | Legacy format (pre-per-market naming convention) |

### Universe & Processed Data

| File | Description |
|---|---|
| `data/processed/sp500/universe.json` | Filtered SP500 tradeable universe (~292 tickers) |
| `data/processed/asx/universe.json` | Filtered ASX tradeable universe (~248 tickers) |
| `data/processed/hk/universe.json` | Filtered HK tradeable universe (~120 tickers) |
| `data/processed/sector_map_sp500.json` | SP500 sector map (204 tickers, 11 GICS sectors) |
| `data/processed/sector_map.json` | ASX sector map |
| `data/cache/{market}/{ticker}.parquet` | Per-ticker OHLCV history (atomic writes, file-locked) |
| `data/cache/earnings/{ticker}_earnings.json` | Earnings date cache (used to avoid entries pre-announcement) |

### Position Monitor

| File | Description |
|---|---|
| `data/position_monitor/positions.json` | Current position snapshot with P&L for monitoring |
| `data/position_monitor/templates.json` | Telegram alert templates for position events |

---

## 9. Pi Skills & Extensions

### Skills (autonomous operation workflows)

| Skill | SKILL.md | When to use |
|---|---|---|
| **atlas-daily** | `pi-package/atlas-ops/skills/atlas-daily/SKILL.md` | Day-to-day trading: ingest → plan → approve → execute → EOD |
| **atlas-healthz** | `pi-package/atlas-ops/skills/atlas-healthz/SKILL.md` | Full system audit (infra, data, config, broker, portfolio, cron, disk) |
| **atlas-reoptimize** | `pi-package/atlas-ops/skills/atlas-reoptimize/SKILL.md` | Degradation → re-optimize → validate → promote with human approval |
| **atlas-research** | `pi-package/atlas-ops/skills/atlas-research/SKILL.md` | Ad-hoc research experiments and validation runs |
| **atlas-research-loop** | `pi-package/atlas-ops/skills/atlas-research-loop/SKILL.md` | Daily research cycle: researcher → backtester → analyst → risk |

### Extensions (Pi TUI integrations)

| Extension | Path | Purpose |
|---|---|---|
| **atlas-artifacts** | `pi-package/atlas-ops/extensions/atlas-artifacts/` | View/diff backtest results + config artifacts in Pi TUI |
| **atlas-jobs** | `pi-package/atlas-ops/extensions/atlas-jobs/` | Run named Atlas scripts as jobs from Pi TUI |
| **atlas-risk-gates** | `pi-package/atlas-ops/extensions/atlas-risk-gates/` | Risk gate checks surfaced in Pi TUI before execution |
| **atlas-state** | `pi-package/atlas-ops/extensions/atlas-state/` | Live portfolio state display in Pi TUI |

### Helper Scripts (used by skills)

| File | Description |
|---|---|
| `pi-package/atlas-ops/skills/atlas-healthz/scripts/healthz.py` | Health audit runner: checks all components, outputs structured report |

---

## 10. Glossary

| Term | Definition |
|---|---|
| **AEST** | Australian Eastern Standard Time (UTC+10). All cron times in AEST unless stated. |
| **Allocation Pool** | Per-strategy position slot cap. Hard pool = strict cap. Soft pool = cap + overflow. Currently disabled (`enabled=false`). See `utils/allocation.py`. |
| **ATR** | Average True Range — daily volatility measure. Used for stop sizing and entry confirmation. |
| **BB Squeeze** | Strategy: Bollinger Bands inside Keltner Channel signals low-volatility compression before explosive moves. |
| **CAGR** | Compound Annual Growth Rate — annualized percentage return. |
| **Claimed** | Experiment status: a worker has reserved the experiment to prevent double-pickup. |
| **Coord descent** | Coordinate descent optimization — per-parameter iterative tuning used in `reoptimize_parallel.py`. |
| **DD / MaxDD** | Drawdown / Maximum Drawdown — peak-to-trough equity decline. |
| **EOD** | End-of-Day settlement: stop checks, P&L updates, equity curve append, state write. |
| **Hard pool** | Allocation mode: strategy is strictly blocked once it hits its per-strategy cap. |
| **IB Gateway** | Interactive Brokers trading gateway (Docker container, `ghcr.io/gnzsnz/ib-gateway:stable`). Port 4001. Used for ASX + HK. |
| **IBeam** | IB REST API wrapper — abandoned (session auth loop bug, `authenticated=False` always). |
| **IBS** | Internal Bar Strength = (close - low) / (high - low). Low IBS = intrabar selling pressure = MR entry signal. |
| **IS / OOS** | In-Sample / Out-of-Sample. IS = training data. OOS = unseen forward data for validation. |
| **IBKR** | Interactive Brokers. Broker for ASX (LIVE) and HK (PAPER). Client IDs: ASX=10, HK=12. |
| **live_enabled** | Config flag. `false` = paper-only mode. HK currently `false`. |
| **MR** | Mean Reversion — buys oversold pullbacks in uptrending stocks. RSI(14) + z-score signals. |
| **OG** | Opening Gap — fades or follows significant overnight gaps. 1–3 day hold. |
| **OpenD** | Moomoo's local API gateway daemon. Must be running for SP500 ops. Port 11111. |
| **OOS ratio** | OOS Sharpe / IS Sharpe. Preferred > 0.7. Ratio > 1.0 = OOS outperforms IS. |
| **PF** | Profit Factor = gross profit / gross loss. PF > 1.0 = profitable. Target > 1.3. |
| **Perturbation test** | Stability check: apply ±15% noise to all params 10 times, measure CAGR distribution. Robust configs survive. |
| **Pi** | [Pi coding agent](https://github.com/mariozechner/pi-coding-agent). Drives autonomous cron operations. |
| **Promotion** | Moving a validated candidate config to `config/active/`. Requires 3 OOS tests + human approval. |
| **RSI** | Relative Strength Index — momentum oscillator. RSI(14) used in MR. RSI(2) tested but rejected (too noisy). |
| **SEHK** | Stock Exchange of Hong Kong. |
| **Sharp peak** | Optimizer converged to a narrow, unstable parameter configuration — the bad outcome. Opposite: robust plateau. |
| **Signal flood** | When a high-volume strategy (momentum_breakout: 460 trades) monopolizes position slots, starving others. |
| **SMA-200** | 200-day Simple Moving Average trend filter. Blocks entries when price < SMA(200). Added in v2.1 (SP500) and v9.3 (ASX). |
| **Soft pool** | Allocation mode: when strategy's own pool is full, it can borrow from `_other` overflow pool. |
| **SoT** | Source of Truth. Live broker state is SoT — always overrides local state. |
| **TF** | Trend Following — rides MA crossover breakouts with ATR trailing stop. 5–20 day hold. |
| **Universe** | Filtered set of liquid, tradeable tickers for a given market after price/volume/sector filters. |
| **VIX** | CBOE Volatility Index. Rejected as a filter for any portfolio with MR (MR profits from high-VIX panic entries). |
| **Walk-forward (WF)** | Backtest method: train on window N, test on window N+1, step forward. More robust than single split. |
| **WF window win rate** | % of walk-forward windows with positive P&L. Target > 50%, prefer > 70%. |
| **Wave** | A themed research batch. Wave 1 = dormant strategy activation. Wave 2 = volume filter + MTF fixes. |
| **Williams VIX Fix** | Stock-level VIX proxy using price range — `calc_wvf()` in `utils/helpers.py`. No index data needed. |
| **Z-score** | Standard deviations from rolling mean. Used in MR to identify statistically oversold entries. |

---

## 11. Navigation Guide — "How do I…"

### Understand the system

| Question | Go to |
|---|---|
| How does the whole thing work? | [`README.md`](../README.md) → How it works + Project structure |
| What markets are live right now? | [`memory/SUMMARY.md`](../memory/SUMMARY.md) → System State table |
| Why was decision X made? | [`docs/DECISIONS.md`](DECISIONS.md) → search by date or topic |
| What patterns should I not repeat? | [`tasks/lessons.md`](../tasks/lessons.md) |
| What bugs were found and fixed? | [`audit/FULL_AUDIT.md`](../audit/FULL_AUDIT.md) |
| How does the research pipeline work? | [`research/README.md`](../research/README.md) |

### Run something

| Task | Command |
|---|---|
| Pre-market workflow | `python3 scripts/cli.py -m sp500 ingest && python3 scripts/cli.py -m sp500 plan` |
| Approve and execute | `python3 scripts/cli.py -m sp500 approve && python3 scripts/cli.py -m sp500 live-run` |
| Portfolio status | `python3 scripts/cli.py -m sp500 status` |
| Emergency halt | `python3 scripts/cli.py halt` |
| EOD settlement | `python3 scripts/eod_settlement.py --market sp500` |
| Health check | `python3 scripts/health_check.py` |
| Re-optimize (degraded) | `python3 scripts/reoptimize_parallel.py --market sp500` |
| Validate OOS | `python3 scripts/validate_oos_parallel.py --market sp500` |
| Run next experiment | `python3 scripts/research_runner.py` |
| Test broker | `python3 scripts/test_moomoo.py` or `python3 scripts/test_ibkr.py` |

### Check a config

| Question | File |
|---|---|
| Current SP500 params? | [`config/active/sp500.json`](../config/active/sp500.json) |
| Current ASX params? | [`config/active/asx.json`](../config/active/asx.json) |
| What were last version's params? | `config/versions/sp500_v2.1.json` etc. |
| Is SMA-200 on? | `strategies.*.sma200_filter` in active config |
| What's the max positions cap? | `risk.max_open_positions` in active config |
| Are allocation pools active? | `allocation.enabled` (currently `false` everywhere) |
| What are the IBKR fee settings? | `config/active/asx.json` → `fees` section |

### Work with experiments

| Task | Steps |
|---|---|
| Add new experiment | Edit `research/queue.json`, add entry with `status: "queued"` and falsifiable hypothesis |
| Run pending experiments | `python3 scripts/research_runner.py` |
| View all experiment results | `research/queue.json` (status field) + `research/experiments/exp-*.json` |
| See what we learned | `research/experiments/exp-{id}.json` → `learnings` field |
| See why an exp failed | `research/experiments/eval-{id}.json` → `verdict` + `rationale` fields |

### Promote a config

1. Run 3 OOS tests: `python3 scripts/validate_oos_parallel.py --market sp500`
2. All must pass: OOS Sharpe > 0, perturbation stability (< 20% negative), WF win rate > 50%
3. Snapshot current: `cp config/active/sp500.json config/versions/sp500_pre_{action}_{YYYYMMDD}.json`
4. Run `python3 scripts/research_promote.py` → writes to `config/candidates/`
5. Review Telegram, copy candidate to `config/active/` after human approval

### Debug a broker issue

| Symptom | Check |
|---|---|
| Moomoo won't connect | Run `python3 scripts/test_moomoo.py` + verify OpenD is running on port 11111 |
| IBKR won't connect | Run `python3 scripts/test_ibkr.py` + verify IB Gateway Docker is running |
| Orders failing silently | Check trade unlock (Lesson #14) — `unlock_trade()` must succeed |
| Wrong portfolio state | Run `python3 scripts/cli.py sync` to reconcile from broker |
| State wiped after restart | Check `broker_data_valid` guard — Lesson #12 (never write state when broker offline) |

### Add a new market

1. Create `markets/{id}.py` implementing `MarketProfile` (see `markets/base.py` + `markets/hk.py`)
2. Add `config/active/{id}.json` (set `live_enabled=false` until OOS validated)
3. Run initial backtest: `python3 scripts/cli.py -m {id} backtest`
4. Log results in `journal/{id}_initial_backtest.md`
5. Run optimization: `python3 scripts/reoptimize_parallel.py --market {id}`
6. Validate: `python3 scripts/validate_oos_parallel.py --market {id}`
7. Set `live_enabled=true` and configure broker after passing 3 OOS tests

---

*This index covers all 90+ Python source files, 5+ Pi skills, 4 extensions, 20+ config snapshots,*
*28+ research experiments, 15+ backtest result files, and all documentation in the Atlas project.*
*To maintain: add new entries as files are created. One line per file is sufficient.*
