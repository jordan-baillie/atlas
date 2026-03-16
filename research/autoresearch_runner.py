#!/usr/bin/env python3
"""Headless autonomous parameter-sweep runner.

Runs a programmatic sweep over one strategy's parameters without calling any
LLM API.  For each parameter in the strategy's current best config the runner
generates a set of candidate values, consults the brain history to skip
already-discarded values, and runs backtest experiments via ResearchSession.

Typical usage (CLI)::

    python3 research/autoresearch_runner.py \\
        --strategy mean_reversion \\
        --market sp500 \\
        --hours 4 \\
        --notify

Or from Python::

    from research.autoresearch_runner import run_session
    run_session(strategy="mean_reversion", market="sp500", hours=2.0)

Design principles:
- Pure programmatic search — no LLM, no external calls
- Brain history awareness — skip already-discarded (strategy, param, value) triples
- Evaluation lock — inherits ResearchSession's SHA-256 lock on engine + data
- Crash resilience — 5 consecutive crashes abort cleanly; single crash continues
- Time budgeted — exits cleanly when ``--hours`` budget is exhausted
"""

import argparse
import copy
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

logger = logging.getLogger("autoresearch_runner")

# ─── Nested-dict Helpers ─────────────────────────────────────────────────────


def get_nested_param(params: dict, dotted_key: str) -> Any:
    """Get a value from a nested dict using dot notation.

    Example::

        get_nested_param({'volume': {'lookback': 20}}, 'volume.lookback')
        # → 20

    Args:
        params:     Parameter dict (may be nested).
        dotted_key: Dot-separated key path, e.g. ``'volume.lookback'``.

    Returns:
        The value at the nested path, or ``None`` if any key is missing.
    """
    keys = dotted_key.split(".")
    node = params
    for k in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def set_nested_param(params: dict, dotted_key: str, value: Any) -> dict:
    """Set a value in a nested dict using dot notation.  Returns a deep copy.

    Example::

        set_nested_param({'volume': {'lookback': 20}}, 'volume.lookback', 30)
        # → {'volume': {'lookback': 30}}

    Args:
        params:     Parameter dict (may be nested).  Not mutated.
        dotted_key: Dot-separated key path, e.g. ``'volume.lookback'``.
        value:      New value to set.

    Returns:
        Deep copy of *params* with the nested key set to *value*.
    """
    result = copy.deepcopy(params)
    keys = dotted_key.split(".")
    node = result
    for k in keys[:-1]:
        if k not in node or not isinstance(node[k], dict):
            node[k] = {}
        node = node[k]
    node[keys[-1]] = value
    return result


# ─── Brain History ───────────────────────────────────────────────────────────


def check_brain_history(
    strategy: str, param_name: str, candidate_value: Any
) -> Optional[str]:
    """Check if the brain already recorded a discarded result for this triple.

    Reads ``research/brain/params/{param_name}.md`` and scans the markdown
    table for rows matching *strategy* + *candidate_value*.

    The table format is::

        | Date | Strategy | Change | Result | Sharpe Δ | New Sharpe |
        | 2026-03-12 13:26 | mean_reversion | 14 → 7 | ❌ discard | -0.03 | 0.41 |

    Args:
        strategy:        Strategy name (e.g. ``'mean_reversion'``).
        param_name:      Dotted key path used as the filename stem
                         (e.g. ``'rsi_period'`` or ``'volume.lookback'``).
        candidate_value: Value to look up.

    Returns:
        ``None`` if no discarded entry is found (safe to try this value).
        A reason string if the value was previously discarded.
    """
    param_file = ATLAS_ROOT / "research" / "brain" / "params" / f"{param_name}.md"
    if not param_file.exists():
        return None  # No history for this param at all

    candidate_str = str(candidate_value)

    try:
        content = param_file.read_text()
    except OSError:
        return None

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("| Date"):
            continue
        # Skip separator rows like |---|---|...
        if set(stripped.replace("|", "").replace("-", "").replace(" ", "")) == set():
            continue

        parts = [p.strip() for p in stripped.split("|")]
        # parts[0] is empty (leading |), parts[-1] is empty (trailing |)
        # Indices: 1=date, 2=strategy, 3=change, 4=result, 5=sharpe_delta, 6=new_sharpe
        if len(parts) < 6:
            continue

        row_strategy = parts[2].strip()
        row_change = parts[3].strip()   # e.g. "14 → 7"
        row_result = parts[4].strip()   # e.g. "❌ discard"

        if row_strategy != strategy:
            continue

        # Parse the new value from the change column "old → new"
        if "→" in row_change:
            _, _, new_val_str = row_change.partition("→")
            new_val_str = new_val_str.strip()
        else:
            new_val_str = row_change.strip()

        if new_val_str != candidate_str:
            continue

        # Matched — was it discarded?
        if "discard" in row_result.lower():
            return f"brain: previously discarded for {strategy} ({row_change})"

    return None


