---
name: atlas-research
description: Run Atlas research and validation experiments (backtests, annealing reviews, strategy checks) and produce structured summaries of hypotheses, metrics, and artifacts. Use for iterative strategy research and postmortems.
---

# Atlas Research

Use this skill for exploratory but traceable research runs.

## Typical tasks

- Run `cli_backtest` and summarize metrics
- Run `anneal_review` to generate hypotheses and candidate changes
- Inspect strategy-specific artifacts under `backtest/results/`
- Prepare follow-up experiment plans with clear acceptance criteria

## Working style

- Prefer small, named experiments over broad script sweeps.
- Record the exact config version and data freshness assumptions.
- Distinguish in-sample improvements from OOS robustness evidence.

## Minimum summary template

- Objective
- Job(s) run
- Artifact paths
- Key metrics
- Decision (accept / reject / needs validation)
- Next experiment
