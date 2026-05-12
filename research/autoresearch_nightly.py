#!/usr/bin/env python3
"""Nightly autoresearch orchestrator — runs all strategies in parallel.

Spawns one ``autoresearch_runner.py`` subprocess per strategy.  Workers share
the same frozen data snapshot but maintain separate backtests, TSV logs, and
brain param files.

Resource budget: each worker uses ~1-2 cores during backtest and ~2 GB RAM.
With ``--workers 5`` on an 8-core VPS, leaves 3 cores for system + cron.

Usage::

    # Parallel sweep of all 5 strategies for 8 hours:
    python3 research/autoresearch_nightly.py --hours 8 --workers 5 --notify

    # Only 2 strategies:
    python3 research/autoresearch_nightly.py --hours 4 --workers 2 \\
        --strategies mean_reversion,trend_following

Concurrency safety:
- Each worker writes to its own ``research/results/{strategy}.tsv``
- Each worker writes to its own ``research/best/{strategy}.json``
- Brain param files are per-param, workers rarely overlap
- ``research/journal.json`` uses ``fcntl.LOCK_EX`` (via ``_locked_append``)
- Evaluation lock files are per-session (unique session IDs)
- The data snapshot is read-only
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

ATLAS_ROOT = Path(__file__).resolve().parent.parent
if str(ATLAS_ROOT) not in sys.path:
    sys.path.insert(0, str(ATLAS_ROOT))

from research.db import log_session, end_session
from research.snapshots import find_latest_snapshot as _find_latest_snapshot

# Strategies covered by the nightly autoresearch sweep.
# Includes everything in scripts/strategy_evaluator.STRATEGY_REGISTRY that has
# active live use OR is enabled in any config/active/*.json, OR has a
# research_best row. (bb_squeeze: research_best sp500 sharpe=0.49;
# mtf_momentum: disabled everywhere + no research_best — intentionally excluded)
DEFAULT_STRATEGIES = [
    "mean_reversion",
    "trend_following",
    "opening_gap",
    "momentum_breakout",
    "sector_rotation",
    "connors_rsi2",
    "short_term_mr",
    "bb_squeeze",
]

RUNNER_SCRIPT = ATLAS_ROOT / "research" / "autoresearch_runner.py"
LOGS_DIR = ATLAS_ROOT / "logs"
RESULTS_DIR = ATLAS_ROOT / "research" / "results"

import logging
_logger = logging.getLogger(__name__)

# Per-universe operator-set FLOORS (lower bounds): alert if the sweep produces
# fewer rows than this.  Calibrated 2026-05-12 against last-11-days production
# data.  Threshold = max(operator_floor, enabled_strategies * MIN_ROWS_PER_STRATEGY)
# so neither floor can weaken the other.
# errors table ids 19,20,21,27-29 db → gold/commodity false-positives fixed by
# lowering calibrated floors to match actual narrow-universe output.
MIN_ROWS_PER_UNIVERSE = {
    "sp500": 50,          # typical 100-330 rows, 2 enabled — preserve alert sensitivity
    "commodity_etfs": 5,  # typical 6-30 rows, 3 enabled (recent runs low)
    "sector_etfs": 20,    # typical 13-44 rows, 2 enabled
    "gold_etfs": 3,       # typical 1-8 rows, 1 enabled strategy
    "treasury_etfs": 10,  # no enabled strategies — conservative sentinel
    "defensive_etfs": 10,
    "crypto": 10,
    "asx": 10,
}
DEFAULT_MIN_ROWS = 10
MIN_ROWS_PER_STRATEGY = 3  # Floor: 3 rows per enabled strategy per sweep



# ─── TSV Parsing ─────────────────────────────────────────────────────────────


def _parse_session_results(
    strategy: str,
    session_start_ts: float,
) -> Dict:
    """Parse a strategy's TSV for experiments written after *session_start_ts*.

    Returns dict with counts and Sharpe values.
    """
    tsv_path = RESULTS_DIR / f"{strategy}.tsv"
    result = {
        "strategy": strategy,
        "screened": 0,
        "promoted": 0,
        "kept": 0,
        "starting_sharpe": 0.0,
        "final_sharpe": 0.0,
    }
    if not tsv_path.exists():
        return result

    # Read lines written during this session (after session_start_ts)
    cutoff = datetime.fromtimestamp(session_start_ts, tz=timezone.utc)
    lines = tsv_path.read_text().strip().split("\n")
    if len(lines) <= 1:
        return result

    session_lines = []
    for line in lines[1:]:  # skip header
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        try:
            row_ts = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            if row_ts.tzinfo is None:
                row_ts = row_ts.replace(tzinfo=timezone.utc)
            if row_ts >= cutoff:
                session_lines.append(parts)
        except (ValueError, IndexError):
            continue

    if not session_lines:
        return result

    # Count by status column (index 7)
    for parts in session_lines:
        status = parts[7].strip() if len(parts) > 7 else ""
        if status == "discard_solo":
            result["screened"] += 1
        elif status == "discard":
            result["screened"] += 1
            result["promoted"] += 1
        elif status == "keep":
            if parts[8].strip() != "baseline":
                result["screened"] += 1
                result["promoted"] += 1
                result["kept"] += 1

    # Sharpe: baseline is first 'keep' with description 'baseline'
    # Final Sharpe: last 'keep' that isn't baseline, or baseline if no keeps
    for parts in session_lines:
        if len(parts) > 7 and parts[7].strip() == "keep" and parts[8].strip() == "baseline":
            try:
                result["starting_sharpe"] = float(parts[1])
                result["final_sharpe"] = float(parts[1])
            except ValueError:
                pass
            break

    # Find the last kept experiment's Sharpe (if any)
    for parts in reversed(session_lines):
        if len(parts) > 7 and parts[7].strip() == "keep" and parts[8].strip() != "baseline":
            try:
                result["final_sharpe"] = float(parts[1])
            except ValueError:
                pass
            break

    return result


def _count_rows_added(universe: str, session_start_ts: float) -> int:
    """Count rows inserted into research_experiments for *universe* since *session_start_ts*.

    Used by silent-failure detection. Returns 0 on any DB error (caller treats
    that as silent failure, which is the correct conservative behavior).

    NOTE: queries the ``universe`` column (the schema column is named ``universe``,
    not ``market`` — log_experiment maps its ``market`` param to this column).
    """
    try:
        from db.atlas_db import get_db
        cutoff = datetime.fromtimestamp(session_start_ts, tz=timezone.utc).isoformat()
        with get_db() as db:
            cur = db.execute(
                "SELECT COUNT(*) FROM research_experiments "
                "WHERE universe = ? AND created_at > ?",
                (universe, cutoff),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        _logger.error("_count_rows_added failed: %s", exc)
        return 0


def _resolve_min_rows(universe: str) -> int:
    """Dynamic silent-failure threshold combining operator floor + per-strategy floor.

    Threshold = max(operator_floor, enabled_strategies * MIN_ROWS_PER_STRATEGY)

    - Operator floor: from MIN_ROWS_PER_UNIVERSE dict — hand-calibrated per universe
      based on typical sweep output. ALWAYS respected as a lower bound — we want
      to alert if sp500 drops from 100+ to 30 rows, even with 2 strategies enabled.
    - Dynamic floor: enabled_strategies * 3 — safety net for universes not in the
      operator dict (so we still alert if a NEW universe is added but forgotten).

    Returns the LARGER of the two so neither can weaken the other.

    Fail-safe: on any error (missing config, corrupt JSON), falls back to the
    operator floor or DEFAULT_MIN_ROWS — better to surface a false-positive alert
    than to silently miss a real silent failure.
    """
    try:
        from pathlib import Path
        import json as _json
        cfg_path = ATLAS_ROOT / "config" / "active" / f"{universe}.json"
        if not cfg_path.exists():
            return MIN_ROWS_PER_UNIVERSE.get(universe, DEFAULT_MIN_ROWS)
        with open(cfg_path) as f:
            cfg = _json.load(f)
        enabled = sum(
            1 for s in cfg.get("strategies", {}).values()
            if s.get("enabled", False)
        )
        if enabled == 0:
            # Universe has no enabled strategies; sweep should not run at all.
            # Use the static ceiling so we still alert if rows ARE produced.
            return MIN_ROWS_PER_UNIVERSE.get(universe, DEFAULT_MIN_ROWS)
        dynamic_floor = max(3, enabled * MIN_ROWS_PER_STRATEGY)
        operator_floor = MIN_ROWS_PER_UNIVERSE.get(universe, DEFAULT_MIN_ROWS)
        return max(operator_floor, dynamic_floor)
    except Exception as exc:
        _logger.warning(
            "_resolve_min_rows(%s) failed: %s — falling back to static threshold",
            universe, exc,
        )
        return MIN_ROWS_PER_UNIVERSE.get(universe, DEFAULT_MIN_ROWS)


# ─── Worker Management ───────────────────────────────────────────────────────


def _spawn_workers(
    strategies: List[str],
    market: str,
    hours: float,
    snapshot_id: Optional[str],
    max_workers: int,
    universe: str = "sp500",
) -> List[Dict]:
    """Spawn autoresearch_runner subprocesses, respecting *max_workers* limit.

    Returns list of worker dicts with keys: strategy, proc, log_path, start_time.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime("%Y%m%d")

    workers: List[Dict] = []
    pending = list(strategies)
    active: List[Dict] = []

    def _launch(strat: str) -> Dict:
        log_path = LOGS_DIR / f"autoresearch_{strat}_{date_str}.log"
        log_fh = open(log_path, "w")
        cmd = [
            sys.executable,
            str(RUNNER_SCRIPT),
            "--strategy", strat,
            "--market", market,
            "--hours", str(hours),
            "--fast-screen",
            "--universe", universe,
        ]
        if snapshot_id:
            cmd.extend(["--snapshot", snapshot_id])
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(ATLAS_ROOT),
        )
        w = {
            "strategy": strat,
            "proc": proc,
            "log_fh": log_fh,
            "log_path": str(log_path),
            "start_time": time.time(),
            "exit_code": None,
        }
        print(f"  ▶ Spawned {strat} (PID {proc.pid}) → {log_path}")
        return w

    # Initial launch up to max_workers
    while pending and len(active) < max_workers:
        strat = pending.pop(0)
        w = _launch(strat)
        active.append(w)
        workers.append(w)

    # Monitor loop: poll every 60s, launch pending when slots open
    while active:
        time.sleep(60)

        still_active = []
        for w in active:
            rc = w["proc"].poll()
            if rc is not None:
                w["exit_code"] = rc
                w["log_fh"].close()
                elapsed = (time.time() - w["start_time"]) / 60
                status = "✓" if rc == 0 else f"✗ (exit {rc})"
                print(f"  {status} {w['strategy']} finished in {elapsed:.1f} min")
            else:
                still_active.append(w)

        active = still_active

        # Fill slots with pending strategies
        while pending and len(active) < max_workers:
            strat = pending.pop(0)
            w = _launch(strat)
            active.append(w)
            workers.append(w)

        # Status line
        running_names = [w["strategy"] for w in active]
        if running_names:
            print(f"  … {len(active)} running: {', '.join(running_names)}")

    return workers


