"""
overlay.sources.news — News intelligence aggregator for the AI overlay layer.

Combines:
    1. Brave Search headlines  (brave_news.js --json, subprocess, 30s timeout)
    2. Geopolitical risk       (data/position_monitor/ceasefire_factors.json)
    3. Macro snapshot          (macro_indicators table in atlas.db)

Public API
----------
    get_news_summary() -> str
        Always returns a non-empty string.  Never raises.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────


def _find_atlas_root() -> Path:
    """
    Walk up the directory tree from this file to find the Atlas project root.

    Supports both the main worktree and swarm builder worktrees (which share
    data via the main repo at /root/atlas/).
    """
    candidate = Path(__file__).resolve()
    for _ in range(10):
        candidate = candidate.parent
        if (candidate / "scripts" / "brave_news.js").exists():
            return candidate
    # Fallback: three levels up from this file
    return Path(__file__).resolve().parent.parent.parent


_ATLAS_ROOT = _find_atlas_root()
_BRAVE_NEWS_JS = _ATLAS_ROOT / "scripts" / "brave_news.js"
_CEASEFIRE_JSON = _ATLAS_ROOT / "data" / "position_monitor" / "ceasefire_factors.json"

_BRAVE_TIMEOUT = 30  # seconds
_MAX_HEADLINES = 10  # cap on headlines returned


# ── Section 1: Brave Search ──────────────────────────────────────────────────

def _fetch_brave_headlines() -> Optional[str]:
    """
    Run brave_news.js --json and parse the result into a markdown bullet list.

    Returns a formatted string on success, None on failure (no exception raised).
    """
    if not _BRAVE_NEWS_JS.exists():
        logger.warning("news: brave_news.js not found at %s", _BRAVE_NEWS_JS)
        return None

    try:
        result = subprocess.run(
            ["node", str(_BRAVE_NEWS_JS), "--json"],
            capture_output=True,
            text=True,
            timeout=_BRAVE_TIMEOUT,
            cwd=str(_ATLAS_ROOT),
        )
    except subprocess.TimeoutExpired:
        logger.warning("news: brave_news.js timed out after %ds", _BRAVE_TIMEOUT)
        return None
    except FileNotFoundError:
        logger.warning("news: node not found — cannot run brave_news.js")
        return None
    except Exception as exc:
        logger.warning("news: brave_news.js subprocess error — %s", exc)
        return None

    if result.returncode != 0:
        logger.warning(
            "news: brave_news.js exited %d — stderr: %s",
            result.returncode,
            result.stderr[:200],
        )
        return None

    stdout = result.stdout.strip()
    if not stdout:
        logger.warning("news: brave_news.js produced no output")
        return None

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("news: brave_news.js JSON parse error — %s", exc)
        return None

    # Combine recent (prioritised) then older items, capped at _MAX_HEADLINES.
    recent = data.get("recent", [])
    older = data.get("older", [])
    all_items = recent + older

    headlines: list[str] = []
    seen: set[str] = set()
    for item in all_items:
        title = (item.get("title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        headlines.append(f"- {title}")
        if len(headlines) >= _MAX_HEADLINES:
            break

    if not headlines:
        logger.warning("news: brave_news.js returned 0 headlines")
        return None

    return "\n".join(headlines)


# ── Section 2: Geopolitical risk ─────────────────────────────────────────────

def _fetch_geopolitical_risk() -> Optional[str]:
    """
    Parse ceasefire_factors.json and return a formatted geopolitical risk block.

    Returns None if the file is missing or malformed.
    """
    if not _CEASEFIRE_JSON.exists():
        logger.info("news: ceasefire_factors.json not found — skipping geopolitical section")
        return None

    try:
        with _CEASEFIRE_JSON.open() as fh:
            data = json.load(fh)
    except Exception as exc:
        logger.warning("news: failed to read ceasefire_factors.json — %s", exc)
        return None

    probability = data.get("probability")
    label = data.get("probability_label", "UNKNOWN")
    portfolio_action = data.get("portfolio_action", "")

    # Summarise active factors (top escalation + top ceasefire).
    factors = data.get("factors", [])
    active_factors = [f for f in factors if f.get("active")]

    ceasefire_factors = sorted(
        [f for f in active_factors if f.get("direction") == "ceasefire"],
        key=lambda f: abs(f.get("weight", 0)),
        reverse=True,
    )
    escalation_factors = sorted(
        [f for f in active_factors if f.get("direction") == "escalation"],
        key=lambda f: abs(f.get("weight", 0)),
        reverse=True,
    )

    factor_lines: list[str] = []
    for f in escalation_factors[:2]:
        factor_lines.append(f"⚠️  {f.get('label', 'unknown')}")
    for f in ceasefire_factors[:2]:
        factor_lines.append(f"✅ {f.get('label', 'unknown')}")

    risk_level = _probability_to_risk(probability)

    lines = [
        f"Current ceasefire probability: {probability}% ({label})",
        f"Risk level: {risk_level}",
    ]
    if factor_lines:
        lines.append("Key factors:")
        lines.extend(f"  {fl}" for fl in factor_lines)
    if portfolio_action:
        lines.append(f"Suggested action: {portfolio_action}")

    return "\n".join(lines)


def _probability_to_risk(probability: Optional[float]) -> str:
    """Map ceasefire probability to a portfolio risk label."""
    if probability is None:
        return "UNKNOWN"
    if probability >= 70:
        return "LOW"       # ceasefire likely → de-escalation
    if probability >= 40:
        return "MODERATE"
    if probability >= 20:
        return "ELEVATED"
    return "HIGH"


# ── Section 3: Macro snapshot ────────────────────────────────────────────────

def _fetch_macro_snapshot() -> Optional[str]:
    """
    Pull the most recent row from macro_indicators and return a formatted block.

    Returns None if SQLite is unavailable or the table is empty.
    """
    try:
        from db.atlas_db import get_db  # local import to avoid hard dependency

        with get_db() as db:
            row = db.execute(
                "SELECT * FROM macro_indicators ORDER BY date DESC LIMIT 1"
            ).fetchone()

        if row is None:
            logger.info("news: macro_indicators table is empty")
            return None

        row_dict = dict(row)
        return _format_macro_snapshot(row_dict)

    except Exception as exc:
        logger.warning("news: failed to read macro_indicators — %s", exc)
        return None


def _format_macro_snapshot(row: dict) -> str:
    """Format a macro_indicators row into a human-readable block."""
    lines: list[str] = []

    # VIX
    vix = row.get("vix")
    if vix is not None:
        vix_label = "elevated" if vix > 25 else ("high" if vix > 20 else "normal")
        lines.append(f"VIX: {vix:.1f} ({vix_label})")

    # Yield curve
    yc = row.get("yield_curve_10y2y")
    if yc is not None:
        yc_label = "inverted (recession signal)" if yc < 0 else "positive"
        lines.append(f"Yield Curve 10y-2y: {yc:+.2f} ({yc_label})")

    # Credit OAS
    oas = row.get("credit_oas")
    if oas is not None:
        oas_label = "wide (stress)" if oas > 2.0 else ("normal" if oas > 1.0 else "tight")
        lines.append(f"Credit IG OAS: {oas:.2f}% ({oas_label})")

    # DXY
    dxy = row.get("dxy")
    if dxy is not None:
        lines.append(f"Dollar (DXY): {dxy:.1f}")

    # Gold
    gold = row.get("gold")
    if gold is not None:
        lines.append(f"Gold: ${gold:.0f}/oz")

    # Date
    date = row.get("date")
    if date:
        lines.append(f"(as of {date})")

    return "\n".join(lines) if lines else "Macro data unavailable"


# ── Public API ───────────────────────────────────────────────────────────────

def get_news_summary() -> str:
    """
    Aggregate news, geopolitical risk, and macro snapshot into a prompt-ready string.

    Sections included when available:
        ## Market News (last 24h)       — Brave Search headlines
        ## Geopolitical Risk            — ceasefire_factors.json
        ## Macro Snapshot               — latest macro_indicators row

    Never raises.  If all sources fail, returns a minimal fallback string.
    """
    sections: list[str] = []

    # ── Market News ──────────────────────────────────────────────────────────
    brave_section = _fetch_brave_headlines()
    if brave_section:
        sections.append("## Market News (last 24h)\n" + brave_section)
    else:
        sections.append("## Market News (last 24h)\nNews unavailable — Brave search failed")

    # ── Geopolitical Risk ────────────────────────────────────────────────────
    geo_section = _fetch_geopolitical_risk()
    if geo_section:
        sections.append("## Geopolitical Risk\n" + geo_section)
    # If missing, skip the section entirely (ceasefire file optional)

    # ── Macro Snapshot ───────────────────────────────────────────────────────
    macro_section = _fetch_macro_snapshot()
    if macro_section:
        sections.append("## Macro Snapshot\n" + macro_section)

    if not sections:
        return "## Market Intelligence\nAll data sources unavailable."

    return "\n\n".join(sections)