# ─── Sweep Plan ──────────────────────────────────────────────────────────────


def _flatten_params(d: dict, prefix: str = ""):
    """Yield ``(dotted_key, value)`` pairs from a possibly-nested dict."""
    for k, v in d.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            yield from _flatten_params(v, full_key)
        else:
            yield full_key, v


def _brain_history_count(strategy: str, param_name: str) -> int:
    """Return the number of history table rows for (strategy, param_name)."""
    param_file = (
        ATLAS_ROOT / "research" / "brain" / "params" / f"{param_name}.md"
    )
    if not param_file.exists():
        return 0
    try:
        content = param_file.read_text()
    except OSError:
        return 0
    count = 0
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or stripped.startswith("| Date"):
            continue
        parts = [p.strip() for p in stripped.split("|")]
        if len(parts) < 6:
            continue
        if parts[2].strip() == strategy:
            count += 1
    return count


def build_sweep_plan(
    strategy: str,
    market: str,
    current_best: dict,
) -> List[Tuple[str, str, Any]]:
    """Build an ordered list of ``(display_name, dotted_key, candidate_value)`` to try.

    For each numeric parameter: generates candidates at ``current * [0.5, 0.75,
    0.9, 1.1, 1.25, 1.5]``, filters out values already discarded in the brain,
    and orders by distance from the current value (smallest perturbation first).

    For boolean parameters: tries the opposite value (if not already discarded).

    Parameters are ordered so those with the most brain history for this strategy
    come first (to build on known data), followed by unexplored parameters.

    Args:
        strategy:     Strategy name (e.g. ``'mean_reversion'``).
        market:       Market ID (unused currently, reserved for future use).
        current_best: Current best parameter dict (may be nested).

    Returns:
        List of ``(display_name, dotted_key, candidate_value)`` tuples in the
        order experiments should be tried.
    """
    # Collect all parameter groups: {dotted_key: [(dist, candidate_value), ...]}
    param_candidates: dict = {}

    for dotted_key, current_val in _flatten_params(current_best):
        candidates: List[Tuple[float, Any]] = []  # (distance, value)

        if isinstance(current_val, bool):
            # Try the opposite
            candidate = not current_val
            reason = check_brain_history(strategy, dotted_key, candidate)
            if reason is None:
                candidates.append((1.0, candidate))

        elif isinstance(current_val, (int, float)):
            multipliers = [0.5, 0.75, 0.9, 1.1, 1.25, 1.5]
            seen = set()
            for m in multipliers:
                raw = current_val * m
                # Keep type: ints stay int, floats rounded to 2dp
                if isinstance(current_val, int):
                    candidate = max(1, round(raw))
                else:
                    candidate = round(raw, 2)

                if candidate == current_val or candidate in seen:
                    continue
                seen.add(candidate)

                reason = check_brain_history(strategy, dotted_key, candidate)
                if reason is not None:
                    logger.debug(
                        "Skipping %s=%s — %s", dotted_key, candidate, reason
                    )
                    continue

                dist = abs(raw - current_val)
                candidates.append((dist, candidate))

            # Sort by ascending distance (smallest perturbation first)
            candidates.sort(key=lambda x: x[0])
        else:
            # Strings, None, etc. — skip
            continue

        if candidates:
            param_candidates[dotted_key] = candidates

    # Order parameters: most brain history first, then unexplored
    sorted_keys = sorted(
        param_candidates.keys(),
        key=lambda k: -_brain_history_count(strategy, k),
    )

    plan: List[Tuple[str, str, Any]] = []
    for dotted_key in sorted_keys:
        current_val = get_nested_param(current_best, dotted_key)
        for _dist, candidate in param_candidates[dotted_key]:
            display = f"{dotted_key}: {current_val} → {candidate}"
            plan.append((display, dotted_key, candidate))

    return plan


