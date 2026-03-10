# Atlas Research Engine v2 — 24/7 Continuous Research with Obsidian Vault

## Vision
The Obsidian vault becomes the **brain** of the research engine — not a report generated after the fact, but the living state that drives decisions. The daemon reads the vault to know what to test next, writes results back in real-time, and accumulates cross-experiment intelligence that compounds over time. Every finding, every parameter insight, every dead-end is captured as a linked note that future experiments can reference.

## Current State
- **Vault**: 41 experiment notes, 15 strategy cards, 5 patterns, 5 waves — batch-generated after each cron run
- **Throughput**: ~6 experiments/day (2 hrs compute, 22 hrs idle)
- **Architecture**: pi-cron → LLM agent → research_runner.py → backtests → build_obsidian_vault.py
- **Data flow**: journal.json → vault (one-way, batch rebuild)
- **Hardware**: 8 cores, 31GB RAM, 364GB free disk — massively underutilized

## Target State
- **Vault**: real-time updates, 500+ experiment notes, parameter insights, hypotheses, daily digests
- **Throughput**: 60-130 experiments/day continuous
- **Architecture**: Agent coordinator ←→ vault ←→ execution daemon
- **Data flow**: vault IS the shared state — agent reasons over it, daemon writes results to it

---

## Architecture: Agent-Coordinated Research

The key insight: **an LLM agent is the strategist, the daemon is the executor.**

