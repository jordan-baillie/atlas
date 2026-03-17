---
name: atlas-lessons
description: "Atlas operational lessons, confirmed patterns, closed decisions, and anti-patterns accumulated from project history. Use when making decisions about strategies, config changes, broker operations, research experiments, or deployments. Also use when something goes wrong — check here first for known patterns."
type: reference
---

# Atlas Lessons & Institutional Knowledge

35+ lessons from project history. **Check these BEFORE acting — they prevent repeated mistakes.**

---

## Quick-Check Gates

Before you do X, verify Y:

| About to... | Check first |
|-------------|-------------|
| Run a backtest | Has brain/ been checked for prior results on this? |
| Promote a config | OOS validation passed all 3 tests? Version bumped? |
| Add a strategy to portfolio | Combined test passed (not just solo)? |
| Write state files | Broker connectivity confirmed? |
| Edit a strategy file | Service restart planned for research-runner? |
| Run dormant strategy | Import check done? API drift bugs fixed? |
| Evaluate solo backtest results | At low equity ($4K), solo metrics are unreliable — use combined |
| Apply VIX filter | Portfolio contains mean_reversion? → DON'T |
| Run parallel research | File locking verified? |
| Trust a filter_test result | filter_param + variants fields present in queue? |
| Deploy code changes | File→service mapping checked? |

---

## Research & Backtesting

**#1 Position contention is THE bottleneck.** High-signal strategies (momentum: 460 trades, short_term_mr: 697) flood the shared position pool, crowding out proven signals. Never evaluate "does this strategy add value?" without combined testing.

**#2 Scoring function must prevent degenerate solutions.** Coord descent converges to 3-4 trade windows where PF=infinity. Required: min_trades=15, cap PF at 4.0, trade count ramp 15→50.

**#3 Blending doesn't improve robustness.** 50/50 blend showed identical stability but 4.5% less CAGR. When the landscape has one ridge, blending moves toward a lower point. Choose the better peak.

**#4 Clean A/B toggle reveals what coord descent hides.** Coord descent rejected SMA-200 filter (too few trades) but clean A/B showed Sharpe +47%. Test filters as clean toggles, not optimizable params.

**#5 VIX filter destroys alpha in MR-heavy portfolios.** MR profits from panic (high-VIX entries). Never apply VIX regime filter to portfolios containing mean_reversion.

**#6 OOS validation before ANY promotion.** Three-test suite required: (1) time-split OOS, (2) perturbation ±15%, (3) walk-forward window win rate >50%. All three must pass. OOS Sharpe > 0 minimum.

**#7 Solo pass ≠ portfolio pass.** Track record: 4/4 dormant strategies passed solo, 0/4 passed combined. Always run combined test.

**#9 Control test is the most valuable experiment.** "Just increase max_positions" (10→15) gave +13% Sharpe — outperformed adding any dormant strategy. Always include a control arm.

**#15 (research) Strategy correlation clusters invalidate naive diversification.** MR, connors_rsi2, opening_gap are 0.94-0.95 correlated — essentially one bet. Cluster strategies by correlation FIRST, then allocate across clusters.

**#16 (research) Solo backtests at $4K equity produce useless Sharpe ratios.** Fee drag at low equity destroys metrics. Use combined-mode sweeps at $0 commission (Alpaca) for promotion decisions.

**#17 (research) Infrastructure blockers masquerade as research failures.** 8 infra blockers contaminated 15+ experiments. Verify whether failure is hypothesis or infrastructure before concluding.

---

## Broker & Execution

**#10 Fee drag determines viable minimum account size.** IBKR ASX: $6/order + $500 min parcel = 2.4% drag. Rule: verify `round_trip_fee / avg_position_size < 1.0%` before deploying.

**#11 Always test broker connectivity before going live.** Moomoo AU API cannot place ASX orders (undocumented server block). IBeam REST had session auth loop. Discover constraints in dev, not production.

**#12 Broker offline → never write state.** Broker returning $0 equity + $0 cash = offline, not empty. Check `broker_data_valid` before writing state. All 7 write paths now guarded.

**#13 Live executor must use MARKET orders for stop-loss exits.** LIMIT orders may not fill on price movement. Stop-loss = urgency-sensitive → MARKET. Entries and TP exits can use LIMIT.

**#14 Moomoo trade unlock failure must be fatal.** `unlock_trade()` failing + `connect()` True = every subsequent order fails silently. Treat unlock failure as connection failure.

---

## Code Architecture

**#15 Dormant strategies accumulate API drift bugs.** All dormant strategies had silent bugs: signature mismatch, wrong calc_atr patterns, Series comparison ambiguity. Before running dormant strategies: read-through for API changes.

**#16 Shared cache files need file locking.** Multiple processes write to same parquet cache. Use atomic write (write-to-temp-then-rename) for all raceable writes: parquet cache, paper state JSON, plan files.

**#17 Parallel builders creating same new file → merge conflict.** Assign new file creation to ONE builder. Others depend on it.

**#18 Research runner exit code matters.** Code errors must exit 2, not 0. Exit 0 prevents auto-recovery. Distinguish: code bug (exit 2) vs research failure (exit 0).

