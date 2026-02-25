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

### Phase 4: Promotion decision checkpoint (human-in-the-loop)

1. Run the reoptimization promotion gate (config + artifact thresholds):
   - `atlas_risk_check_reopt_promotion(candidatePath=<candidate_config_path>, validationPath=<candidate_validation_path>, reoptimizationPath="backtest/results/reoptimization_full_universe.json", baselineValidationPath="backtest/results/v92_oos_validation.json")`
2. Present:
   - health summary
   - reoptimization summary
   - candidate OOS validation summary
   - candidate-vs-baseline validation comparison
   - promotion gate verdict + blockers/warnings
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
