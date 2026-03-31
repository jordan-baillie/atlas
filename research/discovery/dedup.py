#!/usr/bin/env python3
"""Deduplication helpers for Atlas research discovery.

Tracks seen URLs and avoids generating duplicate strategies.
"""

import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

DISCOVERY_DIR = Path(__file__).resolve().parent
ATLAS_ROOT = DISCOVERY_DIR.parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

SEEN_FILE = DISCOVERY_DIR / "seen_urls.txt"

# Strategy directories to scan
_STRATEGY_DIRS = [
    ATLAS_ROOT / "research" / "strategies",
    ATLAS_ROOT / "strategies",
]


def load_seen() -> set:
    """Load all seen URLs from the seen_urls.txt file.

    File format: {iso_timestamp}\\t{outcome}\\t{url}
    Returns set of URLs (3rd column).
    """
    if not SEEN_FILE.exists():
        return set()
    seen = set()
    try:
        for line in SEEN_FILE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                seen.add(parts[2].strip())
            elif len(parts) == 1:
                # Legacy: bare URL
                seen.add(parts[0].strip())
    except Exception:
        pass
    return seen


def is_seen(url: str) -> bool:
    """Return True if the given URL has already been processed."""
    return url.strip() in load_seen()


def mark_seen(url: str, outcome: str) -> None:
    """Record a URL as seen with an outcome label.

    Appends a tab-separated line: {iso_timestamp}\\t{outcome}\\t{url}
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN_FILE, "a") as f:
        f.write(f"{ts}\t{outcome}\t{url}\n")


def load_existing_strategies() -> list:
    """Scan all .py files in strategy directories and extract name + docstring.

    Returns a list of dicts: [{"name": str, "description": str, "path": str}]
    """
    strategies = []
    seen_names = set()

    for strategies_dir in _STRATEGY_DIRS:
        if not strategies_dir.exists():
            continue
        for py_file in sorted(strategies_dir.glob("*.py")):
            if py_file.name.startswith("__"):
                continue
            name = py_file.stem
            if name in seen_names:
                continue
            seen_names.add(name)

            # Read first 50 lines to extract docstring / description
            try:
                lines = py_file.read_text().splitlines()[:50]
                text = "\n".join(lines)
                # Extract triple-quoted docstring
                description = ""
                in_docstring = False
                docstring_lines = []
                for line in lines:
                    stripped = line.strip()
                    if not in_docstring:
                        if stripped.startswith('"""') or stripped.startswith("'''"):
                            in_docstring = True
                            content = stripped.lstrip('"""').lstrip("'''")
                            if content:
                                docstring_lines.append(content)
                            # Check if single-line docstring
                            if stripped.count('"""') >= 2 or stripped.count("'''") >= 2:
                                in_docstring = False
                    else:
                        if '"""' in stripped or "'''" in stripped:
                            in_docstring = False
                            docstring_lines.append(stripped.replace('"""', '').replace("'''", ''))
                            break
                        docstring_lines.append(stripped)
                description = " ".join(docstring_lines[:5]).strip()
                if not description:
                    description = text[:200]
            except Exception:
                description = ""

            strategies.append({
                "name": name,
                "description": description,
                "path": str(py_file),
            })

    return strategies


def _extract_keywords(text: str) -> set:
    """Extract meaningful keywords from rule text for overlap comparison."""
    if not text:
        return set()
    import re
    # Remove punctuation, lowercase, split
    words = re.sub(r"[^a-z0-9_ ]", " ", text.lower()).split()
    # Filter stopwords
    stopwords = {
        "and", "or", "if", "the", "a", "an", "is", "in", "on", "of", "to",
        "with", "when", "than", "at", "by", "for", "as", "be", "are", "was",
        "has", "from", "not", "its", "this", "that", "it", "we", "use",
        "price", "close", "open", "high", "low", "volume", "day", "bar",
        "stock", "market", "signal", "entry", "exit", "buy", "sell",
    }
    return {w for w in words if len(w) > 2 and w not in stopwords}


def is_duplicate_strategy(spec: dict, existing: list = None) -> bool:
    """Check if a strategy spec duplicates an already-implemented strategy.

    Two checks:
    1. The strategy_name already exists as a file.
    2. Keyword overlap between entry/exit rules and existing strategy descriptions > 70%.

    Args:
        spec: Strategy spec dict with at least 'strategy_name', 'entry_rules', 'exit_rules'.
        existing: Pre-loaded list from load_existing_strategies(). Loaded fresh if None.

    Returns True if this appears to be a duplicate.
    """
    if existing is None:
        existing = load_existing_strategies()

    name = spec.get("strategy_name", "")

    # Check 1: name collision
    existing_names = {s["name"] for s in existing}
    if name in existing_names:
        return True

    # Check 2: keyword overlap
    spec_rules = " ".join([
        spec.get("entry_rules", "") or "",
        spec.get("exit_rules", "") or "",
        spec.get("description", "") or "",
    ])
    spec_keywords = _extract_keywords(spec_rules)

    if not spec_keywords:
        return False

    for strat in existing:
        strat_keywords = _extract_keywords(strat.get("description", ""))
        if not strat_keywords:
            continue
        # Jaccard similarity
        intersection = spec_keywords & strat_keywords
        union = spec_keywords | strat_keywords
        if union and len(intersection) / len(union) > 0.70:
            return True

    return False
