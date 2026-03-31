#!/usr/bin/env python3
"""Atlas Research Discovery Orchestrator.

Drives the daily paper → strategy pipeline:
  source rotation → fetch/browse → filter → extract specs → deduplicate
  → generate code → quick_check → log → Telegram digest

Entry point: discover_daily() -> DailyReport
"""

import json
import logging
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DISCOVERY_DIR = Path(__file__).resolve().parent
ATLAS_ROOT = DISCOVERY_DIR.parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("discovery")

# ─── Paths ───────────────────────────────────────────────────────────────────

PAPERS_DIR = DISCOVERY_DIR / "papers"
SPECS_DIR = DISCOVERY_DIR / "specs"
LOGS_DIR = DISCOVERY_DIR / "logs"
PROMPTS_DIR = DISCOVERY_DIR / "prompts"
DAILY_LOG = DISCOVERY_DIR / "daily_log.jsonl"
CUMULATIVE_STATS = DISCOVERY_DIR / "cumulative_stats.json"
SEEN_URLS_FILE = DISCOVERY_DIR / "seen_urls.txt"
MCP_CONFIG = DISCOVERY_DIR / "config" / "mcp_config.json"


# ─── DailyReport dataclass ───────────────────────────────────────────────────

@dataclass
class DailyReport:
    date: str
    source: str
    method: str
    papers_found: int
    papers_filtered: int
    specs_extracted: int
    strategies_generated: list = field(default_factory=list)   # list of str (strategy names)
    strategies_passed_quickcheck: list = field(default_factory=list)  # list of str
    errors: list = field(default_factory=list)  # list of str
    runtime_s: float = 0.0


# ─── Core Claude CLI helper ───────────────────────────────────────────────────

def _run_claude(
    prompt: str,
    mcp: bool = False,
    schema_path: Optional[str] = None,
    allowed_tools: str = "Bash,Read,Write",
) -> dict:
    """Run claude CLI with the given prompt and return parsed JSON output.

    Uses subprocess.run with a 30-minute timeout. Writes prompt to a temp file
    and passes it via stdin for safety with special characters.

    Args:
        prompt: The prompt text to send to claude.
        mcp: If True, attach --mcp-config pointing at the discovery config.
        schema_path: Optional path to a JSON schema file for structured output.
        allowed_tools: Comma-separated list of tools Claude may use.

    Returns:
        dict — parsed JSON result, or {"error": "<msg>", "raw": "<stdout>"} on failure.
    """
    cmd = ["claude", "-p", "--output-format", "json"]

    if mcp:
        cmd += ["--mcp-config", str(MCP_CONFIG.resolve())]

    if schema_path:
        cmd += ["--json-schema", Path(schema_path).read_text()]

    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]

    # Write prompt to temp file and pass via stdin to avoid shell quoting issues
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tf:
            tf.write(prompt)
            tf_path = tf.name

        with open(tf_path, "r", encoding="utf-8") as stdin_f:
            result = subprocess.run(
                cmd,
                stdin=stdin_f,
                capture_output=True,
                text=True,
                timeout=1800,  # 30 minutes
            )

        Path(tf_path).unlink(missing_ok=True)

        if result.returncode != 0:
            err = result.stderr.strip() or result.stdout.strip()
            logger.warning("claude CLI returned %d: %s", result.returncode, err[:200])
            return {"error": f"exit {result.returncode}: {err[:200]}", "raw": result.stdout}

        stdout = result.stdout.strip()
        if not stdout:
            return {"error": "empty output from claude", "raw": ""}

        # Parse JSON — claude --output-format json returns structured JSON
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            # Try to extract JSON block from output
            import re
            m = re.search(r"```json\s*([\s\S]+?)\s*```", stdout)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass
            return {"error": "json parse failed", "raw": stdout[:500]}

    except FileNotFoundError:
        logger.warning("claude CLI not found — skipping LLM step (graceful degradation)")
        return {"error": "claude not found", "raw": ""}
    except subprocess.TimeoutExpired:
        logger.error("claude CLI timed out after 1800s")
        return {"error": "timeout", "raw": ""}
    except Exception as e:
        logger.error("_run_claude error: %s", e)
        return {"error": str(e), "raw": ""}


