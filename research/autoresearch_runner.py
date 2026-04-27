#!/usr/bin/env python3
"""Headless autonomous parameter-sweep runner.

Runs a programmatic sweep over one strategy's parameters without calling any
LLM API.  For each parameter in the strategy's current best config the runner
generates a set of candidate values, consults the brain history to skip
already-discarded values, and runs backtest experiments via ResearchSession.

Three-stage gating (``--fast-screen``, default on):

0. **Vectorised presort** — for supported strategies (mean_reversion),
   runs a vectorised parameter sweep (~5-30 s for hundreds of combos) to
   pre-score entry-signal parameters.  Reorders the sweep plan so the most
   promising parameter regions are tested first, maximising improvement
   per unit time.  Transparent — no experiments are skipped, only reordered.
1. **Solo screen** — solo backtest on top-50 tickers (~20-25 s).
   ``keep_or_discard()`` applied on solo Sharpe.  Discards are logged and
   skipped immediately.
2. **Combined verify** — full combined portfolio backtest on the complete
   universe (~13 min).  ``keep_or_discard()`` applied on combined Sharpe.
   Only experiments that pass *both* stages are kept.

When ``--no-fast-screen`` is passed, every experiment runs the full combined
backtest directly (original behaviour, maximum rigor, lower throughput).
The vectorised presort still runs regardless of ``--fast-screen``.

Typical usage (CLI)::

    python3 research/autoresearch_runner.py \\
        --strategy mean_reversion \\
        --market sp500 \\
        --hours 4 \\
        --notify

    # Disable fast screen for maximum rigor:
    python3 research/autoresearch_runner.py \\
        --strategy mean_reversion --hours 8 --no-fast-screen

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


# ─── Vectorised Pre-Sort ──────────────────────────────────────────────────────

# Mapping from autoresearch dotted param keys → vectorised sweep grid keys
_VEC_PARAM_MAP = {
    "rsi_period":    "rsi_period",
    "rsi_oversold":  "rsi_threshold",
    "zscore_lookback": "zscore_lookback",
    "zscore_entry":  "zscore_threshold",
}

# Strategies that have vectorised sweep support
_VEC_SUPPORTED_STRATEGIES = {"mean_reversion"}


def _vectorised_presort(
    strategy: str,
    plan: List[Tuple[str, str, Any]],
    data: dict,
    current_best: dict,
) -> List[Tuple[str, str, Any]]:
    """Reorder sweep plan using vectorised parameter pre-scoring.

    For mean_reversion, the 4 entry-signal parameters (rsi_period,
    rsi_oversold, zscore_lookback, zscore_entry) can be scored in seconds
    via the vectorised sweep.  Experiments touching these parameters are
    reordered so the most promising candidates come first.  Experiments
    touching other parameters (ATR, volume, breadth, etc.) keep their
    original ordering and are appended after the vectorised-sorted ones.

    For unsupported strategies, returns the plan unchanged.

    Args:
        strategy:     Strategy name.
        plan:         Original sweep plan from build_sweep_plan().
        data:         Full ticker data dict (passed to vectorised sweep).
        current_best: Current best params for this strategy.

    Returns:
        Reordered plan (same items, different order).
    """
    if strategy not in _VEC_SUPPORTED_STRATEGIES:
        return plan

    try:
        from research.vectorised_sweep import sweep_mean_reversion
    except ImportError:
        logger.warning("vectorised_sweep not available — skipping presort")
        return plan

    # Split plan into vectorisable and non-vectorisable experiments
    vec_experiments = []   # (idx, display, key, value) for entry-signal params
    other_experiments = [] # everything else

    for item in plan:
        display_name, dotted_key, candidate_value = item
        if dotted_key in _VEC_PARAM_MAP:
            vec_experiments.append(item)
        else:
            other_experiments.append(item)

    if not vec_experiments:
        return plan

    # Build param grid: current best + all candidates from plan
    grid = {
        "rsi_period": set(),
        "rsi_threshold": set(),
        "zscore_lookback": set(),
        "zscore_threshold": set(),
    }

    # Add current best values
    grid["rsi_period"].add(current_best.get("rsi_period", 14))
    grid["rsi_threshold"].add(current_best.get("rsi_oversold", 35))
    grid["zscore_lookback"].add(current_best.get("zscore_lookback", 30))
    grid["zscore_threshold"].add(current_best.get("zscore_entry", -2.0))

    # Add all candidate values from the plan
    for _display, dotted_key, candidate in vec_experiments:
        grid_key = _VEC_PARAM_MAP[dotted_key]
        grid[grid_key].add(candidate)

    # Convert sets to sorted lists
    param_grid = {k: sorted(v) for k, v in grid.items()}
    hold_days = current_best.get("max_hold_days", 10)

    logger.info(
        "Vectorised presort: %d entry-param experiments, grid %s",
        len(vec_experiments),
        {k: len(v) for k, v in param_grid.items()},
    )

    t0 = time.time()
    try:
        results_df = sweep_mean_reversion(data, param_grid, hold_days=hold_days)
    except Exception as exc:
        logger.warning("Vectorised sweep failed — keeping original order: %s", exc)
        return plan

    elapsed = time.time() - t0
    logger.info("Vectorised presort completed in %.1f s (%d combos scored)",
                elapsed, len(results_df))

    if results_df.empty:
        return plan

    # Build a score lookup: (param_key, candidate_value) → best vectorised score
    # For each experiment that changes ONE param, look up all combos where that
    # param equals the candidate and all others equal current best, then take
    # the best score.
    current_rsi_p = current_best.get("rsi_period", 14)
    current_rsi_th = current_best.get("rsi_oversold", 35)
    current_zsc_lb = current_best.get("zscore_lookback", 30)
    current_zsc_th = current_best.get("zscore_entry", -2.0)

    def _score_for_experiment(dotted_key: str, candidate_value: Any) -> float:
        """Look up vectorised score for a single-param change."""
        grid_key = _VEC_PARAM_MAP[dotted_key]
        # Filter: all params at current best EXCEPT the one being changed
        mask = (
            (results_df["rsi_period"] == (candidate_value if grid_key == "rsi_period" else current_rsi_p)) &
            (results_df["rsi_threshold"] == (candidate_value if grid_key == "rsi_threshold" else current_rsi_th)) &
            (results_df["zscore_lookback"] == (candidate_value if grid_key == "zscore_lookback" else current_zsc_lb)) &
            (results_df["zscore_threshold"] == (candidate_value if grid_key == "zscore_threshold" else current_zsc_th))
        )
        matches = results_df.loc[mask, "score"]
        if matches.empty:
            return float("-inf")
        return float(matches.iloc[0])

    # Score each vectorisable experiment
    scored = []
    for item in vec_experiments:
        display_name, dotted_key, candidate_value = item
        score = _score_for_experiment(dotted_key, candidate_value)
        scored.append((score, item))

    # Sort by descending score (most promising first)
    scored.sort(key=lambda x: x[0], reverse=True)

    # Log top 5 for visibility
    for i, (score, (display, _key, _val)) in enumerate(scored[:5]):
        logger.info("  Presort #%d: score=%.4f  %s", i + 1, score, display)

    # Reconstruct plan: vectorised-sorted experiments first, then others
    reordered = [item for _score, item in scored] + other_experiments
    logger.info(
        "Presort reordered: %d entry-param experiments prioritised, "
        "%d other experiments appended",
        len(vec_experiments), len(other_experiments),
    )
    return reordered


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


def _run_solo_screen(
    strategy: str,
    config: dict,
    data_subset: dict,
    baseline_metrics: dict,
    params: dict,
    description: str,
) -> Tuple[dict, dict]:
    """Stage 1: fast solo backtest on a ticker subset (~20-25 s).

    Runs a solo backtest (only *strategy*, no other strategies) on
    *data_subset* (typically top-50 tickers by volume).  Applies
    ``keep_or_discard()`` against a solo baseline.

    Args:
        strategy:         Strategy name.
        config:           Active config dict.
        data_subset:      Ticker data subset (top N tickers).
        baseline_metrics: Solo baseline metrics on the same subset.
        params:           Candidate parameter dict (full, already merged).
        description:      Human-readable experiment description.

    Returns:
        ``(metrics, verdict)`` where *verdict* is the ``keep_or_discard()``
        result dict.
    """
    from scripts.strategy_evaluator import make_config_with_strategy, run_backtest
    from research.loop import keep_or_discard

    cfg = make_config_with_strategy(config, strategy, params_override=params, solo=True)
    t0 = time.time()
    metrics = run_backtest(cfg, data_subset)
    metrics["runtime_s"] = round(time.time() - t0, 1)

    verdict = keep_or_discard(baseline_metrics, metrics)
    return metrics, verdict


def run_session(
    strategy: str,
    market: str = "sp500",
    universe: str = "sp500",
    hours: float = 4.0,
    notify: bool = False,
    snapshot_id: Optional[str] = None,
    fast_screen: bool = True,
    auto_promote_enabled: bool = True,
) -> dict:
    """Run a headless parameter-sweep research session for *strategy*.

    When *fast_screen* is ``True`` (the default) each candidate goes through a
    two-stage gate:

    1. **Solo screen** — solo backtest on top-50 tickers (~20-25 s).  If
       ``keep_or_discard()`` says DISCARD, the candidate is rejected
       immediately.
    2. **Combined verify** — full combined portfolio backtest on the complete
       universe (~13 min).  Only candidates that pass *both* stages are kept.

    When *fast_screen* is ``False`` every candidate runs the full combined
    backtest directly (maximum rigor, lower throughput).

    Args:
        strategy:    Strategy name to optimise.
        market:      Market ID used for config and snapshot discovery
                     (default ``'sp500'``).
        universe:    Universe name for data loading.  When set to a value
                     other than ``'sp500'``, data is loaded via
                     ``build_from_definition(universe)`` instead of the
                     snapshot pipeline (default ``'sp500'``).  Backward
                     compatible — omitting this arg behaves identically to
                     the pre-universe-flag behaviour.
        hours:       Wall-clock time budget in hours.
        notify:      Send Telegram summary when session completes.
        snapshot_id: Specific snapshot to use (auto-discovered if ``None``).
        fast_screen: Use two-stage solo-screen + combined-verify gating
                     (default ``True``).  Pass ``False`` for full-rigour mode.
        auto_promote_enabled: When ``True`` (default), call
                     ``_promote_session_result()`` after a successful session.
                     Pass ``False`` (``--no-auto-promote`` CLI flag) to disable.

    Returns:
        Summary dict with ``screened``, ``promoted``, ``kept``,
        ``skipped``, ``starting_sharpe``, ``final_sharpe``,
        ``runtime_s``, and ``status``.  If promotion ran, also contains a
        ``promotion`` key with the outcome dict.
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

    _print_banner(strategy, market, hours, snapshot_id, fast_screen)

    # ── Create session ────────────────────────────────────────────────────────
    logger.info("Initialising ResearchSession(%s, %s) ...", strategy, market)
    from research.loop import ResearchSession, keep_or_discard, _append_result
    from research.lockfile import EvaluationLockViolation

    session = ResearchSession(strategy, market, snapshot_id=snapshot_id)
    # Sanity: config market must match the session market (catches cross-universe leaks)
    try:
        assert session._config.get("market") == market, (
            f"ResearchSession market mismatch: config.market={session._config.get('market')!r} "
            f"!= arg={market!r}"
        )
    except AttributeError:
        pass  # session._config may not exist on all code paths

    # ── Override data source for non-sp500 universes ──────────────────────────
    # When --universe is specified, replace session data with data loaded via
    # build_from_definition() so that parameter sweeps run against the correct
    # ticker set.  The config (strategy params, thresholds) is still read from
    # the sp500 active config since strategies share the same parameter schema.
    if universe != "sp500":
        logger.info(
            "Loading %s universe data via build_from_definition() ...", universe
        )
        try:
            from universe.builder import build_from_definition
            universe_data = build_from_definition(universe)
            session._data = universe_data
            if not universe_data:
                msg = (
                    f"Universe '{universe}' returned 0 tickers — no data available. "
                    f"Skipping session."
                )
                logger.error(msg)
                # Send alert instead of silently skipping
                _try_send_telegram(
                    f"⚠️ Universe <b>{universe}</b> has 0 data in SQLite — "
                    f"ingest pipeline may be broken. "
                    f"Run: <code>python3 scripts/cli.py ingest -m {universe}</code>"
                )
                _print_summary(
                    strategy, market, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0,
                    status="skipped",
                )
                return
            # Tag session so results / brain writes use the correct universe key
            session.market = universe
            logger.info(
                "Loaded %d tickers from universe '%s'.",
                len(session._data), universe,
            )
        except Exception as exc:
            logger.error(
                "Failed to load universe '%s' via build_from_definition: %s — "
                "falling back to snapshot data.",
                universe, exc,
            )
            # Don't abort the session — use snapshot data as fallback
    logger.info("Session ID: %s", session.session_id)

    # ── Prepare fast-screen data (top-50 subset) ─────────────────────────────
    solo_baseline_metrics: Optional[dict] = None
    data_subset: Optional[dict] = None

    if fast_screen:
        from research.quick_screen import _top_n_tickers
        from scripts.strategy_evaluator import make_config_with_strategy, run_backtest

        data_subset = _top_n_tickers(session._data, n=50)
        logger.info(
            "Fast-screen enabled: solo baseline on %d tickers ...", len(data_subset)
        )

        # Run solo baseline on the subset to establish the solo bar
        solo_cfg = make_config_with_strategy(
            session._config, strategy,
            params_override=session._best_params, solo=True,
        )
        t0 = time.time()
        solo_baseline_metrics = run_backtest(solo_cfg, data_subset)
        solo_baseline_metrics["runtime_s"] = round(time.time() - t0, 1)
        logger.info(
            "Solo baseline (top-50): Sharpe %.4f, %d trades, %.1f s",
            solo_baseline_metrics.get("sharpe", 0),
            solo_baseline_metrics.get("total_trades", 0),
            solo_baseline_metrics["runtime_s"],
        )

    # ── Full combined baseline ────────────────────────────────────────────────
    logger.info("Running combined baseline ...")
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
    logger.info("Combined baseline Sharpe: %.4f", starting_sharpe)

    # ── Sweep plan ────────────────────────────────────────────────────────────
    current_best_params = dict(session._best_params)
    logger.info("Building sweep plan ...")
    plan = build_sweep_plan(strategy, market, current_best_params)
    logger.info("Sweep plan: %d experiments queued.", len(plan))

    # ── Vectorised presort (reorder plan by signal quality) ───────────────
    plan = _vectorised_presort(
        strategy, plan, session._data, current_best_params,
    )

    # ── Counters ──────────────────────────────────────────────────────────────
    screened = 0       # experiments that ran solo screen (fast_screen only)
    promoted = 0       # passed solo screen → promoted to combined verify
    kept = 0           # passed both stages (or combined-only when no fast_screen)
    skipped = 0        # brain history skip
    solo_pass_combined_fail = 0
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
            skipped += 1
            continue

        # Build params: apply candidate to current best (handles nested keys)
        try:
            exp_params = set_nested_param(
                session._best_params, dotted_key, candidate_value
            )
        except Exception as exc:
            logger.warning("Could not build params for %s: %s", display_name, exc)
            continue

        # ── Stage 1: Solo screen (if fast_screen) ────────────────────────────
        if fast_screen:
            logger.info(
                "[%d/%d] SCREEN (solo top-50)  %s",
                idx + 1, len(plan), display_name,
            )
            try:
                solo_metrics, solo_verdict = _run_solo_screen(
                    strategy, session._config, data_subset,
                    solo_baseline_metrics, exp_params, display_name,
                )
            except Exception as exc:
                logger.error("Solo screen CRASHED: %s", exc)
                traceback.print_exc()
                consecutive_crashes += 1
                if consecutive_crashes >= MAX_CONSECUTIVE_CRASHES:
                    logger.error(
                        "%d consecutive crashes — aborting session.",
                        consecutive_crashes,
                    )
                    break
                continue

            screened += 1
            consecutive_crashes = 0

            solo_sharpe = solo_metrics.get("sharpe", 0) or 0
            solo_decision = solo_verdict["decision"]

            if solo_decision == "discard":
                # Failed solo screen — no need for expensive combined backtest
                logger.info(
                    "  → SOLO DISCARD  Sharpe %.4f (%+.4f)  %s  [%.1f s]",
                    solo_sharpe,
                    solo_verdict["delta_sharpe"],
                    solo_verdict["rationale"],
                    solo_metrics.get("runtime_s", 0),
                )
                # Log solo discard to TSV
                _append_result(
                    strategy, solo_metrics,
                    f"{dotted_key}={candidate_value}",
                    "discard_solo",
                    f"[solo screen] {display_name}",
                )
                continue

            # Passed solo screen — promote to combined verification
            promoted += 1
            logger.info(
                "  → SOLO PASS  Sharpe %.4f (%+.4f)  [%.1f s] → promoting to combined verify",
                solo_sharpe,
                solo_verdict["delta_sharpe"],
                solo_metrics.get("runtime_s", 0),
            )

        else:
            # No fast screen — go straight to combined
            logger.info(
                "[%d/%d] RUNNING (combined)  %s",
                idx + 1, len(plan), display_name,
            )

        # ── Stage 2: Combined verify (full universe) ─────────────────────────
        if time.time() >= deadline:
            logger.info(
                "Time budget exhausted before combined verify — ending cleanly.",
            )
            break

        try:
            result = session.experiment(exp_params, description=display_name)
        except EvaluationLockViolation as exc:
            logger.error("EVALUATION LOCK VIOLATED — aborting session: %s", exc)
            runtime_s = time.time() - session_start
            _summarise_and_notify(
                strategy, market, screened, promoted, kept, skipped,
                solo_pass_combined_fail, starting_sharpe, current_sharpe,
                runtime_s, notify, fast_screen,
                status="lock_violated",
            )
            return {
                "status": "lock_violated",
                "error": str(exc),
                "changed_files": exc.changed,
                "screened": screened,
                "promoted": promoted,
                "kept": kept,
                "skipped": skipped,
            }
        except Exception as exc:
            logger.error("Combined backtest CRASHED: %s", exc)
            traceback.print_exc()
            consecutive_crashes += 1
            if consecutive_crashes >= MAX_CONSECUTIVE_CRASHES:
                logger.error(
                    "%d consecutive crashes — aborting session.",
                    consecutive_crashes,
                )
                break
            continue

        consecutive_crashes = 0
        recommendation = result.get("recommendation", "discard")
        combined_sharpe = result.get("metrics", {}).get("sharpe", 0.0) or 0.0
        rationale = result.get("rationale", "")

        if recommendation == "keep":
            session.keep()
            kept += 1
            current_sharpe = combined_sharpe
            logger.info(
                "  → COMBINED KEEP  Sharpe %.4f (%+.4f)  %s",
                combined_sharpe,
                result.get("delta", {}).get("sharpe", 0.0),
                rationale,
            )
            # Regenerate brain/strategies/{strategy}.md with new best params
            try:
                from research.brain.writer import update_strategy
                keep_metrics = result.get("metrics", {})
                update_strategy(
                    strategy, keep_metrics, exp_params,
                    description=(
                        f"autoresearch_runner keep: {dotted_key}={candidate_value}"
                    ),
                )
            except Exception as _bexc:
                logger.warning("brain update_strategy failed (non-fatal): %s", _bexc)
            # Update current best so subsequent experiments build on it
            current_best_params = dict(session._best_params)

            # Refresh solo baseline when params improve (fast_screen only)
            if fast_screen:
                from scripts.strategy_evaluator import (
                    make_config_with_strategy,
                    run_backtest,
                )
                solo_cfg = make_config_with_strategy(
                    session._config, strategy,
                    params_override=session._best_params, solo=True,
                )
                solo_baseline_metrics = run_backtest(solo_cfg, data_subset)
                logger.info(
                    "  → Solo baseline refreshed: Sharpe %.4f",
                    solo_baseline_metrics.get("sharpe", 0),
                )
        else:
            session.discard()
            if fast_screen:
                # Passed solo but failed combined — log distinctly
                solo_pass_combined_fail += 1
                logger.info(
                    "  → SOLO PASS / COMBINED DISCARD  Sharpe %.4f  %s",
                    combined_sharpe, rationale,
                )
            else:
                logger.info(
                    "  → DISCARD  Sharpe %.4f  %s",
                    combined_sharpe, rationale,
                )

    # ── Final summary ─────────────────────────────────────────────────────────
    runtime_s = time.time() - session_start
    summary = _summarise_and_notify(
        strategy, market, screened, promoted, kept, skipped,
        solo_pass_combined_fail, starting_sharpe, current_sharpe,
        runtime_s, notify, fast_screen,
    )

    # ── Auto-promotion sweep ──────────────────────────────────────────────────
    if auto_promote_enabled:
        promo_outcome = _promote_session_result(
            strategy=strategy,
            market=market,
            universe=universe,
            kept=kept,
            starting_sharpe=starting_sharpe,
            final_sharpe=current_sharpe,
        )
        if promo_outcome is not None:
            summary["promotion"] = promo_outcome
            logger.info("[promo] outcome: %s", promo_outcome.get("reason", "no reason"))
    else:
        logger.info("[promo] disabled by --no-auto-promote")

    return summary


