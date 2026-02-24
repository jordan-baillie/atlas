---
name: atlas-daily
description: "Run Atlas-ASX daily paper-trading operations with explicit approval gates: data refresh, plan generation, risk summary, plan approval, execution, and dashboard refresh. Use for daily operational runs and incident response on daily automation failures."
---

# Atlas Daily

Use this skill when operating the day-to-day paper-trading workflow.

## Primary goals

- Generate or inspect today's plan without bypassing approval requirements
- Execute only approved plans
- Refresh dashboard artifacts after plan or execution changes
- Keep a clear audit trail of which job ran and what artifacts changed

## Preferred tool flow

1. Call `atlas_jobs_list_catalog` once if job names are unclear.
2. Run `atlas_jobs_run` with `job=cli_plan` (optionally `args.date=YYYY-MM-DD`).
3. Summarize `paper_engine/plans/plan_YYYY-MM-DD.json` risk and entries before any approval.
4. Require explicit user approval before `cli_approve` and `cli_paper_run`.
5. Run `atlas_jobs_run` with `job=dashboard_generate_data` after plan or execution changes.

## Safety rules

- Do not use `daily_automation` for normal operations until auto-approval behavior is removed or gated.
- Treat `cli_approve` and `cli_paper_run` as high-risk actions requiring user confirmation.
- If `config/active_config.json` has `"approval_required": true`, preserve that intent.

## Repo-specific notes

- Plan state lives under `paper_engine/plans/`.
- Portfolio state lives at `paper_engine/portfolio_state.json`.
- Dashboard reads portfolio, plan, ledger, and backtest artifacts.