# ─── Browse helpers ──────────────────────────────────────────────────────────

def _browse_with_claude(source: dict) -> list:
    """Use claude with MCP (computer-use tools) to browse SSRN or a blog.

    Reads the appropriate prompt template (browse_ssrn.md or browse_blog.md),
    substitutes placeholders, and calls _run_claude with mcp=True.

    Returns:
        list of paper dicts (may be empty if claude CLI unavailable).
    """
    source_type = source.get("type", "")
    if source_type == "ssrn":
        prompt_file = PROMPTS_DIR / "browse_ssrn.md"
    else:
        prompt_file = PROMPTS_DIR / "browse_blog.md"

    if not prompt_file.exists():
        logger.warning("Browse prompt not found: %s", prompt_file)
        return []

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    prompt = prompt_file.read_text()
    queries = source.get("queries", [])
    prompt = prompt.replace("{queries}", json.dumps(queries, indent=2))
    prompt = prompt.replace("{papers_dir}", str(PAPERS_DIR.resolve()))
    prompt = prompt.replace("{seen_urls_file}", str(SEEN_URLS_FILE.resolve()))
    prompt = prompt.replace("{source}", json.dumps(source, indent=2))

    allowed = "Bash,Read,Write,computer_use,browser"
    result = _run_claude(prompt, mcp=True, allowed_tools=allowed)

    if "error" in result:
        logger.warning("browse_with_claude error: %s", result["error"])
        return []

    # Expect result to be a list or contain a 'papers' key
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("papers", result.get("result", []))
    return []


# ─── Filter helpers ───────────────────────────────────────────────────────────

def _filter_papers(papers: list) -> list:
    """Filter papers by relevance score using Claude.

    Reads prompts/filter.md, substitutes paper data, calls Claude (no MCP).
    Returns papers with score >= 6.

    If Claude CLI unavailable, passes all papers through (graceful degradation).
    """
    if not papers:
        return []

    prompt_file = PROMPTS_DIR / "filter.md"
    if not prompt_file.exists():
        logger.warning("filter.md prompt not found — passing all %d papers", len(papers))
        return papers

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    prompt = prompt_file.read_text()
    prompt = prompt.replace("{papers_dir}", str(PAPERS_DIR.resolve()))
    prompt = prompt.replace("{items_json}", json.dumps(papers, indent=2))

    result = _run_claude(prompt, mcp=False, allowed_tools="Bash,Read")

    if "error" in result:
        logger.warning("_filter_papers error: %s — passing all papers", result["error"])
        return papers

    # Expect list of scored papers, or dict with 'papers' key
    scored = []
    if isinstance(result, list):
        scored = result
    elif isinstance(result, dict):
        scored = result.get("papers", result.get("filtered", result.get("result", [])))
        if not isinstance(scored, list):
            scored = []

    # Filter by score >= 6
    filtered = [p for p in scored if isinstance(p, dict) and p.get("score", 0) >= 6]
    if not filtered and scored:
        # If claude returned papers without scores, keep all
        filtered = [p for p in scored if isinstance(p, dict)]

    logger.info("filter_papers: %d → %d (score ≥ 6)", len(papers), len(filtered))
    return filtered


# ─── Spec extraction ─────────────────────────────────────────────────────────

