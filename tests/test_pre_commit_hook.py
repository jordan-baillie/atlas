"""Test the pre-commit hook blocks config/active edits without audit trail.

Uses a temporary git repo that mirrors the hook logic.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


def _setup_tmp_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with the atlas pre-commit hook installed."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_path, check=True)
    # Copy the canonical pre-commit hook from scripts/git-hooks/
    atlas = Path(__file__).resolve().parent.parent
    src_hook = atlas / "scripts" / "git-hooks" / "pre-commit"
    dst_hook = tmp_path / ".git" / "hooks" / "pre-commit"
    shutil.copy2(src_hook, dst_hook)
    dst_hook.chmod(0o755)
    return tmp_path


def _add_config_and_promotion_log(repo: Path, promotion_entries: list | None = None) -> None:
    """Create config/active/sp500.json and config/promotion_log.json in the repo."""
    cfg_dir = repo / "config" / "active"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sp500.json").write_text(json.dumps({"market": "sp500", "_test": True}))
    log_entries = promotion_entries or []
    (repo / "config" / "promotion_log.json").write_text(json.dumps(log_entries))


def test_hook_blocks_config_active_without_audit(tmp_path: Path):
    """No recent auto_promote entry → commit blocked."""
    repo = _setup_tmp_repo(tmp_path)
    _add_config_and_promotion_log(repo, [])  # empty log

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Tweak active config"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, "Hook should have blocked the commit"
    combined = result.stdout + result.stderr
    assert "BLOCKED" in combined or "auto_promote" in combined, (
        f"Expected BLOCKED message, got: {combined}"
    )


def test_hook_blocks_when_only_old_promotion_entries(tmp_path: Path):
    """Promotion log entry older than 24h → still blocked."""
    repo = _setup_tmp_repo(tmp_path)
    old_entry = {
        "timestamp": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(),
        "strategy": "mean_reversion",
        "market": "sp500",
    }
    _add_config_and_promotion_log(repo, [old_entry])

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Late edit"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, "Old promotion entry (>24h) should not satisfy the gate"


def test_hook_allows_with_recent_promotion_entry(tmp_path: Path):
    """Recent auto_promote log entry within 24h → commit allowed."""
    repo = _setup_tmp_repo(tmp_path)
    recent_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "strategy": "mean_reversion",
        "market": "sp500",
    }
    _add_config_and_promotion_log(repo, [recent_entry])

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Post-promote config update"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Recent promotion entry should allow commit, got: {result.stdout}{result.stderr}"
    )


def test_hook_allows_with_bypass_marker(tmp_path: Path):
    """BYPASS_RESEARCH_GATE env var → allowed even with empty log."""
    repo = _setup_tmp_repo(tmp_path)
    _add_config_and_promotion_log(repo, [])  # empty log

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Emergency fix"],
        cwd=repo, capture_output=True, text=True,
        env={**__import__("os").environ, "BYPASS_RESEARCH_GATE": "emergency rollback"},
    )
    assert result.returncode == 0, (
        f"BYPASS_RESEARCH_GATE env var override should allow commit, got: {result.stdout}{result.stderr}"
    )


def test_hook_ignores_unrelated_changes(tmp_path: Path):
    """Changes to non-config files are not blocked."""
    repo = _setup_tmp_repo(tmp_path)
    (repo / "README.md").write_text("# test repo")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Add readme"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"Non-config changes should not be blocked, got: {result.stderr}"
    )


def test_hook_ignores_config_non_active(tmp_path: Path):
    """Changes to config/ but not config/active/*.json are allowed."""
    repo = _setup_tmp_repo(tmp_path)
    cfg_dir = repo / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "markets.json").write_text(json.dumps({"sp500": True}))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Update markets config"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        f"config/markets.json is not guarded, got: {result.stderr}"
    )


def test_hook_no_promotion_log_file(tmp_path: Path):
    """Missing promotion_log.json → treated as empty → blocked."""
    repo = _setup_tmp_repo(tmp_path)
    # Create config/active but NO promotion_log.json
    cfg_dir = repo / "config" / "active"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "sp500.json").write_text('{"market":"sp500"}')
    # No promotion_log.json

    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    result = subprocess.run(
        ["git", "commit", "-m", "Tweak config"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0, "Missing promotion log should block commit"