# ─── Telegram ────────────────────────────────────────────────────────────────


def _try_send_telegram(text: str) -> None:
    """Attempt to send *text* via Telegram.  Never raises — failures are logged.

    Uses ``utils.telegram.notify`` if available.
    """
    try:
        from utils.telegram import notify
        notify(text, category="autoresearch")
    except ImportError:
        logger.info("Telegram notification not configured (utils.telegram not found).")
    except Exception as exc:
        logger.warning("Telegram notification failed (non-fatal): %s", exc)


# ─── Session Runner ──────────────────────────────────────────────────────────


def run_session(
    strategy: str,
    market: str = "sp500",
    hours: float = 4.0,
    notify: bool = False,
    snapshot_id: Optional[str] = None,
) -> dict:
    """Run a headless parameter-sweep research session for *strategy*.

    Flow:
    1. Set up logging and print session banner.
    2. Create :class:`~research.loop.ResearchSession` (loads data + eval lock).
    3. Run ``session.baseline()`` to establish the bar.
    4. Build sweep plan from baseline params.
    5. Loop through sweep plan:
       - Skip if brain history says already discarded.
       - Run ``session.experiment()``; call ``session.keep()`` or ``session.discard()``.
       - Count consecutive crashes; abort after 5.
       - Break when time budget is exhausted.
    6. Print session summary.
    7. Send Telegram notification if ``notify=True``.

    Args:
        strategy:    Strategy name to optimise.
        market:      Market ID (default ``'sp500'``).
        hours:       Wall-clock time budget in hours.
        notify:      Send Telegram summary when session completes.
        snapshot_id: Specific snapshot to use (auto-discovered if ``None``).

    Returns:
        Summary dict with ``experiments_run``, ``experiments_kept``,
        ``experiments_skipped``, ``starting_sharpe``, ``final_sharpe``,
        ``runtime_s``, and ``status``.
    """
    # ── Logging ──────────────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )

    deadline = time.time() + hours * 3600
    session_start = time.time()

    _print_banner(strategy, market, hours, snapshot_id)

    # ── Create session ────────────────────────────────────────────────────────
    logger.info("Initialising ResearchSession(%s, %s) ...", strategy, market)
    from research.loop import ResearchSession
    from research.lockfile import EvaluationLockViolation

    session = ResearchSession(strategy, market, snapshot_id=snapshot_id)
    logger.info("Session ID: %s", session.session_id)

    # ── Baseline ──────────────────────────────────────────────────────────────
    logger.info("Running baseline ...")
    try:
        baseline_metrics = session.baseline()
    except Exception as exc:
        logger.error("Baseline failed — aborting session: %s", exc)
        traceback.print_exc()
        return {
            "status": "baseline_failed",
            "error": str(exc),
            "runtime_s": round(time.time() - session_start, 1),
        }

    starting_sharpe: float = baseline_metrics.get("sharpe", 0.0) or 0.0
    current_sharpe: float = starting_sharpe
    logger.info("Baseline Sharpe: %.4f", starting_sharpe)

    # ── Sweep plan ────────────────────────────────────────────────────────────
    current_best_params = dict(session._best_params)
    logger.info("Building sweep plan ...")
    plan = build_sweep_plan(strategy, market, current_best_params)
    logger.info("Sweep plan: %d experiments queued.", len(plan))

    # ── Counters ──────────────────────────────────────────────────────────────
    experiments_run = 0
    experiments_kept = 0
    experiments_skipped = 0
    consecutive_crashes = 0
    MAX_CONSECUTIVE_CRASHES = 5

    # ── Main loop ─────────────────────────────────────────────────────────────
    for idx, (display_name, dotted_key, candidate_value) in enumerate(plan):

        # Time budget check
        if time.time() >= deadline:
            logger.info(
                "Time budget exhausted (%.1f h) — ending session cleanly.", hours
            )
            break

        # Re-check brain just before running (another agent might have run it)
        reason = check_brain_history(strategy, dotted_key, candidate_value)
        if reason is not None:
            logger.info(
                "[%d/%d] SKIP  %s — %s",
                idx + 1, len(plan), display_name, reason,
            )
            experiments_skipped += 1
            continue

        logger.info(
            "[%d/%d] RUNNING  %s",
            idx + 1, len(plan), display_name,
        )

        # Build params: apply candidate to current best (handles nested keys)
        try:
            exp_params = set_nested_param(
                session._best_params, dotted_key, candidate_value
            )
        except Exception as exc:
            logger.warning("Could not build params for %s: %s", display_name, exc)
            continue

        # Run experiment
        try:
            result = session.experiment(exp_params, description=display_name)
        except EvaluationLockViolation as exc:
            # This is fatal — the engine or data changed mid-session
            logger.error("EVALUATION LOCK VIOLATED — aborting session: %s", exc)
            _summarise_and_notify(
                strategy, market, experiments_run, experiments_kept,
                experiments_skipped, starting_sharpe, current_sharpe,
                time.time() - session_start, notify,
                status="lock_violated",
            )
            return {
                "status": "lock_violated",
                "error": str(exc),
                "changed_files": exc.changed,
                "experiments_run": experiments_run,
                "experiments_kept": experiments_kept,
                "experiments_skipped": experiments_skipped,
            }
        except Exception as exc:
            logger.error(
                "Experiment %d CRASHED: %s", experiments_run + 1, exc
            )
            traceback.print_exc()
            consecutive_crashes += 1
            if consecutive_crashes >= MAX_CONSECUTIVE_CRASHES:
                logger.error(
                    "%d consecutive crashes — aborting session.", consecutive_crashes
                )
                break
            continue

        experiments_run += 1
        consecutive_crashes = 0  # Reset on any successful experiment

        recommendation = result.get("recommendation", "discard")
        sharpe = result.get("metrics", {}).get("sharpe", 0.0) or 0.0
        rationale = result.get("rationale", "")

        if recommendation == "keep":
            session.keep()
            experiments_kept += 1
            current_sharpe = sharpe
            logger.info(
                "  → KEPT   Sharpe %.4f (+%.4f)  %s",
                sharpe,
                result.get("delta", {}).get("sharpe", 0.0),
                rationale,
            )
            # Update current best so subsequent experiments build on it
            current_best_params = dict(session._best_params)
        else:
            session.discard()
            logger.info(
                "  → DISCARD Sharpe %.4f  %s",
                sharpe,
                rationale,
            )

    # ── Final summary ─────────────────────────────────────────────────────────
    runtime_s = time.time() - session_start
    summary = _summarise_and_notify(
        strategy, market, experiments_run, experiments_kept,
        experiments_skipped, starting_sharpe, current_sharpe,
        runtime_s, notify,
    )
    return summary


