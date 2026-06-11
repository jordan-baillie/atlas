"""Strategy EV (expected value) analytics.

Computes probability-weighted expected value per strategy based on closed
trades. Writes results to the `signal_ev` SQLite table. Consumed by
dashboard, risk API, and daily risk-report cron — NOT by the live signal
pipeline. Relocated from signals/ev_scorer.py 2026-05-11 (it has no
generate_signal()).
"""
import logging
from datetime import datetime, timezone
from typing import Optional
import numpy as np

from atlas.db import get_db

logger = logging.getLogger(__name__)


def compute_strategy_ev(strategy: str, min_trades: int = 5) -> dict:
    """Compute EV metrics for a single strategy from historical trades."""
    with get_db() as db:
        trades = db.execute("""
            SELECT ticker, pnl, pnl_pct, entry_date, exit_date
            FROM trades
            WHERE strategy = ? AND exit_date IS NOT NULL AND pnl IS NOT NULL
              AND (superseded=0 OR superseded IS NULL)
            ORDER BY exit_date
        """, (strategy,)).fetchall()
    
    if len(trades) < min_trades:
        return {
            "strategy": strategy,
            "n_trades": len(trades),
            "status": "insufficient_data",
            "min_required": min_trades,
            "ev_per_trade": 0.0,
            "ev_per_trade_pct": 0.0,
            "classification": "unknown",
        }
    
    pnls = np.array([t['pnl'] for t in trades])
    pnl_pcts = np.array([t['pnl_pct'] for t in trades if t['pnl_pct'] is not None])
    
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    
    n = len(pnls)
    n_wins = len(wins)
    n_losses = len(losses)
    
    win_rate = n_wins / n if n > 0 else 0.0
    avg_win = float(wins.mean()) if len(wins) > 0 else 0.0
    avg_loss = float(losses.mean()) if len(losses) > 0 else 0.0  # negative
    
    # EV = p_win * avg_win + p_loss * avg_loss (avg_loss is negative)
    ev = win_rate * avg_win + (1 - win_rate) * avg_loss
    
    # Profit factor
    total_wins = float(wins.sum()) if len(wins) > 0 else 0.0
    total_losses = float(abs(losses.sum())) if len(losses) > 0 else 0.0
    profit_factor = total_wins / total_losses if total_losses > 0 else None
    
    # Bootstrap CI for EV
    rng = np.random.default_rng(42)
    n_boot = 1000
    boot_evs = []
    for _ in range(n_boot):
        sample = rng.choice(pnls, size=n, replace=True)
        boot_evs.append(float(sample.mean()))
    boot_evs = np.array(boot_evs)
    ci_low = float(np.percentile(boot_evs, 2.5))
    ci_high = float(np.percentile(boot_evs, 97.5))
    
    # Classification
    if ci_low > 0:
        classification = "positive"
    elif ci_high < 0:
        classification = "negative"
    else:
        classification = "uncertain"
    
    ev_pct = float(pnl_pcts.mean()) if len(pnl_pcts) > 0 else 0.0
    
    return {
        "strategy": strategy,
        "n_trades": n,
        "n_wins": n_wins,
        "n_losses": n_losses,
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "ev_per_trade": ev,
        "ev_per_trade_pct": ev_pct,
        "profit_factor": profit_factor,
        "ci_low": ci_low,
        "ci_high": ci_high,
        "classification": classification,
        "status": "ok",
    }


def compute_all_strategies_ev(min_trades: int = 5) -> list:
    """Compute EV for all strategies."""
    with get_db() as db:
        strategies = db.execute("""
            SELECT DISTINCT strategy FROM trades 
            WHERE strategy IS NOT NULL AND exit_date IS NOT NULL
        """).fetchall()
    
    results = []
    for row in strategies:
        try:
            ev = compute_strategy_ev(row['strategy'], min_trades=min_trades)
            results.append(ev)
        except Exception as e:
            logger.error(f"EV computation failed for {row['strategy']}: {e}")
    
    results.sort(key=lambda r: r.get('ev_per_trade', 0), reverse=True)
    return results


def persist_strategy_ev(results: list) -> int:
    """Store EV results in signal_ev table."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS signal_ev (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                as_of TEXT NOT NULL,
                strategy TEXT NOT NULL,
                n_trades INTEGER NOT NULL,
                n_wins INTEGER,
                n_losses INTEGER,
                win_rate REAL,
                avg_win REAL,
                avg_loss REAL,
                ev_per_trade REAL,
                ev_per_trade_pct REAL,
                profit_factor REAL,
                ci_low REAL,
                ci_high REAL,
                classification TEXT,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(as_of, strategy)
            )
        """)
        
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        count = 0
        for r in results:
            try:
                db.execute("""
                    INSERT OR REPLACE INTO signal_ev 
                    (as_of, strategy, n_trades, n_wins, n_losses, win_rate, avg_win, avg_loss,
                     ev_per_trade, ev_per_trade_pct, profit_factor, ci_low, ci_high, classification, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    as_of, r['strategy'], r['n_trades'], r.get('n_wins'), r.get('n_losses'),
                    r.get('win_rate'), r.get('avg_win'), r.get('avg_loss'),
                    r.get('ev_per_trade'), r.get('ev_per_trade_pct'), r.get('profit_factor'),
                    r.get('ci_low'), r.get('ci_high'), r.get('classification'), r.get('status')
                ))
                count += 1
            except Exception as e:
                logger.error(f"Persist failed for {r['strategy']}: {e}")
        db.commit()
    return count


def get_latest_ev_stats() -> dict:
    """Return latest EV stats keyed by strategy (for API consumption)."""
    with get_db() as db:
        rows = db.execute("""
            SELECT * FROM signal_ev 
            WHERE as_of = (SELECT MAX(as_of) FROM signal_ev)
        """).fetchall()
    
    return {r['strategy']: dict(r) for r in rows}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = compute_all_strategies_ev(min_trades=3)
    persisted = persist_strategy_ev(results)
    
    print(f"\nSIGNAL EV SCORING — {len(results)} strategies ({persisted} persisted)")
    print("=" * 105)
    print(f"{'Strategy':20} {'N':>5} {'WR':>7} {'AvgWin':>10} {'AvgLoss':>10} {'EV/Tr':>10} {'PF':>7} {'CI Low':>10} {'CI High':>10} {'Class':>12}")
    print("-" * 105)
    for r in results:
        if r.get('status') != 'ok':
            print(f"{r['strategy']:20} {r['n_trades']:>5}  insufficient (need {r.get('min_required', 5)})")
            continue
        pf = f"{r['profit_factor']:.2f}" if r['profit_factor'] else "inf"
        print(f"{r['strategy']:20} {r['n_trades']:>5} {r['win_rate']*100:>6.1f}% "
              f"{r['avg_win']:>10.2f} {r['avg_loss']:>10.2f} {r['ev_per_trade']:>10.2f} "
              f"{pf:>7} {r['ci_low']:>10.2f} {r['ci_high']:>10.2f} {r['classification']:>12}")
    print("=" * 105)