# ─── Telegram ────────────────────────────────────────────────────────────────


def _send_summary_telegram(
    results: List[Dict],
    runtime_s: float,
    num_workers: int,
) -> None:
    """Send a combined Telegram summary for all strategies."""
    try:
        from utils.telegram import notify
    except ImportError:
        print("Telegram not configured (utils.telegram not found).")
        return

    mins = runtime_s / 60
    total_screened = sum(r["screened"] for r in results)
    total_promoted = sum(r["promoted"] for r in results)
    total_kept = sum(r["kept"] for r in results)
    failures = [r for r in results if r.get("exit_code", 0) != 0]

    lines = [
        "<b>🔬 Nightly Autoresearch Complete</b>",
        f"Runtime: {mins:.0f} min | {len(results)} strategies | {num_workers} workers",
        "",
    ]
    for r in results:
        s = r["strategy"]
        sc = r["screened"]
        pr = r["promoted"]
        kp = r["kept"]
        s_sharpe = r["starting_sharpe"]
        f_sharpe = r["final_sharpe"]
        if r.get("exit_code", 0) != 0:
            lines.append(f"  {s}: ❌ FAILED (exit {r['exit_code']})")
        elif kp > 0:
            lines.append(
                f"  {s}: {sc} screened → {pr} promoted → {kp} kept "
                f"(Sharpe {s_sharpe:.3f} → {f_sharpe:.3f})"
            )
        else:
            lines.append(
                f"  {s}: {sc} screened → {pr} promoted → 0 kept "
                f"(Sharpe {s_sharpe:.3f})"
            )

    lines.append("")
    lines.append(f"Total: {total_screened} screened, {total_promoted} promoted, {total_kept} kept")
    if failures:
        lines.append(f"⚠️ {len(failures)} worker(s) failed")

    try:
        notify("\n".join(lines), category="autoresearch")
    except Exception as e:
        print(f"Telegram send failed (non-fatal): {e}")