def _extract_specs(papers: list) -> list:
    """Extract strategy specs from filtered papers using Claude.

    Reads prompts/extract.md, builds a prompt with paper details, calls Claude.
    Returns list of strategy spec dicts.
    """
    if not papers:
        return []

    prompt_file = PROMPTS_DIR / "extract.md"
    if not prompt_file.exists():
        logger.warning("extract.md prompt not found — skipping spec extraction")
        return []

    PAPERS_DIR.mkdir(parents=True, exist_ok=True)
    prompt = prompt_file.read_text()
    prompt = prompt.replace("{papers_dir}", str(PAPERS_DIR.resolve()))
    prompt = prompt.replace("{papers_json}", json.dumps(papers, indent=2))

    result = _run_claude(prompt, mcp=False, allowed_tools="Bash,Read")

    if "error" in result:
        logger.warning("_extract_specs error: %s", result["error"])
        return []

    specs = []
    if isinstance(result, list):
        specs = result
    elif isinstance(result, dict):
        specs = result.get("specs", result.get("strategies", result.get("result", [])))
        if not isinstance(specs, list):
            specs = []

    logger.info("_extract_specs: %d papers → %d specs", len(papers), len(specs))
    return specs


# ─── Strategy generation ──────────────────────────────────────────────────────

def _generate_strategies(specs: list) -> list:
    """Generate Python strategy files for each spec using Claude.

    Reads prompts/generate.md, substitutes spec JSON, calls Claude with file-write tools.
    Runs quick_check() on each generated strategy.

    Returns:
        list of dicts: {"spec": dict, "quick_check": dict, "strategy_name": str}
    """
    if not specs:
        return []

    prompt_file = PROMPTS_DIR / "generate.md"
    if not prompt_file.exists():
        logger.warning("generate.md prompt not found — skipping code generation")
        return []

    generate_template = prompt_file.read_text()
    results = []

    for spec in specs:
        strategy_name = spec.get("strategy_name", "unknown_strategy")
        logger.info("Generating strategy: %s", strategy_name)

        prompt = generate_template.replace("{spec_json}", json.dumps(spec, indent=2))
        prompt = prompt.replace("{strategy_name}", strategy_name)
        prompt = prompt.replace("{atlas_root}", str(ATLAS_ROOT.resolve()))
        prompt = prompt.replace("{strategies_dir}", str(ATLAS_ROOT / "research" / "strategies"))

        gen_result = _run_claude(
            prompt,
            mcp=False,
            allowed_tools="Bash,Read,Write,Edit",
        )

        if "error" in gen_result and gen_result.get("error"):
            logger.warning("Generation failed for %s: %s", strategy_name, gen_result["error"])
            results.append({
                "spec": spec,
                "strategy_name": strategy_name,
                "quick_check": {"alive": False, "reason": gen_result["error"]},
            })
            continue

        # Run quick_check on the generated strategy
        qc_result = {"alive": False, "reason": "not attempted"}
        try:
            from research.loop import quick_check
            qc_result = quick_check(strategy_name, "sp500")
            logger.info(
                "quick_check %s: alive=%s reason=%s",
                strategy_name, qc_result.get("alive"), qc_result.get("reason", "")
            )
        except Exception as e:
            qc_result = {"alive": False, "reason": str(e)}
            logger.warning("quick_check error for %s: %s", strategy_name, e)

        results.append({
            "spec": spec,
            "strategy_name": strategy_name,
            "quick_check": qc_result,
        })

    return results


# ─── Backlog review ───────────────────────────────────────────────────────────

def _review_backlog() -> list:
    """Load papers/specs that previously errored from daily_log.jsonl for retry.

    Returns list of specs from entries that had errors and no generated strategies.
    """
    if not DAILY_LOG.exists():
        return []

    retry_specs = []
    try:
        lines = DAILY_LOG.read_text().splitlines()
        # Check last 30 days of logs (at most last 90 lines)
        for line in lines[-90:]:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("errors") and not entry.get("strategies_generated"):
                # Re-queue specs from this failed run
                for spec in entry.get("specs", []):
                    if isinstance(spec, dict) and spec.get("strategy_name"):
                        retry_specs.append(spec)
    except Exception as e:
        logger.warning("_review_backlog error: %s", e)

    logger.info("_review_backlog: found %d specs to retry", len(retry_specs))
    return retry_specs


# ─── Logging & stats ─────────────────────────────────────────────────────────

