#!/usr/bin/env bash
# Idempotently install Atlas git hooks. Run from repo root or anywhere.
set -euo pipefail

REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
HOOKS_DIR="$REPO_ROOT/.git/hooks"
SOURCE_DIR="$REPO_ROOT/scripts/git-hooks"

mkdir -p "$HOOKS_DIR"
mkdir -p "$SOURCE_DIR"

# If a canonical pre-commit lives in scripts/git-hooks/, install it.
if [ -f "$SOURCE_DIR/pre-commit" ]; then
    cp "$SOURCE_DIR/pre-commit" "$HOOKS_DIR/pre-commit"
    chmod +x "$HOOKS_DIR/pre-commit"
    echo "✓ installed pre-commit hook from $SOURCE_DIR/pre-commit"
else
    echo "⚠ no canonical hook at $SOURCE_DIR/pre-commit — current hook left as-is"
fi

echo "Hooks status:"
ls -l "$HOOKS_DIR/pre-commit" 2>/dev/null || echo "  (no pre-commit installed)"