# ─── Strategy Filter ─────────────────────────────────────────────────────────


def _filter_enabled_strategies(strategies: List[str], market_or_universe: str) -> List[str]:
    """Drop strategies whose `enabled` flag is False in the active config.

    Args:
        strategies: List of strategy names to filter.
        market_or_universe: Config key to read enabled flags from
            (use universe for non-sp500 sweeps so gold_etfs/sector_etfs
            configs are read instead of sp500).

    Returns the filtered list; logs any strategies that were skipped.
    """
    try:
        from utils.config import get_active_config
        cfg = get_active_config(market_or_universe)
    except Exception as exc:
        print(f"[filter] Could not load active config for {market_or_universe}: {exc} — running all strategies")
        return strategies

    strat_cfg = cfg.get("strategies", {}) or {}
    enabled = []
    for s in strategies:
        entry = strat_cfg.get(s, {})
        # Missing entry → assume enabled (don't silently drop strategies
        # not yet configured for this universe)
        is_enabled = entry.get("enabled", True) if isinstance(entry, dict) else True
        if is_enabled:
            enabled.append(s)
        else:
            print(f"[filter] Skipping {s} — disabled in {market_or_universe} active config")
    return enabled


# ─── Promotion Sweep ──────────────────────────────────────────────────────────


def _run_promotion_sweep(results: List[Dict], market: str, universe: str) -> List[Dict]:
    """Call auto_promote() for each strategy that produced kept experiments.

    Reads per-strategy best params from research/best/{strategy}.json (or
    research/best/{strategy}_{universe}.json for non-sp500). If the best
    beats the current active config's Sharpe, fires auto_promote which
    runs all 4 gates and queues a Telegram APPROVE/REJECT request.

    Returns a list of promotion outcome dicts (one per strategy processed).
    """
    from research.promoter import auto_promote
    from utils.config import get_active_config
    import json as _json

    outcomes = []
    best_dir = ATLAS_ROOT / "research" / "best"

    # Ensure config directories exist
    (ATLAS_ROOT / "config").mkdir(parents=True, exist_ok=True)
    (ATLAS_ROOT / "config" / "candidates").mkdir(parents=True, exist_ok=True)

    try:
        active_cfg = get_active_config(market)
    except Exception as exc:
        print(f"[promo] Could not load active config for {market}: {exc} — skipping promotion sweep")
        return outcomes

    for r in results:
        strategy = r.get("strategy")
        kept = r.get("kept", 0)
        if kept <= 0:
            continue
        if r.get("exit_code", 0) != 0:
            continue  # don't promote from failed workers

        # Read best params from SQLite (canonical), fall back to JSON file.
        best_params = {}
        best_metrics = {}
        try:
            from db.atlas_db import get_research_best
            import json as _json_pkg
            rows = get_research_best(strategy, universe)
            row = rows[0] if rows else None
            if row:
                raw_params = row.get("params", {})
                best_params = (
                    _json_pkg.loads(raw_params)
                    if isinstance(raw_params, str)
                    else (raw_params or {})
                )
                best_metrics = {
                    "sharpe": row.get("sharpe"),
                    "total_trades": row.get("trades"),
                    "max_drawdown_pct": row.get("max_dd_pct"),
                }
        except Exception as exc:
            print(f"[promo] SQLite read failed for {strategy}/{universe}: {exc} — falling back to JSON")

        if not best_params:
            # JSON fallback (legacy path)
            candidate_file = best_dir / f"{strategy}_{universe}.json"
            if not candidate_file.exists():
                candidate_file = best_dir / f"{strategy}.json"
            if not candidate_file.exists():
                print(f"[promo] No best data for {strategy} ({universe}) — skipping")
                continue
            try:
                best_data = _json.loads(candidate_file.read_text())
                best_params = best_data.get("params", {}) or {}
                best_metrics = best_data.get("metrics", {}) or {}
            except Exception as exc:
                print(f"[promo] Failed to read {candidate_file}: {exc}")
                continue
        best_sharpe = best_metrics.get("sharpe")
        if best_sharpe is None:
            # Fall back to r['final_sharpe'] if set
            best_sharpe = r.get("final_sharpe", 0.0) or 0.0

        # Baseline = pre-sweep Sharpe captured by the runner
        initial_sharpe = r.get("starting_sharpe", 0.0) or 0.0

        # Gate against portfolio-contaminated metrics
        try:
            from research.integrity import check_solo
            _is_solo, _solo_frac, _note = check_solo(strategy, universe)
            if _is_solo is False:  # explicit false, not None
                _msg = (
                    f"Refusing to promote {strategy}/{universe} on contaminated portfolio metrics "
                    f"(solo_fraction={_solo_frac:.2%}). Run a true solo backtest first. {_note}"
                )
                print(f"[promo] BLOCKED: {_msg}")
                _logger.warning(_msg)
                outcomes.append({
                    "strategy": strategy,
                    "promoted": False,
                    "reason": f"contaminated_metrics: solo_fraction={_solo_frac:.2%}",
                })
                continue
        except ImportError:
            pass  # integrity module not yet available — skip gate

        # Gate delta-Sharpe client-side so we don't spam promoter
        # with tiny improvements (promoter has its own gates but
        # this saves an OOS validation subprocess per insignificant
        # improvement).
        delta = (best_sharpe or 0.0) - initial_sharpe
        if delta < 0.05:
            print(f"[promo] {strategy}: delta_sharpe={delta:+.4f} below client gate 0.05 — skipping")
            continue

        improvements = [f"nightly sweep: Sharpe {initial_sharpe:.4f} -> {best_sharpe:.4f}"]

        try:
            outcome = auto_promote(
                strategy=strategy,
                improved_params=best_params,
                initial_sharpe=float(initial_sharpe),
                final_sharpe=float(best_sharpe),
                improvements=improvements,
                market=market,
            )
            outcome["strategy"] = strategy
            outcomes.append(outcome)
            print(f"[promo] {strategy}: {outcome.get('reason', 'no reason')}")
        except Exception as exc:
            print(f"[promo] auto_promote failed for {strategy}: {exc}")
            outcomes.append({"strategy": strategy, "promoted": False, "reason": f"exception: {exc}"})

    return outcomes


