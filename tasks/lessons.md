# Atlas — Accumulated Lessons
*Patterns and rules from project history. Review at session start.*

---

## Research & Backtesting

### 1. Position contention is THE bottleneck, not strategy quality
High-signal-volume strategies (momentum_breakout: 460 trades, short_term_mr: 697 trades) flood a shared position pool, crowding out proven signals. Never evaluate "does this strategy add value?" without also testing "does it crowd out existing strategies?". Always run combined test, not just solo test.

### 2. Scoring function must prevent degenerate solutions
Original coord descent converged to 3-4 trade windows where PF=infinity. Fix: min_trades=15, cap PF at 4.0, trade count scaling ramp 15→50. Without this, optimizer finds degenerate "sharp peaks" not robust plateaus.

### 3. Blending doesn't improve robustness
v9.3 (50/50 blend of v9.2 and defaults) showed identical perturbation stability as v9.2 but 4.5% less CAGR. When the landscape has one ridge, blending moves you toward a lower point. Choose the better peak, don't average them.

### 4. Clean A/B toggle reveals what coord descent hides
Coord descent rejected SMA-200 filter (too few trades). Clean A/B toggle confirmed Sharpe +47%. Lesson: when an intervention reduces trades significantly, coord descent's trade-count penalty masks the quality improvement. Test filters as clean toggles, not as optimizable params.

### 5. VIX filter destroys alpha in MR-heavy portfolios
MR profits from panic (high-VIX entries). Do not apply VIX regime filter to any portfolio containing mean_reversion. VIX filter may work for trend-only portfolios — test independently.

### 6. OOS validation before ANY promotion
Three-test suite required: (1) time-split OOS, (2) perturbation (±15%), (3) walk-forward window win rate >50%. All three must pass. OOS Sharpe > 0 is minimum; OOS ratio > 0.7 is preferred.

### 7. Solo pass ≠ portfolio pass
Track record across Wave 1: 4/4 dormant strategies passed solo tests, 0/4 passed combined tests. Always run combined test with existing portfolio before declaring a strategy "ready".

### 8. filter_test experiments need `filter_param` + `variants` fields in queue
Infrastructure failure, not hypothesis failure. When filter experiments fail due to missing params, requeue with correct format — don't mark the hypothesis as rejected.

### 9. Control test is often the most valuable experiment
"Just increase max_positions for current strategies" (max_pos=10→15) gave +13% Sharpe. This outperformed adding any dormant strategy. Always include a control arm.

---

## Broker & Execution

### 10. Fee drag determines viable minimum account size
IBKR ASX: $6/order + $500 min parcel = $12 round-trip = 2.4% drag on smallest position. At $3,999 equity, this makes MR/OG unprofitable. Rule: for any market, verify `round_trip_fee / avg_position_size < 1.0%` before deploying.

### 11. Always test broker connectivity before going live
Moomoo AU API cannot place ASX orders (server-side block — not documented). IBeam REST API had session auth loop. Discover these constraints in dev/dry-run, not in production.

### 12. Broker offline → never write state
Broker returning $0 equity + $0 cash = broker offline, not empty portfolio. Check `broker_data_valid` before writing any state. All 7 write paths now have this guard. Never rely on a single guard.

### 13. Live executor must use MARKET orders for stop-loss exits
LIMIT orders at current price may not fill if price moves during order placement. Stop-loss exits are urgency-sensitive — MARKET order is correct. Entry and target-profit exits can use LIMIT.

### 14. Moomoo trade unlock failure must be fatal
`unlock_trade()` failing but `connect()` returning True causes every subsequent order to fail silently. Treat unlock failure as connection failure for live accounts.

---

## Code Architecture

### 15. Dormant strategies accumulate API drift bugs
All dormant strategies had silent bugs: `generate_signals()` signature mismatch, `calc_atr()` wrong call pattern, Series comparison ambiguity. Before running any dormant strategy in research, do a read-through for: ABC method signatures, scalar vs Series usage, kwargs that have changed.

### 16. Shared cache files need file locking
Multiple processes (EOD, research, CLI) write to the same parquet cache concurrently. Use atomic write (write-to-temp-then-rename) for all file writes that can race. This applies to: parquet cache, paper state JSON, plan files.

### 17. Parallel builders creating the same new file → merge conflict
When a swarm task creates a new shared module, assign creation to ONE builder. Other builders depend on it or work on non-overlapping files. Never assign the same new file to multiple builders.

### 18. Research runner exit code matters
Code errors (TypeError, AttributeError) in experiment execution must exit with code 2, not 0. When exit 0, auto-recovery never fires. Distinguish: code bug (exit 2) vs research failure (exit 0).

### 19. Per-market plan files prevent market overwrites
Plan files must be named `plan_{market_id}_{date}.json`. Shared `plan_{date}.json` means the last market to generate clobbers the other. Same pattern applies to any market-specific output file.

### 20. IBeam REST vs IB Gateway socket
IBeam REST API has a known bug: browser auth session not inherited by REST endpoint → `authenticated=False` loop. Use `ib_insync` + IB Gateway Docker instead. IBeam is abandoned.

---

## Operational

### 21. US Friday session = Saturday AEST
US market hours (9:30-16:00 EST) map to Saturday AEST for Friday's session. Overnight/postclose crons must use day-of-week `2-6` (Tue-Sat), not `1-5`. Premarket (evening AEST) stays `1-5`.

### 22. Swarm coordinator must not pre-scout
Reading files before launching a swarm defeats the purpose of the scout phase. Scouts find unexpected things. Coordinator writes objectives + acceptance criteria, dispatches, tracks. Does NOT read code or run experiments.

### 23. Builder scope = file ownership
Split swarm tasks by FILE, not by concern. Each file belongs to exactly one builder. When you find yourself thinking "both builders need to touch this file" — that's a merge conflict waiting to happen. Assign the file to one builder.

### 24. Config version naming convention
Use `{market}_{version_label}_{YYYYMMDD}.json` for snapshots. Semantic labels (v9.3, v2.2) for promoted configs. Pre-promotion backups: `{market}_pre_{action}_{YYYYMMDD_HHMMSS}.json`.

### 25. Stale ASX cache had US tickers with .AX suffix
`data/cache/asx/` had 36 US ticker parquets with `.AX` suffix from an earlier pipeline bug. `strategy_evaluator.py` loaded ALL parquets, contaminating ASX backtests. Fix: filter loaded tickers against `market.get_formatted_tickers()` before loading.

### 26. Weekly maintenance prevents disk/log bloat
Cron Sunday 06:00: rotate large logs, delete old pi-cron logs (>14d), purge pycache, sweep root-level cache parquets. Without this, atlas.log hits 9+ MB and telegram_bot.log hits 2+ MB within days.

---

## Research Process

### 27. Hypothesis must come BEFORE data
Logging "it passed because X" after seeing results is confusing correlation with causation. Queue entries must have a specific, falsifiable hypothesis BEFORE the backtest runs.

### 28. Volume filter 1.5x is the threshold where quality jumps
Below 1.0x: minimal improvement. At 1.5x: MR Sharpe -0.02→0.38, PF 1.30→1.62, DD reduced. At 2.0x: too few trades (115), Sharpe drops again. The transition is sharp — don't assume linear scaling.

### 29. Sector rotation needs rebalance-aware backtest support
Standard walk-forward engine treats sector rotation as a signal-per-bar strategy. It rebalances every N days, so results with standard engine are unreliable. Don't promote sector rotation results from the current engine until rebalance-aware support is added.
