"""Live Portfolio — broker-backed position/cash tracking.

Live Portfolio — broker-backed position/cash tracking.
Reads positions and cash from the connected broker instead of a JSON file.
Maintains its own closed-trade history and equity curve in
    brokers/state/live_{market_id}.json

Usage:
    from brokers.live_portfolio import LivePortfolio

    lp = LivePortfolio(config, market_id="asx")
    lp.connect()   # connects to broker
    # ... use: lp.positions, lp.cash, lp.equity(), etc.
    lp.disconnect()
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from brokers.base import PositionInfo
from brokers.position import Position

logger = logging.getLogger("atlas.live_portfolio")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROJECT_ROOT = Path(__file__).parent.parent


class LivePortfolio:
    """Broker-backed portfolio for live position and cash tracking.

    Positions and cash come from the live broker.
    Risk-limit checks, plan generation, and equity snapshots all work
    against real broker state — positions from broker directly.
    """

    def __init__(self, config: dict, market_id: str = "asx"):
        self.config = config
        self.market_id = market_id

        # Risk params
        risk = config.get("risk", {})
        self.starting_equity = risk.get("starting_equity", 5000)
        self.max_risk_per_trade = risk.get("max_risk_per_trade_pct", 0.005)
        self.max_positions = risk.get("max_open_positions", 10)
        self.max_sector_conc = risk.get("max_sector_concentration", 2)
        self.max_daily_dd = risk.get("max_daily_drawdown_pct", 0.02)
        self.leverage = risk.get("leverage", 1.0)

        fees = config.get("fees", {})
        self.commission_flat = fees.get("commission_per_trade", 0)
        self.commission_pct = fees.get("commission_pct", 0)

        # State read from broker (populated on connect)
        self.positions: list[Position] = []
        self.cash: float = 0.0
        self.buying_power: float = 0.0
        self._broker_equity: float = 0.0

        # True when broker returned meaningful data; False when broker
        # returned zeroed/empty data (e.g. OpenD up but Futu backend
        # unreachable).  State-mutating methods (record_equity, save_state)
        # refuse to write when this is False to prevent corruption.
        self.broker_data_valid: bool = False

        # Persistent local state (closed trades, equity history)
        self.closed_trades: list[dict] = []
        self.equity_history: list[dict] = []
        self.daily_high_water: float = self.starting_equity
        self.halted: bool = False
        self.halt_reason: str = ""

        self._broker = None
        self._connected = False

        self._load_local_state()

    # ── State file (local, tracks history only) ────────────────

    def _state_path(self) -> Path:
        # IMPORTANT: always use the "live_" prefix.  Legacy files like
        # brokers/state/sp500.json (no prefix) are stale and must NOT be read.
        return PROJECT_ROOT / "brokers" / "state" / f"live_{self.market_id}.json"

    def _load_local_state(self):
        # Only reads from live_{market_id}.json via _state_path() — never the
        # legacy bare {market_id}.json files.  Do not add any fallback here.
        path = self._state_path()
        if path.exists():
            try:
                with open(path) as f:
                    state = json.load(f)
                self.closed_trades = state.get("closed_trades", [])
                self.equity_history = state.get("equity_history", [])
                self.daily_high_water = state.get("daily_high_water", self.starting_equity)
                self.halted = state.get("halted", False)
                self.halt_reason = state.get("halt_reason", "")
                logger.info("Loaded live state: %d closed trades, %d equity pts",
                            len(self.closed_trades), len(self.equity_history))
            except Exception as e:
                logger.warning("Failed to load live state: %s", e)

    def save_state(self):
        """Persist closed-trade history and equity curve.

        Refuses to write if broker_data_valid is False to prevent
        corrupting state with zeroed broker data.
        """
        if not self.broker_data_valid:
            logger.warning(
                "save_state() skipped — broker_data_valid is False "
                "(would corrupt %s)", self._state_path().name
            )
            return

        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "market_id": self.market_id,
            "mode": "live",
            "closed_trades": self.closed_trades,
            "equity_history": self.equity_history,
            "daily_high_water": self.daily_high_water,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "last_saved": datetime.now().isoformat(),
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

    # ── Broker connection ──────────────────────────────────────

    def connect(self) -> bool:
        """Connect to broker and load positions + cash."""
        from brokers.registry import get_broker
        self._broker = get_broker(self.market_id, self.config)
        if self._broker is None:
            logger.warning("LivePortfolio: no broker configured for %s (live_enabled=%s)",
                           self.market_id, self.config.get("trading", {}).get("live_enabled", False))
            return False
        if not self._broker.connect():
            logger.error("LivePortfolio: broker connect failed")
            return False
        self._connected = True
        self._refresh_from_broker()
        return True

    def disconnect(self):
        if self._broker:
            self._broker.disconnect()
        self._connected = False

    def _refresh_from_broker(self):
        """Pull positions and account info from broker.

        Sets self.broker_data_valid to indicate whether the data is
        trustworthy.  When the broker returns zeroed data (OpenD connected
        but Futu backend unreachable), positions/cash are left at their
        previous values and broker_data_valid is set to False.
        """
        if not self._broker:
            return

        # Account info
        acct = self._broker.get_account_info()
        raw_positions = self._broker.get_positions()

        # ── Detect broker returning garbage ─────────────────────
        # OpenD can connect fine but Futu backend returns "Network
        # interruption" — yielding $0 equity, $0 cash, [] positions.
        # If we previously had positions (in equity history or in the
        # current session), this is clearly a data failure, not a
        # genuine empty account.
        if acct.equity == 0 and acct.cash == 0 and not raw_positions:
            prev_had_positions = any(
                pt.get("num_positions", 0) > 0
                for pt in self.equity_history
            )
            if prev_had_positions or self.positions:
                logger.warning(
                    "LivePortfolio: broker returned $0 equity / 0 positions "
                    "but history shows prior positions — treating as OFFLINE. "
                    "State will NOT be updated."
                )
                self.broker_data_valid = False
                return
            # Genuinely empty account (no prior positions either)
            logger.info("LivePortfolio: broker returned empty — appears genuine (no prior positions)")

        self.broker_data_valid = True
        self.cash = acct.cash
        self.buying_power = getattr(acct, 'buying_power', 0.0) or (acct.cash * self.leverage)
        self._broker_equity = acct.equity

        # Convert broker PositionInfo → engine Position objects
        self.positions = []
        for pi in raw_positions:
            # Filter to this market's tickers
            if self.market_id == "asx" and not pi.ticker.endswith(".AX"):
                continue
            if self.market_id == "sp500" and pi.ticker.endswith(".AX"):
                continue

            pos = Position(
                ticker=pi.ticker,
                strategy=pi.strategy or "unknown",
                entry_date=pi.entry_date or pd.Timestamp.now().strftime("%Y-%m-%d"),
                entry_price=pi.entry_price,
                shares=pi.shares,
                stop_price=pi.stop_price,
                take_profit=pi.take_profit,
                confidence=1.0,
                rationale="live broker position",
                sector=pi.sector or "Unknown",
            )
            pos.entry_value = pi.cost_basis or (pi.entry_price * pi.shares)
            self.positions.append(pos)

        # Enrich positions with plan metadata (strategy, entry_date, etc.)
        self._enrich_from_plans()

        # Enrich with LIVE stop prices from broker's open orders
        # (trailing stops update dynamically — plan files only have initial levels)
        self._enrich_from_broker_stops()

        n_atlas = len(self.atlas_positions)
        n_manual = len(self.manual_positions)
        logger.info("LivePortfolio: %d positions (%d atlas, %d manual), cash=$%.2f, "
                     "atlas_equity=$%.2f, broker_equity=$%.2f",
                     len(self.positions), n_atlas, n_manual,
                     self.cash, self.equity(), self._broker_equity)

    def _enrich_from_plans(self):
        """Fill in stop_price, strategy, entry_date from recent trade plans,
        with fallback to the state file for authoritative strategy names.

        The broker doesn't provide stop/TP levels or Atlas strategy names.
        We recover them from: (1) plan files, (2) state file (which has
        strategy names from Alpaca client_order_id parsing).
        """
        meta: dict[str, dict] = {}

        # Source 1: plan files
        plans_dir = PROJECT_ROOT / "plans"
        if plans_dir.exists():
            for plan_file in sorted(plans_dir.glob(f"plan_{self.market_id}_*.json"), reverse=True)[:30]:
                try:
                    with open(plan_file) as f:
                        plan = json.load(f)
                except Exception:
                    continue
                trade_date = plan.get("trade_date", "")
                for entry in plan.get("proposed_entries", []):
                    ticker = entry.get("ticker", "")
                    if ticker and ticker not in meta:
                        meta[ticker] = {
                            "strategy": entry.get("strategy", ""),
                            "entry_date": trade_date,
                            "stop_price": entry.get("stop_price", 0),
                            "take_profit": entry.get("take_profit"),
                            "confidence": entry.get("confidence", 0),
                            "sector": entry.get("sector", "Unknown"),
                        }

        # Source 2: state file (has authoritative strategy names from
        # Alpaca client_order_id parsing — fills gaps where plans are missing)
        state_path = self._state_path()
        if state_path.exists():
            try:
                with open(state_path) as f:
                    state = json.load(f)
                for sp in state.get("positions", []):
                    ticker = sp.get("ticker", "")
                    strategy = sp.get("strategy", "")
                    if ticker and strategy and strategy != "unknown":
                        if ticker not in meta or not meta[ticker].get("strategy"):
                            meta.setdefault(ticker, {})["strategy"] = strategy
                        if sp.get("entry_date"):
                            meta[ticker].setdefault("entry_date", sp["entry_date"])
            except Exception:
                pass

        enriched = 0
        for pos in self.positions:
            m = meta.get(pos.ticker)
            if not m:
                continue
            if pos.stop_price == 0 and m.get("stop_price", 0) > 0:
                pos.stop_price = m["stop_price"]
            if pos.take_profit in (None, 0) and m.get("take_profit"):
                pos.take_profit = m["take_profit"]
            if pos.strategy in ("unknown", ""):
                pos.strategy = m.get("strategy", pos.strategy)
            if pos.entry_date in ("unknown", "") and m.get("entry_date"):
                pos.entry_date = m["entry_date"]
            if pos.sector in ("Unknown", "") and m.get("sector", "Unknown") != "Unknown":
                pos.sector = m["sector"]
            if m.get("confidence", 0) > 0:
                pos.confidence = m["confidence"]
            enriched += 1

        if enriched:
            logger.info("Enriched %d positions with plan/state metadata (stops, strategy)", enriched)

    def _enrich_from_broker_stops(self):
        """Fill in stop_price from broker's open sell orders.

        The broker's open orders contain the actual, current stop levels
        (including trailing stops that have ratcheted up).  This is more
        accurate than plan files which only have the initial stop.

        Also updates the positions list in the state file so the dashboard
        always has current stop prices without a separate sync step.
        """
        if not self._broker:
            return

        try:
            open_orders = self._broker.get_open_orders()
        except Exception as e:
            logger.debug("Could not fetch open orders for stop enrichment: %s", e)
            return

        # Build {ticker: stop_price} from sell orders
        stop_map: dict[str, dict] = {}
        for o in open_orders:
            raw = getattr(o, 'raw', {}) or {}
            side = getattr(o, 'side', raw.get('side', ''))
            side_str = str(side).lower()
            if 'sell' not in side_str:
                continue

            ticker = getattr(o, 'ticker', raw.get('symbol', ''))
            order_type = str(raw.get('order_type', raw.get('type', ''))).lower()
            stop_price = raw.get('stop_price')
            trail_price = raw.get('trail_price')
            limit_price = raw.get('limit_price')

            if stop_price:
                stop_map[ticker] = {
                    'stop_price': float(stop_price),
                    'type': 'trailing' if 'trail' in order_type else 'stop',
                }
            elif limit_price and 'limit' in order_type:
                stop_map[ticker] = {
                    'stop_price': float(limit_price),
                    'type': 'limit_exit',
                }

        enriched = 0
        for pos in self.positions:
            info = stop_map.get(pos.ticker)
            if info and info['stop_price'] > 0:
                pos.stop_price = info['stop_price']
                enriched += 1

        if enriched:
            logger.info("Enriched %d positions with live broker stop prices", enriched)

        # Persist updated stops to state file so dashboard picks them up
        if enriched:
            self._update_state_positions()

    def _update_state_positions(self):
        """Merge current stop prices into state file without clobbering strategy data.

        The state file may have authoritative strategy names (from Alpaca
        client_order_id parsing) that the broker doesn't provide. We only
        update fields where we have better data (stop_price from live
        broker orders), and preserve everything else.
        """
        path = self._state_path()
        if not path.exists():
            return
        try:
            with open(path) as f:
                state = json.load(f)

            # Build lookup from existing state
            existing = {p.get("ticker"): p for p in state.get("positions", [])}

            merged = []
            for pos in self.positions:
                prev = existing.get(pos.ticker, {})
                merged.append({
                    "ticker": pos.ticker,
                    # Prefer existing strategy if broker returns unknown/empty
                    "strategy": prev.get("strategy") if pos.strategy in ("unknown", "") and prev.get("strategy") else pos.strategy,
                    "entry_date": prev.get("entry_date") or pos.entry_date,
                    "entry_price": pos.entry_price,
                    "shares": pos.shares,
                    # Always use live broker stop (more current than state)
                    "stop_price": pos.stop_price if pos.stop_price > 0 else prev.get("stop_price", 0),
                    "order_id": prev.get("order_id", getattr(pos, 'order_id', '')),
                })
            state["positions"] = merged
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug("Could not update state positions: %s", e)

    # ── Portfolio interface ──────────────────────

    def update_positions(self, prices: dict[str, float]):
        """Update MAE/MFE excursions for all positions with current prices.

        # Audit C3: standard update_positions() interface so
        # eod_settlement.py can call it uniformly.
        """
        for pos in self.positions:
            if pos.ticker in prices:
                pos.update_excursions(prices[pos.ticker])

    @property
    def atlas_positions(self) -> list:
        """Positions managed by Atlas (excludes manual/unknown positions)."""
        return [p for p in self.positions if p.strategy not in ("unknown", "")]

    @property
    def manual_positions(self) -> list:
        """Manual positions not managed by Atlas."""
        return [p for p in self.positions if p.strategy in ("unknown", "")]

    def equity(self, prices: dict[str, float] = None) -> float:
        """Atlas-only equity: inferred cash + Atlas position values.

        Manual positions (strategy='unknown') share the same broker account but
        are NOT Atlas-managed.  We therefore do NOT use self.cash (total broker
        cash) — it is inflated when manual positions exist in the account.

        Instead we infer the Atlas cash slice as:
            atlas_cash = starting_equity - sum(entry_value for atlas positions)

        This mirrors the dashboard logic in generate_data.py and keeps Atlas
        equity independent of whatever manual capital is also in the account.
        """
        atlas_pos = self.atlas_positions
        atlas_pos_value = sum(
            p.current_value(prices.get(p.ticker, p.entry_price) if prices else p.entry_price)
            for p in atlas_pos
        )
        # Infer cash: starting capital minus what is currently deployed in Atlas positions.
        atlas_entry_cost = sum(p.entry_value for p in atlas_pos)
        atlas_cash = self.starting_equity - atlas_entry_cost
        return round(atlas_cash + atlas_pos_value, 2)

    def broker_equity(self) -> float:
        """Full broker account equity (all positions including manual)."""
        return self._broker_equity

    def count_positions_by_strategy(self, strategy_name: str) -> int:
        """Count open positions belonging to a given strategy."""
        return sum(1 for p in self.positions if p.strategy == strategy_name)

    def check_risk_limits(self, signal, allocation_pool=None) -> tuple[bool, str]:
        """Validate a proposed trade against risk limits.

        Args:
            signal: Signal object to check.
            allocation_pool: Optional StrategyAllocationPool.  When provided
                             and enabled, per-strategy pool limits are enforced.
        """
        reasons = []

        if len(self.positions) >= self.max_positions:
            reasons.append(f"Max positions ({self.max_positions}) reached")

        # Per-strategy allocation pool check
        if allocation_pool is not None and allocation_pool.is_enabled():
            pos_dicts = [{"strategy": p.strategy} for p in self.positions]
            ok, pool_reason = allocation_pool.can_accept(signal.strategy, pos_dicts)
            if not ok:
                reasons.append(pool_reason)

        sector = getattr(signal, "sector", "Unknown")
        sector_count = sum(1 for p in self.positions if p.sector == sector)
        if sector_count >= self.max_sector_conc:
            reasons.append(f"Max sector concentration ({self.max_sector_conc}) for {sector}")

        if any(p.ticker == signal.ticker for p in self.positions):
            reasons.append(f"Already holding {signal.ticker}")

        risk_amount = abs(signal.entry_price - signal.stop_price) * signal.position_size
        eq = self.equity()
        effective_eq = eq * self.leverage
        max_risk = effective_eq * self.max_risk_per_trade
        if risk_amount > max_risk * 1.1:
            reasons.append(f"Risk ${risk_amount:.2f} exceeds max ${max_risk:.2f}")

        cost = signal.entry_price * signal.position_size
        available_buying_power = self.buying_power if self.buying_power > 0 else self.cash * self.leverage
        if cost > available_buying_power:
            reasons.append(f"Insufficient buying power: need ${cost:.2f}, have ${available_buying_power:.2f}")

        if self.halted:
            reasons.append(f"Trading halted: {self.halt_reason}")

        if reasons:
            return False, "; ".join(reasons)
        return True, "All checks passed"

    def check_daily_drawdown(self, prices: dict[str, float] = None):
        """Check if daily drawdown limit breached."""
        current_eq = self.equity(prices)
        self.daily_high_water = max(self.daily_high_water, current_eq)
        dd = (self.daily_high_water - current_eq) / self.daily_high_water if self.daily_high_water > 0 else 0
        if dd >= self.max_daily_dd:
            self.halted = True
            self.halt_reason = f"Daily drawdown {dd:.2%} >= {self.max_daily_dd:.2%}"
            logger.warning("HALT: %s", self.halt_reason)
            return True, dd
        return False, dd

    def reset_daily_halt(self):
        if self.halted:
            logger.info("Resetting daily halt (was: %s)", self.halt_reason)
        self.halted = False
        self.halt_reason = ""

    def portfolio_summary(self, prices: dict[str, float] = None) -> dict:
        """Build portfolio summary."""
        eq = self.equity(prices)
        total_pnl = eq - self.starting_equity
        total_pnl_pct = round(total_pnl / self.starting_equity * 100, 2) if self.starting_equity else 0

        today_str = datetime.now().strftime("%Y-%m-%d")
        open_positions = []
        for p in self.positions:
            price = prices.get(p.ticker, p.entry_price) if prices else p.entry_price
            open_positions.append({
                "ticker": p.ticker,
                "strategy": p.strategy,
                "entry_date": p.entry_date,
                "entry_price": p.entry_price,
                "current_price": price,
                "shares": p.shares,
                "unrealized_pnl": p.unrealized_pnl(price),
                "unrealized_pnl_pct": p.unrealized_pnl_pct(price),
                "stop_price": p.stop_price,
                "take_profit": p.take_profit,
                "sector": p.sector,
                "mae_pct": round(p.mae * 100, 2),
                "mfe_pct": round(p.mfe * 100, 2),
                "holding_days": p.holding_days(today_str),
            })

        return {
            "date": today_str,
            "equity": eq,
            "cash": self.cash,
            "starting_equity": self.starting_equity,
            "total_pnl": round(total_pnl, 2),
            "total_pnl_pct": total_pnl_pct,
            "open_positions": open_positions,
            "num_open": len(self.positions),
            "num_closed_trades": len(self.closed_trades),
            "halted": self.halted,
        }

    def get_snapshot(self, prices: dict[str, float] = None) -> dict:
        """Convenience wrapper for dashboard/telegram."""
        summary = self.portfolio_summary(prices)
        return {
            "equity": summary["equity"],
            "cash": summary["cash"],
            "open_positions": summary["num_open"],
            "total_pnl": summary["total_pnl"],
            "total_pnl_pct": summary["total_pnl_pct"],
        }

    def record_equity(self, trade_date: str, prices: dict[str, float] = None):
        """Record daily equity snapshot with per-position breakdown.

        Refuses to record if broker_data_valid is False.
        """
        if not self.broker_data_valid:
            logger.warning(
                "record_equity() skipped for %s — broker_data_valid is False",
                trade_date,
            )
            return

        eq = self.equity(prices)
        # Per-position snapshot for future attribution analysis
        position_details = []
        for p in self.positions:
            price = prices.get(p.ticker, p.entry_price) if prices else p.entry_price
            position_details.append({
                "ticker": p.ticker,
                "strategy": p.strategy,
                "shares": p.shares,
                "entry_price": p.entry_price,
                "current_price": price,
                "unrealized_pnl": p.unrealized_pnl(price),
                "mae": round(p.mae * 100, 2),
                "mfe": round(p.mfe * 100, 2),
                "holding_days": p.holding_days(trade_date),
            })
        # Realized P&L from all closed trades
        total_realized = round(sum(t.get("pnl", 0) for t in self.closed_trades), 2)
        self.equity_history.append({
            "date": trade_date,
            "equity": eq,
            "cash": self.cash,
            "positions_value": round(eq - self.cash, 2),
            "num_positions": len(self.positions),
            "total_realized_pnl": total_realized,
            "total_closed_trades": len(self.closed_trades),
            "positions": position_details,
        })
        self.save_state()

    def execute_exit(self, ticker: str, exit_price: float, trade_date: str, exit_type: str) -> dict | None:
        """Remove a position by ticker, record a closed trade, and persist.

        Args:
            ticker: Symbol to exit.
            exit_price: Fill price for the exit.
            trade_date: Date string (YYYY-MM-DD) of the exit.
            exit_type: Reason — 'stop_loss', 'take_profit', etc.

        Returns:
            The closed-trade record dict, or None if ticker not found.
        """
        pos = next((p for p in self.positions if p.ticker == ticker), None)
        if pos is None:
            logger.warning("execute_exit: ticker %s not found in positions", ticker)
            return None

        pnl = round((exit_price - pos.entry_price) * pos.shares, 2)
        commission = round(self.commission_flat + pos.entry_value * self.commission_pct, 2)
        trade_record = {
            "ticker": ticker,
            "strategy": pos.strategy,
            "entry_date": pos.entry_date,
            "entry_price": pos.entry_price,
            "exit_date": trade_date,
            "exit_price": exit_price,
            "shares": pos.shares,
            "pnl": round(pnl - commission - pos.entry_commission, 2),
            "pnl_pct": round((exit_price - pos.entry_price) / pos.entry_price * 100, 2) if pos.entry_price else 0.0,
            "exit_type": exit_type,
            "exit_reason": exit_type,
            "mae": pos.mae,
            "mfe": pos.mfe,
            "sector": pos.sector,
            "holding_days": pos.holding_days(trade_date),
        }

        self.positions = [p for p in self.positions if p.ticker != ticker]
        self.record_closed_trade(trade_record)
        logger.info(
            "execute_exit: %s exited at $%.2f (%s) — PnL $%.2f (%.2f%%)",
            ticker, exit_price, exit_type, trade_record["pnl"], trade_record["pnl_pct"],
        )
        return trade_record

    def record_closed_trade(self, trade_record: dict):
        """Append a closed trade and persist."""
        self.closed_trades.append(trade_record)
        self.save_state()