# ─── Main ────────────────────────────────────────────────────────────────────


def run_nightly(
    strategies: Optional[List[str]] = None,
    market: str = "sp500",
    hours: float = 8.0,
    workers: int = 5,
    notify: bool = False,
    snapshot_id: Optional[str] = None,
    universe: str = "sp500",
    dry_run_telegram: bool = False,
) -> Dict:
    """Run parallel autoresearch sessions for multiple strategies.

    Args:
        strategies:  List of strategy names.  Defaults to :data:`DEFAULT_STRATEGIES`.
        market:      Market ID (default ``'sp500'``).
        hours:       Time budget per worker in hours.
        workers:     Max concurrent worker processes.
        notify:      Send Telegram summary on completion.
        snapshot_id: Explicit snapshot to use (auto-discovered if ``None``).

    Returns:
        Summary dict with per-strategy results and aggregate counts.
    """
    session_start = time.time()

    _logger.info(
        "RESEARCH_NIGHTLY_START universe=%s market=%s timestamp=%s",
        universe, market, datetime.now(timezone.utc).isoformat(),
    )

    # When sweeping a non-sp500 universe, treat universe as the effective market so
    # downstream config loads (get_active_config) hit the universe's config file.
    if universe != "sp500" and market == "sp500":
        market = universe

    # Defensive: after coercion, market must equal universe for non-sp500 sweeps
    if universe != "sp500":
        assert market == universe, (
            f"market ({market}) must equal universe ({universe}) for non-sp500 sweeps"
        )

    strategies = strategies or list(DEFAULT_STRATEGIES)
    strategies = _filter_enabled_strategies(strategies, universe)

    if not strategies:
        print("[filter] No enabled strategies — nothing to run")
        return {
            "status": "no_strategies",
            "strategies": [],
            "total_screened": 0,
            "total_promoted": 0,
            "total_kept": 0,
            "failures": 0,
            "runtime_s": 0.0,
            "snapshot_id": None,
        }

    session_id = None
    try:
        session_id = log_session(mode="nightly_sweep", strategy=",".join(strategies))

        # Resolve snapshot — only sp500 uses file-based snapshots;
        # other universes load from build_from_definition() inside the runner
        if universe == "sp500":
            if snapshot_id is None:
                snapshot_id = _find_latest_snapshot(market)
        else:
            snapshot_id = None  # non-sp500 universes don't use snapshots
        print(
            f"\n{'='*65}\n"
            f"  Atlas Nightly Autoresearch Orchestrator\n"
            f"{'='*65}\n"
            f"  Strategies : {', '.join(strategies)}\n"
            f"  Market     : {market}\n"
            f"  Universe   : {universe}\n"
            f"  Budget     : {hours:.1f} h per worker\n"
            f"  Workers    : {workers}\n"
            f"  Snapshot   : {snapshot_id or '(none — universe uses build_from_definition)'}\n"
            f"  Started    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
            f"{'='*65}\n"
        )

        # Spawn and monitor workers
        worker_list = _spawn_workers(strategies, market, hours, snapshot_id, workers, universe=universe)

        # Collect results
        runtime_s = time.time() - session_start
        results = []
        for w in worker_list:
            r = _parse_session_results(w["strategy"], session_start)
            r["exit_code"] = w["exit_code"]
            r["log_path"] = w["log_path"]
            results.append(r)

        # Print summary
        total_screened = sum(r["screened"] for r in results)
        total_promoted = sum(r["promoted"] for r in results)
        total_kept = sum(r["kept"] for r in results)
        failures = [r for r in results if r.get("exit_code", 0) != 0]
        mins = runtime_s / 60

        print(
            f"\n{'='*65}\n"
            f"  Nightly Autoresearch Summary\n"
            f"{'='*65}"
        )
        for r in results:
            s = r["strategy"]
            sc = r["screened"]
            pr = r["promoted"]
            kp = r["kept"]
            s_sharpe = r["starting_sharpe"]
            f_sharpe = r["final_sharpe"]
            if r.get("exit_code", 0) != 0:
                print(f"  {s:25s} ❌ FAILED (exit {r['exit_code']})")
            elif kp > 0:
                print(
                    f"  {s:25s} {sc:3d} screened → {pr:2d} promoted → {kp:2d} kept "
                    f"(Sharpe {s_sharpe:.3f} → {f_sharpe:.3f})"
                )
            else:
                print(
                    f"  {s:25s} {sc:3d} screened → {pr:2d} promoted →  0 kept "
                    f"(Sharpe {s_sharpe:.3f})"
                )
        print(
            f"\n  Total: {total_screened} screened, {total_promoted} promoted, {total_kept} kept"
        )
        if failures:
            print(f"  ⚠️  {len(failures)} worker(s) failed")
        print(f"  Runtime: {mins:.1f} min")
        print(f"{'='*65}\n")

        # INIT-1: Promotion sweep — queues Telegram APPROVE/REJECT for strategies
        # that improved beyond threshold. Human gate remains intact — this does
        # NOT auto-write to config/active.
        promotion_outcomes = _run_promotion_sweep(results, market, universe)
        print(f"\n[promo] Promotion sweep outcome: {len(promotion_outcomes)} strategies processed")
        for o in promotion_outcomes:
            print(f"  - {o.get('strategy')}: promoted={o.get('promoted')} pending={o.get('pending')} reason={o.get('reason')}")

        # Telegram
        if notify:
            _send_summary_telegram(results, runtime_s, workers)

        result = {
            "status": "complete",
            "strategies": results,
            "total_screened": total_screened,
            "total_promoted": total_promoted,
            "total_kept": total_kept,
            "failures": len(failures),
            "runtime_s": round(runtime_s, 1),
            "snapshot_id": snapshot_id,
        }

        # ─── Silent-failure detection ────────────────────────────────────────────
        # Verify rows were actually inserted into research_experiments.
        # Catches the Apr 22-30 0-byte-log silent failures.
        rows_added = _count_rows_added(universe, session_start)
        min_rows = _resolve_min_rows(universe)
        silent_failure = rows_added < min_rows

        status_str = "SILENT_FAILURE" if silent_failure else "OK"
        _logger.info(
            "RESEARCH_NIGHTLY_END universe=%s rows_added=%d min_required=%d status=%s",
            universe, rows_added, min_rows, status_str,
        )
        result["rows_added"] = rows_added
        result["silent_failure"] = silent_failure

        if silent_failure:
            msg = (
                f"🚨 Research sweep silent failure: "
                f"universe={universe} rows={rows_added} threshold={min_rows} "
                f"(see {LOGS_DIR}/autoresearch_*_{datetime.now().strftime('%Y%m%d')}.log)"
            )
            _logger.error(msg)
            if dry_run_telegram:
                print(f"[TELEGRAM-DRY-RUN] {msg}")
            else:
                try:
                    from utils.telegram import notify as _tg_notify
                    _tg_notify(msg, category="autoresearch_silent_failure")
                except Exception as exc:
                    _logger.warning("Telegram silent-failure alert failed: %s", exc)
            if session_id is not None:
                try:
                    end_session(session_id, experiments_run=total_screened,
                                experiments_kept=total_kept, status="silent_failure")
                except Exception:
                    pass
            return result

        # Normal completion path
        if session_id is not None:
            end_session(session_id, experiments_run=total_screened,
                        experiments_kept=total_kept, status="completed")
        return result

    except Exception:
        if session_id is not None:
            try:
                end_session(session_id, experiments_run=0, experiments_kept=0, status="failed")
            except Exception:
                pass
        raise


