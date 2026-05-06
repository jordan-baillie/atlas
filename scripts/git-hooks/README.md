# Atlas Git Hooks

This directory contains the canonical version of Atlas git hooks.
The live hooks live under `.git/hooks/` (not tracked by git).

## Install

```bash
bash scripts/install-git-hooks.sh
```

This copies `scripts/git-hooks/pre-commit` → `.git/hooks/pre-commit` and marks it executable.

## What the pre-commit hook does

1. **Secret file block** — prevents committing `.env`, `.secrets.json`, `atlas-secrets`
2. **Credential pattern scan** — blocks obvious credential patterns in staged Python/JSON/YAML
3. **Bash syntax check** — runs `bash -n` on staged `.sh` files
4. **Python syntax check** — runs `python3 -m py_compile` on staged `.py` files
5. **Config/active research gate** _(added 2026-05-06, Rec 1.6)_ — blocks direct edits to
   `config/active/*.json` unless:
   - A matching `auto_promote()` log entry exists within the last 24h, OR
   - The commit message contains `BYPASS_RESEARCH_GATE: <reason>`, OR
   - You pass `--no-verify` (git-traced override)

## Config/active gate rationale

The `config/active/*.json` files control which strategies run live with real money.
Historically these were edited by hand, bypassing the research-to-promotion pipeline.
The gate ensures every production config change has an `auto_promote()` audit trail
in `config/promotion_log.json`.

Legitimate bypasses:
- Emergency rollback: use `BYPASS_RESEARCH_GATE: emergency rollback` in commit message
- Infra-only change (not params): use same bypass marker with reason
- Git hook missing on a fresh clone: run `bash scripts/install-git-hooks.sh` first

## Fresh-clone setup

```bash
git clone <repo>
cd atlas
bash scripts/install-git-hooks.sh
```
