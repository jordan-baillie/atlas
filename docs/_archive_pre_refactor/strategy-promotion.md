# Strategy Promotion — Lifecycle Reference

## Overview

Atlas strategies move through a defined lifecycle before trading with real capital:

```
RESEARCH → PAPER → LIVE → RETIRED
```

| State | Meaning |
|-------|---------|
| **RESEARCH** | Actively being backtested / parameter-swept. No live or paper orders placed. |
| **PAPER** | Trading against a paper (simulated) Alpaca account. Real signals, fake money. |
| **LIVE** | Trading real capital against the live Alpaca account. |
| **RETIRED** | Decommissioned. No new signals generated. Historical records preserved. |

The purpose of the PAPER phase is to verify that a strategy's real-time, forward-looking
performance is consistent with its backtested results — before capital is at risk.

---

## The Promotion Bar (PAPER → LIVE)

The following four gates **must all pass** before a strategy is eligible for promotion
from PAPER to LIVE.  These are the *minimum* criteria.  The automated promotion system
in `scripts/auto_promote_paper_to_live.py` adds stricter gates on top (see below).

### Gate 1 — ≥30 Calendar Days in PAPER

```
days_in_paper ≥ 30
```

**Rationale**: Thirty days ensures the strategy has been exposed to multiple
short-term market regimes (at minimum one market-open/close cycle per weekday and
at least one weekly options expiry). A shorter window can capture only a single
market phase, making it impossible to distinguish luck from edge.

### Gate 2 — ≥10 Closed, Non-Superseded Paper Trades

```
trade_count ≥ 10  (status='closed' AND superseded=0)
```

**Rationale**: With fewer than 10 trades the per-trade Sharpe is statistically
meaningless.  The law of large numbers needs at least this many independent
observations before the mean return is a reliable estimate of expected value.

### Gate 3 — Paper Trade-Level Sharpe ≥ 0.3

```
mean(pnl_pct) / stdev(pnl_pct) ≥ 0.3
```

**Rationale**: Computed per-trade (not annualised), this is a conservative floor
that ensures the strategy has a positive risk-adjusted return profile.  A Sharpe
of 0.3 corresponds roughly to a modestly positive win-adjusted reward-to-risk.
Below zero means the strategy is losing money on average relative to its
trade-level volatility.

### Gate 4 — Research Agreement: |paper_sharpe − research_sharpe| < 0.5

```
abs(paper_sharpe − research_sharpe) < 0.5
```

**Rationale**: The paper Sharpe must not diverge too far from the backtest
Sharpe recorded in `research_best`.  A delta ≥ 0.5 suggests overfitting,
data-snooping, or market-regime shift severe enough to invalidate the backtest
hypothesis.  This gate catches strategies where research looked good but
real forward performance disagrees materially.

---

## Automated vs Manual Promotion

### Minimum bar (this document)

The four gates above are the *floor* — the minimum evidence required for any
promotion to be considered.

### Auto-promote gates (stricter)

`scripts/auto_promote_paper_to_live.py` implements **nine gates (A–J)** that
are evaluated automatically every Monday 08:00 AEST.  Additional requirements
include:

- **≥30 paper trades** (vs the documented minimum of 10)
- **OOS Sharpe, trade count, CAGR, max-DD** gates (F–I), populated by the
  backtesting pipeline via `scripts/backfill_oos_metrics_research_best.py`
- **Divergence streak gate (J)**: no active research-vs-paper breach streak
  (from `data/divergence_state.json`)

When auto-promote gates fail, the system logs the reason to `data/promotion_log.json`
and optionally sends a Telegram alert.  A human operator can still manually
promote via the Controls UI or the lifecycle API (`POST /api/strategy-lifecycle/transition`),
but the automated path enforces the stricter gates to protect capital.

---

## Reading the Dashboard Panel

The **Paper Trading Progress** section in the Research tab (bottom of page) renders
a table identical to the `--markdown` CLI output.  Columns:

| Column | Meaning |
|--------|---------|
| Strategy / Universe | `strategy_name / universe` |
| Days | Calendar days since `paper_start_date` (or `entered_state_at` if NULL) |
| Trades | Closed, non-superseded paper trades |
| Sharpe | Per-trade `mean(pnl_pct) / stdev(pnl_pct)` — `—` if < 2 trades |
| Δ Research | `paper_sharpe − research_sharpe` — `—` if either is unknown |
| Gates | ✓/✗ for each of the four gates |
| Status | One of: 🟢 Ready · 🟡 In Progress · 🔴 Failing · ⚪ Insufficient Data |

### Status definitions

| Status | Condition |
|--------|-----------|
| 🟢 **Ready** | All four gates pass |
| 🟡 **Progressing** | At least one of days/trades passes but not all gates |
| 🔴 **Failing** | Enough data (days ≥30 AND trades ≥10) but Sharpe is below threshold |
| ⚪ **Insufficient Data** | Neither days nor trades threshold met yet |

---

## Worked Example — `mean_reversion / sp500`

As of 2026-05-19, `mean_reversion` entered PAPER state on 2026-05-14.
The `paper_trades` table is empty (0 rows — the paper executor is pending PREREQ 2).

CLI output:
```
$ python3 scripts/paper_progress_cli.py --markdown

## 📊 Paper Strategy Progress  ·  2026-05-19 ... UTC

_Promotion bar: ≥30d in paper · ≥10 trades · Sharpe ≥0.3 · |Δ research| < 0.5_

| Strategy       | Universe | Days | Trades | Sharpe | Δ Research | Status                | Gates                   |
|----------------|----------|-----:|-------:|-------:|-----------:|-----------------------|-------------------------|
| connors_rsi2   | sp500    |    5 |      0 |      — |          — | ⚪ Insufficient Data  | ✗30d ✗10tr ✗Sh ✗Δ      |
| mean_reversion | sp500    |    5 |      0 |      — |          — | ⚪ Insufficient Data  | ✗30d ✗10tr ✗Sh ✗Δ      |
| short_term_mr  | sp500    |   13 |      0 |      — |          — | ⚪ Insufficient Data  | ✗30d ✗10tr ✗Sh ✗Δ      |
```

Once the paper executor (PREREQ 2) begins generating closed paper trades, the Gates
column will fill in and status will advance from `insufficient_data` → `progressing`
→ `ready` (or `failing`).

---

## Demotion Criteria (LIVE → PAPER or RETIRED)

A LIVE strategy may be demoted under any of the following conditions:

1. **Sustained negative Sharpe**: Sharpe drops below 0.0 over a rolling 30-trade window
   and remains there for 5 consecutive check cycles (evaluated by
   `scripts/check_live_research_divergence.py`).
2. **Divergence breach**: The live vs research Sharpe gap exceeds the threshold for
   5 consecutive days (auto-rollback to RESEARCH state for PAPER strategies;
   Telegram escalation for LIVE strategies — see `docs/sub-phase-1.4-lifecycle.md`).
3. **Operator decision**: Manual demotion via the Controls tab or
   `POST /api/strategy-lifecycle/transition` with `{"state": "RETIRED"}`.

Demotion does NOT automatically close open live positions.  The operator must decide
whether to close positions immediately or let them run to their stop/TP.

---

## Reference

| File | Purpose |
|------|---------|
| `services/paper_progress.py` | Core gate computation |
| `scripts/paper_progress_cli.py` | CLI / Telegram digest |
| `scripts/auto_promote_paper_to_live.py` | Strict automated gates (A–J) |
| `scripts/check_live_research_divergence.py` | Divergence rollback logic |
| `docs/sub-phase-1.4-lifecycle.md` | Full lifecycle state machine design |
| `monitor/strategy_lifecycle.py` | Lifecycle state transition helper |
| `db/schema.sql` (line 598) | `strategy_lifecycle` table schema |
| `db/schema.sql` (line 633) | `paper_trades` table schema |