The agent reads the vault, reasons about what's most valuable to test next, queues
experiments with rationale, reviews results in batches, detects patterns, generates
hypotheses, builds new strategies, and adjusts research direction. The daemon just
grinds through the queue — running backtests, writing results to the vault, and
signaling the agent when it needs direction.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     COORDINATOR AGENT                               │
│              (pi agent, runs in review cycles)                      │
│                                                                     │
│  🎩 Researcher    → reads vault, reasons about gaps, queues exps    │
│  🧪 Builder       → generates strategy code via LLM                │
│  📊 Analyst       → reviews batch results, updates insights        │
│  🔬 Theorist      → generates hypotheses, detects patterns         │
│  🛡️ Risk          → promotion gating, robustness checks            │
│                                                                     │
│  Wakes up:                                                          │
│    - Every N experiments completed (e.g. every 10)                  │
│    - When queue depth < 5                                           │
│    - On schedule (daily deep review)                                │
│    - On promotion candidate                                         │
│                                                                     │
│  Reads from vault:                                                  │
│    - KNOWLEDGE_BASE.md (full context)                               │
│    - Recent Daily Logs/ (what just happened)                        │
│    - Strategy Universe.md (what's untested)                         │
│    - Parameters/ (what we've learned about each param)              │
│    - Patterns/ (rules to respect)                                   │
│    - Meta/Coverage Map.md (where the gaps are)                      │
│    - Hypotheses/ (what theories need testing)                       │
│                                                                     │
│  Writes to:                                                         │
│    - queue.json (prioritized experiments with rationale)             │
│    - Hypotheses/ (new testable theories)                            │
│    - Patterns/ (newly detected patterns)                            │
│    - strategies/*.py (new strategy code)                            │
│    - Waves/ (wave theme + reasoning)                                │
│    - Telegram (key findings, promotion requests)                    │
└──────────────────┬──────────────────────────────────────────────────┘
                   │
          queue.json + vault notes
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     EXECUTION DAEMON                                 │
│              (Python process, runs 24/7, no LLM)                    │
│                                                                      │
│  Loop:                                                               │
│    1. Pull next experiment from queue                                │
│    2. Quick-screen (signal check, <1s)                               │
│    3. If passes: run full backtest (parallel, 2 simultaneous)       │
│    4. Deterministic evaluation (pass/fail/partial)                  │
│    5. Write results to vault (experiment note, strategy card, etc.)  │
│    6. Auto-advance lifecycle (solo passed → queue optimize)          │
│    7. If queue low or batch done → signal agent to wake up           │
│    8. Repeat                                                         │
│                                                                      │
│  Also handles:                                                       │
│    - Health heartbeat                                                │
│    - Data freshness checks                                           │
│    - Daily log generation                                            │
│    - Dashboard stats refresh                                         │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ research_daemon.py  (systemd, Restart=on-failure)             │  │
│  └────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────┘
         │                                           │
         ▼                                           ▼
   vault notes (real-time)                    agent wake signal
   (experiments, strategy cards,              (/tmp/agent-wake.flag)
    parameters, daily logs)

```

### Agent vs Daemon — Who Does What

| Decision | Who | Why |
|----------|-----|-----|
| "What strategy should we build next?" | Agent | Requires reasoning about gaps, portfolio fit, market conditions |
| "Run this backtest" | Daemon | Mechanical execution, no judgment needed |
| "Did this experiment pass?" | Daemon | Deterministic: compare metrics to acceptance criteria |
| "What does this result MEAN?" | Agent | Connects findings across experiments, spots patterns |
| "Queue the next lifecycle stage" | Daemon | Mechanical: solo passed → queue optimize |
| "Should we change research direction?" | Agent | Strategic judgment: diminishing returns, new opportunities |
| "Is this promotion-worthy?" | Agent | Weighs risk, robustness, portfolio impact |
| "Write experiment note to vault" | Daemon | Template-based, deterministic |
| "Update KNOWLEDGE_BASE.md insights" | Agent | Requires synthesis across all findings |
| "Generate hypothesis from pattern" | Agent | Creative reasoning about implications |
| "Search web for new strategies" | Agent | Needs to evaluate relevance, quality |
| "Generate strategy Python code" | Agent | LLM code generation |
| "Detect that 3+ experiments show same param optimal" | Daemon | Mechanical pattern matching |
| "Decide what that pattern means for future research" | Agent | Strategic interpretation |

### Agent Review Cycles

The agent doesn't run 24/7 — it runs in **review cycles** triggered by the daemon:

**Micro-review (every 10 experiments, ~3-4 hours):**
- Read the last 10 experiment vault notes
- Update parameter insights if new evidence
- Adjust queue priorities if needed
- Queue 5-10 more experiments
- ~5 min agent time, ~$0.10

**Daily deep review (once per day, 08:00):**
- Read full KNOWLEDGE_BASE.md + today's Daily Log
- Synthesize: what did we learn today?
- Detect new patterns across all experiments
- Generate hypotheses from patterns
- Plan next research direction
- Send Telegram daily digest
- ~15 min agent time, ~$0.50

**Strategy generation (when Universe has unbuilt strategies):**
- Pick highest-priority unbuilt strategy
- Web search for academic references and implementation details
- Generate strategy code + param grid
- Validate: instantiation, signal generation, basic sanity
- Queue lifecycle pipeline
- ~10 min agent time, ~$0.30

**Promotion review (when OOS passes):**
- Deep analysis of candidate config
- Robustness assessment
- Risk evaluation
- Send promotion request via Telegram
- ~5 min agent time, ~$0.15
```

---

## Vault Structure (expanded)

```
research/vault/
├── Dashboard.md                    # Auto-updated index with live stats
├── KNOWLEDGE_BASE.md               # Master summary — daemon reads this
├── Strategy Universe.md            # Master list of ALL strategies to test
│
├── Strategies/                     # One card per strategy
│   ├── Mean Reversion.md           # Status, all experiments, best params, learnings
│   ├── Inside Bar Breakout.md      # (new) Generated when strategy is discovered
│   └── ...
│
├── Experiments/                    # One note per experiment (real-time write)
│   ├── wave5_full_reopt.md
│   ├── wave6_inside_bar_solo.md    # Written IMMEDIATELY when experiment completes
│   └── ...
│
├── Parameters/                     # (NEW) Per-parameter insight notes
│   ├── RSI Period.md               # "Optimal range 2-5, evidence: 12 experiments"
│   ├── ATR Stop Multiplier.md      # "2.0-2.5 across all strategies"
│   ├── SMA-200 Filter.md           # "Harmful for MR/OG, neutral for TF"
│   ├── Max Hold Days.md            # "5-10 optimal, >15 always degrades"
│   └── ...
│
├── Hypotheses/                     # (NEW) Testable hypotheses queue
│   ├── H001 - Shorter RSI Always Wins.md       # Status: TESTING
│   ├── H002 - Position Contention at 15.md     # Status: CONFIRMED
│   ├── H003 - Overnight Return Anomaly.md      # Status: QUEUED
│   └── ...
│
├── Patterns/                       # Confirmed research rules
│   ├── Position Slot Contention.md
│   ├── SMA-200 Filter Win.md
│   ├── Fee Drag at Low Equity.md
│   └── ...
│
├── Waves/                          # Wave history
│   ├── Wave 1.md ... Wave 5.md
│   ├── Wave 6.md                   # Auto-generated when daemon starts new wave
│   └── ...
│
├── Portfolio/                      # (NEW) Portfolio-level analysis
│   ├── Correlation Matrix.md       # Strategy signal correlations
│   ├── Allocation Analysis.md      # Position pool findings
│   ├── Risk Budget.md              # Per-strategy risk allocation
│   └── Regime Analysis.md          # VIX/breadth regime performance
│
├── Daily Logs/                     # (NEW) Daily research digest
│   ├── 2026-03-10.md               # "12 experiments, 2 passes, key finding: ..."
│   ├── 2026-03-11.md
│   └── ...
│
├── Config/                         # (NEW) Config version history with context
│   ├── v2.2 (current).md           # Links to experiments that informed this config
│   ├── v2.3 (candidate).md         # The reopt candidate, what changed and why
│   └── ...
│
└── Meta/                           # (NEW) Research health & coverage
    ├── Coverage Map.md             # What's been tested, what hasn't
    ├── Data Exposure Log.md        # Track how many times each data window is used
    ├── Throughput Stats.md         # Experiments/day, time per experiment
    └── Dead Ends.md                # Strategies/params confirmed not worth revisiting
```

### Vault Note Linking Rules
Every note uses Obsidian `[[wikilinks]]`:
- Experiment notes link to: `[[Strategy Name]]`, `[[Wave N]]`, relevant `[[Parameter Name]]`
- Strategy cards link to: all `[[experiment_id]]` notes, relevant `[[Pattern Name]]`
- Parameter notes link to: all experiments that tested that parameter
- Hypothesis notes link to: experiments that test them, patterns they'd confirm/refute
- Daily logs link to: all experiments run that day
- Dashboard links to everything

### Frontmatter Schema (for Dataview queries in Obsidian)
```yaml
---
# Experiment note
experiment_id: wave6_inside_bar_solo
type: experiment
wave: 6
strategy: inside_bar
method: single_strategy_test
verdict: pass|fail|partial
sharpe: 0.45
cagr: 12.3
max_drawdown: 5.2
total_trades: 156
profit_factor: 1.82
date: "2026-03-12"
runtime_s: 342
lifecycle_stage: solo|optimize|combined|oos|promoted
tags: [experiment, strategy/inside-bar, verdict/pass, wave/6]
---
```

---

## Milestone 1: Foundation — Daemon + Real-time Vault Writes
**Goal**: Continuous execution with vault as the primary output.

### 1.1 `research_daemon.py` — systemd service
- Infinite loop: pick experiment → run → evaluate → write to vault → repeat
- Parallel execution: 2 backtests simultaneously (ProcessPoolExecutor)
- Nice to CPU priority 10 (preserve intraday monitor)
- Health heartbeat to `/tmp/research-daemon-heartbeat.json`
- Auto-restart on crash (systemd `Restart=on-failure`)
- Graceful shutdown on SIGTERM
- Data freshness check: refuse to run if market data cache > 36h old

### 1.2 Real-time vault writer (`research/vault_writer.py`)
Replace batch `build_obsidian_vault.py` with incremental writes:
- `write_experiment(experiment_data)` — create/update experiment note immediately
- `update_strategy_card(strategy_id)` — re-aggregate metrics after each experiment
- `update_parameter_insight(param, finding)` — accumulate parameter knowledge
- `write_daily_log(date, summary)` — append to daily digest
- `update_knowledge_base()` — regenerate KNOWLEDGE_BASE.md
- `update_dashboard()` — refresh stats
- All writes are atomic (write to .tmp then rename)
- Keep batch `build_obsidian_vault.py --force` as fallback for full regeneration

### 1.3 Deterministic evaluator
Rule-based verdict from acceptance criteria in queue entry:
- **PASS**: meets all criteria (min_trades, min_sharpe, etc.)
- **FAIL**: below thresholds, code error, or zero trades
- **PARTIAL**: some criteria met, marginal results
- Auto-advance: PASS at stage N → auto-queue stage N+1
- Auto-defer: FAIL at stage N → skip all downstream stages
- Write verdict + rationale to vault experiment note

### 1.4 Retire daily cron research slot
- Remove `research` from pi-cron.sh schedule
- Keep data refresh (postclose) and vault rebuild as fallback
- Daemon manages its own scheduling

---

## Milestone 2: Speed — Quick Screen + Parallel Execution
**Goal**: Kill dead-ends fast, run more experiments per hour.

### 2.1 Signal-only pre-screen (<1 second)
Before any backtest:
- Run `generate_signals()` on full cached data
- **Kill if**: <30 signals total, >80% correlated with existing active strategy
- **Pass if**: >50 signals, distributed across >50% of the test period
- Write screen result to vault: `Experiments/wave6_xyz_screen.md`

### 2.2 Quick backtest (~10 seconds)
For strategies passing signal screen:
- 50 tickers (top liquid only), single-pass (no walk-forward)
- **Kill if**: Sharpe < -1.0 or PF < 0.5
- **Continue if**: Sharpe > -0.5 and trades > 30
- Write quick result to vault experiment note (appended section)

### 2.3 Full-period CAGR fix
- Add `cagr_full_period` metric alongside current test-period CAGR
- Uses full data window (training + test) as denominator
- Vault experiment notes show both: "CAGR (test): 38.1% | CAGR (full): 22.7%"

### 2.4 Parallel execution
- 2-3 backtests simultaneously on 8 cores
- Independent experiments run in parallel
- Dependency chains run serially (solo → opt → combined → OOS)
- Target: 50+ experiments/day at this stage

---

## Milestone 3: Strategy Coverage — Factory + Universe
**Goal**: Systematically test every viable strategy pattern.

### 3.1 Strategy Universe note (`Strategy Universe.md`)
Master registry in the vault — one row per strategy, updated by daemon:
```markdown
## Tier 1 — Academic Strategies
| Strategy | Type | Reference | Status | Solo Sharpe | Combined Sharpe |
|----------|------|-----------|--------|-------------|-----------------|
| Mean Reversion | MR | Connors | ✅ active | 0.63 | 0.75 |
| Inside Bar / NR7 | Breakout | Crabel | ❌ not built | — | — |
| Donchian Breakout | Trend | Turtle Trading | ❌ not built | — | — |
| Williams %R | MR | Larry Williams | ❌ not built | — | — |
| ...
```
Daemon reads this to find what to build/test next.

### 3.2 Strategy factory pipeline
When daemon finds an unbuilt strategy in the universe:
1. Call LLM with: strategy name, description, academic reference, `BaseStrategy` template, existing example
2. LLM generates: `strategies/new_strategy.py`, param grid, acceptance criteria
3. Validate: can instantiate, has `generate_signals()` and `check_exits()`
4. Register: add to config (enabled=false), create strategy card in vault
5. Queue: enter standard lifecycle pipeline
6. Cost: ~1 LLM call per strategy (~$0.05-0.10)

### 3.3 Standard lifecycle pipeline (auto-queued)
```
SCREEN    →  signal count + correlation check         (<1s)
QUICK     →  reduced backtest, kill if terrible       (~10s)
SOLO      →  full walk-forward, strategy alone        (~6 min)
OPTIMIZE  →  coordinate descent on param grid         (~30-90 min)
COMBINED  →  test with active portfolio               (~6 min)
OOS       →  3-test validation suite                  (~60 min)
PROMOTE   →  human approval via Telegram              (manual)
```
Each stage writes a vault note. Each stage gates the next.
Vault strategy card shows current lifecycle stage.

### 3.4 Tier 1 strategy list (18 strategies to build)
| Strategy | Type | Signal | Reference |
|----------|------|--------|-----------|
| Inside Bar / NR7 | Breakout | Narrowest range in N days | Crabel |
| Donchian Breakout | Trend | N-day high/low breakout | Turtle Trading |
| Williams %R | MR | %R oversold bounce | Larry Williams |
| Stochastic Oversold | MR | %K/%D oversold cross | George Lane |
| ADX Trend + Pullback | Trend | Strong ADX + pullback entry | Wilder |
| Overnight Return | Anomaly | Buy close, sell open | Academic |
| PEAD | Event | Post-earnings drift | Academic |
| Keltner Reversion | MR | Keltner channel touch | Chester Keltner |
| RSI Divergence | MR | Bullish divergence | Wilder extension |
| MACD Divergence | Momentum | Histogram divergence | Gerald Appel |
| Volume Climax | MR | Huge volume + reversal | Wyckoff |
| DeMark Sequential | Counter-trend | TD 9/13 exhaustion | Tom DeMark |
| Gap & Go | Momentum | Gap up continuation | Day trading adapted |
| Relative Strength Pullback | Momentum | Strong RS + dip buy | O'Neil / IBD |
| Heikin-Ashi Reversal | Trend | HA candle pattern | HA methodology |
| VWAP Reversion | MR | Daily VWAP deviation | Institutional |
| Monthly Rotation | Rotation | Sector/factor momentum | Academic |
| Put/Call Proxy | Sentiment | VIX term structure | Academic |

### 3.5 Tier 2 — Portfolio experiments (auto-queued after Tier 1)
| Experiment | Method |
|-----------|--------|
| Max positions sweep (5, 10, 15, 20, 25) | param_sweep |
| Risk per trade sweep (0.25%, 0.5%, 1%, 2%) | param_sweep |
| Allocation pools (per strategy type) | filter_test |
| Fee sensitivity ($0 / $1 / $5 per trade) | param_sweep |
| Slippage modeling (0% / 0.05% / 0.1%) | param_sweep |
| Universe size (50 / 100 / 200 / 500) | param_sweep |
| Sector concentration limits (1-5) | param_sweep |
| Walk-forward window sizes (126/252/504 train) | robustness |

### 3.6 Tier 3 — Cross-strategy optimization
| Experiment | Method |
|-----------|--------|
| Strategy correlation matrix | analysis |
| Optimal strategy weights | optimization |
| Entry timing stagger | filter_test |
| Combined regime filtering (VIX + breadth) | filter_test |
| Equity curve regime switching | analysis |

---

## Milestone 4: Intelligence — Meta-Learner via Vault
**Goal**: The vault accumulates intelligence that makes future research smarter.

### 4.1 Parameter insight notes (auto-generated)
After every experiment that tests a parameter, update `Parameters/{param}.md`:
```markdown
---
parameter: rsi_period
type: parameter
optimal_range: [2, 5]
evidence_count: 12
confidence: 0.85
last_updated: "2026-03-12"
tags: [parameter, mean-reversion]
---
# RSI Period

## Evidence
| Experiment | Value | Sharpe | Context |
|-----------|-------|--------|---------|
| [[wave3_rsi_period]] | 5 | 0.61 | combined MR sweep |
| [[wave5_full_reopt]] | 5 | 0.75 | coord descent winner |
| ... | ... | ... | ... |

## Insight
Shorter RSI periods (2-5) consistently outperform default RSI(14) across all MR variants.
Possible explanation: faster mean-reversion signal captures intraday oversold bounces.

## Impact
- Inform all future MR variants to start with RSI(5) as baseline
- Skip RSI(14)+ in parameter sweeps — diminishing returns confirmed
```

### 4.2 Hypothesis tracking
Daemon auto-generates hypotheses from patterns:
```markdown
---
hypothesis_id: H003
type: hypothesis
status: queued|testing|confirmed|rejected
confidence: null
tags: [hypothesis]
---
# H003: Overnight Return Anomaly Exists in SP500

## Hypothesis
Buying at close and selling at open captures a positive overnight premium.
Academic evidence: Cliff et al. (2018) show ~70% of equity returns occur overnight.

## Test Plan
1. Build `overnight_return` strategy
2. Solo test on SP500 universe
3. If >100 trades and PF>1.0: optimize → combined → OOS

## Status
QUEUED — waiting for strategy implementation

## Related
- [[Strategy Universe]]
- [[Mean Reversion]] (potential correlation)
```

### 4.3 Intelligent priority scoring
Daemon reads vault to score next experiment:
- **Novelty bonus**: strategy type not yet in `Strategies/` → +100 priority
- **Hypothesis bonus**: tests a queued hypothesis → +50
- **Expected value**: similar strategies' vault cards show avg Sharpe > 0 → +30
- **Diminishing returns penalty**: >3 experiments on same param → -50
- **Dead-end penalty**: strategy in `Meta/Dead Ends.md` → skip entirely
- **Correlation penalty**: >0.8 signal correlation with active strategy → -30
- **Dependency ready**: all deps in vault show PASS → +20

### 4.4 Pattern auto-detection
After every N experiments, scan vault for patterns:
- If 3+ experiments show same parameter optimal → create Parameter insight note
- If 3+ strategies fail at same lifecycle stage → create Pattern note
- If Sharpe degrades monotonically across universe sizes → create insight
- Write new patterns/insights back to vault

### 4.5 Coverage map (`Meta/Coverage Map.md`)
Auto-updated by daemon:
```markdown
## Strategy Lifecycle Coverage
| Strategy | Screen | Quick | Solo | Opt | Combined | OOS | Promoted |
|----------|--------|-------|------|-----|----------|-----|----------|
| Mean Reversion | ✅ | ✅ | ✅ | ✅ | ✅ | ⏳ | ❌ |
| Inside Bar | ❌ | — | — | — | — | — | — |
| ... | | | | | | | |

## Parameter Coverage
| Parameter | Strategies Tested | Experiments | Confidence |
|-----------|-------------------|-------------|------------|
| rsi_period | 3 | 12 | 0.85 |
| atr_stop_mult | 5 | 8 | 0.70 |
| ... | | | |
```

---

## Milestone 5: Bug Fixes & Debt
**Goal**: Solid foundation before scaling.

### 5.1 Critical bugs
- [x] `stage_candidate()` clobbering reoptimizer output
- [x] Telegram double-multiplication display bug
- [ ] `ConsecutiveDownDays` missing `check_exits()`
- [ ] `MTFMomentum` wrong `generate_signals()` signature
- [ ] `SectorRotation` 0 trades — needs rebalance support or remove
- [ ] CAGR inflation from test-only period
- [ ] `validate_oos.py` hardcoded for ASX (split date, default config)

### 5.2 Technical debt
- [ ] Log rotation for continuous operation
- [ ] Experiment result file cleanup (archive old JSON)
- [ ] Stale lock file detection (daemon crash recovery)
- [ ] Data cache freshness enforcement

---

## Milestone 6: Monitoring & Alerts
**Goal**: Know what's happening without checking.

### 6.1 Telegram notifications (via vault data)
- **Daily digest** (08:00): experiments run, passes/fails, key findings, queue depth — all pulled from `Daily Logs/` in vault
- **Promotion alert** (immediate): when OOS passes, send approve/reject with vault link
- **Weekly summary** (Sunday): coverage map, best strategies found, knowledge graph growth
- **Error alert** (immediate): daemon crash, data stale, disk full

### 6.2 Health checks (daemon self-monitoring)
- Heartbeat file updated every 60s
- Data cache age check before each experiment
- Disk space check (alert if <50GB)
- Memory leak detection (restart if RSS > 2GB)
- Stuck experiment detection (kill if >2h on single experiment)

### 6.3 Vault dashboard (`Dashboard.md`) — auto-refreshed
```markdown
# Atlas Research Dashboard
> Last updated: 2026-03-12 14:30 UTC | Daemon: RUNNING

## Today
- Experiments: 47 run | 6 pass | 12 partial | 29 fail
- Queue depth: 23 experiments
- Current: running `wave7_williams_r_optimize` (ETA 45 min)

## All Time
- Total experiments: 412
- Strategies tested: 24 / 31 (77%)
- Parameters mapped: 18
- Patterns confirmed: 8
- Promotions: 3
- Best portfolio Sharpe: 0.82

## Strategy Leaderboard
| Strategy | Best Solo Sharpe | Combined Sharpe | Stage |
|----------|-----------------|-----------------|-------|
| Mean Reversion | 0.63 | 0.75 | OOS pending |
| Trend Following | 0.84 | 0.75 | OOS pending |
| Inside Bar | 0.52 | — | Optimize |
| ...
```

---

## Implementation Order

### Phase 1: Foundation (days 1-2)
1. Fix critical bugs (M5.1) — CDD, MTF, validate_oos
2. Build `research/vault_writer.py` — incremental vault writes
3. Build `research_daemon.py` — basic loop with vault reads/writes
4. Add deterministic evaluator with auto-advance
5. Systemd service + retire cron
6. **Test**: daemon runs overnight, writes experiment notes to vault in real-time

### Phase 2: Speed (days 3-4)
7. Quick screen filter (signal check + reduced backtest)
8. Parallel execution (2 simultaneous backtests)
9. Full-period CAGR metric
10. Parameter insight notes — auto-generated after each experiment
11. Daily log notes — auto-generated at midnight
12. **Test**: 50+ experiments/day, vault growing in real-time

### Phase 3: Strategy Coverage (days 5-8)
13. Strategy Universe note — master registry
14. Strategy factory — LLM generates strategy code from descriptions
15. Build first 5 Tier 1 strategies via LLM
16. Standard lifecycle auto-queuing
17. Build remaining 13 Tier 1 strategies
18. Coverage map in vault — auto-updated
19. **Test**: all 18 Tier 1 strategies through at least solo test

### Phase 4: Intelligence (days 9-10)
20. Hypothesis tracking notes — auto-generated from patterns
21. Priority scoring from vault state
22. Pattern auto-detection (scan vault every N experiments)
23. Tier 2 portfolio experiments
24. **Test**: daemon self-prioritizes, hypotheses being created/tested automatically

### Phase 5: Polish (days 11-12)
25. Telegram daily digest from vault
26. Dashboard note auto-refresh
27. Health checks + log rotation
28. Tier 3 cross-strategy optimization
29. Config history notes
30. **Test**: hands-off operation for 48+ hours

---

## Success Metrics
| Metric | Current | Target | Stretch |
|--------|---------|--------|---------|
| Experiments/day | 6 | 67 | 130+ |
| Strategy types tested | 7 | 25+ | 35+ |
| Dead-end detection time | 350s | <10s | <1s |
| Vault notes | 65 | 500+ | 1000+ |
| Parameter insights | 0 | 18+ | 30+ |
| Confirmed patterns | 5 | 15+ | 25+ |
| Hypotheses tracked | 0 | 20+ | 50+ |
| LLM cost/day | ~$3 | ~$1 | ~$0.20 |
| Uptime | 2 hrs/day | 23+ hrs/day | 24/7 |
| Promotion candidates | 2 total | 1/week | 2/week |

---

## How the Vault Changes Everything

### Before (batch reports)
```
cron runs → LLM agent decides what to do → backtests → LLM evaluates →
build_obsidian_vault.py regenerates everything → static markdown sits there
```
- Vault is a **byproduct** — generated after the fact
- LLM needed for every decision (expensive, slow)
- No memory between sessions except journal.json
- Each wave starts from scratch

### After (vault as brain)
```
daemon wakes up → reads vault (what's tested? what's next? what patterns exist?)
→ picks highest-priority experiment → runs backtest → writes result to vault
→ updates strategy card, parameter insights, daily log → picks next experiment
→ when queue empties: reads Strategy Universe + Hypotheses → LLM generates new wave
→ repeat forever
```
- Vault is the **brain** — daemon reads it to make decisions
- LLM only for creative work (strategy generation, wave themes)
- Every experiment enriches the vault for future experiments
- Knowledge compounds — 100th experiment is smarter than 1st

### Obsidian-specific benefits
- **Graph view**: visualize strategy → experiment → parameter connections
- **Dataview queries**: `LIST WHERE verdict = "pass" AND strategy = "mean_reversion"`
- **Search**: find all experiments that tested `rsi_period < 10`
- **Tags**: filter by `#verdict/pass`, `#strategy/trend-following`, `#wave/6`
- **Backlinks**: every strategy card shows all experiments that reference it
- **Templates**: standardized experiment/strategy/parameter note templates

---

## Risks & Mitigations
1. **Overfitting at scale** — More experiments = more spurious edges. **Mitigation**: mandatory OOS, perturbation testing, full-period CAGR, data exposure tracking in `Meta/`.
2. **Strategy bloat** — 40 strategies → position contention. **Mitigation**: strict combined test gate, allocation pools, correlation checks.
3. **Data snooping** — Same 3 years tested repeatedly. **Mitigation**: `Meta/Data Exposure Log.md` tracks per-window usage count, flag overexposure.
4. **Vault sprawl** — 1000+ notes become unnavigable. **Mitigation**: strong linking, Dashboard.md as entry point, Coverage Map for overview.
5. **Daemon stability** — Long-running process. **Mitigation**: subprocess isolation, daily self-restart at 04:00, memory monitoring.
