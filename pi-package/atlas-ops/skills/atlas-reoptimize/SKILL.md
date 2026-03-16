---
name: atlas-reoptimize
description: Orchestrate Atlas degradation checks, re-optimization, validation, and config promotion decisions with human approval. Use when performance degrades, after data refreshes, or when testing candidate parameter updates.
---

# Atlas Reoptimize

Use this skill for the `optimize-if-degraded` lifecycle.

This skill is the primary operator workflow for:
- running a health check
- deciding whether reoptimization is required
- running reoptimization + OOS validation
- summarizing artifacts
- stopping at explicit human approval before any promotion/rollback action

## Primary workflow: `optimize-if-degraded`

### Phase 0: Acquire workflow state + lock

1. Create a correlation id:
   - `atlas_state_new_correlation(prefix="reopt")`
2. Acquire a lock so only one reoptimize workflow runs:
   - `atlas_state_lock_acquire(name="optimize-if-degraded", owner=<correlation_id>, ttlSec=21600)`
3. If lock is already held:
   - stop and report who holds it
   - offer `atlas_state_lock_status`
   - do not proceed
4. Persist workflow context:
   - `atlas_state_put(scope="workflows", key=<correlation_id>, value={ phase: "health_check_start", started_at, intent })`

### Phase 1: Health check (always first)

1. Run:
   - `atlas_jobs_run(job="health_check")`
2. Poll until complete:
   - `atlas_jobs_get(runId=...)`
3. If job failed:
   - report manifest path + stderr tail
   - stop and keep lock only if an immediate retry is planned; otherwise release lock
4. Summarize health artifact:
   - `atlas_artifacts_summarize(path="logs/health_check_YYYY-MM-DD.json")`
5. Decision:
   - if health status is healthy / no degradation flags, stop and report `no_reoptimization_needed`
   - update workflow state and release lock

### Phase 2: Reoptimization (staged candidate config)

Default behavior now writes a staged candidate config and does not overwrite `config/active/asx.json`.
Still require explicit user confirmation before running because it is expensive and can produce a poor candidate.

Steps:
1. Record state:
   - `atlas_state_put(... phase="reoptimize_pending_confirmation" ...)`
2. After explicit user approval, run:
   - `atlas_jobs_run(job="reoptimize_full_universe", args={ candidatePath: "config/config_candidate_reoptimized_<timestamp>.json" })`
3. Poll:
   - `atlas_jobs_get(runId=...)`
4. If failed:
   - summarize run manifest/log paths
   - stop and ask whether to inspect logs or retry
5. Summarize optimization artifact:
   - `atlas_artifacts_summarize(path="backtest/results/reoptimization_full_universe.json")`
6. Record artifact/run ids in workflow state, including:
   - `candidate_config_path` from the reoptimization artifact summary/details
7. Confirm `active_config_overwritten` is false in the reoptimization artifact; if true, stop and use:
   - `atlas_risk_list_config_backups`
   - `atlas_risk_restore_config_backup` (if needed)

### Timing & Resource Notes

- Reoptimization typically takes **15–45 minutes** depending on universe size and parameter sweep breadth
- It is CPU-intensive — avoid running during market hours or alongside active research experiments
- Coordinate descent may require multiple rounds to converge; watch for the `converged` flag in the artifact summary
- If a run exceeds 60 minutes, check for stuck worker processes: `systemctl status atlas-research-runner`
- Schedule reoptimization after market close and after the daily post-close cron has completed

### Phase 3: OOS validation (against the staged candidate)

1. Run:
   - `atlas_jobs_run(job="validate_oos", args={ configPath: <candidate_config_path>, outputPath: "backtest/results/v92_oos_validation_candidate_<timestamp>.json" })`
2. Poll:
   - `atlas_jobs_get(runId=...)`
3. Summarize validation artifact:
   - `atlas_artifacts_summarize(path=<candidate_validation_path>)`
4. If you have a previous validation artifact to compare, run:
   - `atlas_artifacts_compare(leftPath="backtest/results/v92_oos_validation.json", rightPath=<candidate_validation_path>, kind="validate_oos")`
5. Update workflow state with summary + verdict notes

### Validation Interpretation Guide

Each OOS validation runs three tests:

| Test | What It Measures | Pass Threshold |
|------|-----------------|----------------|
| **OOS ratio** | CAGR_oos / CAGR_is — how much IS performance survived out-of-sample | ≥ 0.5 |
| **Window win rate** | % of rolling OOS windows with positive CAGR | ≥ 55% |
| **Perturbation robustness** | Stability of results under slight parameter nudges | All or all-but-1 trials positive |

