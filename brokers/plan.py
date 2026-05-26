"""TradePlanGenerator — generates daily trade plans for approval.

Plans are saved to plans/ at the project root.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.allocation import build_allocation_pool


def _get_latest_overlay() -> Optional[dict]:
    """Return the most recent overlay decision from the last 24 h, or None.

    Non-fatal — any error returns None so plan generation is never blocked.
    """
    try:
        from db.atlas_db import get_overlay_decisions
        decisions = get_overlay_decisions(days=1)
        return decisions[0] if decisions else None
    except Exception as exc:
        logger.debug("_get_latest_overlay: %s", exc)
        return None

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent


class PlanAlreadyRejectedError(Exception):
    """Raised when approve_plan() is called on a plan that is already REJECTED.

    Re-approving a rejected plan is a configuration error — an explicit
    rejection must not be silently overridden.  Caller must explicitly
    reset status (via reject→re-generate→approve flow) if intent is to
    approve.
    """
    pass


class TradePlanGenerator:
    """Generates daily trade plans for approval."""

    PLANS_DIR = "plans"

    def __init__(self, portfolio, config: dict):
        self.portfolio = portfolio
        self.config = config

    def generate_plan(self, signals: list, exit_recommendations: list,
                      prices: dict, trade_date: str) -> dict:
        """Generate a daily trade plan."""
        # Filter signals for tickers that are tradable on the broker
        try:
            from brokers.alpaca.tradable_assets import is_tradable
            original_count = len(signals)
            signals = [s for s in signals if is_tradable(s.ticker)]
            filtered = original_count - len(signals)
            if filtered:
                logger.info(
                    "Filtered %d signals for untradable tickers (%d remaining)",
                    filtered, len(signals),
                )
        except Exception as e:
            logger.debug("Tradability filter unavailable: %s", e)

        # Build allocation pool (no-op when allocation.enabled=false)
        allocation_pool = build_allocation_pool(self.config)

        # Lifecycle manager — reduce pool caps for degraded strategies
        lifecycle_mgr = None
        try:
            from monitor.lifecycle import StrategyLifecycleManager
            lifecycle_mgr = StrategyLifecycleManager(self.config)
        except Exception as e:
            logger.warning(f"StrategyLifecycleManager unavailable, skipping lifecycle caps: {e}")

        if lifecycle_mgr and allocation_pool.is_enabled():
            for strat_name in list(allocation_pool.pools.keys()):
                if strat_name == '_other':
                    continue
                override = lifecycle_mgr.get_effective_pool_cap(strat_name)
                if override is not None:
                    original = allocation_pool.pools[strat_name]
                    allocation_pool.pools[strat_name] = override
                    if override != original:
                        logger.info(
                            "Lifecycle override: %s pool cap %d → %d",
                            strat_name, original, override,
                        )

        # ── Sector concentration cap pre-pass (RCA #2C) ────────────────────
        # check_risk_limits only checks existing broker positions — it's blind
        # to other candidates being accepted in the same plan cycle. This
        # pre-pass sorts candidates by confidence DESC and caps per sector,
        # counting existing positions + already-accepted candidates together.
        rejected_entries_pre_list: list[dict] = []
        try:
            from risk.sector_cap import apply_sector_cap
            _sector_cap_limit = self.config.get("risk", {}).get("max_sector_concentration", 999)
            _existing_pos_dicts = [
                {"sector": getattr(p, "sector", "Unknown") or "Unknown"}
                for p in self.portfolio.positions
            ]
            _signal_candidates = [
                {
                    "ticker": s.ticker,
                    "sector": getattr(s, "sector", "Unknown") or "Unknown",
                    "confidence": getattr(s, "confidence", 0.0),
                }
                for s in signals
            ]
            _accepted_tickers = {
                c["ticker"]
                for c in apply_sector_cap(_signal_candidates, _existing_pos_dicts, _sector_cap_limit)
            }
            _sector_rejected = [s for s in signals if s.ticker not in _accepted_tickers]
            for _s in _sector_rejected:
                rejected_entries_pre = {
                    "ticker": _s.ticker,
                    "strategy": _s.strategy,
                    "entry_price": _s.entry_price,
                    "stop_price": _s.stop_price,
                    "take_profit": _s.take_profit,
                    "position_size": _s.position_size,
                    "position_value": round(_s.entry_price * _s.position_size, 2),
                    "risk_amount": round(abs(_s.entry_price - _s.stop_price) * _s.position_size, 2),
                    "confidence": _s.confidence,
                    "rationale": getattr(_s, "rationale", ""),
                    "features": getattr(_s, "features", {}),
                    "sector": getattr(_s, "sector", "Unknown"),
                    "market_id": getattr(_s, "market_id", self.config.get("market", "")),
                    "rejection_reason": (
                        f"Sector concentration cap ({_sector_cap_limit}) for "
                        f"sector={getattr(_s, 'sector', 'Unknown') or 'Unknown'}"
                    ),
                }
                rejected_entries_pre_list.append(rejected_entries_pre)
            signals = [s for s in signals if s.ticker in _accepted_tickers]
            if _sector_rejected:
                logger.info(
                    "Sector cap pre-pass: %d signals rejected (%d accepted), cap=%d",
                    len(_sector_rejected), len(signals), _sector_cap_limit,
                )
        except Exception as _exc:
            logger.warning("Sector cap pre-pass failed (non-fatal, skipping): %s", _exc)
        # ──────────────────────────────────────────────────────────────────

        # Risk check each signal
        proposed_entries = []
        rejected_entries = list(rejected_entries_pre_list)
        min_confidence = self.config.get("risk", {}).get("min_confidence", 0.0)
        max_positions = self.config.get("risk", {}).get("max_open_positions", 5)
        available_slots = max_positions - len(self.portfolio.positions)

        # ── RCA #4B: Position replacement at limit ─────────────────────────────
        ## Position replacement policy (RCA #4B)
        ## When `risk.enable_position_replacement: true` AND len(positions) >= max_open_positions:
        ##   - Find the worst-PnL existing position (lowest unrealized_pnl)
        ##   - Find the best candidate signal (highest confidence)
        ##   - If candidate confidence > existing position confidence at entry,
        ##     queue an exit of the worst position AND admit the new candidate
        ##   - Replacement happens across cycles: exit fills first, new entry next cycle
        ##   - Logged at WARNING with full context for audit
        _enable_replacement = self.config.get("risk", {}).get("enable_position_replacement", False)
        # Make a mutable copy so synthetic replacement exits can be appended
        proposed_exits = list(exit_recommendations)

        if _enable_replacement and len(self.portfolio.positions) >= max_positions and signals:
            # Identify worst-PnL existing position (uses prices dict for live PnL)
            _existing_with_pnl = [
                (
                    p,
                    p.unrealized_pnl(prices.get(p.ticker, p.entry_price))
                    if callable(getattr(p, "unrealized_pnl", None))
                    else (getattr(p, "unrealized_pnl", 0.0) or 0.0),
                )
                for p in self.portfolio.positions
            ]
            if _existing_with_pnl:
                _worst_pos, _worst_pnl = min(_existing_with_pnl, key=lambda x: x[1])
                _best_candidate = max(signals, key=lambda s: getattr(s, "confidence", 0.0))
                _worst_strength = getattr(_worst_pos, "confidence", 0.0) or 0.0
                _new_strength = getattr(_best_candidate, "confidence", 0.0) or 0.0
                if _new_strength > _worst_strength:
                    logger.warning(
                        "POSITION_REPLACEMENT: at limit (%d) — exiting %s "
                        "(pnl=%.2f, conf=%.2f) to admit %s (conf=%.2f)",
                        max_positions,
                        getattr(_worst_pos, "ticker", "?"),
                        _worst_pnl,
                        _worst_strength,
                        _best_candidate.ticker,
                        _new_strength,
                    )
                    proposed_exits.append({
                        "ticker": getattr(_worst_pos, "ticker", ""),
                        "reason": "position_replacement",
                        "shares": getattr(_worst_pos, "shares", 0),
                        "details": (
                            f"Replaced by {_best_candidate.ticker} "
                            f"(conf {_new_strength:.2f} > {_worst_strength:.2f})"
                        ),
                    })
                    # Free one slot so the replacement candidate can pass the cap check
                    available_slots += 1
        # ────────────────────────────────────────────────────────────────────────

        for signal in signals:
            # Build a rich entry dict with all signal data for future analysis
            base_entry = {
                "ticker": signal.ticker,
                "strategy": signal.strategy,
                "entry_price": signal.entry_price,
                "stop_price": signal.stop_price,
                "take_profit": signal.take_profit,
                "position_size": signal.position_size,
                "position_value": round(signal.entry_price * signal.position_size, 2),
                "risk_amount": round(abs(signal.entry_price - signal.stop_price) * signal.position_size, 2),
                "confidence": signal.confidence,
                "rationale": signal.rationale,
                "features": getattr(signal, "features", {}),
                "sector": getattr(signal, "sector", "Unknown"),
                "market_id": getattr(signal, "market_id", self.config.get("market", "")),
            }

            # Cap entries at available position slots
            if len(proposed_entries) >= available_slots:
                base_entry["rejection_reason"] = f"Max positions ({max_positions}) would be exceeded"
                rejected_entries.append(base_entry)
                continue

            # Filter by minimum confidence threshold
            if signal.confidence < min_confidence:
                base_entry["rejection_reason"] = f"Confidence {signal.confidence:.3f} below threshold {min_confidence}"
                rejected_entries.append(base_entry)
                continue

            # Simulate proposed positions for pool check (portfolio positions + already proposed)
            proposed_pos_dicts = [{"strategy": e["strategy"]} for e in proposed_entries]
            passed, reason = self.portfolio.check_risk_limits(signal, allocation_pool=allocation_pool)
            # Additional pool check against already-proposed entries in this plan
            if passed and allocation_pool.is_enabled():
                live_pos_dicts = [{"strategy": p.strategy} for p in self.portfolio.positions]
                combined_pos = live_pos_dicts + proposed_pos_dicts
                pool_ok, pool_reason = allocation_pool.can_accept(signal.strategy, combined_pos)
                if not pool_ok:
                    passed = False
                    reason = pool_reason

            if passed:
                proposed_entries.append(base_entry)
            else:
                base_entry["rejection_reason"] = reason
                rejected_entries.append(base_entry)

        # Event calendar warnings (info-only — does NOT reject signals)
        event_cal_cfg = self.config.get("event_calendar", {})
        if event_cal_cfg.get("enabled", False) and event_cal_cfg.get("warn_in_plan", True):
            try:
                from data.events import EventCalendar
                ec = EventCalendar()
                _trade_date_parsed = None
                try:
                    from datetime import date as _date
                    _trade_date_parsed = _date.fromisoformat(trade_date)
                except Exception as e:
                    logger.warning(f"Trade date parse failed, using raw string for event lookup: {e}")
                for entry in proposed_entries:
                    ref_date = trade_date if _trade_date_parsed is None else trade_date
                    nearby = ec.get_events_near(ref_date, window_days=3)
                    if nearby:
                        warnings = []
                        for ev in nearby:
                            ref = _trade_date_parsed or __import__("datetime").date.today()
                            days_away = (ev.date - ref).days
                            warnings.append({
                                "type": ev.event_type,
                                "date": ev.date.isoformat(),
                                "days_away": days_away,
                                "impact": ev.impact,
                                "description": ev.description,
                            })
                            logger.info(
                                "Event warning for %s: %s in %d days",
                                entry["ticker"], ev.event_type, days_away,
                            )
                        entry["event_warnings"] = warnings
            except Exception as exc:
                logger.debug("Event calendar integration skipped: %s", exc)

        # Entry refinement (if enabled) — refine entry prices using intraday bars
        if self.config.get("intraday", {}).get("entry_refinement", False):
            try:
                from data.intraday import download_intraday_bars
                from strategies.entry_optimizer import refine_entry_prices

                plan_tickers = [e["ticker"] for e in proposed_entries]
                if plan_tickers:
                    intraday = download_intraday_bars(plan_tickers, config=self.config)
                    refinements = refine_entry_prices(proposed_entries, intraday, self.config)
                    for entry, ref in zip(proposed_entries, refinements):
                        entry["order_type"] = ref.order_type
                        entry["limit_price"] = ref.limit_price
                        entry["entry_refinement"] = ref.reason
                        if ref.order_type == "limit" and ref.limit_price:
                            logger.info(
                                "Entry refined: %s limit @ %.2f (%s)",
                                ref.ticker, ref.limit_price, ref.reason,
                            )
            except Exception as e:
                logger.warning("Entry refinement failed, using market orders: %s", e)

        # ── Volatility scaling (portfolio-level position size adjustment) ──
        vol_scale_applied = 1.0
        try:
            from backtest.vol_scaling import VolatilityScaler
            vol_scaler = VolatilityScaler(self.config)
            if vol_scaler.enabled:
                from db import atlas_db
                market_id = self.config.get("market", "sp500")
                eq_curve = atlas_db.get_equity_curve(market_id)
                if eq_curve:
                    equities = [row["equity"] for row in eq_curve if row.get("equity")]
                    for i in range(1, len(equities)):
                        prev_eq = equities[i - 1]
                        if prev_eq > 0:
                            vol_scaler.update((equities[i] - prev_eq) / prev_eq)
                    scale = vol_scaler.scale_factor()
                    logger.info("Vol scaling: scale=%.4f, returns=%d, lookback=%d, conditional=%s",
                                scale, len(vol_scaler._returns), vol_scaler.lookback, vol_scaler.conditional)
                    if scale < 1.0:
                        vol_scale_applied = scale
                        logger.info(
                            "Vol scaling: reducing position sizes by factor %.3f "
                            "(%d equity curve points)",
                            scale, len(equities),
                        )
                        for entry in proposed_entries:
                            original_size = entry["position_size"]
                            entry["position_size"] = max(1, int(original_size * scale))
                            entry["position_value"] = round(
                                entry["entry_price"] * entry["position_size"], 2
                            )
                            entry["risk_amount"] = round(
                                abs(entry["entry_price"] - entry["stop_price"])
                                * entry["position_size"],
                                2,
                            )
                            entry["vol_scale_applied"] = round(scale, 4)
                            entry["vol_scale_original_size"] = original_size
                else:
                    logger.debug("Vol scaling: no equity curve data yet — skipping")
        except Exception as exc:
            logger.warning("Vol scaling failed (non-fatal, sizes unchanged): %s", exc)

        # Portfolio state after proposed trades
        proposed_cost = sum(e["entry_price"] * e["position_size"] for e in proposed_entries)
        proposed_risk = sum(e["risk_amount"] for e in proposed_entries)

        current_eq = self.portfolio.equity(prices)
        summary = self.portfolio.portfolio_summary(prices)

        # Use Atlas-only positions for plan metrics (exclude manual positions)
        atlas_positions = (self.portfolio.atlas_positions
                           if hasattr(self.portfolio, 'atlas_positions')
                           else self.portfolio.positions)
        atlas_open = [op for op in summary["open_positions"]
                      if op.get("strategy", "unknown") not in ("unknown", "")]
        n_atlas = len(atlas_positions)

        # Per-market portfolio exposure: (current open MV + proposed cost) / per-market equity.
        # NOTE: prior formula assumed current_eq - cash = positions_value, which is only
        # true for single-account global accounting. Under per-market accounting, current_eq
        # is the per-market slice while self.portfolio.cash is the FULL broker cash, so the
        # subtraction produced negative leverage values (e.g. -121% sp500 on 2026-05-06).
        # Compute positions_value directly from open positions instead.
        current_positions_value = sum(
            p.shares * prices.get(p.ticker, p.entry_price)
            for p in self.portfolio.positions
        )

        market_id = self.config.get("market", "")
        plan = {
            "trade_date": trade_date,
            "generated_at": datetime.now().isoformat(),
            "market_id": market_id,
            "config_version": self.config.get("version", ""),
            "status": "PENDING_APPROVAL",
            "portfolio_snapshot": {
                "equity": current_eq,
                "cash": self.portfolio.cash,
                "open_positions": n_atlas,
                "total_pnl": summary["total_pnl"],
                "total_pnl_pct": summary["total_pnl_pct"],
            },
            "proposed_entries": proposed_entries,
            "rejected_entries": rejected_entries,
            "proposed_exits": proposed_exits,
            "total_signals_generated": len(signals),
            "risk_summary": {
                "total_proposed_cost": round(proposed_cost, 2),
                "total_proposed_risk": round(proposed_risk, 2),
                "risk_pct_of_equity": round(proposed_risk / current_eq * 100, 2) if current_eq > 0 else 0,
                "positions_after": n_atlas + len(proposed_entries) - len(proposed_exits),
                "cash_after_entries": round(self.portfolio.cash - proposed_cost, 2),
                "portfolio_exposure_pct": round((current_positions_value + proposed_cost) / current_eq * 100, 2) if current_eq > 0 else 0,
            },
            "open_positions": atlas_open if atlas_open else summary["open_positions"],
            "allocation_summary": allocation_pool.counts_summary(
                [{"strategy": p.strategy} for p in self.portfolio.positions]
            ) if allocation_pool.is_enabled() else {},
            "vol_scale_applied": vol_scale_applied,
        }

        # Save plan
        self._save_plan(plan, trade_date)
        return plan

    def _save_plan(self, plan: dict, trade_date: str):
        market_id = plan.get("market_id", "") or self.config.get("market", "")
        plans_dir = PROJECT_ROOT / self.PLANS_DIR
        plans_dir.mkdir(parents=True, exist_ok=True)
        # Per-market plan file (e.g. plan_asx_2026-03-02.json)
        if market_id:
            path = plans_dir / f"plan_{market_id}_{trade_date}.json"
        else:
            path = plans_dir / f"plan_{trade_date}.json"
        with open(path, "w") as f:
            json.dump(plan, f, indent=2, default=str)
        logger.info(f"Trade plan saved: {path}")
        # SQLite dual-write (non-fatal — JSON file is source of truth)
        # UPDATE-or-INSERT: look up the existing row first so every status
        # transition (pending_approval -> approved -> executed) writes to the
        # SAME row instead of inserting a duplicate.
        try:
            from db import atlas_db
            plan_status = plan.get("status", "PENDING_APPROVAL").lower()
            db_market_id = market_id or "sp500"
            existing = atlas_db.get_plan(trade_date, db_market_id)
            if existing:
                atlas_db.update_plan(
                    existing["id"],
                    status=plan_status,
                    approved_at=plan.get("approved_at"),
                    executed_at=plan.get("executed_at"),
                    plan_data=plan,
                )
            else:
                atlas_db.record_plan(
                    date=trade_date,
                    market_id=db_market_id,
                    plan_data=plan,
                    status=plan_status,
                )
        except Exception as e:
            logger.warning(f"SQLite plan dual-write failed: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Regime-aware plan generation pipeline
    # ──────────────────────────────────────────────────────────────────────

    def generate_regime_plan(
        self,
        strategies: list,
        prices: dict,
        trade_date: str,
        equity: float,
        existing_positions: list = None,
        exit_recommendations: list = None,
        sp500_data: dict = None,
    ) -> dict:
        """Orchestrate signal generation and plan building with an optional regime layer.

        Config switch: ``config.get("regime_enabled", False)``

        **False (default)**:
            Run *strategies* against *sp500_data* and call :meth:`generate_plan` —
            identical to the existing ``cli.py`` flow.  *sp500_data* should be the
            same ``{ticker: DataFrame}`` dict that cli.py loads from cache.

        **True**:
            a. Read current regime via ``RegimeModel().classify_current()``.
            b. Derive ``active_universes`` from the regime classification.
            c. Load universe data: ``build_multi_universe(active_universes)``.
            d. Filter *strategies* to those matching the regime's
               ``enabled_strategies`` list (``["all"]`` → run all).
            e. Run each active strategy on each universe's data.
            f. Tag every signal: ``signal.universe = universe_name``.
            g. Route all signals through :class:`PortfolioConstructor`.
            h. Enrich the returned plan dict with regime metadata.

        On any regime-layer exception the method logs a warning and falls back
        to the SP500-only path (same as ``regime_enabled=False``).

        Parameters
        ----------
        strategies:
            Instantiated strategy objects (each exposes ``.name`` and
            ``.generate_signals(data, equity, existing_positions)``).
        prices:
            Latest close prices ``{ticker: float}``.
        trade_date:
            ISO date string (``"YYYY-MM-DD"``).
        equity:
            Current portfolio equity in USD.
        existing_positions:
            Open position objects or dicts from ``portfolio.positions``.
            Defaults to empty list.
        exit_recommendations:
            Pre-computed exit recommendations list.  Defaults to empty list.
        sp500_data:
            Pre-loaded ``{ticker: DataFrame}`` data for the SP500-only fallback
            path.  Ignored when ``regime_enabled=True`` (data is loaded
            internally).  Defaults to empty dict.
        """
        existing_positions = existing_positions or []
        exit_recommendations = exit_recommendations or []
        sp500_data = sp500_data or {}

        if not self.config.get("regime_enabled", False):
            plan = self._run_sp500_plan(
                strategies, sp500_data, prices, trade_date, equity,
                existing_positions, exit_recommendations,
            )
        else:
            # ── Regime gate: reject all signals if regime data unavailable/stale ──
            regime_gate_cfg = self.config.get("regime_gate", {})
            regime_gate_enabled = regime_gate_cfg.get("enabled", True)
            max_stale_days = regime_gate_cfg.get("max_stale_days", 2)

            if regime_gate_enabled:
                gate_passed = False
                gate_reason = ""
                try:
                    from regime.model import RegimeModel
                    _gate_model = RegimeModel()
                    _gate_regime = _gate_model.classify_current()
                    regime_date_str = _gate_regime.date
                    if not regime_date_str:
                        gate_reason = "regime classification date is empty/NULL"
                    else:
                        from datetime import datetime as _dt, timedelta as _td
                        regime_date = _dt.strptime(regime_date_str, "%Y-%m-%d").date()
                        today = _dt.strptime(trade_date, "%Y-%m-%d").date()
                        staleness = (today - regime_date).days
                        if staleness > max_stale_days:
                            gate_reason = (
                                f"regime data is {staleness} days stale "
                                f"(last: {regime_date_str}, max: {max_stale_days})"
                            )
                        else:
                            gate_passed = True
                except Exception as exc:
                    gate_reason = f"regime classification failed: {exc}"

                if not gate_passed:
                    n_signals = len(strategies)  # count of strategies as proxy
                    logger.warning(
                        "REGIME GATE: Rejecting all signals — %s", gate_reason
                    )
                    plan = self.generate_plan(
                        [], exit_recommendations, prices, trade_date
                    )
                    plan["regime_gate_blocked"] = True
                    plan["regime_gate_reason"] = gate_reason
                    self._save_plan(plan, trade_date)
                    return plan

            # Regime-aware path — fall back to SP500-only on any error.
            try:
                plan = self._run_regime_aware_plan(
                    strategies, prices, trade_date, equity,
                    existing_positions, exit_recommendations,
                )
            except Exception as exc:
                import traceback
                logger.warning(
                    "Regime-aware plan generation failed (%s) — "
                    "falling back to SP500-only mode.\n%s",
                    exc,
                    traceback.format_exc(),
                )
                plan = self._run_sp500_plan(
                    strategies, sp500_data, prices, trade_date, equity,
                    existing_positions, exit_recommendations,
                )

        # Annotate plan with latest overlay decision (log-only — never modifies signals).
        try:
            overlay = _get_latest_overlay()
            if overlay:
                plan["overlay_context"] = {
                    "action": overlay.get("action", "no_change"),
                    "sizing_override": overlay.get("sizing_override"),
                    "universes_deactivated": overlay.get("universes_deactivated") or [],
                    "tickers_to_avoid": overlay.get("tickers_avoided") or [],
                    "reasoning": overlay.get("reasoning", ""),
                    "confidence": overlay.get("confidence"),
                }
                logger.info(
                    "Overlay context attached to plan: action=%s confidence=%s",
                    plan["overlay_context"]["action"],
                    plan["overlay_context"]["confidence"],
                )
            else:
                logger.debug("No overlay decision in last 24h — plan unannotated")
        except Exception as exc:
            logger.warning("Overlay annotation failed (non-fatal): %s", exc)

        return plan

    def _run_sp500_plan(
        self,
        strategies: list,
        sp500_data: dict,
        prices: dict,
        trade_date: str,
        equity: float,
        existing_positions: list,
        exit_recommendations: list,
    ) -> dict:
        """Original SP500-only plan pipeline (regime disabled).

        Runs each strategy against *sp500_data*, collects signals, and
        delegates to :meth:`generate_plan`.  Identical in behaviour to the
        current ``cli.py`` flow.
        """
        all_signals: list = []
        all_filter_rejections: list = []
        for strat in strategies:
            try:
                # Precompute indicators if strategy supports it
                if hasattr(strat, 'precompute') and not getattr(strat, '_precomputed', False):
                    strat.precompute(sp500_data)
                sigs = strat.generate_signals(sp500_data, equity, existing_positions)
                all_signals.extend(sigs)
                # Collect filter rejections from strategy (e.g. sma200_filter)
                _filt = getattr(strat, "last_filter_rejections", []) or []
                all_filter_rejections.extend(_filt)
            except Exception as exc:
                logger.error("Strategy %s error: %s", strat.name, exc)

        # Signal enrichment (breadth, RS, earnings blackout)
        try:
            from utils.signal_enrichment import enrich_signals
            logger.info("Enriching %d raw SP500 signals...", len(all_signals))
            all_signals = enrich_signals(all_signals, sp500_data, self.config, trade_date)
            logger.info("%d signals after enrichment", len(all_signals))
        except Exception as exc:
            logger.warning("Signal enrichment failed (non-fatal): %s", exc)

        # Sector map enrichment
        try:
            import json as _json
            _sector_map = {}
            market_id = self.config.get("market", "sp500")
            for _sm_path in [
                PROJECT_ROOT / "data" / "processed" / f"sector_map_{market_id}.json",
                PROJECT_ROOT / "data" / "processed" / "sector_map.json",
            ]:
                if _sm_path.exists():
                    with open(_sm_path) as _f:
                        _sector_map = _json.load(_f)
                    break
            if _sector_map:
                for sig in all_signals:
                    sector = _sector_map.get(sig.ticker, "Unknown")
                    sig.sector = sector
                    if hasattr(sig, 'features'):
                        sig.features["sector"] = sector
        except Exception as exc:
            logger.debug("Sector map enrichment skipped: %s", exc)

        # Sort by confidence descending before plan construction
        all_signals.sort(key=lambda s: s.confidence, reverse=True)

        plan = self.generate_plan(all_signals, exit_recommendations, prices, trade_date)
        # Merge strategy filter rejections (e.g. sma200_filter) into plan.rejected_entries
        if all_filter_rejections:
            _market_id = self.config.get("market", "")
            _normalized: list = []
            for _rej in all_filter_rejections:
                _normalized.append({
                    "ticker": _rej.get("ticker", ""),
                    "strategy": _rej.get("strategy", ""),
                    "entry_price": _rej.get("entry_price", 0.0),
                    "stop_price": _rej.get("stop_price", 0.0),
                    "take_profit": _rej.get("take_profit", None),
                    "position_size": _rej.get("position_size", 0),
                    "position_value": _rej.get("position_value", 0.0),
                    "risk_amount": _rej.get("risk_amount", 0.0),
                    "confidence": _rej.get("confidence", 0.0),
                    "rationale": _rej.get("rationale", ""),
                    "features": _rej.get("features", {}),
                    "sector": _rej.get("sector", "Unknown"),
                    "market_id": _rej.get("market_id", _market_id),
                    "rejection_reason": _rej.get("rejection_reason", "strategy_filter"),
                    "rejection_detail": _rej.get("rejection_detail", {}),
                })
            plan["rejected_entries"] = plan.get("rejected_entries", []) + _normalized
            logger.info(
                "plan: merged %d strategy filter rejections into rejected_entries",
                len(_normalized),
            )
        return plan

    def _run_regime_aware_plan(
        self,
        strategies: list,
        prices: dict,
        trade_date: str,
        equity: float,
        existing_positions: list,
        exit_recommendations: list,
    ) -> dict:
        """Execute the full regime-aware plan pipeline.

        Raises any exception so that :meth:`generate_regime_plan` can catch
        it and fall back to SP500-only mode.
        """
        # Late imports keep the regime/universe modules optional when running
        # in SP500-only mode and avoid circular-import issues at module load.
        from regime.model import RegimeModel
        from universe.builder import build_multi_universe
        from portfolio.constructor import PortfolioConstructor
        from portfolio.limits import resolve_universe_limits

        # a. Classify current regime.
        model = RegimeModel()
        regime = model.classify_current()
        logger.info(
            "Regime classified: %s (universes=%s, sizing=%.2f)",
            regime.state.value, regime.active_universes, regime.sizing_multiplier,
        )

        # b. Get active universes from the classification.
        active_universes: list = regime.active_universes

        # Filter passive universes (#300): skip universes where trading.live_enabled
        # is False so their signals never propagate into the calling market's plan
        # file.  Downstream consumers (execute_approved.py) already filter via
        # BrokerRoutingPolicy.should_skip(), but filtering here avoids loading
        # universe data and generating signals entirely — saving LLM tokens and
        # eliminating noise in plan_<market>_*.json files.
        # Fails open (keeps universe) if its config cannot be loaded.
        try:
            from utils.config import get_active_config as _gac  # local import — avoid circular
            _live_universes: list = []
            for _u in active_universes:
                try:
                    _ucfg = _gac(_u)
                    if _ucfg.get("trading", {}).get("live_enabled", False):
                        _live_universes.append(_u)
                    else:
                        logger.info(
                            "plan: skipping passive universe %s in regime-aware plan "
                            "— trading.live_enabled=false (#300)",
                            _u,
                        )
                except Exception as _exc:
                    logger.debug(
                        "plan: config for universe %s unavailable (fail-open): %s",
                        _u, _exc,
                    )
                    _live_universes.append(_u)  # fail-open: include if config missing
            active_universes = _live_universes
        except Exception as _exc:
            logger.warning("Passive-universe filter failed (fail-open): %s", _exc)

        # c. Load data for each active universe.
        multi_data: dict = build_multi_universe(active_universes)

        # d. Filter strategies to those permitted by the current regime.
        enabled_types: list = regime.enabled_strategies  # e.g. ["all"] or ["mean_reversion"]
        if "all" in enabled_types:
            active_strategies = list(strategies)
        else:
            active_strategies = [s for s in strategies if s.name in enabled_types]
        logger.info(
            "Regime strategy filter: %d/%d strategies active (types=%s)",
            len(active_strategies), len(strategies), enabled_types,
        )

        # e + f. Run active strategies on each universe, tagging signals.
        all_signals: list = []
        all_filter_rejections: list = []
        for universe_name, universe_data in multi_data.items():
            for strat in active_strategies:
                try:
                    # Precompute indicators per universe dataset
                    if hasattr(strat, 'precompute'):
                        strat.precompute(universe_data)
                    sigs = strat.generate_signals(universe_data, equity, existing_positions)
                    for sig in sigs:
                        sig.universe = universe_name  # f. tag with originating universe
                    all_signals.extend(sigs)
                    # Collect filter rejections from strategy (e.g. sma200_filter)
                    _filt = getattr(strat, "last_filter_rejections", []) or []
                    for _frej in _filt:
                        _frej_copy = dict(_frej)
                        _frej_copy.setdefault("universe", universe_name)
                        all_filter_rejections.append(_frej_copy)
                except Exception as exc:
                    logger.error(
                        "Strategy %s / universe %s error: %s",
                        strat.name, universe_name, exc,
                    )

        logger.info(
            "Regime-aware signal generation: %d raw signals across %d universes",
            len(all_signals), len(multi_data),
        )

        # Signal enrichment (breadth, RS, earnings blackout)
        combined_data = {}
        for universe_data in multi_data.values():
            combined_data.update(universe_data)
        try:
            from utils.signal_enrichment import enrich_signals
            logger.info("Enriching %d raw regime signals...", len(all_signals))
            all_signals = enrich_signals(all_signals, combined_data, self.config, trade_date)
            logger.info("%d signals after enrichment", len(all_signals))
        except Exception as exc:
            logger.warning("Signal enrichment failed (non-fatal): %s", exc)

        # Sector map enrichment — load per-universe maps and combine
        # Must run BEFORE PortfolioConstructor so sector concentration checks work.
        try:
            import json as _json
            _combined_sector_map: dict = {}
            # Load per-universe sector maps (e.g. sector_map_sp500.json, sector_map_commodity_etfs.json)
            for _uname in list(multi_data.keys()):
                _sm_path = PROJECT_ROOT / "data" / "processed" / f"sector_map_{_uname}.json"
                if _sm_path.exists():
                    with open(_sm_path) as _f:
                        _combined_sector_map.update(_json.load(_f))
            # Also try the primary market's map (covers the sp500 fallback)
            _primary_map_path = PROJECT_ROOT / "data" / "processed" / f"sector_map_{self.config.get('market', 'sp500')}.json"
            if _primary_map_path.exists():
                with open(_primary_map_path) as _f:
                    _combined_sector_map.update(_json.load(_f))
            if _combined_sector_map:
                logger.info("Regime sector map: %d tickers across %d universes", len(_combined_sector_map), len(multi_data))
                for sig in all_signals:
                    sector = _combined_sector_map.get(sig.ticker, "Unknown")
                    sig.sector = sector
                    if hasattr(sig, "features"):
                        sig.features["sector"] = sector
        except Exception as exc:
            logger.debug("Regime sector map enrichment skipped: %s", exc)

        # Sort by confidence descending
        all_signals.sort(key=lambda s: s.confidence, reverse=True)

        # g. Route all signals through PortfolioConstructor.
        portfolio_positions = (
            self.portfolio.positions
            if hasattr(self.portfolio, "positions") else []
        )
        # Task #358: per-universe deployment limits can be tuned via
        # ``risk.universe_limits`` in the active config.  When the block is
        # absent or malformed, resolve_universe_limits() returns the
        # hardcoded UNIVERSE_LIMITS defaults — i.e. behavior is unchanged.
        resolved_universe_limits = resolve_universe_limits(self.config)
        constructor = PortfolioConstructor(
            regime_classification=regime,
            universe_limits=resolved_universe_limits,
        )
        constructed = constructor.construct(
            all_signals, equity=equity, existing_positions=portfolio_positions
        )
        logger.info(
            "Portfolio construction: %d signals selected, %d rejected",
            len(constructed.signals), len(constructed.rejected),
        )

        # Record today's regime to SQLite (non-fatal).
        try:
            model.classify_and_record()
        except Exception as exc:
            logger.warning("classify_and_record failed (non-fatal): %s", exc)

        # Build the plan from constructor-selected signals.
        plan = self.generate_plan(
            constructed.signals, exit_recommendations, prices, trade_date
        )

        # Merge strategy filter rejections (e.g. sma200_filter) into plan.rejected_entries.
        # These are pre-signal rejections — candidates that never became signals because
        # an internal strategy guard (sma200_filter) blocked them upstream.
        if all_filter_rejections:
            _regime_market_id = self.config.get("market", "")
            _filter_normalized: list = []
            for _rej in all_filter_rejections:
                _filter_normalized.append({
                    "ticker": _rej.get("ticker", ""),
                    "strategy": _rej.get("strategy", ""),
                    "entry_price": _rej.get("entry_price", 0.0),
                    "stop_price": _rej.get("stop_price", 0.0),
                    "take_profit": _rej.get("take_profit", None),
                    "position_size": _rej.get("position_size", 0),
                    "position_value": _rej.get("position_value", 0.0),
                    "risk_amount": _rej.get("risk_amount", 0.0),
                    "confidence": _rej.get("confidence", 0.0),
                    "rationale": _rej.get("rationale", ""),
                    "features": _rej.get("features", {}),
                    "sector": _rej.get("sector", "Unknown"),
                    "market_id": _rej.get("market_id", _regime_market_id),
                    "rejection_reason": _rej.get("rejection_reason", "strategy_filter"),
                    "rejection_detail": _rej.get("rejection_detail", {}),
                    "universe": _rej.get("universe", ""),
                })
            plan["rejected_entries"] = plan.get("rejected_entries", []) + _filter_normalized
            logger.info(
                "plan: merged %d strategy filter rejections into regime plan.rejected_entries",
                len(_filter_normalized),
            )

        # P1-9 fix: inject constructor-rejected signals into plan.rejected_entries.
        # PortfolioConstructor.construct() rejects most signals (e.g. 40 of 43).
        # Without this block those rejections are silently discarded — they never
        # reach cmd_plan's record_signal loop and are never written to the signals
        # table.  We convert constructed.rejected (list[(signal, reason)]) to the
        # same dict shape that generate_plan uses for rejected_entries.
        if constructed.rejected:
            _constructor_rejects: list = []
            for _sig, _reason in constructed.rejected:
                try:
                    _constructor_rejects.append({
                        "ticker": _sig.ticker,
                        "strategy": _sig.strategy,
                        "entry_price": _sig.entry_price,
                        "stop_price": _sig.stop_price,
                        "take_profit": getattr(_sig, "take_profit", None),
                        "position_size": _sig.position_size,
                        "position_value": round(_sig.entry_price * _sig.position_size, 2),
                        "risk_amount": round(
                            abs(_sig.entry_price - _sig.stop_price) * _sig.position_size, 2
                        ),
                        "confidence": _sig.confidence,
                        "rationale": getattr(_sig, "rationale", ""),
                        "features": getattr(_sig, "features", {}),
                        "sector": getattr(_sig, "sector", "Unknown"),
                        "market_id": getattr(
                            _sig, "market_id", self.config.get("market", "")
                        ),
                        "rejection_reason": _reason,
                    })
                except Exception as _ser_exc:
                    logger.debug(
                        "constructor reject serialisation error for %s: %s",
                        getattr(_sig, "ticker", "?"), _ser_exc,
                    )
            # Prepend constructor rejections so they appear before any
            # generate_plan-internal rejections (max-position overflow etc.)
            plan["rejected_entries"] = _constructor_rejects + plan.get("rejected_entries", [])
            logger.info(
                "Injected %d constructor-rejected signals into plan.rejected_entries",
                len(_constructor_rejects),
            )

        # h. Enrich plan with regime metadata.
        plan["regime_state"] = regime.state.value
        plan["active_universes"] = list(active_universes)
        plan["sizing_multiplier"] = regime.sizing_multiplier
        plan["regime_reasoning"] = regime.reasoning

        # Re-persist the plan now that regime fields are present.
        self._save_plan(plan, trade_date)

        return plan

    # ──────────────────────────────────────────────────────────────────────
    # Plan I/O helpers
    # ──────────────────────────────────────────────────────────────────────

    def load_plan(self, trade_date: str, market_id: str = "") -> Optional[dict]:
        plans_dir = PROJECT_ROOT / self.PLANS_DIR
        market_id = market_id or self.config.get("market", "")
        # Try per-market file first, then generic
        candidates = []
        if market_id:
            candidates.append(plans_dir / f"plan_{market_id}_{trade_date}.json")
        candidates.append(plans_dir / f"plan_{trade_date}.json")

        for path in candidates:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        return None

    def approve_plan(
        self,
        trade_date: str,
        market_id: str = "",
        auto: bool = False,
        approver: str = "human",
    ) -> Optional[dict]:
        """Mark a plan as approved.

        Args:
            trade_date: Date string (YYYY-MM-DD) of the plan to approve.
            market_id:  Market identifier (e.g. 'sp500', 'commodity_etfs').
            auto:       When True, annotates the plan as auto-approved by config.
            approver:   Who approved — 'human' (default) or 'auto'.

        Raises:
            PlanAlreadyRejectedError: If the plan's current status is REJECTED.
                Re-approving a rejected plan would silently override an explicit
                rejection — caller must regenerate or explicitly reset status first.
        """
        plan = self.load_plan(trade_date, market_id=market_id)
        if plan:
            current_status = str(plan.get("status", "")).upper()
            if current_status == "REJECTED":
                raise PlanAlreadyRejectedError(
                    f"Plan for {trade_date} (market={market_id or 'default'}) is "
                    f"already REJECTED — refusing to approve. Regenerate the plan "
                    f"or reset its status explicitly if you intend to approve."
                )
            plan["status"] = "APPROVED"
            plan["approved_at"] = datetime.now().isoformat()
            plan["approver"] = approver
            if auto:
                plan["approval_reason"] = "auto_approve_plans config flag"
            self._save_plan(plan, trade_date)
            return plan
        return None

    def format_plan_text(self, plan: dict) -> str:
        """Format trade plan as readable text."""
        # Audit M2: use single quotes inside f-string expressions (Python < 3.12 compat)
        lines = []
        lines.append(f"═══════════════════════════════════════════════")
        lines.append(f"  DAILY TRADE PLAN — {plan['trade_date']}")
        lines.append(f"  Status: {plan['status']}")
        lines.append(f"═══════════════════════════════════════════════")
        lines.append("")

        snap = plan["portfolio_snapshot"]
        lines.append(f"📊 PORTFOLIO: Equity ${snap['equity']:,.2f} | "
                     f"Cash ${snap['cash']:,.2f} | "
                     f"PnL ${snap['total_pnl']:+,.2f} ({snap['total_pnl_pct']:+.1f}%) | "
                     f"Positions {snap['open_positions']}")
        lines.append("")

        # Proposed entries
        if plan["proposed_entries"]:
            lines.append(f"🟢 PROPOSED ENTRIES ({len(plan['proposed_entries'])})")
            lines.append(f"{'Ticker':<8} {'Strategy':<20} {'Entry':>8} {'Stop':>8} {'Size':>5} {'Risk$':>7} {'Conf':>5}")
            lines.append(f"{'─'*8} {'─'*20} {'─'*8} {'─'*8} {'─'*5} {'─'*7} {'─'*5}")
            for e in plan["proposed_entries"]:
                lines.append(f"{e['ticker']:<8} {e['strategy']:<20} "
                             f"${e['entry_price']:>7.2f} ${e['stop_price']:>7.2f} "
                             f"{e['position_size']:>5} ${e['risk_amount']:>6.2f} "
                             f"{e['confidence']:>5.2f}")
                lines.append(f"  → {e['rationale']}")
            lines.append("")

        # Rejected
        if plan["rejected_entries"]:
            lines.append(f"🔴 REJECTED ({len(plan['rejected_entries'])})")
            for e in plan["rejected_entries"]:
                lines.append(f"  {e['ticker']} ({e['strategy']}): {e['rejection_reason']}")
            lines.append("")

        # Exits
        if plan["proposed_exits"]:
            lines.append(f"🟡 PROPOSED EXITS ({len(plan['proposed_exits'])})")
            for ex in plan["proposed_exits"]:
                lines.append(f"  {ex.get('ticker', '?')} — {ex.get('reason', '?')}")
            lines.append("")

        # Risk summary
        risk = plan["risk_summary"]
        lines.append(f"⚠️  RISK: Cost ${risk['total_proposed_cost']:,.2f} | "
                     f"Risk ${risk['total_proposed_risk']:,.2f} | "
                     f"Positions after: {risk['positions_after']} | "
                     f"Exposure: {risk['portfolio_exposure_pct']:,.1f}%")
        lines.append("")

        # Open positions
        if plan["open_positions"]:
            lines.append(f"📋 OPEN POSITIONS ({len(plan['open_positions'])})")
            lines.append(f"{'Ticker':<8} {'Entry':>8} {'Current':>8} {'PnL$':>8} {'PnL%':>7} {'Stop':>8}")
            lines.append(f"{'─'*8} {'─'*8} {'─'*8} {'─'*8} {'─'*7} {'─'*8}")
            for p in plan["open_positions"]:
                lines.append(f"{p['ticker']:<8} ${p['entry_price']:>7.2f} "
                             f"${p['current_price']:>7.2f} "
                             f"${p['unrealized_pnl']:>+7.2f} "
                             f"{p['unrealized_pnl_pct']:>+6.1f}% "
                             f"${p['stop_price']:>7.2f}")
            lines.append("")

        lines.append("⏳ Reply APPROVED to execute, or REJECT to skip.")
        return "\n".join(lines)
