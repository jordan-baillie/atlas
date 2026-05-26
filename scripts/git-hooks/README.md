# Atlas Git Hooks

This directory contains the canonical version of Atlas git hooks.
The live hooks live under `.git/hooks/` (not tracked by git).

## Install

```bash
bash scripts/install-git-hooks.sh
```

This copies `scripts/git-hooks/pre-commit` → `.git/hooks/pre-commit` and marks it executable.

The project also defines hooks via `.pre-commit-config.yaml`. If you have the `pre-commit`
framework installed, run `pre-commit install` to activate those hooks as well:

```bash
pip install pre-commit
pre-commit install
```

## What the pre-commit hook does

1. **Secret file block** — prevents committing `.env`, `.secrets.json`, `atlas-secrets`
2. **Credential pattern scan** — blocks obvious credential patterns in staged Python/JSON/YAML
3. **Bash syntax check** — runs `bash -n` on staged `.sh` files
4. **Python syntax check** — runs `python3 -m py_compile` on staged `.py` files
5. **Runtime artifact guard** _(added 2026-05-26 Repo Reset)_ — blocks mutable Atlas runtime/state/backup/research artifacts from being re-tracked. Use `python3 scripts/git-hooks/check_no_runtime_artifacts.py --all-tracked` to audit the index.
6. **Config/active research gate** _(added 2026-05-06, Rec 1.6; fixed #319 2026-05-11)_ — blocks
   direct edits to `config/active/*.json` unless:
   - A matching `auto_promote()` log entry exists within the last 24h, OR
   - `BYPASS_RESEARCH_GATE` env var is set (see below), OR
   - You pass `--no-verify` (git-traced override)
7. **Strategy lifecycle guard** _(added lifecycle-1.6, 2026-05-14)_ — blocks enabling a strategy
   in `config/active/*.json` unless `strategy_lifecycle` has a `LIVE` or `PAPER` row for that
   `(strategy, universe)` pair. See details below.

## Strategy lifecycle guard (hook #6)

### What it checks

When any staged `config/active/*.json` file has a strategy whose `enabled` flag changes
from `false` → `true` (or is newly added as `true`), the hook queries `data/atlas.db`:

```sql
SELECT state FROM strategy_lifecycle WHERE strategy = ? AND universe = ?
```

- **State is `LIVE` or `PAPER`** → commit proceeds normally (silent pass).
- **State is `RETIRED`, `RESEARCH`, or missing** → commit is **blocked** with explanation.

### Error output example

```
❌ Pre-commit guard: cannot enable strategies without a LIVE/PAPER lifecycle row:

  • config/active/sp500.json: strategy=my_strat, universe=sp500 — lifecycle state is MISSING (must be LIVE or PAPER)

Fix: promote via scripts/promote_strategy_to_paper.py first, OR
     insert a strategy_lifecycle row explicitly.
Bypass (use with caution): git commit --no-verify
```

### Canonical source files

| File | Purpose |
|------|---------|
| `scripts/git-hooks/pre-commit-lifecycle-guard.sh` | Bash entry point (finds staged files, calls Python helper) |
| `scripts/git-hooks/check_lifecycle_for_enabled.py` | Python logic (diffs HEAD vs staged, queries SQLite) |
| `scripts/git-hooks/check_no_runtime_artifacts.py` | Repo hygiene guard for generated runtime artifacts |
| `.pre-commit-config.yaml` hook `lifecycle-enabled-guard` | Pre-commit framework registration |

### Install / re-install

```bash
# Raw bash hook (runs on every git commit without pre-commit framework):
bash scripts/install-git-hooks.sh

# Pre-commit framework (optional, must have pre-commit installed):
pre-commit install
```

## Config/active gate rationale

The `config/active/*.json` files control which strategies run live with real money.
Historically these were edited by hand, bypassing the research-to-promotion pipeline.
The gate ensures every production config change has an `auto_promote()` audit trail
in `config/promotion_log.json`.

## Legitimate bypasses

**Env var (recommended for config/active gate + lifecycle guard):**
```bash
BYPASS_RESEARCH_GATE="emergency rollback" git commit -m "chore: roll back sector_etfs config"
BYPASS_RESEARCH_GATE="infra only, no param change" git commit -m "chore: update comment in sp500.json"
```

The `BYPASS_RESEARCH_GATE` env var skips **both** the auto_promote audit check AND the
lifecycle enabled guard, since both guard the same files.

**--no-verify (last resort, skips ALL hooks):**
```bash
git commit --no-verify -m "..."   # skips ALL hook checks; git records this in reflog
```

> **Note (#319, 2026-05-11):** An in-commit-message marker (e.g. `BYPASS_RESEARCH_GATE: reason`
> in `-m "..."`) does **not** work. Git does not write `.git/COMMIT_EDITMSG` before the
> pre-commit hook runs, so the marker cannot be read. Use the env var approach instead.

## Fresh-clone setup

```bash
git clone <repo>
cd atlas
bash scripts/install-git-hooks.sh
# Optional, if pre-commit framework is installed:
pre-commit install
```
