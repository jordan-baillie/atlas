"""Monte Carlo forward simulation of regime paths + return distributions.

Uses the existing regime transition matrix to simulate future regime states,
then samples returns from the regime-conditional distributions (Phase 1).
Outputs:
  - Expected cumulative return over 30/60/90 days
  - Value-at-Risk and Expected Shortfall per horizon
  - Probability distribution over regime states at each horizon
"""
import logging
import json
from datetime import datetime, timezone
from typing import Optional
import numpy as np

from db.atlas_db import get_db
from regime.distributions import RegimeDistributions

logger = logging.getLogger(__name__)

STATES = ['bull_risk_on', 'bull_risk_off', 'transition_uncertain', 
          'bear_risk_off', 'bear_capitulation', 'recovery_early']


def load_transition_matrix() -> tuple:
    """Load the 6x6 regime transition matrix from regime_history."""
    with get_db() as db:
        rows = db.execute("""
            SELECT date, regime_state FROM regime_history ORDER BY date
        """).fetchall()
    
    if len(rows) < 2:
        raise ValueError("Not enough regime history to compute transitions")
    
    states = STATES
    state_to_idx = {s: i for i, s in enumerate(states)}
    
    counts = np.zeros((len(states), len(states)), dtype=float)
    prev_state = rows[0]['regime_state']
    for r in rows[1:]:
        cur = r['regime_state']
        if prev_state in state_to_idx and cur in state_to_idx:
            counts[state_to_idx[prev_state], state_to_idx[cur]] += 1
        prev_state = cur
    
    row_sums = counts.sum(axis=1, keepdims=True)
    row_sums[row_sums == 0] = 1
    matrix = counts / row_sums
    
    # Set rows that never appeared to stay in current state (self-loop)
    for i in range(len(states)):
        if counts[i].sum() == 0:
            matrix[i, i] = 1.0
    
    return matrix, states


def simulate_regime_paths(
    current_regime: str,
    n_paths: int = 10000,
    n_days: int = 90,
    seed: Optional[int] = None,
) -> tuple:
    """Simulate n_paths regime state sequences."""
    matrix, states = load_transition_matrix()
    state_to_idx = {s: i for i, s in enumerate(states)}
    
    if current_regime not in state_to_idx:
        raise ValueError(f"Unknown regime: {current_regime}")
    
    rng = np.random.default_rng(seed)
    current_idx = state_to_idx[current_regime]
    
    paths = np.zeros((n_paths, n_days), dtype=np.int32)
    current_states = np.full(n_paths, current_idx, dtype=np.int32)
    
    for day in range(n_days):
        new_states = np.zeros(n_paths, dtype=np.int32)
        for row_idx in range(len(states)):
            mask = current_states == row_idx
            n_mask = int(mask.sum())
            if n_mask == 0:
                continue
            next_states = rng.choice(len(states), size=n_mask, p=matrix[row_idx])
            new_states[mask] = next_states
        current_states = new_states
        paths[:, day] = current_states
    
    return paths, states


def simulate_return_paths_from_regime(
    current_regime: str,
    n_paths: int = 10000,
    n_days: int = 90,
    seed: Optional[int] = None,
) -> dict:
    """Full regime-aware MC simulation."""
    regime_paths, states = simulate_regime_paths(current_regime, n_paths, n_days, seed=seed)
    
    rd = RegimeDistributions()
    rd.fit()
    
    rng = np.random.default_rng(seed)
    regime_samples = {}
    for state in states:
        try:
            regime_samples[state] = rd.sample_returns(state, n=100_000, seed=seed)
        except Exception as e:
            logger.warning(f"Failed to sample from {state}: {e}; using zeros")
            regime_samples[state] = np.zeros(100_000)
    
    cumulative_log_returns = np.zeros((n_paths, n_days))
    for day in range(n_days):
        daily_log_returns = np.zeros(n_paths)
        for state_idx, state in enumerate(states):
            mask = regime_paths[:, day] == state_idx
            n_mask = int(mask.sum())
            if n_mask == 0:
                continue
            samples = regime_samples[state]
            idx = rng.integers(0, len(samples), size=n_mask)
            daily_log_returns[mask] = samples[idx]
        
        if day == 0:
            cumulative_log_returns[:, day] = daily_log_returns
        else:
            cumulative_log_returns[:, day] = cumulative_log_returns[:, day - 1] + daily_log_returns
    
    cumulative_simple = np.exp(cumulative_log_returns) - 1
    
    horizons = [h for h in [30, 60, 90] if h <= n_days]
    result = {
        "current_regime": current_regime,
        "n_paths": n_paths,
        "n_days": n_days,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "horizons": {},
    }
    
    for h in horizons:
        horizon_returns = cumulative_simple[:, h - 1]
        
        states_at_h = regime_paths[:, h - 1]
        state_probs = {}
        for idx, state in enumerate(states):
            state_probs[state] = float((states_at_h == idx).mean())
        
        var_5_val = float(np.percentile(horizon_returns, 5))
        var_1_val = float(np.percentile(horizon_returns, 1))
        cvar_5_slice = horizon_returns[horizon_returns <= var_5_val]
        cvar_1_slice = horizon_returns[horizon_returns <= var_1_val]
        
        result["horizons"][f"{h}d"] = {
            "days": h,
            "expected_return": float(horizon_returns.mean()),
            "median_return": float(np.median(horizon_returns)),
            "std": float(horizon_returns.std()),
            "var_5": var_5_val,
            "var_1": var_1_val,
            "cvar_5": float(cvar_5_slice.mean()) if len(cvar_5_slice) > 0 else var_5_val,
            "cvar_1": float(cvar_1_slice.mean()) if len(cvar_1_slice) > 0 else var_1_val,
            "p95": float(np.percentile(horizon_returns, 95)),
            "p75": float(np.percentile(horizon_returns, 75)),
            "p25": float(np.percentile(horizon_returns, 25)),
            "prob_positive": float((horizon_returns > 0).mean()),
            "state_probabilities": state_probs,
        }
    
    return result