# ─── Banner / Summary ────────────────────────────────────────────────────────


def _print_banner(
    strategy: str,
    market: str,
    hours: float,
    snapshot_id: Optional[str],
    fast_screen: bool = True,
) -> None:
    """Print a start-of-session banner to stdout."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "fast-screen (solo top-50 → combined)" if fast_screen else "full combined"
    print(
        f"\n{'='*65}\n"
        f"  Atlas AutoResearch — Headless Parameter Sweep\n"
        f"{'='*65}\n"
        f"  Strategy : {strategy}\n"
        f"  Market   : {market}\n"
        f"  Budget   : {hours:.1f} h\n"
        f"  Mode     : {mode}\n"
        f"  Snapshot : {snapshot_id or 'auto-discover'}\n"
        f"  Started  : {ts}\n"
        f"{'='*65}\n"
    )


def _print_summary(
    strategy: str,
    market: str,
    screened: int,
    promoted: int,
    kept: int,
    skipped: int,
    solo_pass_combined_fail: int,
    starting_sharpe: float,
    final_sharpe: float,
    runtime_s: float,
    fast_screen: bool = True,
    status: str = "complete",
) -> None:
    """Print an end-of-session summary to stdout."""
    delta = final_sharpe - starting_sharpe
    mins = runtime_s / 60.0

    lines = [
        f"\n{'='*65}",
        f"  AutoResearch Session Summary — {strategy} / {market}",
        f"{'='*65}",
        f"  Status         : {status}",
    ]
    if fast_screen:
        lines.extend([
            f"  Screened (solo): {screened}",
            f"  Promoted (→comb): {promoted}",
            f"  Kept (final)   : {kept}",
            f"  Solo✓ / Comb✗  : {solo_pass_combined_fail}",
            f"  Skipped (brain): {skipped}",
        ])
    else:
        total_run = promoted + screened  # in no-fast-screen mode screened=0
        lines.extend([
            f"  Experiments    : {total_run} run, {kept} kept, {skipped} skipped",
        ])
    lines.extend([
        f"  Starting Sharpe: {starting_sharpe:.4f}",
        f"  Final Sharpe   : {final_sharpe:.4f}  ({delta:+.4f})",
        f"  Runtime        : {mins:.1f} min",
        f"{'='*65}",
    ])
    print("\n".join(lines))


def _summarise_and_notify(
    strategy: str,
    market: str,
    screened: int,
    promoted: int,
    kept: int,
    skipped: int,
    solo_pass_combined_fail: int,
    starting_sharpe: float,
    final_sharpe: float,
    runtime_s: float,
    notify: bool,
    fast_screen: bool = True,
    status: str = "complete",
) -> dict:
    """Print summary, optionally send Telegram, and return summary dict."""
    _print_summary(
        strategy, market, screened, promoted, kept, skipped,
        solo_pass_combined_fail, starting_sharpe, final_sharpe,
        runtime_s, fast_screen, status,
    )

    summary = {
        "status": status,
        "strategy": strategy,
        "market": market,
        "screened": screened,
        "promoted": promoted,
        "kept": kept,
        "skipped": skipped,
        "solo_pass_combined_fail": solo_pass_combined_fail,
        "starting_sharpe": round(starting_sharpe, 4),
        "final_sharpe": round(final_sharpe, 4),
        "delta_sharpe": round(final_sharpe - starting_sharpe, 4),
        "runtime_s": round(runtime_s, 1),
        "fast_screen": fast_screen,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if notify:
        delta = final_sharpe - starting_sharpe
        mins = runtime_s / 60.0
        if fast_screen:
            detail = (
                f"Screened: {screened} | Promoted: {promoted} | "
                f"Kept: {kept} | Solo✓Comb✗: {solo_pass_combined_fail} | "
                f"Skipped: {skipped}"
            )
        else:
            total_run = promoted + screened
            detail = f"Run: {total_run} | Kept: {kept} | Skipped: {skipped}"
        msg = (
            f"<b>AutoResearch complete — {strategy} / {market}</b>\n"
            f"Status: {status}\n"
            f"{detail}\n"
            f"Sharpe: {starting_sharpe:.4f} → {final_sharpe:.4f} "
            f"({delta:+.4f})\n"
            f"Runtime: {mins:.1f} min"
        )
        _try_send_telegram(msg)

    return summary



# ─── Promotion Helper ────────────────────────────────────────────────────────


def _promote_session_result(
    strategy: str,
    market: str,
    universe: str,
    kept: int,
    starting_sharpe: Optional[float],
    final_sharpe: Optional[float],
) -> Optional[dict]:
    """Attempt to promote improved parameters from a completed session.

    Called at the end of :func:`run_session` when *kept* > 0 and
    ``auto_promote_enabled`` is ``True``.  Applies cheap client-side guards
    before calling the heavyweight :func:`research.promoter.auto_promote`
    (which runs the 4-gate OOS validation pipeline).

    Args:
        strategy:        Strategy name.
        market:          Market ID (e.g. ``'sp500'``).
        universe:        Universe name (e.g. ``'commodity_etfs'``).
        kept:            Number of kept experiments in this session.
        starting_sharpe: Sharpe at session start (combined baseline).
        final_sharpe:    Sharpe after best improvement (combined).

    Returns:
        Outcome dict with ``promoted`` (bool) and ``reason`` (str),
        or ``None`` if the session is unconditionally skipped (``kept == 0``).
    """
    # ── Guard: no improvements to promote ─────────────────────────────────────
    if kept <= 0:
        return None

    # ── Guard: missing Sharpe values ──────────────────────────────────────────
    if final_sharpe is None or starting_sharpe is None:
        reason = (
            f"missing sharpe values "
            f"(starting={starting_sharpe}, final={final_sharpe})"
        )
        logger.info("[promo] refusing for %s: %s", strategy, reason)
        return {"promoted": False, "reason": reason}

    # ── Guard: negative or zero final Sharpe ─────────────────────────────────
    if final_sharpe <= 0:
        reason = f"negative final sharpe ({final_sharpe:.4f})"
        logger.info("[promo] refusing for %s: %s", strategy, reason)
        return {"promoted": False, "reason": reason}

    # ── Client-side delta gate (matches nightly sweep) ────────────────────────
    delta = final_sharpe - (starting_sharpe or 0.0)
    if delta < 0.05:
        reason = f"delta_sharpe={delta:+.4f} < 0.05 client gate"
        logger.info("[promo] %s: %s — skipping", strategy, reason)
        return {"promoted": False, "reason": reason}

    # ── Read best params from SQLite (canonical), fall back to JSON ───────────
    best_params: dict = {}
    best_sharpe: float = final_sharpe

    try:
        import json as _json_pkg
        from db.atlas_db import get_research_best

        rows = get_research_best(strategy, universe)
        row = rows[0] if rows else None
        if row:
            raw_params = row.get("params", {})
            best_params = (
                _json_pkg.loads(raw_params)
                if isinstance(raw_params, str)
                else (raw_params or {})
            )
            if row.get("sharpe") is not None:
                best_sharpe = float(row["sharpe"])
    except Exception as exc:
        logger.warning(
            "[promo] SQLite read failed for %s/%s: %s — falling back to JSON",
            strategy, universe, exc,
        )

    if not best_params:
        # JSON fallback (legacy path)
        import json as _jf
        best_dir = ATLAS_ROOT / "research" / "best"
        candidate_file = best_dir / f"{strategy}_{universe}.json"
        if not candidate_file.exists():
            candidate_file = best_dir / f"{strategy}.json"
        if candidate_file.exists():
            try:
                best_data = _jf.loads(candidate_file.read_text())
                best_params = best_data.get("params", {}) or {}
                meta = best_data.get("metrics", {}) or {}
                if meta.get("sharpe"):
                    best_sharpe = float(meta["sharpe"])
            except Exception as exc:
                logger.warning(
                    "[promo] JSON fallback read failed for %s: %s", strategy, exc,
                )

    # ── Guard: no params to promote ───────────────────────────────────────────
    if not best_params:
        reason = f"no best params found for {strategy}/{universe}"
        logger.info("[promo] refusing for %s: %s", strategy, reason)
        return {"promoted": False, "reason": reason}

    # ── Freshness guard — reject stale or time-regressing promotes ──────────────
    try:
        from research.freshness import check_freshness as _cf
        import json as _json_fw
        from db.atlas_db import get_research_best as _grb
        _rows = _grb(strategy, universe)
        _row_ts = None
        if _rows:
            _raw_ts = _rows[0].get("updated_at")
            if _raw_ts:
                try:
                    from datetime import datetime, timezone as _tz
                    _row_ts = datetime.fromisoformat(_raw_ts.replace("Z", "+00:00"))
                    if _row_ts.tzinfo is None:
                        _row_ts = _row_ts.replace(tzinfo=_tz.utc)
                except (ValueError, TypeError):
                    pass
        _allow, _reason = _cf(strategy, universe, candidate_timestamp=_row_ts)
        if not _allow:
            logger.warning("[promo] freshness guard blocked %s: %s", strategy, _reason)
            return {"promoted": False, "reason": _reason, "strategy": strategy}
    except Exception as _fg_exc:
        logger.debug("[promo] freshness guard failed (non-fatal): %s", _fg_exc)

    improvements = [
        f"autoresearch_runner sweep: Sharpe {starting_sharpe:.4f} -> {final_sharpe:.4f}"
    ]

    try:
        from research.promoter import auto_promote

        outcome = auto_promote(
            strategy=strategy,
            improved_params=best_params,
            initial_sharpe=float(starting_sharpe),
            final_sharpe=float(best_sharpe),
            improvements=improvements,
            market=market,
        )
        outcome["strategy"] = strategy
        logger.info("[promo] %s: %s", strategy, outcome.get("reason", "no reason"))
        return outcome
    except Exception as exc:
        logger.error("[promo] auto_promote failed for %s: %s", strategy, exc)
        return {
            "promoted": False,
            "reason": f"exception: {exc}",
            "strategy": strategy,
        }


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
            "\n"
            "  # Full-rigour mode (no solo screen, every experiment is combined):\n"
            "  python3 research/autoresearch_runner.py \\\n"
            "      --strategy mean_reversion --hours 8 --no-fast-screen\n"
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
        help="Market ID used for config and snapshot discovery (default: sp500).",
    )
    parser.add_argument(
        "--universe",
        default="sp500",
        help=(
            "Universe name for data loading.  When set to a value other than "
            "'sp500', data is loaded via build_from_definition(universe) instead "
            "of the snapshot pipeline (default: sp500)."
        ),
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
    # --fast-screen (default) / --no-fast-screen
    parser.add_argument(
        "--fast-screen",
        action="store_true",
        default=True,
        dest="fast_screen",
        help="Two-stage gating: solo top-50 screen → combined verify (default).",
    )
    parser.add_argument(
        "--no-fast-screen",
        action="store_false",
        dest="fast_screen",
        help="Disable fast screen — every experiment runs full combined backtest.",
    )
    parser.add_argument(
        "--no-auto-promote",
        action="store_false",
        dest="auto_promote_enabled",
        default=True,
        help="Disable automatic promotion sweep at end of session (safety flag).",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    result = run_session(
        strategy=args.strategy,
        market=args.market,
        universe=args.universe,
        hours=args.hours,
        notify=args.notify,
        snapshot_id=args.snapshot,
        fast_screen=args.fast_screen,
        auto_promote_enabled=args.auto_promote_enabled,
    )
    sys.exit(0 if result.get("status") in ("complete", None) else 1)
