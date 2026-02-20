#!/usr/bin/env python3
"""Phase 1.3: Optimize Opening Gap signal parameters via coordinate descent.
Uses 7-core parallelism."""
import json, os, sys, time, copy
import numpy as np
import pandas as pd
from pathlib import Path
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
os.chdir('/a0/usr/projects/atlas-asx')
sys.path.insert(0, '.')

OG_PARAMS = {
    'gap_threshold':       [-0.005, -0.01, -0.015, -0.02, -0.025, -0.03, -0.04],
    'ibs_confirm':         [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4],
    'rsi14_max':           [25, 30, 35, 40, 45, 50, 55],
    'vol_surge_threshold': [1.0, 1.2, 1.3, 1.5, 1.7, 2.0, 2.5],
    'sma_exit_period':     [3, 4, 5, 6, 7, 8, 10],
}
N_CYCLES = 3
N_CORES = 7

def load_data():
    data_dict = {}
    cache = Path('data/cache')
    for pf in sorted(cache.glob('*.parquet')):
        if pf.stem == 'IOZ_AX': continue
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df.index = pd.to_datetime(df.index)
        if len(df) < 100: continue
        ticker = pf.stem.replace('_AX', '.AX')
        data_dict[ticker] = df
    return data_dict

def do_backtest(cfg, data_dict):
    from backtest.engine import BacktestEngine
    from strategies.mean_reversion import MeanReversion
    from strategies.trend_following import TrendFollowing
    from strategies.opening_gap import OpeningGap
    strats = [MeanReversion(cfg), TrendFollowing(cfg), OpeningGap(cfg)]
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, strats)
    m = result.metrics
    cagr = m.get('cagr', 0)
    cagr = cagr * 100 if abs(cagr) < 2 else cagr
    dd = m.get('max_drawdown', 0)
    dd = dd * 100 if abs(dd) < 1 else dd
    wr = m.get('win_rate', 0)
    wr = wr * 100 if abs(wr) < 2 else wr
    return {
        'cagr': cagr, 'sharpe': m.get('sharpe', 0), 'pf': m.get('profit_factor', 0),
        'max_dd': dd, 'win_rate': wr, 'trades': m.get('total_trades', 0),
        'final_equity': m.get('final_equity', 0), 'result': result,
    }

def worker_backtest(args):
    """Parallel worker: run backtest with given config."""
    cfg, label = args
    os.chdir('/a0/usr/projects/atlas-asx')
    sys.path.insert(0, '.')
    data_dict = load_data()
    r = do_backtest(cfg, data_dict)
    trades = r['trades']
    trade_penalty = min(1.0, trades / 100.0)
    score = (r['cagr'] * 0.4 + r['sharpe'] * 10 * 0.3 + r['pf'] * 3 * 0.2) * trade_penalty
    if r['max_dd'] > 15:
        score *= 0.8
    r['label'] = label
    r['score'] = score
    return r

def worker_perturbation(args):
    """Parallel worker: run perturbation trial."""
    cfg, trial_num, perturb_pct, seed = args
    os.chdir('/a0/usr/projects/atlas-asx')
    sys.path.insert(0, '.')
    data_dict = load_data()
    rng = np.random.RandomState(seed)
    ALL_PARAMS = {
        'mean_reversion': ['rsi_period', 'rsi_entry'],
        'trend_following': ['ema_fast', 'ema_slow', 'atr_period'],
        'opening_gap': list(OG_PARAMS.keys()),
    }
    perturbed = copy.deepcopy(cfg)
    for sn, params in ALL_PARAMS.items():
        for p in params:
            orig = perturbed['strategies'][sn].get(p)
            if orig is None or orig == 0:
                continue
            factor = 1.0 + rng.uniform(-perturb_pct, perturb_pct)
            new_val = orig * factor
            if isinstance(orig, int):
                new_val = max(2, int(round(new_val)))
            else:
                new_val = round(new_val, 6)
            perturbed['strategies'][sn][p] = new_val
    r = do_backtest(perturbed, data_dict)
    return {'trial': trial_num, 'cagr': r['cagr'], 'sharpe': r['sharpe'],
            'pf': r['pf'], 'trades': r['trades'], 'max_dd': r['max_dd']}

