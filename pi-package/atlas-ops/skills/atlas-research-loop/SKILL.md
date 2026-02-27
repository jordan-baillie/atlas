---
name: atlas-research-loop
description: "Drive the daily Atlas research cycle: hypothesis generation, experiment execution, result analysis, and promotion gating. Single agent wearing 4 hats (researcher, backtester, analyst, risk), structured for future multi-agent split."
---

# Atlas Research Loop

This skill drives one full cycle of the continuous research pipeline.
Run daily during the quiet window (09:00–17:00 AEST) when both markets are closed.

## Overview

You cycle through 4 distinct roles per session. Each role reads from and writes to
specific files — all inter-role state passes through JSON files, never in-memory variables.

```
🎩 Researcher → 🧪 Backtester → 📊 Analyst → 🛡️ Risk
    (queue)        (experiments)    (journal)    (candidates)
```

## File Ownership Table

| Role       | Reads                                    | Writes                                         |
|------------|------------------------------------------|-------------------------------------------------|
| Researcher | journal.json, perf data, health check    | queue.json (append new hypotheses)              |
| Backtester | queue.json                               | experiments/exp-*.json, queue.json (status)     |
| Analyst    | experiments/exp-*.json                   | journal.json (append), experiments (annotate)   |
| Risk       | experiments/exp-*.json, journal.json     | config/candidates/*.json, promotion requests    |

---

## 🎩 RESEARCHER HAT — Hypothesis Generation

### Step 1: Read current state
1. Read `research/journal.json` — what's been tried, passed, failed, and why
2. Read latest health check: `python3 scripts/health_check.py --market sp500` and `--market asx`
3. Read paper trading state: `paper_engine/state/paper_sp500.json`, `paper_engine/state/paper_asx.json`
4. Read current active configs: `config/active/sp500.json`, `config/active/asx.json`
5. Check `research/queue.json` for already-queued experiments (don't duplicate)

### Step 2: Reason about what to test next

Apply this priority framework (highest first):

1. **P1 Degradation** — Is any active strategy losing money or showing Sharpe < 0 in recent paper trades? If so, queue a diagnostic experiment.
2. **P2 Dormant activation** — Are there coded strategies in `strategies/` that aren't enabled? Queue solo + combined tests.
3. **P3 Param drift** — Has it been >30 days since last optimization for a market? Queue re-optimization.
4. **P4 New filters** — Are there untested filters (VIX regime, volume, etc.) that could improve signal quality?
5. **P5 New strategies** — Any new strategy ideas from learnings in journal? Queue sandbox development.
6. **P5 Cross-market** — Are there cross-market correlation patterns worth testing?

### Step 3: Write hypothesis to queue

Use the research models to create a QueueEntry:
```python
import sys; sys.path.insert(0, '/root/atlas')
from research.models import QueueEntry, ExperimentType, Priority, append_to_queue, generate_experiment_id

entry = QueueEntry(
    id=generate_experiment_id(),
    title="Test momentum_breakout on SP500",
    category="dormant",
    market="sp500",
    hypothesis="Momentum breakout captures trend initiations that TF misses",
    method=ExperimentType.SINGLE_STRATEGY_TEST,
    acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15, "max_max_drawdown_pct": 10},
    estimated_runtime_min=5,
    priority=Priority.P2_HIGH,
    strategy_name="momentum_breakout",
)
append_to_queue(entry)
```

### CRITICAL: Check journal before queuing
- Search `research/journal.json` for the strategy name / hypothesis
- If a similar experiment FAILED before, do NOT re-queue unless you have a NEW rationale
- Document the new rationale in the `notes` field

---

## 🧪 BACKTESTER HAT — Experiment Execution

### Step 1: Pick next experiment
```bash
cd /root/atlas && python3 scripts/research_runner.py --dry-run
```

### Step 2: Execute
```bash
cd /root/atlas && python3 scripts/research_runner.py --agent-id atlas-research
```

Or run all queued:
```bash
cd /root/atlas && python3 scripts/research_runner.py --run-all --agent-id atlas-research
```

### Step 3: Verify output
Check that `research/experiments/exp-{id}.json` was created with full envelope.

### Budget constraints
- Max 4 hours per experiment (enforced by runner)
- Max 8 hours total per research session
- If a single_strategy_test takes >10 min, something is wrong — investigate

---

## 📊 ANALYST HAT — Result Evaluation

### Step 1: Read experiment results
Load the experiment envelope:
```python
from research.models import load_experiment
exp = load_experiment("20260227_150000_abc123")
```

### Step 2: Apply judgment
For each completed experiment, evaluate:

1. **Statistical significance** — Are there enough trades (>15) for the metrics to be meaningful?
2. **Overfitting risk** — If Sharpe is suspiciously high (>2.0), be skeptical. Check trade count.
3. **OOS vs IS** — If only IS metrics available, note that OOS validation is needed before promotion.
4. **Compared to baseline** — Is this actually better than the current active config?
5. **Trade count impact** — Adding the strategy shouldn't reduce total portfolio trades below 200.
6. **Drawdown check** — Combined DD should stay below 8%.

### Step 3: Annotate experiment
Update the experiment envelope with verdict and learnings:
```python
from research.models import ExperimentEnvelope, update_queue_entry, ExperimentStatus

env = ExperimentEnvelope.load(exp_id)
env.verdict = "pass"  # or "fail" or "partial"
env.verdict_rationale = "Sharpe 0.85 meets min 0.3 threshold, 45 trades sufficient"
env.learnings = [
    "momentum_breakout works on SP500 with default params",
    "Win rate 52% is marginal but PF 1.3 compensates",
]
env.save()

update_queue_entry(exp_id, {"status": ExperimentStatus.PASSED})
```

### Step 4: Append to journal
```python
from research.models import JournalEntry, append_to_journal

journal_entry = JournalEntry(
    experiment_id=exp_id,
    timestamp=datetime.now(timezone.utc).isoformat(),
    market="sp500",
    category="dormant",
    strategy="momentum_breakout",
    hypothesis="Momentum breakout captures trend initiations that TF misses",
    verdict="pass",
    key_metrics={"sharpe": 0.85, "cagr_pct": 8.5, "max_dd_pct": 7.2, "trades": 45},
    delta_vs_baseline={"sharpe": +0.02, "cagr_pct": +1.5},
    learnings=["Works on SP500 with default params", "Needs optimization next"],
)
append_to_journal(journal_entry)
```

### Log learnings from failures too!
Even failed experiments teach us something. Always append to journal with learnings.

---

## 🛡️ RISK HAT — Promotion Gating

### When to activate this role
Only when an experiment has `verdict="pass"` AND the strategy improvement is meaningful
(delta Sharpe >= 0.02 or delta CAGR >= 1pp or delta DD <= -0.5pp).

### Step 1: Stage candidate config
```python
from research.models import CANDIDATES_DIR
import json, shutil
from utils.config import get_active_config

config = get_active_config("sp500")
# Apply the successful experiment's params
config["strategies"]["momentum_breakout"]["enabled"] = True
# ... apply optimized params ...

candidate_path = CANDIDATES_DIR / f"sp500_{exp_id}.json"
candidate_path.parent.mkdir(parents=True, exist_ok=True)
with open(candidate_path, "w") as f:
    json.dump(config, f, indent=2)
```

### Step 2: Run OOS validation
```bash
cd /root/atlas && python3 scripts/validate_oos.py \
    --config-path config/candidates/sp500_{exp_id}.json \
    --output-path backtest/results/oos_{exp_id}.json
```
All 3 tests MUST pass before promotion.

### Step 3: Regression check
Run combined backtest with candidate config and compare to current active:
```bash
cd /root/atlas && python3 scripts/strategy_evaluator.py \
    --market sp500 --active-only  # Current baseline
```
Compare metrics — candidate must not degrade any metric by >10%.

### Step 4: Rate limit check
```python
from research.models import get_recent_promotions
recent = get_recent_promotions("sp500", days=7)
if len(recent) >= 1:
    print("Rate limit: max 1 promotion per week per market. Defer.")
```

### Step 5: Send promotion request
Use Telegram to send a structured promotion request:
```bash
cd /root/atlas && python3 scripts/telegram_notify.py \
    --message "🔬 Research Promotion Request\n\nExperiment: {exp_id}\nStrategy: momentum_breakout\nMarket: SP500\n\nBefore → After:\nSharpe: 1.04 → 1.08\nCAGR: 15.69% → 17.2%\nDD: 5.39% → 5.1%\n\nOOS: ALL PASS\n\nApprove? Reply YES to promote."
```

### Step 6: NEVER auto-promote
- Wait for human approval via Telegram
- On approve: copy candidate to `config/active/`, version to `config/versions/`
- On reject: log reason, archive candidate, update queue status to REJECTED

### Step 7: Rollback watchdog
After promotion, if paper trading shows degradation within 5 trading days:
- Auto-flag for review (don't auto-rollback — let human decide)
- Send Telegram alert with degradation metrics

---

## Session Flow Summary

```
1. Read state (journal, health check, paper state, queue)
2. Generate 1-3 hypotheses → append to queue
3. Execute queued experiments (research_runner.py --run-all)
4. Evaluate results → annotate experiments → append to journal
5. If any pass: evaluate for promotion → stage candidate → OOS validate
6. If promotion worthy: send Telegram request → wait for human approval
7. Summary: what was tested, what passed/failed, what's next
```

## Max Session Duration: 8 hours
Kill any experiment exceeding 4 hours. Total session should complete in 2-6 hours
depending on number of queued experiments and their complexity.

---

## 🌊 WAVE PLANNING — Designing the Next Research Wave

This mode is triggered when the experiment queue is empty. The cron generates a
wave brief at `research/waves/wave_N_brief.json` and gives you a planning prompt.

### Web Research with Brave Search

Use the brave-search skill to research ideas. Run from the skill directory:

```bash
# Search for strategy research
/root/.pi/agent/skills/pi-skills/brave-search/search.js "quantitative swing trading position sizing research" -n 5 --content

# Search for specific topics from previous findings
/root/.pi/agent/skills/pi-skills/brave-search/search.js "regime detection stock trading volatility filter backtest" -n 5

# Get content from a specific article
/root/.pi/agent/skills/pi-skills/brave-search/content.js https://example.com/article
```

Run **3-5 searches** covering:
1. The specific patterns/gaps identified in the wave brief
2. Recent quantitative trading research (quantifiedstrategies.com, quantpedia.com, alphaarchitect.com)
3. Academic papers or practitioner blogs on the wave theme

### Wave Design Principles

1. **One central theme** — every experiment in the wave relates to a single research question
2. **Progressive depth** — start with quick feasibility tests, then optimize, then validate
3. **Build on learnings** — reference specific findings from previous waves
4. **6-12 experiments** — enough to thoroughly explore the theme, not so many it takes weeks
5. **Clear acceptance criteria** — every experiment has measurable pass/fail thresholds

### Theme Selection — Profit First

Every wave must directly target making the live trading system more profitable.
Pick themes that either find new profitable strategies or optimise existing ones:

1. **New strategies from web research** — find published backtested strategies with Sharpe > 0.5, implement and test them
2. **Optimise existing strategy params** — re-tune for higher Sharpe/CAGR (especially if >30 days since last optimisation)
3. **Unlock portfolio capacity** — position allocation, per-strategy pools, signal priority (enables more strategies = more profit)
4. **Better exits** — adaptive stops, trailing stops, profit targets that capture more per trade
5. **New signal sources** — uncorrelated entry signals that add returns without competing for positions

Do NOT pick themes like 'diagnostics', 'monitoring', 'infrastructure', or 'data quality'.
Every experiment must have acceptance criteria tied to profitability metrics (Sharpe, CAGR, PF, win rate).

### Seeding Experiments

Use the research models to create queue entries:

```python
import sys; sys.path.insert(0, '/root/atlas')
from research.models import (
    QueueEntry, ExperimentType, Priority,
    append_to_queue, generate_experiment_id,
)

entry = QueueEntry(
    id="wave2_theme_test1",
    title="Test: <description>",
    category="<theme_category>",
    market="sp500",
    hypothesis="<clear hypothesis>",
    method=ExperimentType.SINGLE_STRATEGY_TEST,
    acceptance_criteria={"min_sharpe": 0.3, "min_trades": 15},
    estimated_runtime_min=30,
    priority=Priority.P2_HIGH,
    strategy_name="<strategy>",
    param_grid={"param1": [1, 2, 3], "param2": [0.5, 1.0]},
)
append_to_queue(entry)
```

### After Seeding

1. Update the wave brief file with theme, rationale, web findings, and experiment list
2. Run `python3 scripts/wave_planner.py --status` to verify
3. Send notification: `python3 scripts/telegram_notify.py research-wave-planned`

---

## Do NOT:
- Re-test failed ideas without NEW rationale
- Skip OOS validation before promotion
- Auto-promote configs (always require human approval)
- Leave experiments in "running" or "evaluating" state (clean up on exit)
- Exceed 1 promotion per week per market
