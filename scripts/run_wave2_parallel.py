#!/usr/bin/env python3
"""Run Wave 2 experiments in parallel across available cores.

Independent experiments run concurrently. Dependent chains run sequentially
after their prerequisites complete.

Usage:
    python3 scripts/run_wave2_parallel.py [--workers N]
"""
import json
import multiprocessing as mp
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT)
sys.path.insert(0, str(PROJECT))

from research.models import (
    read_queue, claim_experiment, update_queue_entry,
    ExperimentStatus, atomic_json_write,
)
from scripts.research_runner import run_experiment

AGENT_ID = "atlas-wave2-parallel"


def run_one(exp_id: str) -> dict:
    """Run a single experiment in a subprocess. Returns summary dict."""
    os.chdir(PROJECT)
    sys.path.insert(0, str(PROJECT))

    queue = read_queue()
    entry = next((e for e in queue if e['id'] == exp_id), None)
    if not entry:
        return {'id': exp_id, 'verdict': 'error', 'error': 'not found in queue'}

    claimed = claim_experiment(exp_id, AGENT_ID)
    if not claimed:
        return {'id': exp_id, 'verdict': 'error', 'error': f'could not claim (status={entry.get("status")})'}

    try:
        result = run_experiment(claimed, AGENT_ID, dry_run=False)
        return {
            'id': exp_id,
            'verdict': result.get('verdict', 'unknown'),
            'rationale': result.get('verdict_rationale', '')[:200],
        }
    except Exception as e:
        return {'id': exp_id, 'verdict': 'error', 'error': str(e)[:200]}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers', type=int, default=min(8, mp.cpu_count()))
    args = parser.parse_args()

    queue = read_queue()
    wave2 = [e for e in queue if e['id'].startswith('wave2_') and e.get('status') in ('queued',)]

    # Split into independent and dependent
    independent = [e for e in wave2 if not e.get('depends_on')]
    dependent = [e for e in wave2 if e.get('depends_on')]

    print(f"=== Wave 2 Parallel Runner ===")
    print(f"Workers: {args.workers}")
    print(f"Independent: {len(independent)} experiments")
    print(f"Dependent:   {len(dependent)} experiments")
    print(f"Start: {datetime.now().strftime('%H:%M:%S')}")
    print()

    results = []
    t0 = time.time()

    # Phase 1: Run all independent experiments in parallel
    print(f"── Phase 1: Independent experiments ({len(independent)}) ──")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, e['id']): e['id'] for e in independent}
        for future in as_completed(futures):
            exp_id = futures[future]
            try:
                result = future.result(timeout=3600)
            except Exception as e:
                result = {'id': exp_id, 'verdict': 'error', 'error': str(e)[:200]}
            verdict = result.get('verdict', '?')
            symbol = '✅' if verdict == 'pass' else '❌' if verdict == 'fail' else '⚠️'
            elapsed = time.time() - t0
            print(f"  {symbol} {exp_id}: {verdict} ({elapsed:.0f}s)")
            results.append(result)

    # Phase 2: Run dependent experiments in topological order with skip propagation
    if dependent:
        print(f"\n── Phase 2: Dependent experiments ({len(dependent)}) ──")

        # result_by_id is the source of truth for dependency checking.
        # Verdicts that mean "this dependency is a dead end".
        TERMINAL_VERDICTS = {'fail', 'error', 'skipped'}

        result_by_id: dict = {r['id']: r for r in results}
        remaining = list(dependent)
        # worst case: one experiment unblocked per round, plus cascading skips
        max_rounds = len(dependent) + 2

        for round_num in range(max_rounds):
            if not remaining:
                break

            runnable = []   # deps all passed → can run now
            skippable = []  # at least one dep failed/skipped → skip immediately
            # anything else is still waiting for an in-progress dep

            for e in remaining:
                deps = e.get('depends_on', [])
                dep_verdicts = {d: result_by_id.get(d, {}).get('verdict') for d in deps}

                # Find the first dependency that failed/was skipped
                failed_dep = next(
                    (d for d, v in dep_verdicts.items() if v in TERMINAL_VERDICTS),
                    None,
                )
                if failed_dep:
                    skippable.append((e, failed_dep))
                elif all(v == 'pass' for v in dep_verdicts.values()):
                    runnable.append(e)
                # else: dep not yet resolved → stays in remaining this round

            # --- Skip experiments whose dependencies failed ---
            for e, failed_dep in skippable:
                rationale = f"dependency {failed_dep} failed"
                print(f"  ⏭️  {e['id']}: SKIPPED ({rationale})")
                skip_result = {'id': e['id'], 'verdict': 'skipped', 'rationale': rationale}
                results.append(skip_result)
                result_by_id[e['id']] = skip_result
                remaining.remove(e)
                update_queue_entry(e['id'], {
                    'status': ExperimentStatus.SKIPPED,
                    'notes': f"[auto-skipped] {rationale}",
                })

            if not runnable:
                if not skippable:
                    # Nothing was runnable and nothing was skipped — we're stuck.
                    if remaining:
                        print(f"  ⚠️  {len(remaining)} experiments stuck "
                              f"(deps unresolvable): "
                              f"{[e['id'] for e in remaining]}")
                    break
                # We skipped some but nothing to run yet → loop to catch cascades
                continue

            # --- Run this round's experiments in parallel ---
            print(f"  Round {round_num + 1}: running {len(runnable)} experiment(s)")
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(run_one, e['id']): e['id'] for e in runnable}
                for future in as_completed(futures):
                    exp_id = futures[future]
                    try:
                        result = future.result(timeout=3600)
                    except Exception as exc:
                        result = {'id': exp_id, 'verdict': 'error', 'error': str(exc)[:200]}
                    verdict = result.get('verdict', '?')
                    symbol = '✅' if verdict == 'pass' else '❌' if verdict == 'fail' else '⚠️'
                    elapsed = time.time() - t0
                    print(f"  {symbol} {exp_id}: {verdict} ({elapsed:.0f}s)")
                    results.append(result)
                    result_by_id[exp_id] = result

            for e in runnable:
                remaining.remove(e)

    # Summary
    elapsed = time.time() - t0
    passed = sum(1 for r in results if r.get('verdict') == 'pass')
    failed = sum(1 for r in results if r.get('verdict') in ('fail', 'error'))
    skipped = sum(1 for r in results if r.get('verdict') == 'skipped')

    print(f"\n=== Wave 2 Complete ===")
    print(f"Total: {len(results)} | Pass: {passed} | Fail: {failed} | Skip: {skipped}")
    print(f"Time: {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"End: {datetime.now().strftime('%H:%M:%S')}")

    # Save results atomically
    out_path = PROJECT / 'research' / 'waves' / 'wave_2_results.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(out_path, {
        'started': t0,
        'elapsed_s': elapsed,
        'results': results,
    })
    print(f"Results saved: {out_path}")

    return 0 if failed == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
