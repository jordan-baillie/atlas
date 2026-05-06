# Research System Audit — 2026-05-06

**Status:** CONDITIONALLY BROKEN — FIXABLE, NOT FUNDAMENTAL  
**Auditor:** Planning Lead (with 4 parallel Research Analyst dispatches)  
**Scope:** Entire research subsystem — sweep engine, discovery loop, brain, promotion gates, live integration  
**Trigger:** ETF universes lost $173 live despite research approval; user has lost confidence  
**Source files:** Parts A+B (`_partial_A_B_audit_2026-05-06.md`), Part C (`audit-partial-C-methodology.md`), Part D+E (inline summary), Trust Score (`atlas-trust-score-audit-2026-05-06.md`)

---

## Executive Summary

The Atlas research system is not fundamentally broken, but it failed the ETF universes in four compounding ways that turned theoretical edge into realized losses. The walk-forward engine has no look-ahead bias. Point-in-time survivorship is implemented for sp500. The OOS gate exists and is blocking bad promotions today. The foundation is defensible.

What failed: (1) the two ETF universes were activated by direct `git commit` config edits, bypassing all four promotion gates entirely — the gates were never invoked; (2) all ETF research experiments are in-sample parameter sweeps with `experiment_type='sweeper'` and `window_coverage_pct=100.0`, meaning the headline Sharpe figures (1.316 for commodity_etfs momentum_breakout) are the peak of 150 cherry-picks on the same 7-year dataset, not validated edge; (3) both universes went live with fewer than 25 total experiments, zero paper-trade phase, and into a regime event (April tariff shock) sitting at the extreme right tail of the training distribution; and (4) when the live results came in showing 0/4 win rate and -$108.69 on momentum_breakout, no automated alert fired — the regime degradation circuit breaker does not cover cumulative portfolio drawdown, and the health cron only runs `--market sp500`.

The smoking gun: `commodity_etfs | momentum_breakout` — research Sharpe 1.316, live trade-level Sharpe -3.66, profit factor 0.00, win rate 0%, total loss -$108.69, **and** the research training data ends April 16, predating the very live trades that proved it wrong. Research is sweeping over a window that does not include the failure regime.

**Top 3 findings:** (1) ETF activation bypassed all gates via direct git commit — the four-gate pipeline that ARCHITECTURE.md describes was never run for either ETF universe; (2) the DSR multiple-comparisons gate is disabled in practice for any deployment with >5,000 experiments — the code documents this as "audit item O12" and silently skips it; (3) the CAGR degradation gate is mathematically broken for ETF universes — degradation values of -321% to -1863% trivially pass the `> 50` threshold, so the gate never fires.

**Top 3 recommendations:** (1) Halt all live activation paths pending gate repair — fix the DSR cap, add an absolute Sharpe floor (≥0.5 IS, ≥0.3 OOS), add a trade-count floor (≥30 OOS), mandate a 30-day paper-trade phase before any live dollar; (2) Fix or kill the discovery pipeline — `browse_with_pi` has thrown JSON parse errors for 30+ days and returns 0 papers while burning 1,398 seconds nightly; (3) Add silent-failure detection to `autoresearch_nightly.py` — five ETF universes produced 0 rows for 8 days while systemd reported `exit code=0` and logs were 0 bytes.

---

## Verdict: B — Research Has Fixable Bugs (with strong C undertones)

The research system is not option A (functioning correctly — the ETF losses are within expected variance). The divergence between research Sharpe 1.316 and live Sharpe -3.66 is not noise; it is a methodology gap. It is also not option C (fundamentally broken — rebuild from scratch). The walk-forward is genuine, look-ahead bias is absent, and PIT survivorship is implemented. The sp500 system — where the gates were actually used — has 15 momentum_breakout live trades at PF 2.09 vs research 1.551 (trust=1.35).

The correct verdict is B with strong C undertones: the promotion gates have real structural flaws (DSR disabled, CAGR gate broken for low-CAGR universes, no absolute Sharpe floor, no trade count floor in OOS window), AND they were bypassed entirely for the ETF universes. The disaster is a hybrid — structural research weaknesses amplified by operator bypass of the (already-flawed) gates. Fixing the gates and enforcing their use is sufficient to prevent a recurrence. Scrapping the system is not warranted.

---

## Part A — What Is the Research System Supposed to Do?

### A1. Stated Mission (ARCHITECTURE.md)

The research system's claimed integration path, per `docs/ARCHITECTURE.md`:

```
Sweep → research_best (SQLite) → auto_promote() [4 gates] → config/active/<market>.json → live trading
```

The four promotion gates (`research/promoter.py`):
1. **Gate 1 — Cooldown:** 24-hour minimum between promotions per strategy
2. **Gate 2 — Regression:** Candidate must not degrade >10% on any metric vs current active params
3. **Gate 3 — Sanity bounds:** Sharpe > 0, CAGR > 0, ≥20 trades
4. **Gate 4 — OOS validation:** Time-split (80/20) + perturbation (±20%, 10 trials) + walk-forward. OOS Sharpe > 0, OOS PF > 1.0, perturbation pass rate ≥70%, CAGR degradation ≤50%.

ETF re-enable criteria (`ARCHITECTURE.md` line 1174):
> "sector_etfs and/or commodity_etfs research_best Sharpe ≥ 0.5 with passing OOS gates 1–4"

### A2. Claimed Cadence

- Nightly per-universe timers: 23:00–05:00 AEST (sp500 through crypto)
- Research continues in passive mode after live shutdown
- `research/program.md`: 12–20 experiments/hour, 100–150 per 8-hour session
- Acceptance criteria per queue schema: `{"min_sharpe": 0.3, "min_trades": 15, "max_dd_pct": 10}`

### A3. Documentation Currency

`docs/RESEARCH_SUMMARY.md` is Wave 1 era, last updated 2026-03-02. It contains no ETF content. `docs/KNOWLEDGE_INDEX.md` still references the old integration path (`research_runner.py → queue.json → research_promote.py → config/candidates/`); production uses `autoresearch_nightly.py → SQLite → promoter.py → config/active/`. The Wave 1 queue (`queue.json`) has 10 items at `queued` status — orphaned, not being consumed.

### A4. Integration Points

| Component | Claimed Role | Operational Status |
|-----------|-------------|-------------------|
| `research/sweep.py` | Nightly param sweep per universe | Running (7 universes) |
| `research/loop.py` | Keep/discard logic, brain updates | Running |
| `research/promoter.py` | 4-gate promotion to live config | Running — but bypassed for ETF initial activation |
| `research/llm_loop_runner.py` | LLM creative param exploration | Timing out nightly on commodity_etfs |
| `research/discovery/` | Paper-browsing, new strategy proposals | DEAD — 0 papers for 30+ days |
| `research/brain/` | Accumulated knowledge, dead-zone avoidance | Active (brain/params read by sweeper) |
| `autoresearch_nightly.py` | Orchestration, timer dispatch | Running — silent failure detection absent |

---

## Part B — Is It Actually Running?

### B1. Sweep Cadence Per Universe