# ─── Banner / Summary ────────────────────────────────────────────────────────


def _print_banner(
    strategy: str,
    market: str,
    hours: float,
    snapshot_id: Optional[str],
) -> None:
    """Print a start-of-session banner to stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(
        f"\n{'='*65}\n"
        f"  Atlas AutoResearch — Headless Parameter Sweep\n"
        f"{'='*65}\n"
        f"  Strategy : {strategy}\n"
        f"  Market   : {market}\n"
        f"  Budget   : {hours:.1f} h\n"
        f"  Snapshot : {snapshot_id or 'auto-discover'}\n"
        f"  Started  : {ts}\n"
        f"{'='*65}\n"
    )


def _print_summary(
    strategy: str,
    market: str,
    experiments_run: int,
    experiments_kept: int,
    experiments_skipped: int,
    starting_sharpe: float,
    final_sharpe: float,
    runtime_s: float,
    status: str = "complete",
) -> None:
    """Print an end-of-session summary to stdout."""
    delta = final_sharpe - starting_sharpe
    mins = runtime_s / 60.0
    print(
        f"\n{'='*65}\n"
        f"  AutoResearch Session Summary — {strategy} / {market}\n"
        f"{'='*65}\n"
        f"  Status         : {status}\n"
        f"  Experiments    : {experiments_run} run, {experiments_kept} kept, "
        f"{experiments_skipped} skipped\n"
        f"  Starting Sharpe: {starting_sharpe:.4f}\n"
        f"  Final Sharpe   : {final_sharpe:.4f}  ({delta:+.4f})\n"
        f"  Runtime        : {mins:.1f} min\n"
        f"{'='*65}\n"
    )


def _summarise_and_notify(
    strategy: str,
    market: str,
    experiments_run: int,
    experiments_kept: int,
    experiments_skipped: int,
    starting_sharpe: float,
    final_sharpe: float,
    runtime_s: float,
    notify: bool,
    status: str = "complete",
) -> dict:
    """Print summary, optionally send Telegram, and return summary dict."""
    _print_summary(
        strategy, market, experiments_run, experiments_kept,
        experiments_skipped, starting_sharpe, final_sharpe, runtime_s, status,
    )

    summary = {
        "status": status,
        "strategy": strategy,
        "market": market,
        "experiments_run": experiments_run,
        "experiments_kept": experiments_kept,
        "experiments_skipped": experiments_skipped,
        "starting_sharpe": round(starting_sharpe, 4),
        "final_sharpe": round(final_sharpe, 4),
        "delta_sharpe": round(final_sharpe - starting_sharpe, 4),
        "runtime_s": round(runtime_s, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if notify:
        delta = final_sharpe - starting_sharpe
        mins = runtime_s / 60.0
        msg = (
            f"<b>AutoResearch complete — {strategy} / {market}</b>\n"
            f"Status: {status}\n"
            f"Experiments: {experiments_run} run | {experiments_kept} kept "
            f"| {experiments_skipped} skipped\n"
            f"Sharpe: {starting_sharpe:.4f} → {final_sharpe:.4f} "
            f"({delta:+.4f})\n"
            f"Runtime: {mins:.1f} min"
        )
        _try_send_telegram(msg)

    return summary


# ─── CLI Entry Point ─────────────────────────────────────────────────────────


def _parse_args(argv=None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Headless Atlas parameter-sweep research runner.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 research/autoresearch_runner.py \\\n"
            "      --strategy mean_reversion --hours 4 --notify\n"
        ),
    )
    parser.add_argument(
        "--strategy",
        required=True,
        help="Strategy name to optimise (e.g. mean_reversion).",
    )
    parser.add_argument(
        "--market",
        default="sp500",
        help="Market ID (default: sp500).",
    )
    parser.add_argument(
        "--hours",
        type=float,
        required=True,
        help="Wall-clock time budget in hours.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="Send Telegram summary at end of session.",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="Snapshot ID to use (auto-discovered if omitted).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    result = run_session(
        strategy=args.strategy,
        market=args.market,
        hours=args.hours,
        notify=args.notify,
        snapshot_id=args.snapshot,
    )
    sys.exit(0 if result.get("status") in ("complete", None) else 1)