**Green flags (strong candidate):**
- OOS Sharpe ≥ 0.5 and OOS Sharpe / IS Sharpe ≥ 0.7
- Window win rate ≥ 60%
- All perturbation trials positive
- Trade count ≥ 20 in the OOS period (statistically meaningful)

**Red flags (reject or defer):**
- OOS Sharpe < 0 — strategy loses money out-of-sample, a clear failure
- CAGR degradation > 50% (OOS CAGR less than half IS CAGR) — likely overfit
- Perturbation collapses ≥ 2 — parameter region is fragile, not robust
- Trade count < 10 in OOS — too few trades to trust any derived metrics

### Phase 4: Promotion decision checkpoint (human-in-the-loop)

1. Run the reoptimization promotion gate (config + artifact thresholds):
   - `atlas_risk_check_reopt_promotion(candidatePath=<candidate_config_path>, validationPath=<candidate_validation_path>, reoptimizationPath="backtest/results/reoptimization_full_universe.json", baselineValidationPath="backtest/results/v92_oos_validation.json")`
2. Present:
   - health summary
   - reoptimization summary
   - candidate OOS validation summary
   - candidate-vs-baseline validation comparison
   - promotion gate verdict + blockers/warnings
### Decision Framework

Use this guide to frame your recommendation before asking for human input:

**Promote** when all of:
- Promotion gate returns no blockers (warnings are acceptable)
- Clear improvement on ≥ 2 key metrics (Sharpe, CAGR, or max drawdown) vs baseline
- OOS validation passes all 3 tests
- Trade count ≥ 20 in OOS period

**Reject** when any of:
- Promotion gate fails with blocker-level issues
- OOS Sharpe < 0 or CAGR degradation > 50%
- Perturbation collapses ≥ 2 (fragile parameter region)
- Improvement < 5% across all metrics — marginal gain not worth operational risk

**Defer** when:
- Mixed signals (some tests pass, some are marginal)
- Close to end of week or an upcoming market session
- Trade count too low for statistical confidence
- Additional live data would meaningfully change the decision

**Rollback** when (post-promotion issue discovered):
- Post-promotion health check surfaces new degradation
- Live performance diverges significantly from backtested expectations within 1–2 weeks
- Use `atlas_risk_restore_config_backup` with the pre-promotion timestamped backup

3. Ask for explicit human decision:
   - promote candidate now:
     - `atlas_risk_promote_config(candidatePath=<candidate_config_path>, confirmed=true, ...)`
   - reject and keep active unchanged
   - rollback from backup (if needed):
     - `atlas_risk_list_config_backups`
     - `atlas_risk_restore_config_backup(useLatest=true, confirmed=true)` (or specify `backupPath`)
   - defer for more analysis

### Phase 5: Cleanup

1. Update workflow state to terminal status (`completed`, `aborted`, `needs_review`)
2. Release lock:
   - `atlas_state_lock_release(name="optimize-if-degraded", owner=<correlation_id>)`
3. If release fails due owner mismatch, report and stop (do not force by default)

## Resume / recovery workflow

Use this when a run fails or Pi session is interrupted.

1. Inspect lock:
   - `atlas_state_lock_status(name="optimize-if-degraded")`
2. List recent workflow state keys:
   - `atlas_state_list(scope="workflows", prefix="reopt_")`
3. Load the latest correlation record:
   - `atlas_state_get(scope="correlations", key=<correlation_id>)`
   - `atlas_state_get(scope="workflows", key=<correlation_id>)`
4. List recent job runs:
   - `atlas_jobs_list_runs(limit=10)`
5. Resume from the last safe completed phase:
   - if `health_check` completed and reoptimize not started, continue at Phase 2
   - if `reoptimize_full_universe` completed and `validate_oos` not run, continue at Phase 3
   - if validation completed, continue at Phase 4 decision checkpoint
6. If state is inconsistent, stop and present manifests/logs instead of guessing

## Polling and reporting behavior

- Poll long-running jobs with `atlas_jobs_get` and report status transitions only (`queued` -> `running` -> `succeeded/failed`)
- Always include:
  - `runId`
  - manifest path
  - stdout/stderr log paths
  - summarized artifact paths
- Prefer artifact summaries over raw JSON dumps

## Guardrails

