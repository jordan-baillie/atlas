# Atlas Research Engine — Efficient Team Structure

> Restructure the research engine from a monolithic sweep-on-timer into a coordinated team of specialized processes, while preserving the autoresearch fundamentals (headless grinding, LLM-for-strategy, vault-as-brain).

## What We Have Now

```
┌──────────────────────────────────────────────────────────┐
│  systemd timer (5x/day weekdays, 4x/day weekends)       │
│         │                                                │
│         ▼                                                │
│  research_cron.sh                                        │
│         │                                                │
│         ▼                                                │
│  sweep.py (2h window, 6 workers)                         │
│     - grinds through PARAM_GRIDS for each strategy       │
│     - writes to research/best/, brain/, journal.json     │
│     - calls promoter.py when improvement found           │
│                                                          │
│  EVERYTHING ELSE sits unused:                            │
│     research_runner.py (queue-based) — not called         │
│     discovery.py (hypothesis generation) — not called    │
│     hypothesis_tracker.py — not called                   │
│     portfolio_optimizer.py — manual only                 │
│     monitoring.py — not called                           │
│     quick_screen.py — not called                         │
│     evaluator.py (DSR, lifecycle) — partially used       │
│     loop.py (LLM interactive) — manual only              │
└──────────────────────────────────────────────────────────┘
```

### Problems
1. **Only sweep runs.** The queue (118 experiments including our 12 Sharpe Roadmap P1s) never executes — `research_runner.py` is not called by anything automated.
2. **No lifecycle advancement.** Experiments don't auto-advance (solo→optimize→combined→OOS→promote). The sweep finds good params but nothing pushes them through the pipeline.
3. **No portfolio-level research.** Portfolio optimizer, correlation matrix, vol scaling experiments — all manual.
4. **No hypothesis tracking.** The `hypothesis_tracker.py` and `discovery.py` modules exist but nothing triggers them.
5. **No statistical validation.** DSR is wired in but there's no automated OOS/CPCV/stability testing.
6. **Sweep is strategy-local.** It optimizes each strategy in isolation, never testing portfolio-level changes (vol_scaling, regime filters, allocation weights).

## Target Architecture: Three Processes, One Vault

```
┌─────────────────────────────────────────────────────────────────────┐
│                         VAULT (brain/)                              │
│   experiments/ strategies/ params/ hypotheses/ Portfolio/ Meta/     │
│   queue.json   journal.json   best/*.json   results/               │
│                                                                     │
│   The single source of truth. Every process reads and writes here.  │
└──────────┬──────────────────┬───────────────────┬───────────────────┘
           │                  │                   │
     ┌─────▼─────┐    ┌──────▼──────┐    ┌───────▼───────┐
     │  GRINDER   │    │  RUNNER     │    │  DIRECTOR     │
     │ (sweep.py) │    │ (daemon)    │    │ (pi agent)    │
     │            │    │             │    │               │
     │ Headless   │    │ Queue-based │    │ LLM-powered   │
     │ 24/7       │    │ lifecycle   │    │ periodic      │
     │ param grids│    │ management  │    │ strategy      │
     └────────────┘    └─────────────┘    └───────────────┘
```

### Process 1: GRINDER (sweep.py) — what it does today, better

**What:** Headless parameter grid sweep. No LLM. Runs 24/7.
**Trigger:** systemd timer, as today.
**Owns:** Per-strategy param optimization, `research/best/*.json`.

Changes from today:
- Already works well. Keep it.
- The new `signal_mode`, `momentum_lookback`, `momentum_skip` are already in its grid.
- Add a post-sweep hook: after each full strategy cycle, **drop a lifecycle entry into queue.json** for any strategy whose best Sharpe improved beyond a threshold. This connects the grinder to the runner.

### Process 2: RUNNER (new daemon) — the missing piece

**What:** Pulls experiments from `queue.json` in priority order, executes them via `research_runner.py`, evaluates via `evaluator.py`, auto-advances lifecycle, writes results to vault.
**Trigger:** systemd service, runs continuously, sleeps when queue empty.
**Owns:** Queue consumption, lifecycle advancement, experiment results, DSR computation.