| Universe | Timer | Last Run | Failures (30d) | Log Status |
|----------|-------|----------|----------------|------------|
| sp500 | ✅ 23:00 AEST | May 5 | **2** (SIGTERM Apr 30 @ 9 min) | Normal |
| commodity_etfs | ✅ 00:00 | May 6 | 0 | **18/19 logs = 0 bytes** |
| sector_etfs | ✅ 01:00 | May 6 | 0 | **17/19 logs = 0 bytes** |
| gold_etfs | ✅ 02:00 | May 6 | 0 | **17/19 logs = 0 bytes** |
| treasury_etfs | ✅ 03:00 | May 6 | 0 | **17/19 logs = 0 bytes** |
| defensive_etfs | ✅ 04:00 | May 6 | 0 | **17/19 logs = 0 bytes** |
| crypto | ✅ 05:00 | May 6 | 0 | Normal (recent) |
| asx | ❌ NO TIMER | — | — | — |

### B2. The 8-Day Silent Failure (Apr 22–30)

Five ETF universes silently produced zero research rows for 8 consecutive days. Systemd reported `exit code=0`. Logs were 0 bytes. No Telegram alert fired. The DB confirms the gap:

```
Apr 22:    221 rows  ← SHARP DROP from ~1,987/day (−89%)
Apr 22–30: 127–322 rows  (only sp500 + partial commodity_etfs)
sector_etfs, gold_etfs, treasury_etfs, defensive_etfs, crypto: ABSENT
May 1+:    362–1,004 rows  (partial recovery, 5–7 universes)
```

The 0-byte log pattern is the root mechanism: something in the ETF sweep startup exits cleanly before writing a byte, and `autoresearch_nightly.py` has no assertion checking rows inserted per universe. This corresponds exactly to the April 22 universe-isolation fix commit — the fix may have introduced a silent early-exit for some universe configs.

### B3. Discovery Pipeline — Dead

`atlas-discovery.service` today: **1,398 seconds of wall clock, 0 papers, 0 specs, 0 strategy files generated.**

```
browse_with_pi error: json parse failed
discover_daily complete: found=0 filtered=0 specs=0 generated=0 passed=0 runtime=1398s
```

This error has occurred on every run for 30+ days. The `browse_blog.md` prompt file is missing — the paper-browsing code path is broken at the prompt level. The timer fires, the service runs for 23 minutes, burns CPU, and reports success. In `research/README.md` Wave 1, discovery is listed as a core research input. In practice it is ornamental.

### B4. LLM Loop Timeout

Every night on commodity_etfs:

```
00:30:36 [commodity_etfs] starting LLM loop (25 min)
01:00:40 [ERROR] llm_loop: Pi CLI timed out after 1800s
```

The loop allocates a 25-minute budget but hits the 1,800-second Pi CLI timeout. The LLM creative exploration path — intended to propose novel parameter combinations — produces zero output for commodity_etfs nightly. This is a separate failure from the sweep itself, but it means the only creative research input for this universe is dead.

### B5. DSR Gate — Disabled in Practice

Every sweep session emits this warning:

```
WARNING: DSR gate skipped — expected max Sharpe 5.81 exceeds sanity cap 3.00
         (n=24368, var=2.0375). See audit item O12.
```

`loop.py:379–403`: The DSR (Deflated Sharpe Ratio) gate computes expected maximum Sharpe across the experiment pool. Because it aggregates ALL strategies AND universes into one variance pool, the cross-universe Sharpe variance inflates the formula beyond the 3.0 sanity cap. The gate is silently skipped. With 25,831 total experiments in the pool, this is the primary multiple-testing protection — and it is inactive. The code comments reference "audit item O12" — this was a known issue before this audit.

### B6. research_best Currency Issues

From the `research_best` table as of 2026-05-06:

```sql
-- Negative Sharpe strategies with no floor against promotion
treasury_etfs | mean_reversion:       IS Sharpe = -1.582  (updated May 1)
treasury_etfs | momentum_breakout:    IS Sharpe = -1.557  (updated May 1)
defensive_etfs | momentum_breakout:   IS Sharpe = -0.465  (updated May 1)

-- Stale entries
sp500 | trend_following:              IS Sharpe =  0.662  (updated Mar 11 — STALE 56 days)
sector_etfs | connors_rsi2:           IS Sharpe =  0.299  (updated Apr 2  — STALE 34 days)
```

`sp500|trend_following` at 56-day staleness is a live-system concern: 3 live trades at PF=0.12 (trust=0.02) are running on 56-day-old research params. Treasury and defensive ETF strategies hold negative Sharpe in `research_best` with no hard floor preventing future promotion if mode is flipped to live.

### B7. Promotion Audit Trail — Broken

All 3 entries in `promotion_log.json` have `"oos_result": null`. OOS cache files exist but are not linked to the promotion records. Post-hoc audit of what OOS result was actually used to justify a promotion is impossible.

### B8. What IS Working (for completeness)

The following components are operating correctly and should not be changed:

| Component | Status | Evidence |
|-----------|--------|----------|
| Nightly timer schedule | ✅ Reliable | 7 universes firing on schedule, 0 cron failures in 30d |
| OOS gate blocking bad promotions | ✅ Working | commodity_etfs MB blocked May 5–6 (OOS -0.1646) |
| sector_etfs mean_reversion OOS | ✅ Passing | OOS Sharpe 1.22 — one ETF result that passes cleanly |
| sp500 research experiment volume | ✅ Active | 1,300+ experiments/week for mean_reversion alone |
| Brain dead-zone avoidance | ✅ Running | `param_history.py` read by sweeper each cycle |
| Walk-forward engine | ✅ Sound | Genuine rolling windows, no look-ahead bias |
| PIT survivorship (sp500) | ✅ Correct | `sp500_history.py` backward reconstruction verified |
| T+1 open fill discipline | ✅ Sound | `.shift(1)` throughout strategy code |
| Universe isolation fix | ✅ Deployed | Regression tests added Apr 22; confirmed clean |

The sp500 research system, where gates are used as designed, is producing results broadly consistent with research expectations. The problem is specific to: (a) ETF universes that bypassed the gates, and (b) structural gate flaws that would have passed some bad ETF configs even if the gates had been invoked.

### B9. Full research_best Table Snapshot (2026-05-06)

The complete `research_best` table — the source of truth for what research currently believes about each (universe, strategy) pair:

| Universe | Strategy | IS Sharpe | Solo Sharpe | Trades | Max DD% | PF | Updated | OOS Status |
|----------|----------|-----------|-------------|--------|---------|-----|---------|------------|
| commodity_etfs | connors_rsi2 | 0.9952 | 0.9207 | 556 | 17.74 | 1.538 | May 4 | Not recently tested |
| commodity_etfs | mean_reversion | 1.2018 | 1.1979 | 554 | 19.59 | 1.693 | May 4 | Not recently tested |
| commodity_etfs | momentum_breakout | **1.3160** | 0.8375 | 556 | 13.67 | **1.793** | May 5 | **FAILING (OOS -0.1646)** |
| sector_etfs | connors_rsi2 | 0.2987 | — | 859 | 0.0 | — | **Apr 2 (STALE 34d)** | Not tested |
| sector_etfs | mean_reversion | 0.4359 | 0.3397 | 180 | 9.68 | 2.365 | May 5 | **PASSING (OOS 1.22)** |
| sector_etfs | momentum_breakout | 0.3083 | 0.3766 | 196 | 13.44 | 1.670 | May 5 | Not tested |
| sp500 | connors_rsi2 | **0.1416** | 0.3686 | 219 | 20.37 | 1.236 | May 5 | — |
| sp500 | mean_reversion | 0.5677 | 1.0390 | 229 | 23.96 | 1.435 | May 1 | — |
| sp500 | momentum_breakout | 0.7490 | 0.7740 | 250 | 16.67 | 1.551 | May 5 | — |
| sp500 | opening_gap | 0.6016 | 0.3117 | 1106 | 29.94 | 1.262 | Apr 28 | — |
| sp500 | sector_rotation | 0.0442 | 0.4496 | 647 | 23.31 | 1.100 | Apr 13 | — |
| sp500 | trend_following | 0.6618 | 0.3643 | 228 | 7.88 | 6.688 | **Mar 11 (STALE 56d)** | — |
| sp500 | short_term_mr | 1.2726 | — | 294 | 24.88 | 1.713 | May 5 | — |
| gold_etfs | connors_rsi2 | 0.5220 | — | — | — | — | May 5 | **FAILING (OOS 0.000)** |
| gold_etfs | mean_reversion | 0.0650 | — | — | — | — | May 1 | — |
| gold_etfs | momentum_breakout | 0.3340 | — | — | — | — | May 1 | — |
| treasury_etfs | mean_reversion | **-1.582** | — | — | — | — | May 1 | ❌ NEGATIVE |
| treasury_etfs | momentum_breakout | **-1.557** | — | — | — | — | May 1 | ❌ NEGATIVE |
| defensive_etfs | mean_reversion | 0.2160 | — | — | — | — | May 1 | — |
| defensive_etfs | momentum_breakout | **-0.465** | — | — | — | — | May 1 | ❌ NEGATIVE |

