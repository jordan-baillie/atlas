"""
Tests for the remediation team entry in /root/.pi/teams/config.yaml.

Assertions:
 1.  config.yaml is valid YAML
 2.  teams.remediation exists
 3.  lead.name == "Remediation Lead"
 4.  lead.model == "claude-opus-4-7"
 5.  Two members: "Fix Worker" + "Review Worker"
 6.  Fix Worker domain.write does NOT include any NEVER-list path
 7.  Fix Worker domain.write includes tests/**, dashboard-ui/src/**, docs/**
 8.  Review Worker domain.write only includes its own expertise dir
 9.  All 3 system_prompt files exist on disk
10.  fix-worker.md contains the NEVER list and "ESCALATE:" instruction
11.  review-worker.md contains "default verdict is REJECT"
12.  review-worker.md contains all 8 APPROVE conditions
"""

import re
from pathlib import Path

import pytest
import yaml

CONFIG_PATH = Path("/root/.pi/teams/config.yaml")
PROMPTS_DIR = Path("/root/.pi/teams/prompts")

NEVER_LIST_PREFIXES = [
    "brokers/",
    "risk/",
    "regime/",
    "signals/",
    "monitor/",
    "portfolio/",
    "overlay/",
    "strategies/",
    "core/reconcile",
    "plans/",
    "approve/",
    "kill_switch",
    "live_executor",
    "services/telegram_bot",
    "db/atlas_db",
    "db/schema.sql",
    "scripts/migrations/",
    "config/active/",
    "data/",
    "brokers/state/",
    ".atlas-secrets",
    ".env",
]


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def config() -> dict:
    with open(CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def remediation(config) -> dict:
    return config["teams"]["remediation"]


@pytest.fixture(scope="module")
def fix_worker(remediation) -> dict:
    members = {m["name"]: m for m in remediation["members"]}
    return members["Fix Worker"]


@pytest.fixture(scope="module")
def review_worker(remediation) -> dict:
    members = {m["name"]: m for m in remediation["members"]}
    return members["Review Worker"]


# ── tests ─────────────────────────────────────────────────────────────────────

def test_1_config_is_valid_yaml():
    """config.yaml must parse without error."""
    with open(CONFIG_PATH) as fh:
        result = yaml.safe_load(fh)
    assert isinstance(result, dict), "Parsed YAML must be a dict"


def test_2_remediation_team_exists(config):
    assert "remediation" in config["teams"], "teams.remediation key is missing"


def test_3_lead_name(remediation):
    assert remediation["lead"]["name"] == "Remediation Lead"


def test_4_lead_model(remediation):
    assert remediation["lead"]["model"] == "claude-opus-4-7"


def test_5_two_members(remediation):
    names = [m["name"] for m in remediation["members"]]
    assert "Fix Worker" in names, f"Fix Worker missing; got {names}"
    assert "Review Worker" in names, f"Review Worker missing; got {names}"
    assert len(names) == 2, f"Expected exactly 2 members, got {len(names)}: {names}"


def test_6_fix_worker_write_excludes_never_list(fix_worker):
    write_paths = fix_worker["domain"]["write"]
    violations = []
    for entry in write_paths:
        entry_norm = str(entry).lstrip("/")
        for bad_prefix in NEVER_LIST_PREFIXES:
            if entry_norm.startswith(bad_prefix) or entry_norm == bad_prefix.rstrip("/"):
                violations.append(f"'{entry}' matches NEVER-list prefix '{bad_prefix}'")
    assert not violations, "Fix Worker write domain includes NEVER-list paths:\n" + "\n".join(violations)


def test_7_fix_worker_write_includes_required_paths(fix_worker):
    write_paths = fix_worker["domain"]["write"]
    required = ["tests/**", "dashboard-ui/src/**", "docs/**"]
    for req in required:
        assert req in write_paths, (
            f"Fix Worker domain.write missing required path '{req}'. "
            f"Current write list: {write_paths}"
        )


def test_8_review_worker_write_only_expertise(review_worker):
    write_paths = review_worker["domain"]["write"]
    assert write_paths == [".pi/expertise/review-worker/**"], (
        f"Review Worker domain.write should only contain its expertise dir. "
        f"Got: {write_paths}"
    )


def test_9_system_prompt_files_exist(remediation):
    prompt_refs = [
        remediation["lead"]["system_prompt"],
        *[m["system_prompt"] for m in remediation["members"]],
    ]
    missing = []
    for ref in prompt_refs:
        candidate = Path("/root") / ref if not ref.startswith("/") else Path(ref)
        if not candidate.exists():
            missing.append(str(candidate))
    assert not missing, "Missing system_prompt files:\n" + "\n".join(missing)


def test_10_fix_worker_prompt_has_never_list_and_escalate():
    text = (PROMPTS_DIR / "fix-worker.md").read_text()
    for expected in ["brokers/", "risk/", "kill_switch", "live_executor"]:
        assert expected in text, f"fix-worker.md missing NEVER-list entry: '{expected}'"
    assert "ESCALATE:" in text, "fix-worker.md missing 'ESCALATE:' instruction"


def test_11_review_worker_prompt_has_default_reject():
    text = (PROMPTS_DIR / "review-worker.md").read_text()
    assert re.search(r"default verdict is REJECT", text, re.IGNORECASE), (
        "review-worker.md must contain 'default verdict is REJECT'"
    )


def test_12_review_worker_prompt_has_all_8_approve_conditions():
    text = (PROMPTS_DIR / "review-worker.md").read_text()
    conditions = [
        "root cause",
        "capital loss",
        "error handling",
        "catch",
        "assertion",
        "retry",
        "risk threshold",
        "xfail",
    ]
    missing = [c for c in conditions if c.lower() not in text.lower()]
    assert not missing, (
        "review-worker.md is missing coverage for APPROVE condition keywords: "
        + str(missing)
    )
