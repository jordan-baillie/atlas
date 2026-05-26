# Repo Reset Sprint — Runtime-State Quarantine

**Date:** 2026-05-26
**Tasks:** #362 (repo hygiene), #363 seed inventory
**Decision source:** `/root/ceo-board/memos/2026-05-26-atlas-codebase-simplification-vs-v2/memo.md`

## Scope

This sweep implements the first Atlas simplification target: stop mutable runtime state from dirtying git. It does **not** change live trading semantics, SP500 strategy parameters, broker behavior, kill switches, or risk gates.

Dirty-tree write swarms were intentionally not used: the board's red line is no write-agent dispatch while the working tree is dirty. The work was performed as direct gated repo hygiene after read-only inventory.

## Inventory summary

`python3 scripts/git-hooks/check_no_runtime_artifacts.py --all-tracked` identified **696 tracked runtime/generated artifacts** before cleanup:

| Category | Count | Examples |
|---|---:|---|
| Pi local agent state | 10 | `.pi/prompts/*`, `.pi/expertise/*` |
| Top-level batch backups | 20 | `backups-pre-batch-*`, `backups-pre-batch-LATEST.txt` |
| Broker live state | 11 | `brokers/state/live_*.json`, state backups |
| Config runtime/cache/audit | 29 | `config/.oos_cache/*`, `config/audit_log/*`, promotion logs |
| Data runtime/audits/verification | 56 | `data/*_state.json`, `data/dual_write_verification.json`, `data/audit/*` |
| Data snapshots | 408 | `data/snapshots/**` parquet snapshots |
| Generated trade plans | 51 | `plans/plan_*.json` |
| Generated research outputs | 108 | `research/best/**`, generated brain strategy summaries, queue/journal/results |
| Journal backup files | 3 | `journal/archive/**/*.bak.*` |

All targeted files were removed from git's index with `git rm --cached` so local runtime files remain on disk.

## Guardrails added

- Extended `.gitignore` for runtime state, generated plans, data snapshots, research outputs, config caches/audit logs, and batch backups.
- Added `scripts/git-hooks/check_no_runtime_artifacts.py`:
  - default mode blocks staged runtime artifacts in pre-commit;
  - `--all-tracked` audits the entire git index;
  - deletions are allowed so cleanup commits can proceed.
- Registered the guard in both:
  - `.pre-commit-config.yaml` (`no-runtime-artifacts` local hook), and
  - raw hook `scripts/git-hooks/pre-commit`.

## Config cleanup note

`config/active/commodity_etfs.json` and `config/active/sector_etfs.json` were already absent from the working tree before this sweep and were staged as deletions. This is treated as dormant/consolidated-market cleanup only. `config/active/sp500.json` is unchanged.

Rollback path for inactive configs remains available through:

- `config/active/archive/commodity_etfs.json.archived-20260525`
- `config/active/archive/sector_etfs.json.archived-20260525`
- git history before this cleanup commit

## Untracked artifact handling

- Moved one untracked one-off runner out of the repo: `/var/atlas/repo-reset-20260526/untracked-artifacts/run_clean_solo_batch_a.py`.
- Removed empty stray file: `/root/atlas/file`.
- Kept new planning docs under `docs/` as durable project documentation.

## Verification commands

Run after staging/commit:

```bash
python3 scripts/git-hooks/check_no_runtime_artifacts.py --all-tracked
python3 -m py_compile scripts/git-hooks/check_no_runtime_artifacts.py
bash -n scripts/git-hooks/pre-commit
python3 scripts/cli.py status
```

Final acceptance for #362 still requires observing `git status --short` after the next full daily ops cycle. Do not execute live trades solely for this acceptance check.
