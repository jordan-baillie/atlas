#!/usr/bin/env python3
"""Daily risk computation: portfolio VaR, vol cones, regime distributions.

Runs at 9 AM ET (23:00 AEST) after market open. Persists to SQLite.

Usage:
    python3 -m scripts.compute_daily_risk
"""
from __future__ import annotations
import sys
import logging
from pathlib import Path
from datetime import datetime, timedelta

# Path setup
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("compute_daily_risk")


def get_open_positions_with_prices() -> tuple[list[dict], float]:
    """Fetch open positions with current prices from broker, fall back to entry."""
    import json
    import dataclasses
    from db.atlas_db import get_db

    config_path = PROJECT_ROOT / "config" / "active" / "sp500.json"
    with open(config_path) as f:
        config = json.load(f)

    equity = 0.0
    current_prices: dict = {}
    try:
        from brokers.registry import get_live_broker
        broker = get_live_broker(config)
        if broker and broker.connect():
            ai = broker.get_account_info()
            equity = float(ai.equity or 0)
            for p in broker.get_positions():
                pd = dataclasses.asdict(p)
                current_prices[pd.get("ticker", "")] = float(pd.get("current_price", 0) or 0)
    except Exception as e:
        logger.warning("Broker fetch failed: %s — using entry prices", e)

    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, strategy, entry_price, stop_price, shares "
            "FROM trades WHERE exit_date IS NULL"
        ).fetchall()

    positions = []
    for r in rows:
        d = dict(r)
        ticker = d["ticker"]
        positions.append({
            "ticker": ticker,
            "strategy": d.get("strategy"),
            "shares": int(d["shares"] or 0),
            "entry_price": float(d["entry_price"] or 0),
            "current_price": current_prices.get(ticker, float(d["entry_price"] or 0)),
            "stop_price": float(d["stop_price"]) if d.get("stop_price") else None,
        })
    return positions, equity


def main() -> int:
    logger.info("=" * 60)
    logger.info("Daily Risk Compute — start")
    logger.info("=" * 60)

    # 1. Fetch open positions
    try:
        positions, equity = get_open_positions_with_prices()
        logger.info("Loaded %d open positions, equity=$%.2f", len(positions), equity)
    except Exception as e:
        logger.exception("Position load failed: %s", e)
        return 1

    # 2. Get current regime
    try:
        from db.atlas_db import get_current_regime
        regime_data = get_current_regime() or {}
        current_regime = (
            regime_data.get("regime_state")
            or regime_data.get("state")
            or "transition_uncertain"
        )
        logger.info("Current regime: %s", current_regime)
    except Exception as e:
        logger.warning("Regime fetch failed: %s — using transition_uncertain", e)
        current_regime = "transition_uncertain"

    # 3. Compute portfolio VaR
    var_result = None
    if positions and equity > 0:
        try:
            from risk.portfolio_var import compute_portfolio_var_regime_aware, persist_portfolio_var
            var_result = compute_portfolio_var_regime_aware(
                positions=positions,
                current_regime=current_regime,
                lookback_days=60,
                n_paths=10000,
                horizons=(1, 5),
                seed=42,
                equity=equity,
            )
            logger.info(
                "Portfolio VaR computed: ENB=%.2f, 1d VaR95=$%.2f, 5d VaR95=$%.2f",
                var_result.get("effective_bets", 0.0),
                var_result.get("horizons", {}).get("1d", {}).get("var_95", 0.0),
                var_result.get("horizons", {}).get("5d", {}).get("var_95", 0.0),
            )
            try:
                persist_portfolio_var(var_result)
                logger.info("Portfolio VaR persisted to SQLite")
            except Exception as pe:
                logger.warning("Portfolio VaR persist failed: %s", pe)
        except Exception as e:
            logger.exception("Portfolio VaR compute failed: %s", e)
    else:
        logger.warning("Skipping VaR — no positions or zero equity")

    # 4. Update vol cones for each open position
    vol_cone_count = 0
    try:
        from indicators.vol_cones import compute_vol_cone, persist_vol_cone
        for p in positions:
            try:
                vc = compute_vol_cone(p["ticker"])
                if vc and not vc.get("error"):
                    persist_vol_cone(vc)
                    vol_cone_count += 1
            except Exception as ve:
                logger.warning("Vol cone failed for %s: %s", p["ticker"], ve)
        logger.info("Vol cones updated for %d/%d positions", vol_cone_count, len(positions))
    except Exception as e:
        logger.exception("Vol cone batch failed: %s", e)

    # 5. Refresh regime distributions if stale (>7 days)
    try:
        from db.atlas_db import get_db
        from regime.distributions import RegimeDistributions

        stale = True
        try:
            with get_db() as db:
                row = db.execute(
                    "SELECT MAX(updated_at) AS last FROM regime_distributions"
                ).fetchone()
                if row and row["last"]:
                    last = datetime.fromisoformat(str(row["last"]).split(".")[0])
                    age_days = (datetime.utcnow() - last).days
                    stale = age_days > 7
                    logger.info("Regime distributions age: %d days (stale=%s)", age_days, stale)
        except Exception:
            stale = True

        if stale:
            logger.info("Refreshing regime distributions...")
            rd = RegimeDistributions()
            rd.fit(lookback_years=10)
            logger.info("Regime distributions refreshed")
        else:
            logger.info("Regime distributions are fresh — skipping")
    except Exception as e:
        logger.exception("Regime distributions refresh failed: %s", e)

    # 6. Compute strategy EV scoring
    try:
        from signals.ev_scorer import compute_all_strategies_ev, persist_strategy_ev
        ev_results = compute_all_strategies_ev(min_trades=3)
        n_persisted = persist_strategy_ev(ev_results)
        logger.info("Strategy EV computed for %d strategies (%d persisted)", len(ev_results), n_persisted)
    except Exception as e:
        logger.exception("Strategy EV compute failed: %s", e)

    # 7. Regime forward Monte Carlo forecast
    try:
        from regime.forward_mc import simulate_return_paths_from_regime, persist_forecast
        forecast_result = simulate_return_paths_from_regime(
            current_regime, n_paths=5000, n_days=90, seed=42
        )
        persist_forecast(forecast_result)
        h30 = forecast_result.get("horizons", {}).get("30d", {})
        logger.info(
            "Regime forecast: 30d E[R]=%.2f%%, P(+)=%.0f%%",
            (h30.get("expected_return") or 0) * 100,
            (h30.get("prob_positive") or 0) * 100,
        )
    except Exception as e:
        logger.exception("Regime forecast failed: %s", e)

    # 8. Portfolio probability of ruin
    try:
        from risk.ruin_probability import compute_for_current_portfolio, persist_ruin_probability
        ruin_result = compute_for_current_portfolio(floor_pct=0.70)
        persist_ruin_probability(ruin_result)
        h90 = ruin_result.get("horizons", {}).get("90d", {})
        logger.info(
            "Ruin probability: 90d=%.2f%% (floor=$%.0f)",
            (h90.get("prob_ruin") or 0) * 100,
            ruin_result.get("floor", 0),
        )
    except Exception as e:
        logger.exception("Ruin probability failed: %s", e)

    logger.info("=" * 60)
    logger.info("Daily Risk Compute — done")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