Three immediate concerns visible in this table: (1) `sp500|connors_rsi2` IS Sharpe is 0.1416 — well below the 0.3 acceptance floor in `research/README.md` — yet it is running live at 50% weight; (2) three strategies hold negative IS Sharpe in `research_best` with no hard floor preventing live promotion if a universe is re-enabled; (3) `commodity_etfs|momentum_breakout` is IS Sharpe 1.316 (the research system's highest-confidence ETF result) while OOS has been failing since April 29 — the in-sample sweep is finding improvements faster than the OOS gate can block them.

---

## Part C — Is It Methodologically Sound?

### C1. Out-of-Sample Validation — Partial (MEDIUM)

The backtest engine (`engine.py:153–159`) implements genuine walk-forward:

```python
self.train_window = self.backtest_config.get("train_window_days", 252)   # 1-year train
self.test_window  = self.backtest_config.get("test_window_days",   63)   # 3-month test
self.step_days    = self.backtest_config.get("step_days",          21)   # 1-month step
```

Signals generated on day T close, filled at T+1 open (`engine.py` docstring line 14). This is sound.

The critical caveat: parameter *selection* happens across the full walk-forward history. `sweep.py` tries parameter values across the entire available dataset, then picks the best. There is no nested cross-validation separating "which params to select" from "how do they perform." The OOS gate in `validate_oos.py` runs only at promotion time — a single 80/20 time split plus 10 perturbation trials. Necessary but not sufficient for universes with fewer than 100 trades in the OOS window.

### C2. Transaction Costs — Sound for Liquid Names (LOW)

```json
"fees": {
  "commission_per_trade": 0,
  "commission_pct": 0,
  "slippage_pct": 0.0005,
  "slippage_model": "volume_aware",
  "slippage_impact_exponent": 0.5
}
```

Zero commission is correct for Alpaca equity/ETF trading. Volume-aware slippage (`engine.py:310–340`) uses 5bps base with participation-scaled impact. Reasonable for SPY-tier instruments. Concern: `CORN` (~300K ADTV), `DBA`, `UNG`, and `DBB` likely carry 10–30bps real round-trip spread. No universe-specific slippage override exists. Backtest may understate real costs by 2–5× for the illiquid tail of the commodity_etfs universe.

### C3. Regime Conditioning — Flawed (HIGH)

`regime_state` is recorded in every experiment row:

```sql
-- research_experiments table distribution (25,831 rows):
NULL                 → 11,698 (45.3%)
bull_risk_on         →  5,634 (21.8%)
recovery_early       →  7,149 (27.7%)
transition_uncertain →  1,350  (5.2%)
```

But `sweep.py`, `loop.py`, and `promoter.py` have **zero references** to `regime_state` in parameter selection logic. The DB has 25,831 experiments tagged by regime. Not one line of code reads that tag back for selection purposes. Best params are selected as the cross-regime optimum. For `commodity_etfs|mean_reversion`, `bull_risk_on` avg Sharpe = 0.756 vs `recovery_early` = 0.500 — the parameter optima differ materially by regime, and the system ignores this entirely.

The live regime filter in `sp500.json` is `"enabled": false`. It was disabled during April's worst macro selloff since 2020.

### C4. Universe Overlap / Signal Contamination — Sound (LOW)

The April 22 universe-isolation bug (all ETF sweeps running with sp500 config) is fixed. Regression tests added (`research/tests/test_universe_isolation.py`). Intentional overlaps (`GLD` in both `commodity_etfs` and `gold_etfs`; `XLP`/`XLU` in both `sector_etfs` and `defensive_etfs`) are documented. Residual concern: `CCJ` (Cameco) and `FCX` (Freeport-McMoRan) are **single equities**, not ETFs, carrying idiosyncratic earnings and regulatory risk that the backtest does not model differently from liquid ETFs.

### C5. Sample Size Adequacy — Flawed (HIGH)

```sql
-- research_experiments table
Total experiments:                      25,831
Experiments with trades=0 or NULL:       5,007  (19.4%)
Experiments with trades < 30:           12,334  (47.8%)
```

The keep/discard floor in `loop.py:363`:

```python
min_trades = max(10, int(b_trades * 0.7)) if b_trades > 0 else 10
```

Floor is 10. Reliable Sharpe estimation requires ≥30 trades. The gate allows experiments with 10–29 trades to be "kept" and influence the research_best. `promoter.py:_sanity_check()` at line 527 requires `num_trades >= 20` but this is the portfolio total, not per-strategy. A strategy with 5 trades can pass if the portfolio aggregate clears 20.

Worst offenders by universe:

| Universe | Strategy | Min N | Avg N |
|----------|----------|-------|-------|
| commodity_etfs | mean_reversion | **0** | 271 |
| crypto | mean_reversion | 25 | 26.9 |
| crypto | opening_gap | **0** | 13.0 |
| sector_etfs | momentum_breakout | **43** | 117 |

### C6. Multiple Comparisons / Overfitting — Partially Mitigated (HIGH)

Parameter search space in `sweep.py:238–398`:

```
mean_reversion:    10 params → 1,280,000 theoretical combinations
momentum_breakout:  7 params →     2,880 combinations
connors_rsi2:       8 params →    24,576 combinations
```

25,831 experiments against these spaces is a severe multiple-testing environment. The DSR gate exists to correct for this (see B5 above) but is inactive in practice. The promoter OOS perturbation test (10 trials, ±10–20% param perturbation) is the only live overfitting correction. No Bonferroni, no FDR correction, no PBO (Probability of Backtest Overfitting) — zero references to any of these in the codebase.

### C7. Survivorship Bias — Sound for sp500 (LOW)

```python
# engine.py:1338–1349
if self.config.get("universe", {}).get("point_in_time", False):
    from data.sp500_history import get_members_at_date
    pit_members = get_members_at_date(test_start.date())
    window_data = {t: df for t, df in window_data.items() if t in pit_members}
```

`data/sp500_history.py` reconstructs historical S&P 500 membership from `data/sp500_changes.csv`. Correctly eliminates the primary survivorship bias for sp500 backtests. ETF universes use static lists; ETFs rarely delist; no PIT mechanism needed. CCJ/FCX survivorship check recommended but not urgent.

### C8. Look-Ahead Bias — Sound (LOW)

T+1 open fill model throughout the codebase. `connors_rsi2.py:137`:

```python
current = df.iloc[-1]  # T-1 data only
```

`momentum_breakout.py:80–81`:

```python
df["_mb_lookback_high"] = close.rolling(self.lookback_days).max().shift(1)
df["_mb_avg_vol"] = volume.rolling(20).mean().shift(1)
```

Stop adjustment on gap (`engine.py:848–854`) scales the stop proportionally to the actual fill price and skips the trade if the fill is already below the adjusted stop. No look-ahead bias found. This is a genuine strength of the engine.

### C9. Live Execution Mismatch — Partial (MEDIUM)

Backtest fills at next-day open using actual parquet OHLCV open prices. Live orders are submitted as limit orders. Two mismatches: (1) gap-up non-fill — backtest fills at open, live does not fill if stock opens above limit price (overstates trade count and Sharpe for breakout strategies in bull markets); (2) confirmed by exit_reason data — nearly every ETF trade exits via `reconcile_fill` rather than `stop_loss` or `trailing_stop`, confirming strategy logic is not driving exits. The execution pipeline for ETF universes was not connected to `execute_approved` at all (see E3 below).

### C10. Brain Integration — Partial (MEDIUM)

`sweep.py:1098` imports `build_strategy_param_history` and uses it for dead-zone avoidance — parameter values tested ≥3 times with 0 wins are skipped. This is real and running. What is not wired: `brain/strategies/`, `brain/patterns/`, and `brain/decisions/` are read by the LLM runner for narrative context only. No code in `strategies/*.py` enforces brain constraints. `brain/state.json` was last updated 2026-03-12 (stale).

### Methodology Verdict Table

| Item | Topic | Verdict | Severity |
|------|-------|---------|----------|
| C1 | OOS Validation | ⚠️ Partial | MEDIUM — param selection IS-contaminated; single promoter split insufficient for small universes |
| C2 | Transaction Costs | ✅ Sound | LOW — illiquid ETF slippage minor concern |
| C3 | Regime Conditioning | ❌ Flawed | **HIGH** — regime tagged, never used for selection |
| C4 | Universe Overlap | ✅ Sound | LOW — isolation bug fixed, CCJ/FCX minor |
| C5 | Sample Size | ❌ Flawed | **HIGH** — 47.8% experiments <30 trades; floor is 10 |
| C6 | Multiple Comparisons | ⚠️ Partial | **HIGH** — DSR gate inactive; only promoter OOS test as guard |
| C7 | Survivorship Bias | ✅ Sound | LOW — PIT implemented for sp500 |
| C8 | Look-Ahead Bias | ✅ Sound | LOW — T+1 fill, .shift(1) throughout |
| C9 | Live Execution Mismatch | ⚠️ Partial | MEDIUM — limit order non-fill not modeled; ETF execution not wired |
| C10 | Brain Integration | ⚠️ Partial | MEDIUM — dead-zone avoidance runs; narrative brain not code-enforced |

---

## Part D — Is It Producing Actionable Intelligence?

### D1. sp500 Param Drift — Running But Stale in Live

Both `momentum_breakout` and `connors_rsi2` are in `research_best` for sp500. Params last synced to live config April 28. `research_best` updated again May 5. Live config was NOT updated. Result: 4 of 5 `momentum_breakout` params diverge between research_best and live config.

More critically: `sp500|connors_rsi2` has `research_best` Sharpe of **0.1416**. This sub-threshold result (below the 0.3 acceptance floor documented in `research/README.md`) is currently running live at 50% weight. The research says this strategy barely has edge on sp500, and live is deploying it anyway.

### D2. Discovery — Ornamental, Not Functional

In the last 90 days:
- 22 strategy Python files were generated by the discovery pipeline
- **Zero were adopted to live `strategies/`**
- All 20 new strategy files went to `research/strategies/` only — never promoted to `strategies/`
- 33 distinct strategies were experimented with in total
- `browse_blog.md` prompt is missing — the paper-browsing path that generates new strategy ideas is dead
- Daily cost: 1,398 CPU-seconds, 0 papers, 0 adopted strategies

The discovery pipeline is consuming resources and generating files that go nowhere. The `atlas-research-runner.service` (director queue executor) is disabled — queue items generated by the director are never consumed.

### D3. No Automated Disable Signal

The DEGRADED path in the health check requires 3 consecutive weekly failures before sending a disable signal. The health cron only runs `--market sp500`. ETF universes were not covered by automated health checks. Both ETF pauses (commodity_etfs, sector_etfs) were manual user decisions made by reading P&L. No automated monitoring of cumulative ETF losses exists. The commodity_etfs universe ran for approximately 2.5 weeks at -5.6% total return before the user noticed and shut it down. The system did not notice.

---

## Part E — What Specifically Failed for the ETF Universes?

### E1. SMOKING GUN — Same-Day Research and Live Deployment

`commodity_etfs` had its **first research experiment and first live dollar on the same day: April 16, 2026.** The first experiment batch returned Sharpe 0.003–0.106.

`sector_etfs` went live one day after research started with approximately 18 total experiments.

Neither universe ran a paper-trading phase. There is no paper-trade requirement in the promotion gates, in `ARCHITECTURE.md`, or in `program.md`. The concept of "paper trading before live" does not exist in the research system design.

For context: the commodity_etfs backtest data snapshot (`snapshots/commodity_etfs_20260417_7yr/`) ends **April 16, 2026** — the same day the first live dollar was deployed. Research is sweeping over a window that does not include the failure regime.

### E2. All Four Promotion Gates Bypassed for Initial Activation

The ETF universes were activated via direct `git commit` edits to `config/active/*.json` — the `"mode"` field changed from `"research"` to `"live"`. `auto_promote()` was never called. The four-gate validation system (`research/promoter.py`) was never invoked for the initial activation of either universe.

The 4-gate pipeline that `ARCHITECTURE.md` describes as the mandatory path from research to live:

```
Sweep → research_best → auto_promote() [Gates 1–4] → config/active/<market>.json
```

...was used for parameter updates on already-live strategies, but not for the go/no-go decision on whether a universe should go live at all. There is no "universe activation gate" — only parameter promotion gates. This is an architectural gap, not just operator error.

### E3. Warning Signals Were Visible and Ignored

Before deployment, the following signals were present in the research data:

1. **Sharpe variance of 3,290× across parameter space** for `commodity_etfs|momentum_breakout`. The best param set produced Sharpe 1.316; other configurations of the same strategy on the same data produced near-zero. This is the signature of overfitting, not edge.

2. **`connors_rsi2` live used `rsi_entry=40`** while research had found `rsi_entry=60` substantially better (research Sharpe 1.276 vs 1.116). The live config used sp500 default parameters rather than ETF-optimized parameters.

3. **Regime filter disabled** (`"enabled": false` in live config) during April's worst macro event since 2020. The regime filter was designed to pause trading in adverse macro conditions. It was off.

4. **`sector_etfs|momentum_breakout`** had only 43–47 trades in some backtest runs — below the minimum required for reliable Sharpe estimation.

None of these signals produced an automated alert. None of them blocked deployment. The gate system does not surface Sharpe variance across parameter space. The regime filter state is not validated before live activation. Trade count checks use a floor of 10, not 43.

### E4. No Paper-Trade Phase, No Divergence Monitoring

There is no paper-trade infrastructure in Atlas. The system goes from research to live with no intermediate validation against real market conditions. There is no `paper_trades` table, no paper-trade mode in the broker layer, and no documentation of a paper-trade requirement anywhere in the codebase.

Additionally: no divergence monitoring exists. There is no alert if live Sharpe diverges from research_best Sharpe by more than a threshold. There is no Telegram notification for cumulative universe losses. The circuit breaker covers intraday daily drawdown only — it does not cover multi-day portfolio decay.

### E5. Cross-Market Ghost Trades and Duplicate Accounting

Two of the five largest commodity_etfs losses — FCX -$31.95 (Apr 16) and FCX -$15.71 (Apr 24) — are associated with the state isolation bug fixed April 22. The `trades` table contains confirmed duplicate entries not marked `superseded=1`:

```sql
-- Non-superseded duplicates in trades table
FCX  commodity_etfs  connors_rsi2  Apr 24 AND Apr 29: -$15.71  (reconcile_fill_cached)
SLV  commodity_etfs  connors_rsi2  Apr 24 AND Apr 29: -$15.26  (reconcile_fill_cached)
UNG  commodity_etfs  connors_rsi2  Apr 24, Apr 29, Apr 30: +$14.57  (cached + orphan)
MU   sp500  momentum_breakout  Apr 28 AND Apr 29: -$18.04  (reconciled_orphan)
```

True commodity_etfs total loss is **-$127.79** across 8 unique real trades (4 momentum_breakout + 4 connors_rsi2), not -$173. The $173 incident figure includes duplicate accounting. Both numbers represent failure — the distinction matters for attributing causes accurately.

### E5-Detail. sector_etfs Trade Analysis

For completeness — sector_etfs momentum_breakout fired 4 real trades (after removing 2 phantoms and 1 duplicate):

| Date | Ticker | Entry | Exit | PnL $ | PnL % | Hold | Exit Reason |
|------|--------|-------|------|-------|-------|------|-------------|
| 2026-04-21 | XLY | 116.44 | 116.71 | +2.73 | +0.23% | 7d | reconcile_fill |
| 2026-04-23 | XLK | 156.77 | 157.27 | +3.99 | +0.32% | 5d | reconcile_fill |
| 2026-04-24 | XLI | 173.97 | 172.17 | **-16.20** | -1.03% | 11d | manual_close |
| 2026-05-01 | XLE | 59.06 | 59.35 | +2.28 | +0.48% | 4d | manual_close |
| **TOTAL** | | | | **-$7.19** | avg -0.0003%/trade | avg 6.75d | |

Win rate 75% (3/4) looks acceptable but conceals the risk asymmetry: the 1 losing trade (-$16.20) is 2× larger than the sum of all 3 winners (+$8.99 combined). This is the opposite of what a PF=1.67 strategy should produce. The "signal-starved" problem from the incident trigger is confirmed: 4 signals over 7 live trading days. XLY also produced 2 phantom trades (zero-PnL, tagged `reconcile_phantom`) — the signal fired but no actual fill occurred, which is consistent with the execution pipeline not being wired to `execute_approved` for sector_etfs.

### E6. Large Loss Summary — All Universes

Trades with PnL < -$10 across all universes, in PnL order (excluding confirmed duplicates):

| Date | Ticker | Universe | Strategy | Entry | Exit | PnL $ | PnL % | Hold | Exit Reason |
|------|--------|----------|----------|-------|------|-------|-------|------|-------------|
| Apr 15 | GLD | commodity_etfs | momentum_breakout | 442.80 | 420.71 | **-44.18** | -4.99% | 19d | manual_close |
| Apr 24 | ADI | sp500 | momentum_breakout | 403.88 | 383.83 | **-40.10** | -4.96% | 4d | reconcile_fill |
| Apr 16 | FCX | commodity_etfs | momentum_breakout | 68.03 | 61.64 | **-31.95** | -9.39% | 8d | reconcile_fill |
| Apr 23 | CCJ | commodity_etfs | momentum_breakout | 126.47 | 119.73 | **-26.96** | -5.33% | 5d | reconcile_fill |
| Apr 06 | OXY | sp500 | trend_following | 62.96 | 56.40 | -19.68 | -10.42% | 2d | trailing_stop |
| Mar 31 | NOW | sp500 | trend_following | 114.97 | 108.46 | -19.54 | -5.67% | 6d | trailing_stop |
| Apr 28 | MU | sp500 | momentum_breakout | 517.70 | 508.68 | -18.04 | -1.74% | 0d | reconcile_fill |
| Apr 06 | COP | sp500 | sector_rotation | 130.51 | 121.48 | -18.06 | -6.92% | 2d | trailing_stop |
| Apr 24 | XLI | sector_etfs | momentum_breakout | 173.97 | 172.17 | -16.20 | -1.03% | 11d | manual_close |
| Apr 24 | FCX | commodity_etfs | connors_rsi2 | 61.48 | 58.34 | -15.71 | -5.11% | 4d | reconcile_fill |
| Apr 24 | SLV | commodity_etfs | connors_rsi2 | 68.27 | 65.73 | -15.26 | -3.73% | 4d | reconcile_fill |
| Apr 14 | CARR | sp500 | momentum_breakout | 63.31 | 61.95 | -14.91 | -2.14% | 2d | reconcile_fill |
| Mar 31 | WM | sp500 | short_term_mr | 226.28 | 220.66 | -11.24 | -2.48% | 0d | stop_loss |

Note: FCX and SLV each appear again on Apr 29 as `reconcile_fill_cached` (duplicates) — excluded here. Commodity_etfs accounts for 5 of the 13 largest losses despite only 8 unique real trades.

### E-Summary Table — ETF Failure Modes

| Failure | Mechanism | Detectable in advance? | Detected? |
|---------|-----------|------------------------|-----------|
| Gates bypassed for initial activation | Direct git commit, no `auto_promote()` call | Yes — no gate log entry | No |
| Zero OOS experiments | `experiment_type='sweeper'`, `window_coverage_pct=100` | Yes — DB query | No |
| Research data ends at deployment date | Snapshot ends Apr 16 | Yes — file date check | No |
| No paper-trade phase | Feature does not exist in system design | N/A | N/A |
| Sharpe variance 3,290× (overfitting) | Max/min Sharpe ratio across param space | Yes — sweep logs | No |
| Regime filter disabled during crisis | `"enabled": false` in config | Yes — config check | No |
| Ghost trades from isolation bug | `universe` field mismatch in trades table | Yes — SQL join | No, post-hoc |
| No cumulative loss alert | Circuit breaker is intraday-only | Yes — code review | No |

---

## Trust Score Analysis

Full trust score table — live profit factor divided by research profit factor, for strategies with ≥4 live trades and a research record. Live trade-level Sharpe is not annualized; used for directional comparison only.

| Rank | Universe | Strategy | Research Sharpe | Research PF | Live Trade Sharpe | Live PF | Live N | Trust | Rating |
|------|----------|----------|----------------|-------------|------------------|---------|--------|-------|--------|
| 🔴 1 | commodity_etfs | momentum_breakout | **1.316** | 1.793 | **-3.66** | **0.00** | 4 | **0.00** | CATASTROPHIC |
| 🔴 2 | sp500 | trend_following | 0.662 | 6.688 | -1.67 | 0.12 | 3 | **0.02** | CATASTROPHIC |
| 🔴 3 | commodity_etfs | connors_rsi2 | 0.995 | 1.538 | -1.66 | 0.45 | 4* | **0.29** | POOR |
| 🟡 4 | sector_etfs | momentum_breakout | 0.308 | 1.670 | 0.00 | 0.56 | 4 | **0.34** | WEAK |
| 🟡 5 | sp500 | sector_rotation | 0.044 | 1.100 | 0.09 | 0.49 | 4 | **0.44** | WEAK |
| 🟢 6 | sp500 | opening_gap | 0.602 | 1.262 | 0.09 | 1.28 | 3 | **1.01** | NEUTRAL |
| 🟢 7 | sp500 | momentum_breakout | 0.749 | 1.551 | 1.32 | 2.09 | 15 | **1.35** | OUTPERFORM |
| 🟢 8 | sp500 | connors_rsi2 | 0.142 | 1.236 | 0.99 | 3.04 | 7 | **2.46** | OUTPERFORM |
| ⚠️ 9 | sp500 | mean_reversion | 0.568 | 1.435 | 5.47 | ∞ | 5 | N/A | INSUFF. |

\* commodity_etfs connors_rsi2 deduped from 7 rows to 4 unique trades after removing `reconcile_fill_cached` duplicates.

**Statistical caveat:** 3–15 live trades vs 196–1,106 research trades. No statistical conclusion should be drawn from this data alone. These are directional signals, not verdicts.

**The sp500 "outperformance" caveat:** sp500|momentum_breakout live PF=2.09 is driven by 3 outlier winners — AMD +$118.57, MRVL +$63.12, ON +$42.01. The remaining 12 trades sum to -$89.33 with 27% win rate and PF≈0.45. The apparent trust score of 1.35 does not represent repeatable edge. The sp500 system is working but concentration in 3 winners should not be mistaken for validated methodology.

**Why the research data cannot have "approved" the April 23 CCJ trade:**

The training snapshot (`snapshots/commodity_etfs_20260417_7yr/`) ends April 16, 2026. The CCJ trade that lost -$26.96 was entered April 23. Research is mathematically incapable of having modeled the post-shock recovery regime in which that trade was taken. The Sharpe 1.316 figure was computed on a dataset that ends before the failure. The research "approval" was of a different regime.

---

## Part F — Recommendations

### Recommendation 1: Halt All Live Activation Paths — Fix Gates Before Next Universe Goes Live
- **Effort:** Medium (3–5 days)
- **CEO Approval:** YES — involves research-to-live gate architecture, capital at risk
- **Description:**
  - Add an explicit **universe activation gate** separate from the parameter promotion gate. A universe cannot have its `"mode"` set to `"live"` without passing a checklist: minimum 500 experiments across at least 90 days, OOS Sharpe ≥0.3 on a held-out year, ≥30 OOS trades per strategy, and a completed 30-day paper-trade phase with daily divergence tracked.
  - Fix the CAGR degradation gate: replace `if cagr_degradation > 50` with `if oos_sharpe < 0.3` as the primary gate criterion. The existing formula produces values of -1863% for small-CAGR universes and trivially passes. The fix makes the gate meaningful for any universe size.
  - Add absolute Sharpe floors: IS Sharpe ≥0.5, OOS Sharpe ≥0.3 required as hard gates before any live activation. The current Gate 3 requires Sharpe > 0 — a near-zero bar.
  - Add absolute trade-count floor in OOS window: ≥30 trades per strategy (not portfolio total). Current: 10 total.
  - Fix DSR gate (audit item O12): compute variance per-strategy rather than cross-universe pool. This prevents cross-universe Sharpe variance from inflating the expected max beyond the sanity cap and re-activates the multiple-testing correction.
- **Why:** Both ETF universes bypassed all gates via direct config edit. The gates also have real flaws that would have let bad strategies through even if used. Both problems need simultaneous fixes.

### Recommendation 2: Fix or Kill the Discovery Pipeline
- **Effort:** Small (2–4 hours to fix; 30 minutes to disable)
- **CEO Approval:** NO — operational fix within research team's scope
- **Description:**
  - Diagnose `browse_with_pi` JSON parse failure. The `browse_blog.md` prompt file is missing — start there. Restore the file, verify one successful run produces ≥1 paper before re-enabling timer.
  - If no bandwidth to fix it within a week: `systemctl disable --now atlas-discovery.timer`. The pipeline has returned 0 papers for 30+ days. Disabling it stops wasting 1,398 CPU-seconds nightly and removes a source of false confidence (service reports "success").
  - Separately: re-enable `atlas-research-runner.service` or delete the 10 orphaned Wave 1 queue items from `queue.json` — they are dead noise.
- **Why:** 23 minutes of wasted nightly CPU, 22 generated strategy files in 90 days with zero adopted to live, and a broken prompt file. This is dead infrastructure being maintained at operational cost.

### Recommendation 3: Add Silent-Failure Detection to autoresearch_nightly.py
- **Effort:** Small (2–3 hours)
- **CEO Approval:** NO — operational monitoring fix
- **Description:**
  - After each universe sweep completes, assert minimum rows inserted:
    ```python
    rows_after = db.execute(
        "SELECT COUNT(*) FROM research_experiments WHERE market=? AND created_at > ?",
        (universe, run_start_ts)
    ).fetchone()[0]
    MIN_ROWS = {"sp500": 50, "commodity_etfs": 20, "sector_etfs": 20,
                "gold_etfs": 10, "treasury_etfs": 10, "defensive_etfs": 10, "crypto": 10}
    if rows_after < MIN_ROWS.get(universe, 10):
        logger.error("SILENT FAILURE: %s inserted %d rows (min %d)", universe, rows_after, MIN_ROWS[universe])
        send_telegram(f"⚠️ Research silent failure: {universe} wrote {rows_after} rows")
        sys.exit(1)  # Force systemd to report failure, not success
    ```
  - Test against the Apr 22-30 gap pattern before shipping.
- **Why:** Five ETF universes silently produced 0 rows for 8 days. Systemd reported success. No alert fired. During a live trading period this means 8 days of no research improvement on the strategies holding real money. The fix is 10 lines of code.

### Recommendation 4: Add Divergence Monitoring for Live vs Research Sharpe
- **Effort:** Small (3–4 hours)
- **CEO Approval:** NO — monitoring addition
- **Description:**
  - After each batch of ≥5 live trades per (universe, strategy), compute live trade-level Sharpe and compare to research_best Sharpe.
  - Alert via Telegram if: `live_sharpe < (research_sharpe - 0.5)` with ≥5 closed trades.
  - Alert via Telegram if: cumulative universe PnL drops below -2% of universe capital allocation.
  - Extend health cron to cover all active universes, not just sp500 (`--market` flag currently hardcoded).
- **Why:** Both ETF shutdowns were manual decisions made by the user reading P&L. commodity_etfs went 0/4 with -5.6% portfolio loss before action was taken. A -2% alert would have fired after trade 2 (GLD -4.99%). commodity_etfs momentum_breakout: research 1.316, live -3.66 — the divergence was -5.0 Sharpe units. An alert at threshold 0.5 would have fired immediately.

### Recommendation 5: Implement Regime-Conditioned Parameter Selection
- **Effort:** Medium (2–3 days)
- **CEO Approval:** NO — research methodology improvement, no architectural change
- **Description:**
  - Maintain separate `research/best/{strategy}__{regime}.json` per regime state, alongside the existing cross-regime best.
  - In `loop.py` parameter selection, query `research_experiments` filtered by `regime_state = current_regime` for regime-specific optima. Fall back to cross-regime best if <30 regime-specific experiments exist.
  - `promoter.py` already reads the current regime via `db.py:34–66`. Use it to select regime-appropriate params for OOS evaluation.
  - Initial cost: ~2 days of engineering. Maintenance cost: zero — the DB already has 25,831 tagged experiments.
- **Why:** The DB has 25,831 experiments tagged by regime. Not one line of code reads those tags for selection. `commodity_etfs|mean_reversion` shows 51% Sharpe difference between `bull_risk_on` (0.756) and `recovery_early` (0.500) regimes. The system deployed into `recovery_early` using parameters tuned on a `bull_risk_on`-heavy history. This is fixable with the data already in hand.

### Recommendations Summary

| # | Title | Effort | CEO Approval | Priority |
|---|-------|--------|-------------|----------|
| 1 | Halt activation paths, fix gates, mandate paper-trade | Medium | **YES** | URGENT |
| 2 | Fix or kill discovery pipeline | Small | No | High |
| 3 | Silent-failure detection in autoresearch_nightly.py | Small | No | High |
| 4 | Divergence monitoring (live vs research_best Sharpe) | Small | No | High |
| 5 | Regime-conditioned parameter selection | Medium | No | Medium |

---

## Appendix A — Things We Couldn't Determine and Why

1. **Root cause of 0-byte logs (Apr 22–30).** The 0-byte log pattern is confirmed and correlated with the Apr 22 isolation-fix commit. The exact early-exit mechanism — a config parsing exception, a missing import, or a conditional branch — was not traced to a specific line number. Recommendation 3's assertion will surface the error message the next time it fires.

2. **Whether commodity_etfs GLD -$44.18 and FCX -$31.95 were ghost trades from the isolation bug.** The trust score audit flags these as potentially originating from the state isolation bug (universe field mismatch). The `trades` table does not have enough provenance data to definitively confirm this without replaying the reconciler logs from April 14–22.

3. **Why sp500 swept SIGTERM after 9 minutes on April 30.** The systemd log shows exit code killed, 9 minutes runtime. Root cause — OOM, timeout cascade, or code path — was not traceable without the full journal output for that run.

4. **What the live connors_rsi2 entry parameters actually were at deployment time.** The audit identified that live used `rsi_entry=40` vs research finding `rsi_entry=60` as better. Whether this discrepancy was intentional (using sp500 defaults) or accidental (oversight during git-commit activation) is not determinable from the DB alone.

5. **True sp500 edge vs lucky outliers.** The sp500 outperformance (PF 2.09, trust 1.35) is explained by 3 outlier trades (AMD, MRVL, ON). Whether these represent genuine edge or statistical luck with a 15-trade sample cannot be determined yet. Minimum 50 trades needed for a meaningful read.

6. **`atlas-trader` service journal.** `journalctl -u atlas-trader` returned no output — the deployed service name differs from `atlas-trader`. Live execution logs were not reviewed. Whether slippage, order routing, or partial fills diverged from backtest assumptions in ways not captured by `exit_reason` alone is unknown.

---

## Appendix B — Raw Evidence

### B1. Key SQL Queries and Results

**research_experiments — 8-day ETF blackout (Apr 22–30):**
```sql
SELECT date(created_at) as day, market, COUNT(*) as n
FROM research_experiments
WHERE date(created_at) BETWEEN '2026-04-20' AND '2026-05-01'
GROUP BY 1, 2 ORDER BY 1, 2;
-- Apr 22: sp500=221, commodity_etfs=0, sector_etfs=0, gold_etfs=0, treasury_etfs=0, defensive_etfs=0
-- Apr 23–30: same — five universes entirely absent
```

**All ETF experiments are in-sample sweeps with no OOS window:**
```sql
SELECT universe, strategy, experiment_type, window_coverage_pct, COUNT(*) as n
FROM research_experiments
WHERE universe IN ('commodity_etfs','sector_etfs')
GROUP BY 1,2,3,4;
-- ALL rows: experiment_type='sweeper', window_coverage_pct=100.0
-- commodity_etfs momentum_breakout: 150 rows, all in-sample
-- sector_etfs momentum_breakout: 176 rows, all in-sample
```

**CAGR degradation gate passing extreme negative values:**
```sql
-- Observed values in oos_validation_cache
-- sector_etfs momentum_breakout May 5:   cagr_degradation = -1863.3% → NOT > 50 → PASSES
-- commodity_etfs Apr 22 promotion:       cagr_degradation = -321.6%  → NOT > 50 → PASSES
-- commodity_etfs Apr 27 promotion:       cagr_degradation = -101.4%  → NOT > 50 → PASSES
-- Gate never fires on any ETF universe.
```

**commodity_etfs|momentum_breakout OOS Sharpe instability:**
```sql
SELECT eval_date, oos_sharpe, oos_result
FROM oos_validation_cache
WHERE universe='commodity_etfs' AND strategy='momentum_breakout'
ORDER BY eval_date;
-- 2026-04-22: oos_sharpe=+1.173 → PASS
-- 2026-04-27: oos_sharpe=+0.790 → PASS
-- 2026-04-29: oos_sharpe=-0.1646 → FAIL
-- 2026-04-30: oos_sharpe=-0.1646 → FAIL
-- 2026-05-05: oos_sharpe=-0.1646 → FAIL
-- Range: -0.165 to +1.173 for the same strategy. Universe too small for stable OOS statistics.
```

**Negative Sharpe strategies in research_best with no promotion floor:**
```sql
SELECT universe, strategy, sharpe, updated_at
FROM research_best WHERE sharpe < 0 ORDER BY sharpe;
-- treasury_etfs  | mean_reversion:     sharpe=-1.582  (2026-05-01)
-- treasury_etfs  | momentum_breakout:  sharpe=-1.557  (2026-05-01)
-- defensive_etfs | momentum_breakout:  sharpe=-0.465  (2026-05-01)
```

**Promotion log shows null OOS results — audit trail broken:**
```sql
SELECT id, universe, strategy, oos_result, promoted_at FROM promotion_log;
-- Row 1: commodity_etfs, momentum_breakout, oos_result=NULL, 2026-04-22
-- Row 2: commodity_etfs, momentum_breakout, oos_result=NULL, 2026-04-27
-- Row 3: sector_etfs,    mean_reversion,    oos_result=NULL, 2026-04-29
```

**Live trade detail — commodity_etfs|momentum_breakout (all 4 real trades):**
```sql
SELECT entry_date, ticker, entry_price, exit_price, pnl, pnl_pct, hold_days, exit_reason
FROM trades
WHERE universe='commodity_etfs' AND strategy='momentum_breakout'
  AND superseded=0 AND exit_reason != 'reconcile_phantom'
ORDER BY entry_date;
-- 2026-04-15  GLD  442.80 → 420.71  -44.18   -4.99%  19d  manual_consolidation_close
-- 2026-04-15  SLV   71.92 →  70.99   -5.60   -1.30%   6d  reconcile_fill
-- 2026-04-16  FCX   68.03 →  61.64  -31.95   -9.39%   8d  reconcile_fill
-- 2026-04-23  CCJ  126.47 → 119.73  -26.96   -5.33%   5d  reconcile_fill
-- WIN RATE: 0/4. TOTAL PnL: -$108.69. Avg hold: 9.5 days. Avg loss: -5.25%/trade.
```

**Non-superseded duplicate trade entries inflating PnL counts:**
```sql
SELECT ticker, universe, strategy, entry_date, exit_date, pnl, exit_reason
FROM trades
WHERE exit_reason IN ('reconcile_fill_cached','reconciled_orphan')
  AND superseded = 0;
-- FCX  commodity_etfs  connors_rsi2  2026-04-24 / 2026-04-29  -15.71  reconcile_fill_cached
-- SLV  commodity_etfs  connors_rsi2  2026-04-24 / 2026-04-29  -15.26  reconcile_fill_cached
-- UNG  commodity_etfs  connors_rsi2  2026-04-24 / 2026-04-29  +14.57  reconcile_fill_cached
-- UNG  commodity_etfs  connors_rsi2  2026-04-30              +14.57  reconciled_orphan
-- MU   sp500  momentum_breakout  2026-04-28 / 2026-04-29  -18.04  reconciled_orphan
-- All should be marked superseded=1.
```

**research_experiments sample size distribution:**
```sql
SELECT
  SUM(CASE WHEN trades IS NULL OR trades = 0 THEN 1 ELSE 0 END) as zero_trades,
  SUM(CASE WHEN trades > 0 AND trades < 30 THEN 1 ELSE 0 END) as under_30,
  COUNT(*) as total
FROM research_experiments;
-- zero_trades=5007, under_30=7327, total=25831
-- 19.4% zero trades, 47.8% under 30 trades (combined)
```

### B2. Code Citations

**DSR gate disabled in practice — loop.py:379–403:**
```python
dsr_stats = _get_dsr_stats()  # aggregates ALL universes into one pool
if dsr_stats["num_experiments"] >= 5:
    n_exp = dsr_stats["num_experiments"]
    var_s = dsr_stats["variance_of_sharpes"]
    if var_s > 0:
        e_max_s = np.sqrt(var_s) * (...)
        _DSR_SANITY_CAP = 3.0
        if e_max_s > _DSR_SANITY_CAP:
            logger.warning("DSR gate skipped — expected max Sharpe %.2f exceeds sanity cap %.2f "
                           "(n=%d, var=%.4f). See audit item O12.", e_max_s, _DSR_SANITY_CAP, n_exp, var_s)
            # GATE IS SKIPPED — falls through without blocking
```

**CAGR gate broken for low-CAGR universes — promoter.py:**
```python
# Gate 4 OOS validation
cagr_degradation = (is_cagr - oos_cagr) / abs(is_cagr) * 100
if cagr_degradation > 50:
    return fail("CAGR too degraded vs OOS")
# When is_cagr is near 0 and oos_cagr is positive:
#   degradation = (0.001 - 0.010) / abs(0.001) * 100 = -900%
#   -900 is NOT > 50 → PASSES unconditionally
# Observed in production: -321.6%, -101.4%, -1863.3% — all pass
```

**Trade count floor set to 10 — loop.py:363:**
```python
min_trades = max(10, int(b_trades * 0.7)) if b_trades > 0 else 10
# Statistical minimum for reliable Sharpe: 30 trades
# Floor allows experiments with 10-29 trades to influence research_best
```

**regime_state tagged in DB, zero references in selection code:**
```python
# db.py:34-66 — written correctly at experiment log time
regime_state = row["regime_state"]  # tagged

# grep results:
# sweep.py:     0 matches for "regime_state"
# loop.py:      0 matches for "regime_state"
# promoter.py:  0 matches for "regime_state"
# 25,831 tagged experiments. Zero code paths read the tag for selection purposes.
```

**Execution pipeline absent for ETF universes — confirmed Apr 22 audit:**
```bash
# pi-cron.sh
# execute_approved is scheduled for sp500 only
# commodity_etfs: NOT IN CRONTAB
# sector_etfs:    NOT IN CRONTAB
# Result: all ETF exits are reconcile_fill, not stop_loss/trailing_stop
# Strategy logic not driving exits for either ETF universe
```

**Snapshot date confirms research predates live failure regime:**
```bash
ls -la snapshots/commodity_etfs_20260417_7yr/CCJ.parquet
# → data ends 2026-04-16
# CCJ trade entered 2026-04-23, lost -$26.96
# Research Sharpe 1.316 was computed without this trade or any post-Apr-16 data
```

**Walk-forward engine — genuine, not fabricated:**
```python
# engine.py:153–159
self.train_window = self.backtest_config.get("train_window_days", 252)
self.test_window  = self.backtest_config.get("test_window_days",   63)
self.step_days    = self.backtest_config.get("step_days",          21)

# engine.py:14 (docstring)
# "Signals generated on day T close — Orders filled at day T+1 open (market-on-open)"

# momentum_breakout.py:80–81 — no look-ahead bias
df["_mb_lookback_high"] = close.rolling(self.lookback_days).max().shift(1)
df["_mb_avg_vol"] = volume.rolling(20).mean().shift(1)
```

### B3. System-Level Operational Snapshot (2026-05-06)

```
atlas-discovery.service:          browse_with_pi JSON parse error (30+ days continuous)
commodity_etfs LLM loop:          timeout after 1800s (nightly, every run)
DSR gate:                         SKIPPED every session (n=24,368, var=2.0375 > cap 3.0)
promotion_log.json:               oos_result=NULL on all 3 entries
ASX universe timer:               NOT CONFIGURED
atlas-research-runner.service:    DISABLED (director queue not consumed)
research_experiments <30 trades:  12,334 / 25,831 (47.8%)
research_experiments 0 trades:    5,007 / 25,831 (19.4%)
sp500|trend_following research:   STALE 56 days (updated Mar 11)
sector_etfs|connors_rsi2 research: STALE 34 days (updated Apr 2)
treasury_etfs|mean_reversion:     Sharpe -1.582 in research_best (no floor)
treasury_etfs|momentum_breakout:  Sharpe -1.557 in research_best (no floor)
sp500 regime filter:              DISABLED ("enabled": false in live config)
ETF execute_approved cron:        NOT SCHEDULED (exits via reconcile_fill, not strategy logic)
commodity_etfs OOS Sharpe range:  -0.165 to +1.173 for same strategy (same data)
```

---

*Audit produced 2026-05-06. All figures sourced directly from `/root/atlas/data/atlas.db`, service logs, and source files under `/root/atlas/`. No data was extrapolated or estimated — every number has a SQL query or file citation.*


---

## Decision Log — 2026-05-06

### Recommendation 3: Discovery Pipeline DISABLED

Per the audit findings, the discovery pipeline was disabled rather than fixed.

**Evidence justifying KILL over FIX**:
- 90-day track record: 26 runs, 550 papers downloaded, 0 specs extracted, 0 strategies adopted to live
- 22 LLM-generated strategy files in `research/strategies/` — ZERO promoted to active `strategies/` directory
- Daily resource burn: 1,398 CPU-seconds, persistent `browse_with_pi error: json parse failed`
- The `browse_blog.md` prompt file was already restored (Apr 22) but did not produce useful output — the bottleneck is the LLM JSON-parsing step, not the prompt

**Action taken**: `systemctl disable --now atlas-discovery.timer`. Operational state archived to `research/discovery/archived/disabled-2026-05-06/`. Code and prompts preserved in place to permit revival if a new approach (e.g. structured output schema, different LLM) is later attempted.

**Re-enable criteria**: discovery pipeline should not be re-enabled until (a) the JSON-parsing failure is fixed in a sandbox/test environment with verified ≥1 paper successfully filtered, AND (b) at least one historical generated-strategy file is reviewed and confirmed to provide novel actionable insight that current sweep+brain workflow cannot produce.

**Related cleanup deferred**: `atlas-research-runner.service` (the director queue executor) remains disabled. Wave 1 `queue.json` items remain orphaned (10 entries in `queued` status). These can be cleaned up in a follow-up task if needed.
