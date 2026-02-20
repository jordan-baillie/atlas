#!/usr/bin/env python3
"""
Bayesian Optimization for Atlas-ASX Trading System
Uses Optuna with TPE sampler and robustness-aware objective function.
Each trial evaluates baseline + perturbation variants to optimize for stability.
"""

import sys
import json
import copy
import random
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import optuna

sys.path.insert(0, "/a0/usr/projects/atlas-asx")
from backtest.engine import BacktestEngine
from strategies.mean_reversion import MeanReversion
from strategies.trend_following import TrendFollowing
from strategies.bb_squeeze import BBSqueeze
from strategies.opening_gap import OpeningGap

PROJECT = Path("/a0/usr/projects/atlas-asx")
DATA_DIR = PROJECT / "data" / "cache"
RESULTS_DIR = PROJECT / "backtest" / "results"

# Suppress optuna info logging (too verbose)
optuna.logging.set_verbosity(optuna.logging.WARNING)

# ---------------------------------------------------------------------------
# Search space: 14 parameters that changed between v9.1 and v9.2
# Ranges are WIDER than v9.1-v9.2 range for thorough exploration
# Format: (type, low, high)
# ---------------------------------------------------------------------------
SEARCH_SPACE = {
    "mean_reversion": {
        "rsi_oversold": ("int", 25, 45),
        "max_hold_days": ("int", 3, 12),
    },
    "trend_following": {
        "fast_ma": ("int", 5, 30),
        "slow_ma": ("int", 25, 70),
        "max_hold_days": ("int", 10, 35),
    },
    "bb_squeeze": {
        "bb_std": ("float", 1.5, 4.0),
        "kc_atr_mult": ("float", 1.0, 3.5),
        "max_hold_days": ("int", 5, 30),
        "trailing_stop_atr_mult": ("float", 1.0, 4.0),
    },
    "opening_gap": {
        "gap_threshold": ("float", -0.03, -0.005),
        "ibs_confirm": ("float", 0.1, 0.5),
        "rsi14_max": ("int", 30, 60),
        "max_hold_days": ("int", 3, 20),
    },
}

# Known good parameter sets for seeding the study
KNOWN_PARAMS_V91 = {
    "mean_reversion__rsi_oversold": 40,
    "mean_reversion__max_hold_days": 5,
    "trend_following__fast_ma": 10,
    "trend_following__slow_ma": 30,
    "trend_following__max_hold_days": 20,
    "bb_squeeze__bb_std": 2.5,
    "bb_squeeze__kc_atr_mult": 2.5,
    "bb_squeeze__max_hold_days": 10,
    "bb_squeeze__trailing_stop_atr_mult": 2.0,
    "opening_gap__gap_threshold": -0.015,
    "opening_gap__ibs_confirm": 0.3,
    "opening_gap__rsi14_max": 50,
    "opening_gap__max_hold_days": 7,
}

KNOWN_PARAMS_V92 = {
    "mean_reversion__rsi_oversold": 35,
    "mean_reversion__max_hold_days": 7,
    "trend_following__fast_ma": 20,
    "trend_following__slow_ma": 50,
    "trend_following__max_hold_days": 25,
    "bb_squeeze__bb_std": 3.0,
    "bb_squeeze__kc_atr_mult": 2.0,
    "bb_squeeze__max_hold_days": 20,
    "bb_squeeze__trailing_stop_atr_mult": 3.0,
    "opening_gap__gap_threshold": -0.01,
    "opening_gap__ibs_confirm": 0.2,
    "opening_gap__rsi14_max": 40,
    "opening_gap__max_hold_days": 15,
}

