#!/usr/bin/env python3
"""Atlas Hypothesis Tracker — track, test, and detect patterns.

The coordinator agent generates hypotheses. The daemon uses this module to:
1. Check if experiment results confirm/reject any queued hypotheses
2. Detect mechanical patterns that should be flagged for agent review
3. Read/write hypothesis vault notes

Hypotheses lifecycle: proposed → queued → testing → confirmed | rejected
"""

import sys
from pathlib import Path

ATLAS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATLAS_ROOT))

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("hypothesis_tracker")

HYPOTHESES_DIR = ATLAS_ROOT / "research" / "vault" / "Hypotheses"
PATTERNS_DIR = ATLAS_ROOT / "research" / "vault" / "Patterns"
PRIORITIES_PATH = ATLAS_ROOT / "research" / "vault" / "Meta" / "Research Priorities.md"
JOURNAL_PATH = ATLAS_ROOT / "research" / "journal.json"
PARAMS_DIR = ATLAS_ROOT / "research" / "vault" / "Parameters"
EXPERIMENTS_DIR = ATLAS_ROOT / "research" / "experiments"


# ─── YAML Frontmatter Helpers ────────────────────────────────────────────────


def _yaml_str(val: Any) -> str:
    """Render a YAML scalar (matching vault_writer behaviour)."""
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    s = str(val)
    needs_quote = any(
        c in s
        for c in (':', '#', '[', ']', '{', '}', ',', '&', '*', '?',
                  '|', '-', '<', '>', '=', '!', '%', '@', '`',
                  '\n', '"', "'")
    )
    if needs_quote:
        escaped = s.replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _build_frontmatter(fields: Dict[str, Any]) -> str:
    """Build YAML frontmatter block from an ordered dict."""
    lines = ["---"]
    for key, val in fields.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {_yaml_str(item)}")
        else:
            lines.append(f"{key}: {_yaml_str(val)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from markdown content.

    Handles scalar values and simple lists (``key:\\n  - item`` style).
    Strips surrounding quotes from values. Returns empty dict if no frontmatter.
    """
    if not content.startswith("---"):
        return {}
    lines = content.split("\n")
    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}

    fm: Dict[str, Any] = {}
    current_key: Optional[str] = None
    current_list: Optional[List[str]] = None

    def _strip_quotes(s: str) -> str:
        if len(s) >= 2 and (
            (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")
        ):
            return s[1:-1]
        return s

    for line in lines[1:end_idx]:
        # List continuation (leading spaces + "- ")
        list_match = re.match(r"^  - (.*)$", line)
        if list_match and current_list is not None:
            current_list.append(_strip_quotes(list_match.group(1).strip()))
            continue

        # Flush pending list before moving to next key
        if current_key is not None and current_list is not None:
            fm[current_key] = current_list
            current_list = None

        if not line or line[0] == " ":
            continue  # indented non-list line — skip

        if ": " in line:
            key, _, val = line.partition(": ")
            current_key = key.strip()
            fm[current_key] = _strip_quotes(val.strip())
            current_list = None
        elif line.endswith(":"):
            current_key = line[:-1].strip()
            current_list = []
        # else: unrecognised line format — ignore

    # Flush final list
    if current_key is not None and current_list is not None and current_key not in fm:
        fm[current_key] = current_list

    return fm


def _replace_frontmatter(content: str, updated_fields: Dict[str, Any]) -> str:
    """Merge *updated_fields* into existing frontmatter and return new content.

    Preserves all existing fields not mentioned in *updated_fields*.
    """
    existing = _parse_frontmatter(content)
    existing.update(updated_fields)

    # Locate old frontmatter end
    lines = content.split("\n")
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        # No existing frontmatter — prepend new one
        return _build_frontmatter(existing) + "\n" + content

    body = "\n".join(lines[end_idx + 1:])
    return _build_frontmatter(existing) + "\n" + body


# ─── Hypothesis CRUD ────────────────────────────────────────────────────────


def _next_hypothesis_id() -> str:
    """Return the next sequential hypothesis ID (H1, H2, …)."""
    HYPOTHESES_DIR.mkdir(parents=True, exist_ok=True)
    existing = list(HYPOTHESES_DIR.glob("H*.md"))
    max_n = 0
    for path in existing:
        m = re.match(r"^H(\d+)", path.stem)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"H{max_n + 1}"


def create_hypothesis(
    title: str,
    hypothesis: str,
    test_plan: str,
    expected_outcome: str,
    related_experiments: Optional[List[str]] = None,
    related_strategies: Optional[List[str]] = None,
    source: str = "agent",
) -> Path:
    """Create a new hypothesis vault note.

    Args:
        title: Short descriptive title (used in filename).
        hypothesis: The statement being tested.
        test_plan: How to run the test.
        expected_outcome: What result confirms/rejects.
        related_experiments: List of experiment IDs (becomes [[wiki-links]]).
        related_strategies: List of strategy names.
        source: "agent" or "pattern_detector".

    Returns:
        Path to the created file.
    """
    HYPOTHESES_DIR.mkdir(parents=True, exist_ok=True)

    hyp_id = _next_hypothesis_id()
    now = datetime.now(timezone.utc)
    created_str = now.isoformat()

    # Sanitise title for use in filename (strip chars unsafe on most FSes)
    safe_title = re.sub(r'[<>:"/\\|?*]', "", title).strip()
    filename = f"{hyp_id} - {safe_title}.md"
    path = HYPOTHESES_DIR / filename

    fm = {
        "id": hyp_id,
        "title": title,
        "status": "proposed",
        "source": source,
        "created": created_str[:10],
        "tags": [
            "hypothesis",
            "status/proposed",
        ],
    }

    related_exp = related_experiments or []
    related_strat = related_strategies or []

    exp_links = "\n".join(f"- [[{e}]]" for e in related_exp) if related_exp else "_None_"
    strat_links = "\n".join(f"- [[{s}]]" for s in related_strat) if related_strat else "_None_"

    body_lines = [
        _build_frontmatter(fm),
        "",
        f"# {hyp_id}: {title}",
        "",
        f"> **Status:** `proposed` | **Source:** {source} | **Created:** {created_str[:10]}",
        "",
        "## Hypothesis",
        "",
        hypothesis,
        "",
        "## Test Plan",
        "",
        test_plan,
        "",
        "## Expected Outcome",
        "",
        expected_outcome,
        "",
        "## Related Experiments",
        "",
        exp_links,
        "",
        "## Related Strategies",
        "",
        strat_links,
        "",
    ]
    path.write_text("\n".join(body_lines))
    logger.info("Created hypothesis %s: %s", hyp_id, title)
    return path


def list_hypotheses(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all hypotheses, optionally filtered by status.

    Args:
        status: If given, only return hypotheses with this status string.

    Returns:
        List of dicts with keys: id, title, status, source, created, path, hypothesis.
        Sorted by creation date (oldest first).
    """
    HYPOTHESES_DIR.mkdir(parents=True, exist_ok=True)
    results = []

    for path in sorted(HYPOTHESES_DIR.glob("H*.md")):
        try:
            content = path.read_text()
        except OSError:
            continue
        fm = _parse_frontmatter(content)

        hyp_status = fm.get("status", "proposed")
        if status is not None and hyp_status != status:
            continue

        # Extract hypothesis text from body (section after ## Hypothesis)
        hyp_text = ""
        m = re.search(r"## Hypothesis\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if m:
            hyp_text = m.group(1).strip()

        results.append(
            {
                "id": fm.get("id", path.stem.split(" - ")[0]),
                "title": fm.get("title", path.stem),
                "status": hyp_status,
                "source": fm.get("source", "unknown"),
                "created_date": fm.get("created", ""),
                "path": path,
                "hypothesis": hyp_text,
            }
        )

    # Sort by creation date string (ISO date sorts lexicographically)
    results.sort(key=lambda h: h.get("created_date", ""))
    return results


def update_hypothesis_status(
    hypothesis_id: str,
    new_status: str,
    result_summary: Optional[str] = None,
    confirming_experiments: Optional[List[str]] = None,
) -> Optional[Path]:
    """Update hypothesis status and optionally append a result section.

    Args:
        hypothesis_id: E.g. "H1".
        new_status: One of: proposed, queued, testing, confirmed, rejected.
        result_summary: Free-form text describing the result (appended to file).
        confirming_experiments: Experiment IDs that support this conclusion.

    Returns:
        Path to updated file, or None if not found.
    """
    HYPOTHESES_DIR.mkdir(parents=True, exist_ok=True)
    matches = list(HYPOTHESES_DIR.glob(f"{hypothesis_id} - *.md"))
    if not matches:
        logger.warning("Hypothesis %s not found in %s", hypothesis_id, HYPOTHESES_DIR)
        return None

    path = matches[0]
    content = path.read_text()

    # Update frontmatter
    content = _replace_frontmatter(
        content,
        {
            "status": new_status,
            "tags": [
                "hypothesis",
                f"status/{new_status}",
            ],
        },
    )

    # Replace status line in H1 header callout if present
    content = re.sub(
        r"(?m)(\*\*Status:\*\* `)[\w/]+(`)",
        rf"\g<1>{new_status}\g<2>",
        content,
    )

    # Append result section
    if result_summary or confirming_experiments:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        append_lines = [
            "",
            f"## Result — {now_str}",
            "",
            f"**Status updated to:** `{new_status}`",
            "",
        ]
        if result_summary:
            append_lines += [result_summary, ""]
        if confirming_experiments:
            append_lines.append("**Supporting experiments:**")
            append_lines.append("")
            for exp in confirming_experiments:
                append_lines.append(f"- [[{exp}]]")
            append_lines.append("")
        content = content.rstrip("\n") + "\n" + "\n".join(append_lines)

    path.write_text(content)
    logger.info("Updated hypothesis %s → %s", hypothesis_id, new_status)
    return path


# ─── Mechanical Pattern Detection ────────────────────────────────────────────


def _load_journal() -> List[Dict[str, Any]]:
    """Load journal.json, deduplicated by experiment_id (latest timestamp wins).

    Returns empty list on error or if file doesn't exist.
    """
    if not JOURNAL_PATH.exists():
        return []
    try:
        with open(JOURNAL_PATH) as f:
            raw: List[Dict[str, Any]] = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    # Deduplicate: keep latest entry per experiment_id
    seen: Dict[str, Dict[str, Any]] = {}
    for entry in raw:
        eid = entry.get("experiment_id", "")
        if not eid:
            continue  # skip entries with no ID
        existing = seen.get(eid)
        if existing is None or entry.get("timestamp", "") >= existing.get("timestamp", ""):
            seen[eid] = entry
    return list(seen.values())


def _load_experiment(exp_id: str) -> Optional[Dict[str, Any]]:
    """Load an experiment file by ID (tries both exp- and eval- prefixes)."""
    for prefix in ("exp-", "eval-", ""):
        path = EXPERIMENTS_DIR / f"{prefix}{exp_id}.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
    return None


def _detect_param_convergence(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """PARAM_CONVERGENCE: Same param found optimal across 3+ strategies.

    Reads Parameters/ vault notes for recorded optimal_values. Falls back to
    scanning experiment outputs for param_sweep results.
    """
    patterns: List[Dict[str, Any]] = []

    # ── From Parameters/ vault notes ──────────────────────────────────────
    param_optima: Dict[str, List[Dict[str, Any]]] = defaultdict(list)  # param_name → [{strategy, optimal}]

    if PARAMS_DIR.exists():
        for note in PARAMS_DIR.glob("*.md"):
            try:
                content = note.read_text()
            except OSError:
                continue
            fm = _parse_frontmatter(content)
            param_name = fm.get("param_name")
            strategy_id = fm.get("strategy_id")
            optimal_value = fm.get("optimal_value")
            if param_name and strategy_id and optimal_value is not None:
                param_optima[param_name].append(
                    {
                        "strategy": strategy_id,
                        "optimal": optimal_value,
                        "source_note": note.name,
                    }
                )

    # ── From experiment sweep outputs ──────────────────────────────────────
    # Look for param_sweep journal entries and extract best param value
    sweep_by_param: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in journal:
        if entry.get("category") not in ("param_drift", "dormant", "filter"):
            continue
        exp_id = entry.get("experiment_id", "")
        exp = _load_experiment(exp_id)
        if exp is None:
            continue
        inputs = exp.get("inputs") or {}
        outputs = exp.get("outputs") or {}
        method = inputs.get("method") or (exp.get("queue_entry") or {}).get("method", "")
        if method != "param_sweep":
            continue
        sweep_param = inputs.get("params_override", {}).get("sweep_param") or outputs.get("sweep_param")
        if not sweep_param:
            continue
        sweep_results = outputs.get("sweep_results", [])
        if not sweep_results:
            continue

        # Find optimal value (max Sharpe)
        best = max(sweep_results, key=lambda r: r.get("sharpe", -999), default=None)
        if best is None:
            continue
        strategy = inputs.get("strategy") or entry.get("strategy", "unknown")
        sweep_by_param[sweep_param].append(
            {
                "strategy": strategy,
                "optimal": best.get("param_value"),
                "sharpe": best.get("sharpe"),
                "experiment_id": exp_id,
            }
        )

    # Merge vault notes + sweep data
    for param, records in sweep_by_param.items():
        param_optima[param].extend(records)

    # Flag params with 3+ consistent optima
    for param_name, records in param_optima.items():
        if len(records) < 3:
            continue
        # Check for rough agreement: all optima within 2× of each other
        try:
            values = [float(r["optimal"]) for r in records if r.get("optimal") is not None]
        except (TypeError, ValueError):
            continue
        if len(values) < 3:
            continue
        if max(values) <= 2 * min(values) or (max(values) - min(values)) <= 5:
            strategies = [r["strategy"] for r in records]
            evidence = [r.get("experiment_id", r.get("source_note", "?")) for r in records]
            patterns.append(
                {
                    "type": "PARAM_CONVERGENCE",
                    "description": (
                        f"`{param_name}` shows consistent optimal range {min(values):.0f}–{max(values):.0f} "
                        f"across {len(records)} strategies: {', '.join(strategies)}"
                    ),
                    "evidence": evidence,
                    "suggested_action": (
                        f"Consider using `{param_name}` ≈ {sum(values)/len(values):.1f} as default "
                        f"for new strategies in these categories."
                    ),
                    "severity": "opportunity",
                }
            )

    return patterns


def _detect_strategy_saturation(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """STRATEGY_SATURATION: 3+ consecutive experiments of same strategy type all fail/partial."""
    patterns: List[Dict[str, Any]] = []

    # Group by strategy (use category as fallback)
    by_strategy: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for entry in journal:
        key = entry.get("strategy") or entry.get("category", "unknown")
        by_strategy[key].append(entry)

    for strategy, entries in by_strategy.items():
        if len(entries) < 3:
            continue
        # Check last 3 entries (sorted by timestamp for reliability)
        sorted_entries = sorted(entries, key=lambda e: e.get("timestamp", ""))
        last3 = sorted_entries[-3:]
        non_pass_count = sum(
            1 for e in last3 if e.get("verdict") in ("fail", "partial", "deferred")
        )
        if non_pass_count >= 3:
            evidence = [e.get("experiment_id", "?") for e in last3]
            verdicts = [e.get("verdict", "?") for e in last3]
            patterns.append(
                {
                    "type": "STRATEGY_SATURATION",
                    "description": (
                        f"Last {len(last3)} experiments for `{strategy}` all non-passing "
                        f"({', '.join(verdicts)}) — may be hitting diminishing returns."
                    ),
                    "evidence": evidence,
                    "suggested_action": (
                        f"De-prioritise further `{strategy}` variants. "
                        "Shift focus to under-explored strategy types."
                    ),
                    "severity": "warning",
                }
            )

    return patterns


def _detect_combined_bottleneck(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """COMBINED_BOTTLENECK: Strategy passes solo but fails combined repeatedly."""
    patterns: List[Dict[str, Any]] = []

    # Bucket experiments by strategy + test type (solo vs combined)
    solo_passes: Dict[str, List[str]] = defaultdict(list)   # strategy → [exp_ids]
    combined_fails: Dict[str, List[str]] = defaultdict(list)

    for entry in journal:
        exp_id = entry.get("experiment_id", "")
        strategy = entry.get("strategy") or ""
        verdict = entry.get("verdict", "")

        is_solo = "solo" in exp_id or entry.get("category") in ("dormant", "new_strategy")
        is_combined = (
            "combined" in exp_id
            or entry.get("category") == "portfolio"
            or strategy == "combined"
        )

        if is_solo and verdict == "pass" and strategy:
            solo_passes[strategy].append(exp_id)
        elif is_combined and verdict in ("fail", "partial") and strategy and strategy != "combined":
            combined_fails[strategy].append(exp_id)

    # Also check None-strategy combined entries (combined portfolio experiments)
    for entry in journal:
        exp_id = entry.get("experiment_id", "")
        verdict = entry.get("verdict", "")
        if "combined" in exp_id and verdict in ("fail", "partial"):
            combined_fails["portfolio_combined"].append(exp_id)

    for strategy, solo_ids in solo_passes.items():
        fail_ids = combined_fails.get(strategy, [])
        if len(fail_ids) >= 1:
            patterns.append(
                {
                    "type": "COMBINED_BOTTLENECK",
                    "description": (
                        f"`{strategy}` passes solo ({len(solo_ids)}x) but fails/partial "
                        f"in combined context ({len(fail_ids)}x) — "
                        "likely position contention or correlation drag."
                    ),
                    "evidence": solo_ids[:3] + fail_ids[:3],
                    "suggested_action": (
                        f"Investigate `{strategy}` trade overlap with active strategies. "
                        "Consider time-of-day or sector filters to reduce contention."
                    ),
                    "severity": "warning",
                }
            )

    return patterns


def _detect_diminishing_returns(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """DIMINISHING_RETURNS: Param sweep shows flat Sharpe curve."""
    patterns: List[Dict[str, Any]] = []

    for entry in journal:
        exp_id = entry.get("experiment_id", "")
        exp = _load_experiment(exp_id)
        if exp is None:
            continue
        inputs = exp.get("inputs") or {}
        outputs = exp.get("outputs") or {}
        method = inputs.get("method") or (exp.get("queue_entry") or {}).get("method", "")
        if method != "param_sweep":
            continue
        sweep_results = outputs.get("sweep_results", [])
        if len(sweep_results) < 3:
            continue
        sweep_param = outputs.get("sweep_param") or inputs.get("params_override", {}).get("sweep_param")
        if not sweep_param:
            continue

        sharpes = [r.get("sharpe", None) for r in sweep_results]
        sharpes = [s for s in sharpes if s is not None]
        if len(sharpes) < 3:
            continue

        sharpe_range = max(sharpes) - min(sharpes)
        strategy = inputs.get("strategy") or entry.get("strategy", "?")

        if sharpe_range < 0.15:
            param_values = [r.get("param_value") for r in sweep_results]
            patterns.append(
                {
                    "type": "DIMINISHING_RETURNS",
                    "description": (
                        f"`{strategy}` `{sweep_param}` sweep shows flat Sharpe curve "
                        f"(range {sharpe_range:.3f}) across values {param_values} — "
                        "parameter has little impact."
                    ),
                    "evidence": [exp_id],
                    "suggested_action": (
                        f"Stop optimising `{sweep_param}` for `{strategy}`. "
                        "Use default value and focus on higher-leverage parameters."
                    ),
                    "severity": "info",
                }
            )

    return patterns


def _detect_uncorrelated_opportunity(journal: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """UNCORRELATED_OPPORTUNITY: Solo-passing strategy has unique trade timing.

    Uses a simple heuristic: if two passing strategies are in different categories
    and both pass solo, flag as potential diversification opportunity.
    """
    patterns: List[Dict[str, Any]] = []

    # Find strategies that pass solo
    passing_solo: Dict[str, Dict[str, Any]] = {}
    for entry in journal:
        exp_id = entry.get("experiment_id", "")
        strategy = entry.get("strategy") or ""
        category = entry.get("category", "")
        verdict = entry.get("verdict", "")
        if "solo" in exp_id and verdict == "pass" and strategy and strategy not in passing_solo:
            passing_solo[strategy] = {"category": category, "experiment_id": exp_id}

    if len(passing_solo) < 2:
        return patterns

    # Group by category
    by_category: Dict[str, List[str]] = defaultdict(list)
    for strat, info in passing_solo.items():
        by_category[info["category"]].append(strat)

    # Flag cross-category pairs
    categories = list(by_category.keys())
    if len(categories) >= 2:
        all_strats = list(passing_solo.keys())
        evidence_ids = [passing_solo[s]["experiment_id"] for s in all_strats[:4]]
        patterns.append(
            {
                "type": "UNCORRELATED_OPPORTUNITY",
                "description": (
                    f"{len(passing_solo)} solo-passing strategies span {len(categories)} categories "
                    f"({', '.join(categories[:3])}) — "
                    f"candidates: {', '.join(all_strats[:4])}."
                ),
                "evidence": evidence_ids,
                "suggested_action": (
                    "Run combined portfolio test pairing strategies from different categories "
                    "to exploit potential decorrelation."
                ),
                "severity": "opportunity",
            }
        )

    return patterns


def detect_patterns(recent_experiments: Optional[List[dict]] = None) -> List[Dict[str, Any]]:
    """Scan journal/vault for mechanical patterns worth flagging.

    Called by daemon after each batch of experiments.

    Patterns detected:
    1. PARAM_CONVERGENCE: 3+ experiments show same param in optimal range
    2. STRATEGY_SATURATION: 3+ consecutive strategies of same type fail
    3. COMBINED_BOTTLENECK: strategy passes solo but fails combined repeatedly
    4. DIMINISHING_RETURNS: param sweep shows flat Sharpe curve
    5. UNCORRELATED_OPPORTUNITY: solo-passing strategies span multiple categories

    Args:
        recent_experiments: Optional list of experiment result dicts to limit scope.
                            If None, full journal is used.

    Returns:
        List of pattern dicts:
        {
            "type": str,
            "description": str,
            "evidence": List[str],    # experiment IDs or vault note filenames
            "suggested_action": str,
            "severity": "info" | "warning" | "opportunity",
        }
    """
    journal = _load_journal()
    if not journal:
        logger.debug("No journal entries — skipping pattern detection")
        return []

    # If caller supplied a limited set, append to journal for freshness
    if recent_experiments:
        existing_ids = {e.get("experiment_id") for e in journal}
        for exp in recent_experiments:
            if exp.get("experiment_id") not in existing_ids:
                journal.append(exp)

    all_patterns: List[Dict[str, Any]] = []

    try:
        all_patterns.extend(_detect_param_convergence(journal))
    except Exception:
        logger.exception("PARAM_CONVERGENCE detection failed")

    try:
        all_patterns.extend(_detect_strategy_saturation(journal))
    except Exception:
        logger.exception("STRATEGY_SATURATION detection failed")

    try:
        all_patterns.extend(_detect_combined_bottleneck(journal))
    except Exception:
        logger.exception("COMBINED_BOTTLENECK detection failed")

    try:
        all_patterns.extend(_detect_diminishing_returns(journal))
    except Exception:
        logger.exception("DIMINISHING_RETURNS detection failed")

    try:
        all_patterns.extend(_detect_uncorrelated_opportunity(journal))
    except Exception:
        logger.exception("UNCORRELATED_OPPORTUNITY detection failed")

    logger.info("Pattern detection: %d patterns found", len(all_patterns))
    return all_patterns


# ─── Hypothesis ↔ Experiment Matching ───────────────────────────────────────


def check_hypotheses_against_result(experiment_result: dict) -> List[Dict[str, Any]]:
    """Check if an experiment result confirms or rejects any queued hypotheses.

    Called by daemon after each experiment completes.

    For each hypothesis with status 'testing':
    - Checks if the experiment's strategy/category/id matches the hypothesis
      test plan using simple case-insensitive string matching.
    - On match: updates hypothesis status to 'confirmed' (pass) or evidence
      of failure is noted (partial/fail).

    Args:
        experiment_result: JournalEntry-like dict with keys:
            experiment_id, strategy, category, verdict, key_metrics.

    Returns:
        List of update dicts: {"hypothesis_id", "new_status", "experiment_id"}
    """
    updates: List[Dict[str, Any]] = []

    exp_id = experiment_result.get("experiment_id", "")
    exp_strategy = (experiment_result.get("strategy") or "").lower()
    exp_category = (experiment_result.get("category") or "").lower()
    exp_verdict = experiment_result.get("verdict", "fail")

    testing_hyps = list_hypotheses(status="testing")
    if not testing_hyps:
        return []

    for hyp in testing_hyps:
        hyp_id = hyp["id"]
        hyp_title = hyp["title"].lower()
        hyp_text = hyp["hypothesis"].lower()

        # Load full file for test plan
        path: Path = hyp["path"]
        content = path.read_text()
        test_plan_match = re.search(r"## Test Plan\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL)
        test_plan = test_plan_match.group(1).strip().lower() if test_plan_match else ""

        # Simple matching: does the experiment's strategy/category appear in the hypothesis?
        matched = (
            (exp_strategy and exp_strategy in hyp_text)
            or (exp_strategy and exp_strategy in test_plan)
            or (exp_category and exp_category in hyp_text)
            or (exp_category and exp_category in test_plan)
            or (exp_id.lower() in hyp_text)
            or (exp_id.lower() in test_plan)
        )

        if not matched:
            continue

        # Determine new status based on verdict
        sharpe = (experiment_result.get("key_metrics") or {}).get("sharpe")
        if exp_verdict in ("pass", "promoted"):
            new_status = "confirmed"
        elif exp_verdict == "partial":
            new_status = "testing"  # keep open — partial evidence
        else:
            new_status = "rejected"

        metrics_str = ""
        if sharpe is not None:
            metrics_str = f"Experiment `{exp_id}` reported Sharpe={sharpe:.3f}, verdict=`{exp_verdict}`."

        result_summary = (
            f"Matched experiment `{exp_id}` (strategy=`{exp_strategy}`, verdict=`{exp_verdict}`). "
            + metrics_str
        )

        updated_path = update_hypothesis_status(
            hyp_id,
            new_status,
            result_summary=result_summary,
            confirming_experiments=[exp_id],
        )

        if updated_path:
            updates.append(
                {
                    "hypothesis_id": hyp_id,
                    "new_status": new_status,
                    "experiment_id": exp_id,
                    "path": updated_path,
                }
            )
            logger.info(
                "Hypothesis %s updated to %s via experiment %s",
                hyp_id, new_status, exp_id,
            )

    return updates


# ─── Research Priorities ─────────────────────────────────────────────────────


def read_priorities() -> Dict[str, Any]:
    """Read agent-set research priorities from Meta/Research Priorities.md.

    Returns:
        Dict with keys:
        - focus_areas: list of strategy types / themes to prioritise
        - deprioritize: list of things to de-emphasise
        - notes: free-form agent notes
        - updated: date string (from frontmatter)
        - set_by: who last updated this
    """
    if not PRIORITIES_PATH.exists():
        return {
            "focus_areas": [],
            "deprioritize": [],
            "notes": "",
            "updated": "",
            "set_by": "unknown",
        }

    try:
        content = PRIORITIES_PATH.read_text()
    except OSError:
        return {"focus_areas": [], "deprioritize": [], "notes": ""}

    fm = _parse_frontmatter(content)

    # Extract sections
    focus_areas: List[str] = []
    deprioritize: List[str] = []
    notes: str = ""

    # Focus Areas section
    fa_match = re.search(
        r"## Focus Areas\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL
    )
    if fa_match:
        for line in fa_match.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                focus_areas.append(line[2:].strip())

    # De-prioritize section (various header variants)
    dp_match = re.search(
        r"## De-?prioritize[^\n]*\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL | re.IGNORECASE
    )
    if dp_match:
        for line in dp_match.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                deprioritize.append(line[2:].strip())

    # Notes section
    notes_match = re.search(
        r"## Notes\s*\n+(.*?)(?=\n##|\Z)", content, re.DOTALL
    )
    if notes_match:
        notes = notes_match.group(1).strip()

    return {
        "focus_areas": focus_areas,
        "deprioritize": deprioritize,
        "notes": notes,
        "updated": fm.get("updated", ""),
        "set_by": fm.get("set_by", "unknown"),
    }


def write_priorities(
    focus_areas: List[str],
    deprioritize: Optional[List[str]] = None,
    notes: str = "",
    set_by: str = "agent",
) -> None:
    """Write/update research priorities (called by coordinator agent).

    Args:
        focus_areas: List of strategy types / themes to prioritise.
        deprioritize: List of things to de-emphasise.
        notes: Free-form notes from the agent.
        set_by: Identity of caller (default "agent").
    """
    PRIORITIES_PATH.parent.mkdir(parents=True, exist_ok=True)

    deprioritize = deprioritize or []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    fm = {
        "tags": ["meta", "priorities"],
        "updated": today,
        "set_by": set_by,
    }

    fa_lines = "\n".join(f"- {item}" for item in focus_areas) if focus_areas else "_None set_"
    dp_lines = "\n".join(f"- {item}" for item in deprioritize) if deprioritize else "_None set_"

    content_lines = [
        _build_frontmatter(fm),
        "",
        "# Research Priorities",
        "",
        "Current focus and direction for the research engine.",
        "",
        "## Focus Areas",
        "",
        fa_lines,
        "",
        "## De-prioritize",
        "",
        dp_lines,
        "",
        "## Notes",
        "",
        notes if notes else "No additional notes.",
        "",
    ]
    PRIORITIES_PATH.write_text("\n".join(content_lines))
    logger.info("Research priorities updated by %s", set_by)