if __name__ == '__main__':
    print("=" * 70)
    print("PHASE 1.3: Opening Gap Parameter Optimization (7-core parallel)")
    print("=" * 70)
    t_start = time.time()

    with open('config/config_phase1_2_3strat_fixed.json') as f:
        base_cfg = json.load(f)

    print("\nCurrent OG parameters:")
    for k in sorted(OG_PARAMS.keys()):
        print("  {}: {}".format(k, base_cfg['strategies']['opening_gap'].get(k)))

    history = []

    for cycle in range(N_CYCLES):
        print("\n" + "=" * 50)
        print("CYCLE {}/{}".format(cycle + 1, N_CYCLES))
        print("=" * 50)
        changed = False

        for param_name, candidates in OG_PARAMS.items():
            print("\n--- Optimizing: {} ---".format(param_name))
            current_val = base_cfg['strategies']['opening_gap'][param_name]
            print("  Current: {}, Candidates: {}".format(current_val, candidates))

            tasks = []
            for val in candidates:
                cfg = copy.deepcopy(base_cfg)
                cfg['strategies']['opening_gap'][param_name] = val
                tasks.append((cfg, "{}={}".format(param_name, val)))

            results = []
            t0 = time.time()
            with ProcessPoolExecutor(max_workers=N_CORES) as executor:
                futures = {executor.submit(worker_backtest, task): task[1] for task in tasks}
                for future in as_completed(futures):
                    label = futures[future]
                    try:
                        r = future.result()
                        results.append(r)
                    except Exception as e:
                        print("  FAILED {}: {}".format(label, e))

            elapsed = time.time() - t0
            results.sort(key=lambda r: r['score'], reverse=True)

            print("  Completed {} evals in {:.0f}s".format(len(results), elapsed))
            for i, r in enumerate(results):
                best_mark = " <-- BEST" if i == 0 else ""
                curr_mark = " [cur]" if r['label'] == "{}={}".format(param_name, current_val) else ""
                print("    {:35s} CAGR={:6.2f}% Sh={:.3f} PF={:.3f} DD={:.1f}% Tr={:3d} Sc={:.2f}{}{}".format(
                    r['label'], r['cagr'], r['sharpe'], r['pf'],
                    r['max_dd'], r['trades'], r['score'], best_mark, curr_mark))

            best = results[0]
            best_val = None
            for val in candidates:
                if best['label'] == "{}={}".format(param_name, val):
                    best_val = val
                    break

            if best_val is not None and best_val != current_val:
                print("  >>> Updating {}: {} -> {}".format(param_name, current_val, best_val))
                base_cfg['strategies']['opening_gap'][param_name] = best_val
                changed = True
            else:
                print("  >>> Keeping {} = {}".format(param_name, current_val))

            history.append({
                'cycle': cycle + 1, 'param': param_name,
                'old_val': current_val,
                'new_val': best_val if best_val else current_val,
                'best_score': round(best['score'], 3),
                'best_cagr': round(best['cagr'], 2),
                'best_sharpe': round(best['sharpe'], 3),
                'best_trades': best['trades'],
            })

        if not changed:
            print("\nNo changes in cycle {}, converged.".format(cycle + 1))
            break

    # === FINAL RESULTS ===
    print("\n" + "=" * 70)
    print("OPTIMIZATION COMPLETE - Running final backtest + perturbation")
    print("=" * 70)

    print("\nOptimized OG parameters:")
    for k in sorted(OG_PARAMS.keys()):
        print("  {}: {}".format(k, base_cfg['strategies']['opening_gap'].get(k)))

    data_dict = load_data()
    t0 = time.time()
    final = do_backtest(base_cfg, data_dict)
    elapsed = time.time() - t0
    print("\nFinal 3-Strategy Results (completed in {:.0f}s):".format(elapsed))
    print("  Trades: {}".format(final['trades']))
    print("  CAGR: {:.2f}%".format(final['cagr']))
    print("  Sharpe: {:.3f}".format(final['sharpe']))
    print("  PF: {:.3f}".format(final['pf']))
    print("  MaxDD: {:.2f}%".format(final['max_dd']))
    print("  WinRate: {:.1f}%".format(final['win_rate']))
    print("  Final equity: ${:.2f}".format(final['final_equity']))

    if hasattr(final['result'], 'trades') and final['result'].trades:
        strat_counts = Counter(t.get('strategy', 'unknown') for t in final['result'].trades)
        print("\nPer-Strategy:")
        for strat, count in sorted(strat_counts.items()):
            st = [t for t in final['result'].trades if t.get('strategy') == strat]
            spnl = sum(t.get('pnl', 0) for t in st)
            sw = sum(1 for t in st if t.get('pnl', 0) > 0)
            swr = sw / count * 100 if count > 0 else 0
            ah = np.mean([t.get('hold_days', 0) for t in st])
            print("  {}: {} trades, PnL=${:.2f}, WR={:.1f}%, AvgHold={:.1f}d".format(
                strat, count, spnl, swr, ah))

    # Perturbation test
    N_PERTURB = 20
    PERTURB_PCT = 0.15
    print("\n=== PERTURBATION TEST (+/-15%, {} trials, {} cores) ===".format(N_PERTURB, N_CORES))
    perturb_args = [(base_cfg, i, PERTURB_PCT, 42 + i) for i in range(N_PERTURB)]
    perturb_results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=N_CORES) as executor:
        futures = {executor.submit(worker_perturbation, a): a[1] for a in perturb_args}
        for future in as_completed(futures):
            try:
                r = future.result()
                perturb_results.append(r)
                print("  Trial {}/{}: CAGR={:.2f}%, Sh={:.3f}, PF={:.3f}, Tr={}".format(
                    r['trial'] + 1, N_PERTURB, r['cagr'], r['sharpe'], r['pf'], r['trades']))
            except Exception as e:
                print("  Trial FAILED: {}".format(e))
    p_elapsed = time.time() - t0
    print("Perturbation completed in {:.0f}s".format(p_elapsed))

    perturb_results.sort(key=lambda r: r['trial'])
    pcagrs = [r['cagr'] for r in perturb_results]
    mean_p = np.mean(pcagrs)
    min_p = np.min(pcagrs)
    max_p = np.max(pcagrs)
    std_p = np.std(pcagrs)
    ret = mean_p / final['cagr'] * 100 if final['cagr'] > 0 else 0
    worst_ret = min_p / final['cagr'] * 100 if final['cagr'] > 0 else 0

    print("\n=== PERTURBATION RESULTS ===")
    print("Baseline CAGR: {:.2f}%".format(final['cagr']))
    print("Mean perturbed CAGR: {:.2f}% (retention: {:.1f}%)".format(mean_p, ret))
    print("Min perturbed CAGR: {:.2f}% (worst: {:.1f}%)".format(min_p, worst_ret))
    print("Max perturbed CAGR: {:.2f}%".format(max_p))
    print("Std CAGR: {:.2f}%".format(std_p))
    print("Positive CAGR: {}/{}".format(sum(1 for c in pcagrs if c > 0), N_PERTURB))

    print("\n=== COMPARISON ===")
    print("Phase 1.1 (MR+TF):     CAGR=8.31%, Sh=0.515, PF=1.608, DD=7.46%, Tr=196, Ret=76.4%")
    print("Phase 1.2 (MR+TF+OG):  CAGR=7.68%, Sh=0.436, PF=1.421, DD=8.04%, Tr=245, Ret=98.2%")
    print("Phase 1.3 (OG optim):   CAGR={:.2f}%, Sh={:.3f}, PF={:.3f}, DD={:.2f}%, Tr={}, Ret={:.1f}%".format(
        final['cagr'], final['sharpe'], final['pf'], final['max_dd'], final['trades'], ret))

    total_elapsed = time.time() - t_start
    print("\nTotal optimization time: {:.0f}s ({:.1f} min)".format(total_elapsed, total_elapsed / 60))

    # Save results
    output = {
        'final_metrics': {k: v for k, v in final.items() if k != 'result'},
        'og_params': {k: base_cfg['strategies']['opening_gap'].get(k) for k in OG_PARAMS.keys()},
        'optimization_history': history,
        'perturbation': {
            'n_trials': N_PERTURB, 'pct': PERTURB_PCT,
            'mean_cagr': round(mean_p, 2), 'min_cagr': round(min_p, 2),
            'max_cagr': round(max_p, 2), 'std_cagr': round(std_p, 2),
            'retention_pct': round(ret, 1), 'worst_retention_pct': round(worst_ret, 1),
            'positive_trials': sum(1 for c in pcagrs if c > 0),
            'trials': perturb_results,
        }
    }
    os.makedirs('backtest/results', exist_ok=True)
    with open('backtest/results/phase1_3_og_optimization.json', 'w') as f:
        json.dump(output, f, indent=2)
    with open('config/config_phase1_3_og_optimized.json', 'w') as f:
        json.dump(base_cfg, f, indent=2)

    print("\nResults: backtest/results/phase1_3_og_optimization.json")
    print("Config: config/config_phase1_3_og_optimized.json")
    print("Done.")
