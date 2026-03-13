"""Param history reader — parse brain/params/*.md and compute statistics.

Called by sweep.py before each strategy sweep to:
1. Identify parameter values that have been tested 3+ times and always failed
2. Determine the best-performing direction for jitter bias
3. Detect stale strategies (no wins in recent experiments)

All functions are read-only (no writes); brain/writer.py handles writes.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

BRAIN_ROOT = Path(__file__).resolve().parent / "brain"
PARAMS_DIR = BRAIN_ROOT / "params"
STALE_RESET_PATH = BRAIN_ROOT / "staleness_reset.json"


# ─── Value parsing ────────────────────────────────────────────────────────────

def _parse_value(s: str) -> Any:
    """Parse a parameter value string to its Python type."""
    s = s.strip()
    if s == "None":
        return None
    if s == "True":
        return True
    if s == "False":
        return False
    # Try integer before float to preserve type (e.g. 14 not 14.0)
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s  # categorical / string


def _parse_sharpe_delta(s: str) -> float:
    """Parse sharpe delta string like '+0.0405' or '-0.1234' or '+0.0000'."""
    try:
        return float(s.strip())
    except ValueError:
        return 0.0


# ─── Load raw param history ───────────────────────────────────────────────────

def load_param_history(param_name: str) -> List[Dict]:
    """Parse brain/params/{param_name}.md table.

    Returns list of dicts with keys:
        date (str): "YYYY-MM-DD HH:MM"
        strategy (str)
        old_value (Any)
        new_value (Any)
        kept (bool)
        sharpe_delta (float)
        new_sharpe (float)

    Entries are in file order (typically chronological, oldest first).
    Returns [] if the file does not exist.
    """
    path = PARAMS_DIR / f"{param_name}.md"
    if not path.exists():
        return []

    results: List[Dict] = []
    in_table = False

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue

        # Header row — marks start of the data table
        if "Date" in stripped and "Strategy" in stripped and "Change" in stripped:
            in_table = True
            continue

        # Separator row (| --- | --- | ...)
        if in_table and re.match(r"\|[-\s|]+\|", stripped):
            continue

        if not in_table:
            continue

        # Data row
        cols = [c.strip() for c in stripped.split("|")]
        cols = [c for c in cols if c]  # drop empty strings from leading/trailing |
        if len(cols) < 6:
            continue

        try:
            date = cols[0]
            strategy = cols[1]
            change = cols[2]
            result_str = cols[3]
            sharpe_delta = _parse_sharpe_delta(cols[4])
            new_sharpe = float(cols[5])
        except (IndexError, ValueError):
            continue

        # Parse "old_value → new_value" (unicode arrow U+2192)
        if " → " in change:
            left, right = change.split(" → ", 1)
            old_value = _parse_value(left)
            new_value = _parse_value(right)
        else:
            old_value = None
            new_value = _parse_value(change)

        results.append({
            "date": date,
            "strategy": strategy,
            "old_value": old_value,
            "new_value": new_value,
            "kept": "✅" in result_str,
            "sharpe_delta": sharpe_delta,
            "new_sharpe": new_sharpe,
        })

    return results


# ─── Per-param statistics ─────────────────────────────────────────────────────

def get_param_win_rate(param_name: str, strategy: str) -> Dict:
    """For a given param+strategy, return statistics on all tests.

    Returns dict with keys:
        total_tests (int)
        wins (int): tests where result was "kept"
        losses (int): tests where result was "discard"
        win_rate (float): wins / total_tests (0.0 if no tests)
        best_value (Any): value with highest sharpe_delta (None if no tests)
        worst_value (Any): value with lowest sharpe_delta (None if no tests)
        avg_sharpe_delta (float): mean sharpe_delta across all tests
        values_tried (set): all values that have been tested
    """
    history = load_param_history(param_name)
    rows = [e for e in history if e["strategy"] == strategy]

    total_tests = len(rows)
    wins = sum(1 for e in rows if e["kept"])
    losses = total_tests - wins
    win_rate = wins / total_tests if total_tests > 0 else 0.0

    values_tried: Set[Any] = set()
    best_value: Any = None
    worst_value: Any = None
    best_delta = float("-inf")
    worst_delta = float("inf")

    for entry in rows:
        v = entry["new_value"]
        try:
            values_tried.add(v)
        except TypeError:
            pass
        d = entry["sharpe_delta"]
        if d > best_delta:
            best_delta = d
            best_value = v
        if d < worst_delta:
            worst_delta = d
            worst_value = v

    avg_sharpe_delta = (
        sum(e["sharpe_delta"] for e in rows) / total_tests
        if total_tests > 0
        else 0.0
    )

    return {
        "total_tests": total_tests,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "best_value": best_value,
        "worst_value": worst_value,
        "avg_sharpe_delta": avg_sharpe_delta,
        "values_tried": values_tried,
    }


# ─── Per-strategy comprehensive history ──────────────────────────────────────

def build_strategy_param_history(strategy: str) -> Dict[str, Dict]:
    """Build per-value statistics for all params for a given strategy.

    Scans all brain/params/*.md files and collects entries for this strategy.

    Returns:
        {
            param_name: {
                value: {
                    "tests": int,
                    "wins": int,
                    "sharpe_deltas": [float, ...]
                }
            }
        }

    Used by sweep.expand_grid() to:
    - Skip values tested 3+ times that always lost
    - Bias jitter toward best-performing value direction
    """
    result: Dict[str, Dict] = {}

    if not PARAMS_DIR.exists():
        return result

    for md_file in sorted(PARAMS_DIR.glob("*.md")):
        if md_file.name.startswith("_"):
            continue

        param_name = md_file.stem
        rows = [e for e in load_param_history(param_name) if e["strategy"] == strategy]

        if not rows:
            continue

        value_stats: Dict[Any, Dict] = {}
        for entry in rows:
            v = entry["new_value"]
            try:
                hash(v)
            except TypeError:
                v = str(v)  # fallback for unhashable values

            if v not in value_stats:
                value_stats[v] = {"tests": 0, "wins": 0, "sharpe_deltas": []}
            value_stats[v]["tests"] += 1
            value_stats[v]["wins"] += int(entry["kept"])
            value_stats[v]["sharpe_deltas"].append(entry["sharpe_delta"])

        result[param_name] = value_stats

    return result


# ─── Staleness detection ──────────────────────────────────────────────────────

def _load_stale_reset() -> Optional[datetime]:
    """Load the staleness reset timestamp from brain/staleness_reset.json."""
    if not STALE_RESET_PATH.exists():
        return None
    try:
        data = json.loads(STALE_RESET_PATH.read_text())
        ts_str = data.get("reset_at", "")
        if ts_str:
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception:
        pass
    return None


def get_strategy_staleness(strategy: str, window: int = 50) -> Dict:
    """Check if a strategy is stale (no wins in recent experiments).

    Scans ALL brain/params/*.md files for entries involving this strategy,
    sorts chronologically, and checks the last `window` entries for wins.
    A strategy must have at least window//2 recent experiments to be declared
    stale (avoids false positives for freshly-added strategies).

    Args:
        strategy: Strategy name (e.g. 'mean_reversion').
        window: Number of recent experiments to examine (default: 50).

    Returns dict with keys:
        total_recent (int): number of entries examined (≤ window)
        recent_wins (int): kept results in that window
        is_stale (bool): True iff recent_wins==0 AND total_recent >= window//2
        last_win_date (str or None): date of most recent kept result, any window
    """
    if not PARAMS_DIR.exists():
        return {
            "total_recent": 0,
            "recent_wins": 0,
            "is_stale": False,
            "last_win_date": None,
        }

    all_entries: List[Dict] = []

    for md_file in PARAMS_DIR.glob("*.md"):
        if md_file.name.startswith("_"):
            continue
        param_name = md_file.stem
        for entry in load_param_history(param_name):
            if entry["strategy"] == strategy:
                all_entries.append(entry)

    if not all_entries:
        return {
            "total_recent": 0,
            "recent_wins": 0,
            "is_stale": False,
            "last_win_date": None,
        }

    # Sort chronologically ("YYYY-MM-DD HH:MM" is lexicographically correct)
    all_entries.sort(key=lambda e: e["date"])

    # Apply staleness reset: ignore experiments before the reset timestamp
    reset_after = _load_stale_reset()
    if reset_after is not None:
        filtered = []
        for e in all_entries:
            try:
                entry_dt = datetime.strptime(e["date"], "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
                if entry_dt >= reset_after:
                    filtered.append(e)
            except ValueError:
                filtered.append(e)  # include if we can't parse the date
        if filtered:
            all_entries = filtered  # only swap if filter left something

    # Take last `window` entries
    recent = all_entries[-window:]
    total_recent = len(recent)
    recent_wins = sum(1 for e in recent if e["kept"])

    # Only declare stale if we have enough data (at least window//2 entries)
    is_stale = (total_recent >= window // 2) and (recent_wins == 0)

    # Last win date across ALL (potentially reset-filtered) entries
    last_win_date: Optional[str] = None
    for entry in reversed(all_entries):
        if entry["kept"]:
            last_win_date = entry["date"]
            break

    return {
        "total_recent": total_recent,
        "recent_wins": recent_wins,
        "is_stale": is_stale,
        "last_win_date": last_win_date,
    }


def reset_staleness() -> None:
    """Write a staleness reset marker so all strategies get re-evaluated.

    After calling this, get_strategy_staleness() will only count experiments
    that occurred after this moment, preventing any strategy from being
    declared stale until it accumulates enough new failures.

    Called by sweep.py when --reset-stale is passed.
    """
    STALE_RESET_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "reset_at": datetime.now(timezone.utc).isoformat(),
        "reason": "manual reset via --reset-stale CLI flag",
    }
    STALE_RESET_PATH.write_text(json.dumps(data, indent=2) + "\n")