def get_current_regime() -> str:
    """Get the most recent regime state."""
    with get_db() as db:
        row = db.execute("""
            SELECT regime_state FROM regime_history ORDER BY date DESC LIMIT 1
        """).fetchone()
    return row['regime_state'] if row else 'transition_uncertain'


def persist_forecast(result: dict) -> None:
    """Store forecast in regime_forecast table."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS regime_forecast (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                current_regime TEXT NOT NULL,
                n_paths INTEGER,
                horizon_days INTEGER NOT NULL,
                expected_return REAL,
                median_return REAL,
                std REAL,
                var_5 REAL,
                var_1 REAL,
                cvar_5 REAL,
                cvar_1 REAL,
                p95 REAL,
                p75 REAL,
                p25 REAL,
                prob_positive REAL,
                state_probabilities TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, current_regime, horizon_days)
            )
        """)
        
        for h_key, h_data in result["horizons"].items():
            db.execute("""
                INSERT OR REPLACE INTO regime_forecast
                (as_of, current_regime, n_paths, horizon_days, expected_return, median_return,
                 std, var_5, var_1, cvar_5, cvar_1, p95, p75, p25, prob_positive, state_probabilities)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result['as_of'], result['current_regime'], result['n_paths'], h_data['days'],
                h_data['expected_return'], h_data['median_return'], h_data['std'],
                h_data['var_5'], h_data['var_1'], h_data['cvar_5'], h_data['cvar_1'],
                h_data['p95'], h_data['p75'], h_data['p25'], h_data['prob_positive'],
                json.dumps(h_data['state_probabilities'])
            ))
        db.commit()


def get_latest_forecast() -> dict:
    """Load the latest forecast from DB (or empty if none)."""
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM regime_forecast 
            WHERE as_of = (SELECT MAX(as_of) FROM regime_forecast)
            ORDER BY horizon_days
        """).fetchall()
    
    if not rows:
        return {}
    
    first = rows[0]
    result = {
        "as_of": first['as_of'],
        "current_regime": first['current_regime'],
        "n_paths": first['n_paths'],
        "horizons": {}
    }
    for r in rows:
        result["horizons"][f"{r['horizon_days']}d"] = {
            "days": r['horizon_days'],
            "expected_return": r['expected_return'],
            "median_return": r['median_return'],
            "std": r['std'],
            "var_5": r['var_5'],
            "var_1": r['var_1'],
            "cvar_5": r['cvar_5'],
            "cvar_1": r['cvar_1'],
            "p95": r['p95'],
            "p75": r['p75'],
            "p25": r['p25'],
            "prob_positive": r['prob_positive'],
            "state_probabilities": json.loads(r['state_probabilities']) if r['state_probabilities'] else {},
        }
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    current = get_current_regime()
    print(f"\nCurrent regime: {current}")
    print("Running Monte Carlo forward simulation (10,000 paths, 90 days)...")
    
    result = simulate_return_paths_from_regime(current, n_paths=10000, n_days=90, seed=42)
    persist_forecast(result)
    
    print(f"\nREGIME FORWARD FORECAST — starting from {current}")
    print("=" * 100)
    print(f"{'Horizon':>10} {'E[Return]':>12} {'Median':>12} {'StdDev':>10} {'VaR 5%':>10} {'CVaR 5%':>10} {'P95':>10} {'P(+)':>8}")
    print("-" * 100)
    for h_key, h in result['horizons'].items():
        print(f"{h_key:>10} "
              f"{h['expected_return']*100:>10.2f}% "
              f"{h['median_return']*100:>10.2f}% "
              f"{h['std']*100:>9.2f}% "
              f"{h['var_5']*100:>9.2f}% "
              f"{h['cvar_5']*100:>9.2f}% "
              f"{h['p95']*100:>9.2f}% "
              f"{h['prob_positive']*100:>6.1f}%")
    print("=" * 100)
    
    print(f"\nMost likely regime at 30d:")
    probs_30d = result['horizons'].get('30d', {}).get('state_probabilities', {})
    for state, p in sorted(probs_30d.items(), key=lambda x: -x[1])[:6]:
        bar = '█' * int(p * 40)
        print(f"  {state:22} {p*100:5.1f}% {bar}")
