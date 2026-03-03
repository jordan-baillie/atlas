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
    ExperimentStatus,
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

    # Phase 2: Run dependent experiments sequentially (respecting chains)
    if dependent:
        print(f"\n── Phase 2: Dependent experiments ({len(dependent)}) ──")
        # Sort by dependency depth
        done_ids = {r['id'] for r in results if r.get('verdict') == 'pass'}
        remaining = list(dependent)
        max_rounds = 10
        round_num = 0

        while remaining and round_num < max_rounds:
            round_num += 1
            runnable = [e for e in remaining
                        if all(d in done_ids for d in e.get('depends_on', []))]

            if not runnable:
                # Check if blocked by failures
                failed_ids = {r['id'] for r in results if r.get('verdict') != 'pass'}
                blocked = [e for e in remaining
                           if any(d in failed_ids for d in e.get('depends_on', []))]
                for e in blocked:
                    print(f"  ⏭️  {e['id']}: SKIPPED (dependency failed)")
                    results.append({'id': e['id'], 'verdict': 'skipped', 'error': 'dependency failed'})
                    remaining.remove(e)
                    update_queue_entry(e['id'], {'status': ExperimentStatus.FAILED})
                if not runnable:
                    break

            # Run this round in parallel
            with ProcessPoolExecutor(max_workers=args.workers) as pool:
                futures = {pool.submit(run_one, e['id']): e['id'] for e in runnable}
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
                    if verdict == 'pass':
                        done_ids.add(exp_id)

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

    # Save results
    out_path = PROJECT / 'research' / 'waves' / 'wave_2_results.json'
    with open(out_path, 'w') as f:
        json.dump({
            'started': t0,
            'elapsed_s': elapsed,
            'results': results,
        }, f, indent=2, default=str)
    print(f"Results saved: {out_path}")

    return 0 if failed == 0 else 2


if __name__ == '__main__':
    sys.exit(main())