This is the process that doesn't exist today. It's the only thing needed to make the queue (including our 12 Sharpe Roadmap experiments) actually run.

Loop:
```
while True:
    experiment = queue.pop_highest_priority()
    if experiment is None:
        sleep(300)  # check every 5 minutes
        continue
    
    # Quick screen (if solo stage and strategy is new)
    if experiment.stage == "solo" and not previously_screened(experiment.strategy):
        screen_result = quick_screen(experiment.strategy)
        if screen_result.dead_end:
            mark_dead_end(experiment)
            continue
    
    # Run the experiment
    result = research_runner.run_experiment(experiment)
    
    # Evaluate with DSR
    verdict = evaluator.evaluate(experiment, result.metrics)
    
    # Write to vault
    vault_writer.write_experiment(experiment, result, verdict)
    
    # Auto-advance lifecycle
    next_entry = evaluator.auto_advance(
        experiment.id, verdict, experiment.stage, experiment.strategy
    )
    if next_entry:
        if next_entry["action"] == "promote":
            send_telegram_promotion_request(next_entry)
        else:
            queue.append(next_entry)
    
    # Update portfolio optimization after every combined/OOS pass
    if verdict == "pass" and experiment.stage in ("combined", "oos"):
        queue.append(portfolio_reoptimization_entry())
```

Key behaviors:
- Respects priority: P1 Sharpe Roadmap experiments run before P4 backlog
- Handles **all** experiment types: filter_test (vol_scaling), param_sweep, single_strategy, combined, OOS
- DSR attached to every result automatically
- Auto-advances lifecycle: solo→optimize→combined→OOS→promote signal
- Sends Telegram for promotion candidates with DSR warning
- Re-runs portfolio optimizer when strategies pass combined/OOS gates

### Process 3: DIRECTOR (pi agent skill) — the strategist

**What:** LLM-powered periodic review. Reads the vault, reasons about gaps, queues new experiments, generates hypotheses, builds new strategies.
**Trigger:** Scheduled (daily) or on-demand.
**Owns:** Research direction, hypothesis generation, strategy factory, weekly reports.

This already exists as the `atlas-research-loop` and `atlas-director` skills. The change is making it a scheduled job that reads vault state and acts on it:

Daily cycle:
```
1. Read vault: coverage map, recent experiment results, queue depth
2. Check: is the queue running dry? (<5 experiments) → generate more
3. Check: did any strategy pass OOS? → flag for promotion review
4. Check: is any strategy cluster over-correlated? → queue ablation
5. Check: has any param been tested >5 times with no improvement? → mark dead end
6. Run portfolio_optimizer.py --vault → refresh correlation matrix
7. Generate daily digest → Telegram
8. Generate weekly report on Sundays
```

What it queues (in priority order):
- Sharpe Roadmap experiments (vol_scaling, signal_mode, stops)
- Lifecycle advances for strategies stuck at a gate
- New strategies from the factory for untested Tier 1 entries
- Ablation studies for correlated strategy clusters
- Sensitivity/robustness tests for promotion candidates

## Implementation Plan

### Phase 1: Build the Runner daemon (the critical missing piece)

Create `research/runner_daemon.py` — a systemd service that:
1. Reads `queue.json` sorted by priority
2. Pops the next experiment
3. Calls the appropriate `research_runner.py` function
4. Runs `evaluator.evaluate()` on results (includes DSR)
5. Calls `evaluator.auto_advance()` for lifecycle
6. Writes experiment note to vault via `brain/writer.py`
7. Sleeps when queue is empty
8. Heartbeat to `/tmp/research-runner-heartbeat.json`

The runner uses the **same** `research_runner.py` functions that already exist — it just calls them automatically instead of waiting for a human to invoke them.