# ─── CLI ─────────────────────────────────────────────────────────────────────


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nightly autoresearch orchestrator — parallel strategy sweeps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  python3 research/autoresearch_nightly.py --hours 8 --workers 5 --notify\n"
            "\n"
            "  # Only specific strategies:\n"
            "  python3 research/autoresearch_nightly.py --hours 4 --workers 2 \\\n"
            "      --strategies mean_reversion,trend_following\n"
        ),
    )
    parser.add_argument(
        "--hours",
        type=float,
        default=8.0,
        help="Time budget per worker in hours (default: 8).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        help="Max concurrent worker processes (default: 5).",
    )
    parser.add_argument(
        "--market",
        default="sp500",
        help="Market ID (default: sp500).",
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help="Comma-separated strategy list (default: top 5 by weight).",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        default=False,
        help="Send Telegram summary on completion.",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="Snapshot ID (auto-discovered if omitted).",
    )
    parser.add_argument(
        "--universe",
        default="sp500",
        help="Universe ID (default: sp500). Non-sp500 universes use build_from_definition() instead of snapshots.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print config and exit without spawning workers.",
    )
    parser.add_argument(
        "--dry-run-telegram",
        action="store_true",
        default=False,
        help=(
            "Replace Telegram silent-failure alerts with stdout prints. "
            "Used by the test harness to verify alert dispatch without spamming."
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    strats = args.strategies.split(",") if args.strategies else None
    if args.dry_run:
        print(
            f"\n{'='*65}\n"
            f"  Dry-run mode — no workers will be spawned\n"
            f"{'='*65}\n"
            f"  Strategies : {', '.join(strats or ['(defaults)'])}\n"
            f"  Market     : {args.market}\n"
            f"  Universe   : {args.universe}\n"
            f"  Budget     : {args.hours:.1f} h per worker\n"
            f"  Workers    : {args.workers}\n"
            f"  Snapshot   : {args.snapshot or '(auto-discover or none for non-sp500)'}\n"
            f"{'='*65}\n"
        )
        sys.exit(0)
    result = run_nightly(
        strategies=strats,
        market=args.market,
        hours=args.hours,
        workers=args.workers,
        notify=args.notify,
        snapshot_id=args.snapshot,
        universe=args.universe,
        dry_run_telegram=args.dry_run_telegram,
    )
    failures = result.get("failures", 0)
    silent_failure = result.get("silent_failure", False)
    if silent_failure:
        sys.exit(2)  # distinct exit code so systemd journal shows failure mode
    elif failures == len(result.get("strategies", [])):
        sys.exit(1)
    else:
        sys.exit(0)