- Do not overwrite `config/active/asx.json` directly from a heuristic summary.
- Treat `auto_reoptimize` as high-risk; prefer explicit tool-orchestrated steps.
- Preserve backups and record exact artifact paths used in the decision.
- Require explicit user confirmation before `reoptimize_full_universe` because it is long-running and expensive.
- Validate the staged candidate via `validate_oos --config-path` before any promotion.
- Use `atlas_risk_check_reopt_promotion` before `atlas_risk_promote_config`.

## Current artifact expectations (Atlas repo)

- Health report: `logs/health_check_YYYY-MM-DD.json`
- Reoptimization report: `backtest/results/reoptimization_full_universe.json`
- Active OOS validation report: `backtest/results/v92_oos_validation.json`
- Candidate OOS validation report (recommended): `backtest/results/v92_oos_validation_candidate_<timestamp>.json`

## Known codebase constraints

- `reoptimize_full_universe.py` now stages a candidate by default; `--promote-active` is opt-in and high-risk.
- `atlas_risk_check_reopt_promotion` applies conservative artifact thresholds; tune thresholds before unattended use.
- `auto_reoptimize.py` remains high-risk legacy automation even after staged-candidate improvements.

## Comparison Workflow

Use `atlas_artifacts_compare` to produce numeric deltas between the candidate and baseline validation artifacts:

```
atlas_artifacts_compare(
    leftPath="backtest/results/v92_oos_validation.json",   # baseline (left = before)
    rightPath=<candidate_validation_path>,                  # candidate (right = after)
    kind="validate_oos"
)
```

**Key degradation thresholds:**

| Metric | Reject candidate if degraded by |
|--------|---------------------------------|
| Sharpe ratio | > 10% drop |
| CAGR | > 20% drop |
| Max drawdown | > 15% worse (higher absolute value) |
| Win rate | > 10% drop |
| Trade count | > 30% fewer (may indicate over-filtering) |

- Also compare IS reoptimization results: `atlas_artifacts_compare(leftPath=<baseline_reopt>, rightPath="backtest/results/reoptimization_full_universe.json", kind="reoptimization_full_universe")`
- If no baseline artifact exists, use `atlas_risk_list_config_backups` to identify the nearest pre-promotion backup and treat its corresponding artifact as the reference
- Numeric deltas alone are not sufficient — always read full artifact summaries for qualitative context

## Known Pitfalls

Lessons learned from Atlas operational history. Check these before and after each reoptimization run.

**Pitfall 1 — stage_candidate() clobbering the active config** (Lesson #34)
Always verify `candidate_config_path ≠ active_config_path` before and after reoptimization. If `active_config_overwritten: true` appears in the artifact summary, stop immediately and restore from backup with `atlas_risk_restore_config_backup`. This is a silent failure mode — the workflow does not error out.

**Pitfall 2 — Degenerate optimization: 3–4 trades, PF = Infinity** (Lesson #2)
A result showing very few trades and profit factor = Infinity means the optimizer found parameters that almost never trade. This is not alpha — it is overfitting to silence. Check the `min_trades` threshold in the reoptimization config; set it to ≥ 20 for any statistically meaningful result.

**Pitfall 3 — Blending config parameters across competing peaks** (Lesson #3)
If reoptimization surfaces two competing parameter peaks (e.g., fast vs slow lookback), do not average them. Blended configs degrade performance at both peaks. Identify the dominant peak via OOS validation and use it exclusively.

**Pitfall 4 — Skipping OOS validation before promotion** (Lesson #6)
Reoptimization in-sample performance alone cannot justify promotion. All three OOS tests (OOS ratio, window win rate, perturbation robustness) must pass. Skipping even one test has historically led to promoted configs that failed in live trading within weeks.

**Pitfall 5 — Solo parameter sweeps at low equity** (Lesson #30)
At low account equity (< $10k), single-parameter sweeps produce noisy results because position sizing amplifies variance on each individual trade. Use combined mode (sweeping multiple parameters together under constraints) for more stable results. Solo sweeps are acceptable only after 30+ closed trades have accumulated.

**Pitfall 6 — Running reoptimization during market hours**
The reoptimizer and the intraday monitor can conflict on the candidate config path. Check `atlas_state_lock_status(name="daily-workflow")` before starting; if the daily workflow lock is held, wait until after market close before proceeding.

**Pitfall 7 — Stale candidate configs from interrupted prior runs**
An interrupted reoptimization may have left a stale `config_candidate_reoptimized_*.json` on disk. Always use a fresh timestamped path; verify with `ls config/` before the run to avoid accidentally loading a previous run's candidate as the baseline.