Systemd unit:
```ini
[Unit]
Description=Atlas Research Runner — queue-based experiment execution
After=network.target

[Service]
Type=simple
WorkingDirectory=/root/atlas
ExecStart=/usr/bin/python3 -u research/runner_daemon.py
Restart=on-failure
RestartSec=60
Nice=12
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Resource coordination with the grinder:
- Runner checks for grinder lock (`/tmp/atlas-research-cron.lock`) before starting an experiment
- If grinder is active, runner sleeps until grinder finishes
- This prevents CPU contention between the two processes
- Alternative: combine into one process that alternates between sweep cycles and queue experiments

### Phase 2: Schedule the Director

Add a daily pi-cron entry (or systemd timer) that runs the atlas-director skill:
1. Morning review (08:00 AEST): read vault state, queue experiments, generate digest
2. Evening review (20:00 AEST): review day's results, adjust priorities

The director skill already exists. The only change is scheduling it and ensuring it reads the portfolio optimization results.

### Phase 3: Connect the dots

Wire up the missing integrations:
1. **Grinder → Runner:** When sweep.py finds an improvement, it drops a "combined test" entry into queue.json for the runner to pick up
2. **Runner → Director:** When runner completes a batch, it writes a flag file. Director reads it on next wake.
3. **Runner → Portfolio:** When a strategy passes combined/OOS, runner queues a portfolio re-optimization
4. **Director → Runner:** Director queues experiments. Runner executes them. No direct coupling.

### Phase 4: Collapse redundant modules

Several research/ modules overlap or are unused:
- `mr_creative.py`, `tf_creative.py`, `mr_research.py`, `run_mr.py` → fold into the Director skill's prompts
- `batch_experiment.py` → superseded by runner_daemon.py
- `loop.py` → keep for interactive sessions, but the daemon replaces its automation role
- `portfolio_experiments.py` → fold experiment generation into the Director's queue logic

## What This Changes Day-to-Day

### Today
```
Timer fires → sweep grinds for 2h → finds some param improvements → stops
Queue has 118 experiments → nobody runs them
Sharpe Roadmap vol_scaling experiments → sit in queue forever
Portfolio optimization → only when you manually run it
Weekly report → only when you manually run it
```

### After
```
Timer fires → sweep grinds for 2h → finds param improvement → drops combined-test into queue
Runner daemon picks up the combined-test → runs it → passes → queues OOS
Runner picks up OOS → runs it → passes → sends Telegram "promote?"
Runner also sees 12 P1 Sharpe Roadmap experiments → runs vol_scaling tests → writes results
Director wakes at 08:00 → sees vol_scaling results → queues next batch → sends digest
Portfolio optimizer runs weekly → updates correlation matrix → report shows new weights
```

### What Stays The Same
- Sweep engine: still the workhorse, still headless, still 24/7
- Vault: still the brain, still accumulates knowledge
- Autoresearch program: `research/program.md` still describes the interactive LLM loop
- Strategy factory: still LLM-generated code
- Evaluator: still deterministic verdicts, now with DSR on every result

## Effort Estimate

| Phase | What | Effort | Impact |
|-------|------|--------|--------|
| **1** | Runner daemon | 1 day | Unlocks the entire queue (118 experiments) |
| **2** | Director scheduling | 2 hours | Automated daily reviews and experiment generation |
| **3** | Wiring (grinder→runner→portfolio) | Half day | Closed-loop lifecycle advancement |
| **4** | Module cleanup | Half day | Reduced complexity, fewer dead code paths |

**Phase 1 is the only one that matters right now.** The runner daemon is the single missing process that connects all the infrastructure we've built. Without it, vol_scaling experiments, signal_mode comparisons, and lifecycle advancement all require manual intervention.

## Success Criteria

- [ ] `queue.json` drains automatically (experiments execute without human)
- [ ] Sharpe Roadmap experiments produce results within 48h of queueing
- [ ] Strategy lifecycle advances automatically: solo→optimize→combined→OOS→promote signal
- [ ] Portfolio optimizer runs weekly, correlation matrix in every report
- [ ] DSR is computed on every experiment, shown in vault notes
- [ ] Director sends daily Telegram digest with queue depth, passes, findings
- [ ] No manual `python3 research/portfolio_optimizer.py` needed — it runs on schedule