**#19 Per-market plan files prevent overwrites.** Plan files = `plan_{market_id}_{date}.json`. Shared naming means last market clobbers others.

**#32 New strategies accumulate calc_position_size dict bugs.** `calc_position_size()` returns `{shares: N, ...}` dict, not int. Always extract `pos_result["shares"]`.

**#34 stage_candidate() clobbers reoptimizer output.** Without `strategy_params`, it overwrites candidate with active config copy. Rule: any function writing to a shared path must check-before-clobber.

**#35 Double-multiplication bug in Telegram formatters.** Metrics ending in `_pct` are ALREADY in percent. Never multiply by 100 again. Split into `_DECIMAL_PCT` (needs ×100) and `_ALREADY_PCT` (display as-is).

---

## Operational

**#21 US Friday session = Saturday AEST.** Overnight/postclose crons: day-of-week `2-6` (Tue-Sat). Premarket (evening AEST): `1-5`.

**#24 Config version naming.** `{market}_{label}_{YYYYMMDD}.json` for snapshots. Semantic labels (v9.3, v2.2) for promoted. Backups: `{market}_pre_{action}_{YYYYMMDD_HHMMSS}.json`.

**#25 Stale ASX cache had US tickers with .AX suffix.** Filter loaded tickers against `market.get_formatted_tickers()` before loading.

**#26 Weekly maintenance prevents disk/log bloat.** Cron Sunday 06:00: rotate logs, delete old pi-cron logs (>14d), purge pycache, sweep cache parquets.

---

## Research Process Rules

**#8 filter_test needs filter_param + variants.** Infrastructure failure, not hypothesis failure. Requeue with correct format.

**#27 Hypothesis must come BEFORE data.** Queue entries must have specific, falsifiable hypothesis BEFORE backtest runs.

**#28 Volume filter 1.5x is the quality jump threshold.** Below 1.0x: minimal. At 1.5x: MR Sharpe -0.02→0.38. At 2.0x: too few trades. Non-linear.

**#29 Sector rotation needs rebalance-aware backtest.** Standard engine treats it as signal-per-bar. Don't promote sector rotation results until rebalance support added.

**#30 Solo param sweeps unreliable at low equity.** $4K equity + fees → all strategies show negative Sharpe solo. Combined portfolio has 0.87. Solo gives relative rankings only.

**#31 filter_test doesn't support nested config params.** `volume.min_ratio` set as `volume_min_ratio` is ignored. Support dot-path or deep-merge.

**#33 Parallel research runner has file locking issues.** ProcessPoolExecutor causes concurrent writes to queue.json/journal.json. Sequential `--run-all` works. Fix locking before production parallel.

---

## Confirmed Patterns (tested and verified)

| Pattern | Evidence | Rule |
|---------|----------|------|
| MR profits from panic | VIX filter study: MR Sharpe drops from 0.38→negative with VIX filter | Never VIX-filter MR strategies |
| Position contention dominates | 4/4 dormant strategies fail combined test | Always test combined before promoting |
| Volume filter has sharp threshold | 1.0x→1.5x→2.0x sweep, quality jumps at 1.5x | Use 1.5x as default volume filter |
| Fee drag destroys small-account solo metrics | Same strategy: Sharpe -3.67 at $4K vs +0.23 at $25K | Use $0 commission for comparison |
| Correlation clustering needed | MR/connors/OG 0.94-0.95 correlated | Cluster before allocating |
| Control arms win | max_pos 10→15 outperformed all dormant strategies | Always include control experiment |

---

## Anti-Patterns (things that DON'T work)

| Don't | Why |
|-------|-----|
| Add strategies based on solo backtest only | Solo pass ≠ portfolio pass (#7) |
| Use VIX filter on MR portfolios | Destroys the signal source (#5) |
| Blend config versions | Moves to lower point on same ridge (#3) |
| Trust filter_test without checking format | Missing params → silent no-op (#8, #31) |
| Run parallel research without file locks | Silent data loss (#33) |
| Evaluate dormant strategies without import check | API drift bugs guaranteed (#15) |
| Use Sharpe from $4K solo backtests for decisions | Fee drag makes all solo Sharpes negative (#30) |
| Write state when broker returns $0 | Broker offline, not empty (#12) |
| Skip OOS validation before promotion | Three-test suite is non-negotiable (#6) |
| Stage candidate without checking existing file | Clobbers reoptimizer output (#34) |
| Multiply `_pct` metrics by 100 | Already in percent form (#35) |

---

## Closed Decisions

| Decision | Verdict | Date | Reasoning |
|----------|---------|------|-----------|
| Moomoo vs Alpaca for US | **Alpaca** | 2026-03 | Commission-free, REST API works, Moomoo has ASX order block |
| VIX regime filter for portfolio | **Rejected** | 2026-03 | Destroys MR alpha, portfolio is MR-heavy |
| Blend vs pick best config | **Pick best** | 2026-03 | Blending loses 4.5% CAGR for zero robustness gain |
| Solo vs combined sweeps | **Combined** | 2026-03 | Solo metrics unreliable at current equity |
| max_positions 10 vs 15 | **15** | 2026-03 | +13% Sharpe, best single change tested |
| Sector rotation inclusion | **Deferred** | 2026-03 | Needs rebalance-aware engine first |
