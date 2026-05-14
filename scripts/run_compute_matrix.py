#!/usr/bin/env python3
"""Compute matrix repopulation driver (#216).

Sweeps every (strategy x universe) combination through
research/autoresearch_nightly.py, rebuilding the research_best table
with fresh in-sample metrics for all universe/strategy pairs.

Each universe sweep delegates to research/autoresearch_nightly.py
(the proven sweep entry point, matching research_window_universe.sh).
Universes run *sequentially* to avoid system overload — each sweep
already spawns multiple internal worker processes.

Progress is checkpointed to data/compute_matrix/progress_{run_id}.json
after each universe so the job can be restarted mid-run.

Usage:
    python3 scripts/run_compute_matrix.py                     # all universes
    python3 scripts/run_compute_matrix.py --universes sp500   # one universe
    python3 scripts/run_compute_matrix.py --universes sp500,commodity_etfs
    python3 scripts/run_compute_matrix.py --dry-run           # plan only
    python3 scripts/run_compute_matrix.py --hours 0.5         # override budget
    python3 scripts/run_compute_matrix.py --workers 2         # override workers
    python3 scripts/run_compute_matrix.py --resume            # skip completed
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT / "data" / "compute_matrix"
NIGHTLY_SCRIPT = PROJECT / "research" / "autoresearch_nightly.py"

# Universe sweep configuration — hours budget and worker count per universe.
# Calibrated to match research_window_universe.sh (production-tested values).
# crypto included but can be excluded via --universes.
UNIVERSE_CONFIG: dict[str, dict] = {
    "sp500":          {"hours": 1.0, "workers": 3},
    "commodity_etfs": {"hours": 0.5, "workers": 2},
    "sector_etfs":    {"hours": 0.25, "workers": 1},
    "gold_etfs":      {"hours": 0.25, "workers": 1},
    "treasury_etfs":  {"hours": 0.25, "workers": 1},
    "defensive_etfs": {"hours": 0.25, "workers": 1},
    "crypto":         {"hours": 0.25, "workers": 1},
}

# Default universe list (excludes crypto — unstable data; add explicitly if needed)
DEFAULT_UNIVERSES = [
    "sp500", "commodity_etfs", "sector_etfs",
    "gold_etfs", "treasury_etfs", "defensive_etfs",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("run_compute_matrix")


# Statuses that indicate a successful run (≥0 kept, or a known graceful no-op)
_SUCCESS_STATUSES = frozenset({"ok", "dry_run", "no_keeps", "benchmark_unavailable", "config_missing"})


def _parse_log_for_outcome(log_path: "Path", returncode: int) -> str:
    """Parse autoresearch_nightly log to determine the sweep outcome.

    Precedence:
      1. ATLAS_NIGHTLY_STATUS sentinel JSON (written by nightly.py #216 fix)
      2. Summary "Total: X screened, Y promoted, Z kept" line
      3. "Config file not found" / missing-config patterns
      4. "Benchmark … data unavailable" patterns
      5. returncode as final fallback

    Returns one of: "ok", "no_keeps", "benchmark_unavailable", "config_missing",
    "error".
    """
    import json as _json
    import re as _re

    try:
        text = log_path.read_text(errors="replace")
    except Exception:
        return "error" if returncode != 0 else "ok"

    # ── 1. Sentinel JSON (most authoritative) ──────────────────────────────
    for line in text.splitlines():
        if line.startswith("ATLAS_NIGHTLY_STATUS:"):
            try:
                data = _json.loads(line[len("ATLAS_NIGHTLY_STATUS:"):].strip())
                status = data.get("status", "")
                if status == "completed_no_keeps":
                    return "no_keeps"
                if status in _SUCCESS_STATUSES:
                    return status
            except Exception:
                pass

    # ── 2. Summary "Total: X screened, Y promoted, Z kept" ─────────────────
    m = _re.search(r"Total:\s+(\d+)\s+screened,\s+\d+\s+promoted,\s+(\d+)\s+kept", text)
    if m:
        screened = int(m.group(1))
        kept = int(m.group(2))
        if screened > 0:
            # Workers ran; 0 kept is a legitimate no-op
            return "ok" if kept > 0 else "no_keeps"

    # ── 3. Missing config (workers failed because no config/active/*.json) ──
    _config_missing_signals = (
        "Config file not found" in text
        or "Could not load active config" in text
        or "No active config for market" in text
        or "ResearchSession market mismatch: config.market=None" in text
    )
    if _config_missing_signals and returncode != 0:
        # Workers likely all crashed before producing any results
        return "config_missing"

    # ── 4. Benchmark unavailable (workers continued past DBC.AX warning) ───
    if ("data unavailable" in text or "no data returned from any source" in text):
        if returncode == 0:
            return "benchmark_unavailable"

    # ── 5. returncode fallback ──────────────────────────────────────────────
    return "ok" if returncode == 0 else "error"


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_progress(run_id: str) -> dict:
    """Load existing progress checkpoint, return empty dict on miss."""
    path = OUTPUT_DIR / f"progress_{run_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _save_progress(run_id: str, progress: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / f"progress_{run_id}.json").write_text(
        json.dumps(progress, indent=2)
    )


# ---------------------------------------------------------------------------
# Universe sweep
# ---------------------------------------------------------------------------

def run_universe_sweep(
    universe: str,
    hours: float,
    workers: int,
    dry_run: bool = False,
) -> dict:
    """Run autoresearch_nightly.py for one universe. Returns result dict."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = OUTPUT_DIR / f"{universe}_{ts}.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(NIGHTLY_SCRIPT),
        "--universe", universe,
        "--market",   universe,
        "--hours",    str(hours),
        "--workers",  str(workers),
    ]

    result: dict = {
        "universe":     universe,
        "cmd":          " ".join(cmd),
        "log":          str(log_path),
        "started_at":   datetime.now(timezone.utc).isoformat(),
        "hours_budget": hours,
        "workers":      workers,
    }

    if dry_run:
        logger.info("[DRY-RUN] %s  ->  %s", universe, " ".join(cmd))
        result["status"] = "dry_run"
        return result

    # Generous timeout: sweep budget + 50% headroom + 5-min grace
    timeout_s = int(hours * 3600 * 1.5 + 300)

    logger.info(
        "Starting sweep: universe=%s  hours=%.2f  workers=%d  timeout=%ds  log=%s",
        universe, hours, workers, timeout_s, log_path,
    )
    t0 = time.time()
    try:
        with log_path.open("w") as lf:
            proc = subprocess.run(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                cwd=str(PROJECT),
                timeout=timeout_s,
            )
        elapsed = round(time.time() - t0, 1)
        # Parse the log to determine the actual outcome (ok / no_keeps /
        # benchmark_unavailable / config_missing / error).
        outcome = _parse_log_for_outcome(log_path, proc.returncode)
        result.update({
            "status":      outcome,
            "returncode":  proc.returncode,
            "elapsed_s":   elapsed,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        if outcome in _SUCCESS_STATUSES:
            if outcome == "no_keeps":
                logger.info(
                    "Sweep completed: universe=%s, 0 keeps above silent-failure "
                    "threshold (rc=%d treated as no-op)  elapsed=%ss",
                    universe, proc.returncode, elapsed,
                )
            elif outcome == "benchmark_unavailable":
                logger.warning(
                    "Sweep completed: universe=%s, benchmark data unavailable "
                    "(rc=%d treated as no-op)  elapsed=%ss",
                    universe, proc.returncode, elapsed,
                )
            elif outcome == "config_missing":
                logger.warning(
                    "Sweep skipped: universe=%s, active config file not found "
                    "(create config/active/%s.json to enable sweeps)  elapsed=%ss",
                    universe, universe, elapsed,
                )
            else:
                logger.info("Sweep done: universe=%s  elapsed=%ss", universe, elapsed)
        else:
            logger.error(
                "Sweep failed: universe=%s  rc=%d  outcome=%s  elapsed=%ss",
                universe, proc.returncode, outcome, elapsed,
            )

    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - t0, 1)
        result.update({"status": "timeout", "elapsed_s": elapsed})
        logger.error("Sweep timed out: universe=%s  elapsed=%ss", universe, elapsed)

    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        result.update({"status": "error", "error": str(exc), "elapsed_s": elapsed})
        logger.error("Sweep exception: universe=%s  error=%s", universe, exc)

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute matrix repopulation — sweeps all strategy x universe combos (#216)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/run_compute_matrix.py\n"
            "  python3 scripts/run_compute_matrix.py --universes sp500,commodity_etfs\n"
            "  python3 scripts/run_compute_matrix.py --dry-run\n"
            "  python3 scripts/run_compute_matrix.py --universes sp500 --hours 2 --workers 4\n"
        ),
    )
    parser.add_argument(
        "--universes",
        default=",".join(DEFAULT_UNIVERSES),
        help=(
            "Comma-separated universes to sweep "
            f"(default: {','.join(DEFAULT_UNIVERSES)}). "
            "Add 'crypto' explicitly if needed."
        ),
    )
    parser.add_argument(
        "--workers", type=int, default=None,
        help="Override worker count for all universes (default: per-universe calibration).",
    )
    parser.add_argument(
        "--hours", type=float, default=None,
        help="Override hours budget for all universes (default: per-universe calibration).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan without executing anything.",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Skip universes already marked ok in a prior progress checkpoint.",
    )
    args = parser.parse_args(argv)

    # Resolve and validate universe list
    requested = [u.strip() for u in args.universes.split(",") if u.strip()]
    unknown = [u for u in requested if u not in UNIVERSE_CONFIG]
    if unknown:
        logger.error("Unknown universes (not in UNIVERSE_CONFIG): %s", unknown)
        logger.error("Valid choices: %s", list(UNIVERSE_CONFIG.keys()))
        return 1

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    progress: dict = _load_progress(run_id) if args.resume else {}

    logger.info(
        "=== Compute Matrix Repopulation (#216) ===  run_id=%s  universes=%s",
        run_id, requested,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for universe in requested:
        # Resume: skip completed universes
        if args.resume and progress.get(universe, {}).get("status") in _SUCCESS_STATUSES:
            logger.info("Skipping %s (already done in checkpoint, status=%s)", universe, progress[universe].get("status"))
            results.append(progress[universe])
            continue

        cfg = UNIVERSE_CONFIG[universe]
        hours   = args.hours   if args.hours   is not None else float(cfg["hours"])
        workers = args.workers if args.workers is not None else int(cfg["workers"])

        r = run_universe_sweep(universe, hours, workers, dry_run=args.dry_run)
        results.append(r)
        progress[universe] = r
        _save_progress(run_id, progress)
        # Emit explicit outcome message for non-ok success statuses so it
        # appears in the run log regardless of how run_universe_sweep is called.
        r_status = r.get("status", "")
        if r_status == "no_keeps":
            logger.info(
                "Sweep completed: universe=%s, 0 keeps above silent-failure threshold "
                "(sweep ran cleanly — no improvement beat the threshold)",
                universe,
            )
        elif r_status == "config_missing":
            logger.warning(
                "Sweep skipped: universe=%s, no active config (create "
                "config/active/%s.json to enable sweeps)",
                universe, universe,
            )
        elif r_status == "benchmark_unavailable":
            logger.warning(
                "Sweep skipped: universe=%s, benchmark data unavailable",
                universe,
            )

    # Write final summary
    summary_path = OUTPUT_DIR / f"summary_{run_id}.json"
    summary_path.write_text(json.dumps(results, indent=2))
    logger.info("Summary written -> %s", summary_path)

    # Touch done sentinel
    (OUTPUT_DIR / f"compute_matrix_{run_id}.done").touch()

    # Report outcomes
    n_ok       = sum(1 for r in results if r.get("status") == "ok")
    n_no_keeps = sum(1 for r in results if r.get("status") == "no_keeps")
    n_skip     = sum(1 for r in results if r.get("status") in ("benchmark_unavailable", "config_missing"))
    n_dry      = sum(1 for r in results if r.get("status") == "dry_run")
    failed     = [r for r in results if r.get("status") not in _SUCCESS_STATUSES]

    logger.info(
        "=== Finished ===  ok=%d  no_keeps=%d  skipped=%d  dry_run=%d  failed=%d",
        n_ok, n_no_keeps, n_skip, n_dry, len(failed),
    )
    if failed:
        logger.error(
            "Failed universes (genuine sweep errors): %s",
            [f["universe"] for f in failed],
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