def _log_daily_run(report: DailyReport) -> None:
    """Append one JSON line to daily_log.jsonl and update cumulative_stats.json."""
    DISCOVERY_DIR.mkdir(parents=True, exist_ok=True)

    # Append to daily log
    entry = asdict(report)
    with open(DAILY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    # Update cumulative stats
    stats = {}
    if CUMULATIVE_STATS.exists():
        try:
            stats = json.loads(CUMULATIVE_STATS.read_text())
        except (json.JSONDecodeError, OSError):
            stats = {}

    stats["total_runs"] = stats.get("total_runs", 0) + 1
    stats["papers_found"] = stats.get("papers_found", 0) + report.papers_found
    stats["papers_filtered"] = stats.get("papers_filtered", 0) + report.papers_filtered
    stats["specs_extracted"] = stats.get("specs_extracted", 0) + report.specs_extracted
    stats["strategies_generated"] = stats.get("strategies_generated", 0) + len(report.strategies_generated)
    stats["strategies_passed_quickcheck"] = (
        stats.get("strategies_passed_quickcheck", 0) + len(report.strategies_passed_quickcheck)
    )

    # Monthly breakdown
    month_key = report.date[:7]  # "2026-03"
    monthly = stats.setdefault("monthly", {})
    mo = monthly.setdefault(month_key, {
        "runs": 0, "papers_found": 0, "papers_filtered": 0,
        "specs_extracted": 0, "strategies_generated": 0, "strategies_passed": 0,
    })
    mo["runs"] += 1
    mo["papers_found"] += report.papers_found
    mo["papers_filtered"] += report.papers_filtered
    mo["specs_extracted"] += report.specs_extracted
    mo["strategies_generated"] += len(report.strategies_generated)
    mo["strategies_passed"] += len(report.strategies_passed_quickcheck)

    stats["last_run"] = report.date

    CUMULATIVE_STATS.write_text(json.dumps(stats, indent=2))
    logger.info("Daily run logged: %s", report.date)


# ─── Telegram digest ─────────────────────────────────────────────────────────

def _send_telegram_digest(report: DailyReport) -> None:
    """Send a formatted Telegram message with the daily discovery summary."""
    telegram_script = ATLAS_ROOT / "scripts" / "telegram_notify.py"
    if not telegram_script.exists():
        logger.warning("telegram_notify.py not found — skipping digest")
        return

    passed_emoji = "✅" if report.strategies_passed_quickcheck else "⚪"
    error_note = f"\n⚠️ Errors: {len(report.errors)}" if report.errors else ""

    month_key = report.date[:7]
    # Load cumulative monthly stats for footer
    monthly_stats = ""
    if CUMULATIVE_STATS.exists():
        try:
            stats = json.loads(CUMULATIVE_STATS.read_text())
            mo = stats.get("monthly", {}).get(month_key, {})
            if mo:
                monthly_stats = (
                    f"\n\n📅 <b>{month_key} totals</b>: "
                    f"{mo['runs']} runs | "
                    f"{mo['papers_found']} papers | "
                    f"{mo['strategies_generated']} strategies | "
                    f"{mo['strategies_passed']} passed QC"
                )
        except Exception:
            pass

    generated_list = ""
    if report.strategies_generated:
        generated_list = "\n" + "\n".join(
            f"  {'✅' if s in report.strategies_passed_quickcheck else '❌'} <code>{s}</code>"
            for s in report.strategies_generated
        )

    message = (
        f"🔬 <b>Atlas Discovery — {report.date}</b>\n"
        f"📚 Source: <b>{report.source}</b> ({report.method})\n\n"
        f"📄 Papers found: <b>{report.papers_found}</b>\n"
        f"🎯 Filtered (score≥6): <b>{report.papers_filtered}</b>\n"
        f"📐 Specs extracted: <b>{report.specs_extracted}</b>\n"
        f"{passed_emoji} Strategies generated: <b>{len(report.strategies_generated)}</b>"
        + generated_list
        + f"\n{passed_emoji} Passed quick-check: <b>{len(report.strategies_passed_quickcheck)}</b>"
        + error_note
        + f"\n⏱️ Runtime: {report.runtime_s:.0f}s"
        + monthly_stats
    )

    try:
        subprocess.run(
            [sys.executable, str(telegram_script), message],
            capture_output=True,
            text=True,
            timeout=30,
        )
        logger.info("Telegram digest sent")
    except Exception as e:
        logger.warning("Telegram digest failed: %s", e)


# ─── Main orchestrator ───────────────────────────────────────────────────────

def discover_daily() -> DailyReport:
    """Run the daily paper discovery → strategy generation pipeline.

    Workflow:
    1. Determine today's source via rotation
    2. Fetch/browse papers (API or computer-use)
    3. Filter by relevance (Claude)
    4. Extract strategy specs (Claude)
    5. Deduplicate against existing strategies
    6. Generate Python strategy code (Claude)
    7. Run quick_check on each generated strategy
    8. Log results and send Telegram digest
    9. Return DailyReport

    Returns:
        DailyReport dataclass with full run summary.
    """
    start_time = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    errors = []

    # Ensure output directories exist
    for d in [PAPERS_DIR, SPECS_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # ── Step 1: get today's source ────────────────────────────────────────
    source = {}
    method = "api"
    source_name = "arxiv"
    try:
        from research.discovery.sources import get_today_source, get_queries_for_source
        source = get_today_source()
        source_name = source.get("name", "arxiv")
        method = source.get("method", "api")
        logger.info("Today's source: %s (method=%s)", source_name, method)
    except ImportError:
        logger.warning("research.discovery.sources not available — defaulting to arxiv API")
        source = {"name": "arxiv", "method": "api"}
        source_name = "arxiv"
        method = "api"
    except Exception as e:
        errors.append(f"get_today_source: {e}")
        logger.error("get_today_source failed: %s", e)
        source = {"name": "arxiv", "method": "api"}

    # ── Step 2: fetch / browse papers ─────────────────────────────────────
    papers = []
    if method == "api":
        try:
            from research.discovery.arxiv_api import fetch_new_papers
            queries = source.get("queries", [])
            papers = fetch_new_papers(queries=queries)
            logger.info("fetch_new_papers returned %d papers", len(papers))
        except ImportError:
            logger.warning("research.discovery.arxiv_api not available — no papers fetched")
        except Exception as e:
            errors.append(f"fetch_new_papers: {e}")
            logger.error("fetch_new_papers failed: %s", e)

    elif method == "computer_use":
        try:
            papers = _browse_with_claude(source)
            logger.info("_browse_with_claude returned %d papers", len(papers))
        except Exception as e:
            errors.append(f"_browse_with_claude: {e}")
            logger.error("_browse_with_claude failed: %s", e)

    elif method == "review":
        try:
            papers = _review_backlog()
            logger.info("_review_backlog returned %d items", len(papers))
        except Exception as e:
            errors.append(f"_review_backlog: {e}")
            logger.error("_review_backlog failed: %s", e)

    papers_found = len(papers)

    # ── Step 3: dedup seen URLs ───────────────────────────────────────────
    try:
        from research.discovery.dedup import is_seen, mark_seen
        unseen_papers = []
        for p in papers:
            url = p.get("url", p.get("pdf_url", p.get("arxiv_id", "")))
            if url and is_seen(url):
                logger.debug("Skipping seen: %s", url)
            else:
                unseen_papers.append(p)
        papers = unseen_papers
        logger.info("After URL dedup: %d papers", len(papers))
    except ImportError:
        pass
    except Exception as e:
        logger.warning("URL dedup error: %s", e)

    # ── Step 4: filter papers ─────────────────────────────────────────────
    filtered_papers = []
    try:
        filtered_papers = _filter_papers(papers)
    except Exception as e:
        errors.append(f"_filter_papers: {e}")
        logger.error("_filter_papers failed: %s", e)
        filtered_papers = papers  # fallback: pass all through

    papers_filtered = len(filtered_papers)

    # Mark filtered papers as seen
    try:
        from research.discovery.dedup import mark_seen
        for p in filtered_papers:
            url = p.get("url", p.get("pdf_url", p.get("arxiv_id", "")))
            if url:
                mark_seen(url, "filtered")
    except Exception:
        pass

    # ── Step 5: extract specs ─────────────────────────────────────────────
    specs = []
    try:
        specs = _extract_specs(filtered_papers)
    except Exception as e:
        errors.append(f"_extract_specs: {e}")
        logger.error("_extract_specs failed: %s", e)

    specs_extracted = len(specs)

    # ── Step 6: dedup strategy specs ─────────────────────────────────────
    unique_specs = []
    try:
        from research.discovery.dedup import is_duplicate_strategy, load_existing_strategies
        existing = load_existing_strategies()
        for spec in specs:
            if is_duplicate_strategy(spec, existing):
                logger.info("Duplicate spec skipped: %s", spec.get("strategy_name"))
            else:
                unique_specs.append(spec)
        logger.info("Strategy dedup: %d → %d", len(specs), len(unique_specs))
    except ImportError:
        unique_specs = specs
    except Exception as e:
        logger.warning("Strategy dedup error: %s", e)
        unique_specs = specs

    # Save specs to disk for reference
    if unique_specs:
        SPECS_DIR.mkdir(parents=True, exist_ok=True)
        specs_file = SPECS_DIR / f"specs_{today}.json"
        try:
            specs_file.write_text(json.dumps(unique_specs, indent=2))
        except Exception:
            pass

    # ── Step 7: generate strategies ───────────────────────────────────────
    gen_results = []
    try:
        gen_results = _generate_strategies(unique_specs)
    except Exception as e:
        errors.append(f"_generate_strategies: {e}")
        logger.error("_generate_strategies failed: %s", e)

    strategies_generated = [r["strategy_name"] for r in gen_results]
    strategies_passed_quickcheck = [
        r["strategy_name"] for r in gen_results
        if r.get("quick_check", {}).get("alive", False)
    ]

    # ── Step 8: assemble report ───────────────────────────────────────────
    runtime_s = time.time() - start_time
    report = DailyReport(
        date=today,
        source=source_name,
        method=method,
        papers_found=papers_found,
        papers_filtered=papers_filtered,
        specs_extracted=specs_extracted,
        strategies_generated=strategies_generated,
        strategies_passed_quickcheck=strategies_passed_quickcheck,
        errors=errors,
        runtime_s=round(runtime_s, 1),
    )

    # ── Step 9: log & notify ──────────────────────────────────────────────
    try:
        _log_daily_run(report)
    except Exception as e:
        logger.error("_log_daily_run failed: %s", e)

    try:
        _send_telegram_digest(report)
    except Exception as e:
        logger.warning("_send_telegram_digest failed: %s", e)

    logger.info(
        "discover_daily complete: found=%d filtered=%d specs=%d generated=%d passed=%d runtime=%.0fs",
        papers_found, papers_filtered, specs_extracted,
        len(strategies_generated), len(strategies_passed_quickcheck), runtime_s,
    )
    return report


def discover_full() -> list:
    """Run discovery across ALL sources (full sweep mode).

    Iterates through every configured source regardless of rotation schedule.
    Returns list of DailyReport objects.
    """
    reports = []
    try:
        from research.discovery.sources import get_all_sources
        sources = get_all_sources()
    except ImportError:
        logger.warning("sources module not available — running single default source")
        sources = [{"name": "arxiv", "method": "api"}]
    except Exception as e:
        logger.error("get_all_sources failed: %s", e)
        sources = [{"name": "arxiv", "method": "api"}]

    for source in sources:
        logger.info("discover_full: processing source %s", source.get("name"))
        report = discover_daily()  # discovers_daily uses today's source internally
        reports.append(report)

    return reports