KNOWN_PARAMS_V93 = {
    "mean_reversion__rsi_oversold": 38,
    "mean_reversion__max_hold_days": 6,
    "trend_following__fast_ma": 15,
    "trend_following__slow_ma": 40,
    "trend_following__max_hold_days": 23,
    "bb_squeeze__bb_std": 2.75,
    "bb_squeeze__kc_atr_mult": 2.25,
    "bb_squeeze__max_hold_days": 15,
    "bb_squeeze__trailing_stop_atr_mult": 2.5,
    "opening_gap__gap_threshold": -0.0125,
    "opening_gap__ibs_confirm": 0.25,
    "opening_gap__rsi14_max": 45,
    "opening_gap__max_hold_days": 11,
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def normalize_cagr(cagr):
    """Normalize CAGR to percentage (engine sometimes returns fraction, sometimes %)."""
    return cagr * 100 if abs(cagr) < 2 else cagr


def normalize_dd(dd):
    """Normalize max drawdown to fraction (0-1 range)."""
    return dd / 100 if dd > 1 else dd


def load_data(min_rows=100):
    """Load all parquet data files from cache directory."""
    data_dict = {}
    for pf in sorted(DATA_DIR.glob("*.parquet")):
        if pf.stem == "IOZ_AX":
            continue
        ticker = pf.stem.replace("_AX", ".AX")
        df = pd.read_parquet(pf)
        df.columns = [c.lower() for c in df.columns]
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
        df.index = pd.to_datetime(df.index)
        if len(df) >= min_rows:
            data_dict[ticker] = df
    return data_dict


def build_strategies(cfg):
    """Instantiate enabled strategy objects from config."""
    strategies = []
    if cfg["strategies"].get("mean_reversion", {}).get("enabled", True):
        strategies.append(MeanReversion(cfg))
    if cfg["strategies"].get("trend_following", {}).get("enabled", True):
        strategies.append(TrendFollowing(cfg))
    if cfg["strategies"].get("bb_squeeze", {}).get("enabled", True):
        strategies.append(BBSqueeze(cfg))
    if cfg["strategies"].get("opening_gap", {}).get("enabled", True):
        strategies.append(OpeningGap(cfg))
    return strategies


def run_bt(cfg, data_dict):
    """Run a single walk-forward backtest and return metrics dict."""
    engine = BacktestEngine(cfg)
    result = engine.run_walkforward(data_dict, build_strategies(cfg))
    return result.metrics


# ---------------------------------------------------------------------------
# Perturbation and parameter application
# ---------------------------------------------------------------------------

def perturb_config(cfg, pct=0.10, seed=42):
    """Perturb all numeric strategy parameters by random factor within ±pct."""
    rng = random.Random(seed)
    pc = copy.deepcopy(cfg)
    for sname, sconf in pc["strategies"].items():
        if not isinstance(sconf, dict):
            continue
        for k, v in list(sconf.items()):
            if isinstance(v, bool) or k in ("enabled", "name"):
                continue
            if isinstance(v, int):
                factor = rng.uniform(1 - pct, 1 + pct)
                sconf[k] = max(1, round(v * factor))
            elif isinstance(v, float):
                factor = rng.uniform(1 - pct, 1 + pct)
                sconf[k] = round(v * factor, 4)
    return pc


def apply_trial_params(trial, base_cfg):
    """Apply Optuna trial suggestions to base config, return modified config."""
    cfg = copy.deepcopy(base_cfg)
    for strategy_name, params in SEARCH_SPACE.items():
        for param_name, (ptype, low, high) in params.items():
            key = f"{strategy_name}__{param_name}"
            if ptype == "int":
                val = trial.suggest_int(key, low, high)
            elif ptype == "float":
                val = trial.suggest_float(key, low, high)
            else:
                continue
            cfg["strategies"][strategy_name][param_name] = val

    # Constraint: slow_ma must be > fast_ma + 10
    fast = cfg["strategies"]["trend_following"]["fast_ma"]
    slow = cfg["strategies"]["trend_following"]["slow_ma"]
    if slow <= fast + 10:
        cfg["strategies"]["trend_following"]["slow_ma"] = fast + 15

    return cfg


# ---------------------------------------------------------------------------
# Objective function (robustness-aware)
# ---------------------------------------------------------------------------

def robust_objective(trial, base_cfg, data_dict, n_perturbations=2):
    """
    Robustness-aware objective: evaluates baseline + perturbation variants.
    Returns composite score combining CAGR, stability, Sharpe, and drawdown.
    """
    cfg = apply_trial_params(trial, base_cfg)

    # Run baseline backtest
    t0 = time.time()
    try:
        metrics_base = run_bt(cfg, data_dict)
    except Exception as e:
        print(f"  Trial {trial.number}: FAILED baseline - {e}")
        return -999.0
    el_base = time.time() - t0

    cagr_base = normalize_cagr(metrics_base.get("cagr", 0))
    sharpe_base = metrics_base.get("sharpe", 0)
    pf_base = metrics_base.get("profit_factor", 0)
    trades_base = metrics_base.get("total_trades", 0)
    maxdd_base = normalize_dd(metrics_base.get("max_drawdown", 1))

    # Minimum trade count filter
    if trades_base < 50:
        print(f"  Trial {trial.number}: REJECTED - only {trades_base} trades (<50)")
        return -999.0

    # Run perturbation variants
    perturbed_cagrs = [cagr_base]
    for seed_idx in range(n_perturbations):
        seed = seed_idx + trial.number * 100
        try:
            pcfg = perturb_config(cfg, pct=0.10, seed=seed)
            pm = run_bt(pcfg, data_dict)
            perturbed_cagrs.append(normalize_cagr(pm.get("cagr", 0)))
        except Exception as e:
            print(f"  Trial {trial.number}: perturbation {seed_idx} failed - {e}")
            perturbed_cagrs.append(0.0)

    el_total = time.time() - t0

    # Composite score: weighted combination
    # 40% baseline CAGR + 30% mean perturbed CAGR + 20% Sharpe + 10% (1 - MaxDD)
    mean_pert_cagr = sum(perturbed_cagrs) / len(perturbed_cagrs)
    score = (
        0.40 * cagr_base +
        0.30 * mean_pert_cagr +
        0.20 * sharpe_base * 10 +   # Scale sharpe to similar magnitude
        0.10 * (1 - maxdd_base) * 10  # Lower drawdown = higher score
    )

    # Store metrics as trial user attributes for analysis
    trial.set_user_attr("cagr_base", round(cagr_base, 4))
    trial.set_user_attr("mean_pert_cagr", round(mean_pert_cagr, 4))
    trial.set_user_attr("sharpe", round(sharpe_base, 4))
    trial.set_user_attr("profit_factor", round(pf_base, 4))
    trial.set_user_attr("max_drawdown", round(maxdd_base, 4))
    trial.set_user_attr("total_trades", trades_base)
    stability = round(mean_pert_cagr / max(cagr_base, 0.01), 4)
    trial.set_user_attr("stability_ratio", stability)
    trial.set_user_attr("runtime_s", round(el_total, 1))

    print(f"  Trial {trial.number}: score={score:.4f} CAGR={cagr_base:.2f}% "
          f"MeanPert={mean_pert_cagr:.2f}% Sh={sharpe_base:.4f} "
          f"PF={pf_base:.4f} DD={maxdd_base:.4f} T={trades_base} "
          f"Stab={stability:.2f} [{el_total:.0f}s]")
    sys.stdout.flush()

    return score


def seed_with_known_params(study):
    """Enqueue known-good parameter sets (v9.1, v9.2, v9.3) as starting points."""
    for name, params in [("v9.1", KNOWN_PARAMS_V91),
                          ("v9.2", KNOWN_PARAMS_V92),
                          ("v9.3", KNOWN_PARAMS_V93)]:
        try:
            study.enqueue_trial(params)
            print(f"  Seeded {name} params as starting point")
        except Exception as e:
            print(f"  Warning: failed to seed {name}: {e}")


# ---------------------------------------------------------------------------
# Results saving and reporting
# ---------------------------------------------------------------------------

def save_results(study, args, base_cfg=None, data_dict=None):
    """Save study results to JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"bayesian_{args.study_name}.json"

    trials_data = []
    for t in study.trials:
        if t.state != optuna.trial.TrialState.COMPLETE:
            continue
        td = {
            "number": t.number,
            "value": t.value,
            "params": t.params,
            "user_attrs": t.user_attrs,
        }
        trials_data.append(td)

    best = study.best_trial
    report = {
        "timestamp": datetime.now().isoformat(),
        "study_name": args.study_name,
        "n_trials": len(study.trials),
        "n_complete": len(trials_data),
        "best_trial": {
            "number": best.number,
            "score": best.value,
            "params": best.params,
            "metrics": best.user_attrs,
        },
        "trials": trials_data,
    }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)


def build_best_config(study, base_cfg):
    """Build a complete config from the best trial parameters."""
    cfg = copy.deepcopy(base_cfg)
    best = study.best_trial
    for strategy_name, params in SEARCH_SPACE.items():
        for param_name, (ptype, low, high) in params.items():
            key = f"{strategy_name}__{param_name}"
            if key in best.params:
                val = best.params[key]
                cfg["strategies"][strategy_name][param_name] = val

    # Apply constraint
    fast = cfg["strategies"]["trend_following"]["fast_ma"]
    slow = cfg["strategies"]["trend_following"]["slow_ma"]
    if slow <= fast + 10:
        cfg["strategies"]["trend_following"]["slow_ma"] = fast + 15

    return cfg


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Bayesian Optimization for Atlas-ASX")
    parser.add_argument("--n-trials", type=int, default=50,
                        help="Number of optimization trials (default: 50)")
    parser.add_argument("--perturbations", type=int, default=2,
                        help="Perturbation runs per trial (default: 2)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output config path (default: auto-generated)")
    parser.add_argument("--study-name", type=str, default="atlas_robust",
                        help="Optuna study name (default: atlas_robust)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume existing study instead of creating new")
    parser.add_argument("--base-config", type=str, default=None,
                        help="Base config path (default: active_config.json)")
    args = parser.parse_args()

    print("=" * 70)
    print("BAYESIAN OPTIMIZATION - Atlas-ASX Trading System")
    print(f"Started: {datetime.now().isoformat()}")
    print(f"Trials: {args.n_trials}, Perturbations/trial: {args.perturbations}")
    print(f"Total backtests: ~{args.n_trials * (1 + args.perturbations)}")
    print(f"Estimated runtime: ~{args.n_trials * (1 + args.perturbations) * 5:.0f} min")
    print("=" * 70)
    sys.stdout.flush()

    # Load base config
    cfg_path = args.base_config or str(PROJECT / "config" / "active_config.json")
    with open(cfg_path) as f:
        base_cfg = json.load(f)
    print(f"Base config: {cfg_path} (version: {base_cfg.get('version', 'unknown')})")

    # Load data once (expensive)
    print("\nLoading market data...")
    sys.stdout.flush()
    data_dict = load_data()
    print(f"Loaded {len(data_dict)} tickers")
    sys.stdout.flush()

    # Create/load Optuna study with SQLite storage for persistence
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    storage = f"sqlite:///{RESULTS_DIR}/optuna_{args.study_name}.db"

    # TPE sampler: better than GP for high dimensions, supports constraints
    sampler = optuna.samplers.TPESampler(
        seed=42,
        n_startup_trials=10,  # Random exploration before TPE kicks in
        multivariate=True,    # Model parameter interactions
    )

    if args.resume:
        study = optuna.load_study(
            study_name=args.study_name, storage=storage, sampler=sampler
        )
        completed = len([t for t in study.trials
                         if t.state == optuna.trial.TrialState.COMPLETE])
        print(f"Resuming study: {len(study.trials)} total, {completed} completed")
    else:
        # Delete existing DB to start fresh
        db_path = RESULTS_DIR / f"optuna_{args.study_name}.db"
        if db_path.exists():
            db_path.unlink()
            print("Deleted existing study database")
        study = optuna.create_study(
            study_name=args.study_name,
            storage=storage,
            sampler=sampler,
            direction="maximize",
        )
        # Seed with known-good parameter sets as starting points
        seed_with_known_params(study)

    # Progress callback
    def trial_callback(study, trial):
        if trial.state != optuna.trial.TrialState.COMPLETE:
            return
        best = study.best_trial
        n_complete = len([t for t in study.trials
                          if t.state == optuna.trial.TrialState.COMPLETE])
        print(f"\n>>> Best so far: Trial {best.number} "
              f"score={best.value:.4f} "
              f"CAGR={best.user_attrs.get('cagr_base', 0):.2f}% "
              f"Stab={best.user_attrs.get('stability_ratio', 0):.2f} "
              f"[{n_complete}/{args.n_trials} done]")
        sys.stdout.flush()
        # Save intermediate results after every trial
        save_results(study, args)

    # Run optimization
    print(f"\nStarting Bayesian optimization: {args.n_trials} trials")
    print(f"Each trial = 1 baseline + {args.perturbations} perturbations "
          f"= {1 + args.perturbations} backtests (~{(1 + args.perturbations) * 5} min)")
    print("-" * 70)
    sys.stdout.flush()

    t_start = time.time()

    study.optimize(
        lambda trial: robust_objective(
            trial, base_cfg, data_dict, n_perturbations=args.perturbations
        ),
        n_trials=args.n_trials,
        callbacks=[trial_callback],
        show_progress_bar=False,
    )

    elapsed = time.time() - t_start

    # ===================================================================
    # Post-optimization reporting
    # ===================================================================
    print("\n" + "=" * 70)
    print("OPTIMIZATION COMPLETE")
    print("=" * 70)
    print(f"Total time: {elapsed/60:.1f} minutes ({elapsed:.0f}s)")

    n_complete = len([t for t in study.trials
                      if t.state == optuna.trial.TrialState.COMPLETE])
    n_failed = len([t for t in study.trials
                    if t.state != optuna.trial.TrialState.COMPLETE])
    print(f"Trials completed: {n_complete}, Failed/pruned: {n_failed}")

    best = study.best_trial
    print(f"\n--- BEST TRIAL: #{best.number} ---")
    print(f"  Composite Score: {best.value:.4f}")
    ba = best.user_attrs
    print(f"  CAGR (baseline): {ba.get('cagr_base', 0):.2f}%")
    print(f"  Mean Perturbed CAGR: {ba.get('mean_pert_cagr', 0):.2f}%")
    print(f"  Stability Ratio: {ba.get('stability_ratio', 0):.4f}")
    print(f"  Sharpe: {ba.get('sharpe', 0):.4f}")
    print(f"  Profit Factor: {ba.get('profit_factor', 0):.4f}")
    print(f"  Max Drawdown: {ba.get('max_drawdown', 0):.4f}")
    print(f"  Total Trades: {ba.get('total_trades', 0)}")

    print("\n--- BEST PARAMETERS ---")
    for key, val in sorted(best.params.items()):
        sn, pn = key.split("__")
        print(f"  {sn:20s} {pn:30s} = {val}")

    # Show top 5 trials
    completed_trials = [t for t in study.trials
                        if t.state == optuna.trial.TrialState.COMPLETE]
    top5 = sorted(completed_trials, key=lambda t: t.value, reverse=True)[:5]
    print("\n--- TOP 5 TRIALS ---")
    hdr = f"{'#':>5} {'Score':>8} {'CAGR%':>8} {'PertCAGR':>8} {'Stab':>6} {'Sharpe':>8} {'PF':>6} {'Trades':>7}"
    print(hdr)
    print("-" * 65)
    for t in top5:
        ua = t.user_attrs
        print(f"{t.number:>5} {t.value:>8.2f} "
              f"{ua.get('cagr_base',0):>8.2f} "
              f"{ua.get('mean_pert_cagr',0):>8.2f} "
              f"{ua.get('stability_ratio',0):>6.2f} "
              f"{ua.get('sharpe',0):>8.4f} "
              f"{ua.get('profit_factor',0):>6.2f} "
              f"{ua.get('total_trades',0):>7}")

    # Build and save best config
    best_cfg = build_best_config(study, base_cfg)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    best_cfg["version"] = f"v10.0_bayesian_{ts}"
    best_cfg["optimization"] = {
        "method": "bayesian_tpe",
        "study_name": args.study_name,
        "n_trials": n_complete,
        "best_trial": best.number,
        "composite_score": best.value,
        "metrics": ba,
        "timestamp": datetime.now().isoformat(),
    }

    if args.output:
        out_cfg_path = Path(args.output)
    else:
        out_cfg_path = PROJECT / "config" / f"config_v10.0_bayesian_{ts}.json"

    with open(out_cfg_path, "w") as f:
        json.dump(best_cfg, f, indent=2, default=str)
    print(f"\nBest config saved: {out_cfg_path}")

    # Save final study results
    save_results(study, args)
    print(f"Study results: {RESULTS_DIR}/bayesian_{args.study_name}.json")
    print(f"Study database: {RESULTS_DIR}/optuna_{args.study_name}.db")

    print("\n" + "=" * 70)
    print("To activate this config:")
    print(f"  cp {out_cfg_path} {PROJECT}/config/active_config.json")
    print("Then run validation:")
    print(f"  python3 {PROJECT}/scripts/validate_oos.py")
    print("=" * 70)


if __name__ == "__main__":
    main()

