"""Live Portfolio — broker-backed position/cash tracking.

Live Portfolio — broker-backed position/cash tracking.
Reads positions and cash from the connected broker instead of a JSON file.
Maintains its own closed-trade history and equity curve in
    brokers/state/live_{market_id}.json

Usage:
    from brokers.live_portfolio import LivePortfolio

    lp = LivePortfolio(config, market_id="sp500")
    lp.connect()   # connects to broker
    # ... use: lp.positions, lp.cash, lp.equity(), etc.
    lp.disconnect()
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from brokers.base import PositionInfo
from brokers.position import Position

logger = logging.getLogger("atlas.live_portfolio")

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PROJECT_ROOT = Path(__file__).parent.parent

# Module-level constant for state directory — patchable in tests to prevent
# production state file pollution (see tests/conftest.py _isolate_state_files).
_STATE_DIR: Path = PROJECT_ROOT / "brokers" / "state"

# Traded markets tracked for per-market cash flow attribution (FIX-PMEQ-001).
# Must match the markets for which market_equity_history rows are written
# by eod_settlement.py + portfolio/market_equity_attribution.py.
_ALL_TRADED_MARKETS: tuple[str, ...] = ("sp500", "sector_etfs", "commodity_etfs")


class LivePortfolio:
    """Broker-backed portfolio for live position and cash tracking.

    Positions and cash come from the live broker.
    Risk-limit checks, plan generation, and equity snapshots all work
    against real broker state — positions from broker directly.
    """

    def __init__(self, config: dict, market_id: str = "sp500"):
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
        self.daily_high_water_date: Optional[str] = None
        self.halted: bool = False
        self.halt_reason: str = ""
        # Cooldown: HWM reset timestamp — suppresses marginal false-positive halts
        # within 1h of a session reset (see check_daily_drawdown)
        self._hwm_reset_at: Optional[datetime] = None

        # True when last _get_per_market_equity call could not reconcile activities
        # (Alpaca activities API down/unavailable).  In degraded mode, kill switch
        # is suppressed for non-catastrophic drawdowns (see check_daily_drawdown).
        self._per_market_equity_degraded: bool = False

        # Throttle: save_state warning fires only once per instance (not 30+ times)
        self._save_state_warned: bool = False

        self._broker = None
        self._connected = False

        self._load_local_state()

    # ── State file (local, tracks history only) ────────────────

    def _state_path(self) -> Path:
        # IMPORTANT: always use the "live_" prefix.  Legacy files like
        # brokers/state/sp500.json (no prefix) are stale and must NOT be read.
        # Uses module-level _STATE_DIR so tests can redirect via monkeypatch
        # without touching production brokers/state/ files.
        return _STATE_DIR / f"live_{self.market_id}.json"

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
                self.daily_high_water_date = state.get("daily_high_water_date", None)
                self.halted = state.get("halted", False)
                self.halt_reason = state.get("halt_reason", "")
                # Guard: HWM > 5× starting_equity means it was written when
                # check_daily_drawdown used global broker equity, before per-market
                # attribution existed.  Reset to starting_equity so the new
                # per-market path has a sensible baseline.
                if (
                    self.starting_equity > 0
                    and self.daily_high_water > self.starting_equity * 5
                ):
                    logger.warning(
                        "_load_local_state %s: HWM=$%.2f is >5× starting_equity=$%.2f — "
                        "likely set from global broker equity before per-market attribution. "
                        "Resetting HWM to starting_equity for correct per-market drawdown.",
                        self.market_id, self.daily_high_water, self.starting_equity,
                    )
                    self.daily_high_water = self.starting_equity
                    self.daily_high_water_date = None  # force date-reset on first drawdown check
                logger.info("Loaded live state: %d closed trades, %d equity pts",
                            len(self.closed_trades), len(self.equity_history))
            except Exception as e:
                logger.warning("Failed to load live state: %s", e)

    def save_state(self):
        """Persist closed-trade history, equity curve, and current positions.

        Refuses to write if broker_data_valid is False to prevent
        corrupting state with zeroed broker data.
        """
        if not self.broker_data_valid:
            if not self._save_state_warned:
                logger.warning(
                    "save_state() skipped — broker_data_valid is False "
                    "(would corrupt live_%s.json)", self.market_id
                )
                self._save_state_warned = True
            return

        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        # Serialize current positions so reconciliation and dashboard
        # can read them without a live broker connection.
        positions_list = []
        for pos in self.positions:
            positions_list.append({
                "ticker": pos.ticker,
                "strategy": pos.strategy,
                "entry_date": pos.entry_date,
                "entry_price": pos.entry_price,
                "shares": pos.shares,
                "stop_price": pos.stop_price,
                "order_id": getattr(pos, 'order_id', ''),
                "stop_order_id": getattr(pos, 'stop_order_id', ''),
                "tp_order_id": getattr(pos, 'tp_order_id', ''),
            })

        state = {
            "market_id": self.market_id,
            "mode": "live",
            "positions": positions_list,
            "closed_trades": self.closed_trades,
            "equity_history": self.equity_history,
            "daily_high_water": self.daily_high_water,
            "daily_high_water_date": self.daily_high_water_date,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "last_saved": datetime.now().isoformat(),
        }
        with open(path, "w") as f:
            json.dump(state, f, indent=2)

        # ── Dual-write to SQLite (feature-flagged) ─────────────────────────
        if self.config.get("dual_write_market_state", True):
            try:
                from db.atlas_db import get_db
                with get_db() as db:
                    # UPSERT market_state
                    db.execute(
                        """
                        INSERT INTO market_state
                            (market_id, halted, halt_reason, halted_at, mode,
                             daily_high_water, hwm_date, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(market_id) DO UPDATE SET
                          halted           = excluded.halted,
                          halt_reason      = excluded.halt_reason,
                          halted_at        = excluded.halted_at,
                          mode             = excluded.mode,
                          daily_high_water = excluded.daily_high_water,
                          hwm_date         = excluded.hwm_date,
                          updated_at       = datetime('now')
                        """,
                        (
                            self.market_id,
                            int(bool(self.halted)),
                            self.halt_reason or None,
                            datetime.now().isoformat() if self.halted else None,
                            "live",
                            self.daily_high_water,
                            self.daily_high_water_date,
                        ),
                    )
                    # Append latest equity_history entry (INSERT OR IGNORE for idempotence)
                    if self.equity_history:
                        latest = self.equity_history[-1]
                        db.execute(
                            """
                            INSERT OR IGNORE INTO equity_history
                                (market_id, date, equity, pnl)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                self.market_id,
                                latest.get("date"),
                                latest.get("equity"),
                                latest.get("pnl"),
                            ),
                        )
            except Exception as _dw_exc:
                logger.error(
                    "dual_write_market_state FAILED for %s: %s",
                    self.market_id, _dw_exc,
                )
                # Telegram-loud notification (best-effort)
                try:
                    from utils.telegram import send_message
                    send_message(
                        f"⚠️ dual_write_market_state failed for "
                        f"{self.market_id}: {_dw_exc}"
                    )
                except Exception as _tg_exc:
                    logger.warning(
                        "dual_write_market_state telegram alert failed (non-blocking): %s",
                        _tg_exc,
                    )
                # DO NOT re-raise — JSON write must succeed even if SQLite fails

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
        # Filter to this market's ticker universe (each universe sees only its own positions)
        try:
            from markets import get_market
            market_profile = get_market(self.market_id)
            universe_tickers = set(market_profile.get_formatted_tickers())
        except Exception as e:
            logger.warning("Universe ticker load failed for market %s: %s", self.market_id, e)
            universe_tickers = None

        self.positions = []
        for pi in raw_positions:
            # Filter to this market's ticker universe
            if universe_tickers is not None:
                if pi.ticker not in universe_tickers:
                    continue
            else:
                # Fallback: legacy suffix-based filters if market profile unavailable
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
        SQLite trades, and the state file.

        The broker doesn't provide stop/TP levels or Atlas strategy names.
        We recover them from: (0) SQLite trades table (most authoritative),
        (1) plan files, (2) state file.
        """
        meta: dict[str, dict] = {}

        # Source 0: SQLite trades table (most authoritative — strategy
        # is recorded at entry time by execute_entry/record_open_trade)
        try:
            from db.atlas_db import get_db
            with get_db() as db:
                rows = db.execute(
                    "SELECT ticker, strategy, entry_date, stop_price, "
                    "take_profit, confidence "
                    "FROM trades WHERE status = 'open' "
                    "ORDER BY entry_date DESC"
                ).fetchall()
                for row in rows:
                    ticker = row["ticker"]
                    strategy = row["strategy"]
                    entry_date = row["entry_date"]
                    stop_price = row["stop_price"]
                    take_profit = row["take_profit"]
                    confidence = row["confidence"]
                    if ticker and ticker not in meta:
                        meta[ticker] = {
                            "strategy": strategy or "",
                            "entry_date": (entry_date or "").split("T")[0],
                            "stop_price": stop_price or 0,
                            "take_profit": take_profit,
                            "confidence": confidence or 0,
                        }
        except Exception as e:
            logger.warning("_enrich_from_plans: SQLite lookup failed: %s", e)

        # Source 1: plan files
        plans_dir = PROJECT_ROOT / "plans"
        if plans_dir.exists():
            for plan_file in sorted(plans_dir.glob(f"plan_{self.market_id}_*.json"), reverse=True)[:30]:
                try:
                    with open(plan_file) as f:
                        plan = json.load(f)
                except Exception as e:
                    logger.warning("_enrich_from_plans: failed to load plan file %s: %s", plan_file, e)
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
            except Exception as e:
                logger.warning("_enrich_from_plans: state file parse failed: %s", e)
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

            order_id = getattr(o, 'order_id', '') or str(raw.get('id', ''))
            if stop_price:
                stop_map[ticker] = {
                    'stop_price': float(stop_price),
                    'type': 'trailing' if 'trail' in order_type else 'stop',
                    'order_id': order_id,
                }
            # NOTE: limit sell orders (take-profits) are intentionally ignored.
            # In OCO pairs, the limit leg is the take-profit target, not a stop.
            # Previously an elif here treated limit_price as stop_price, which
            # caused false stop_hit exits when TP was above current price.

        enriched = 0
        for pos in self.positions:
            info = stop_map.get(pos.ticker)
            if info and info['stop_price'] > 0:
                pos.stop_price = info['stop_price']
                pos.stop_order_id = info.get('order_id', '')
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

            # Universe-membership guard: prevents cross-market ticker contamination
            # (e.g., sector_etfs tickers bleeding into commodity_etfs state).
            # self.positions is already filtered on load, but this is defence-in-depth
            # for code paths (e.g. reconcile_entry_fills) that append to self.positions
            # directly without going through _refresh_from_broker.
            try:
                from markets import get_market as _get_market
                _market_profile = _get_market(self.market_id)
                _universe_tickers: set | None = set(_market_profile.get_formatted_tickers())
            except Exception as e:
                logger.warning(f"Market universe lookup failed, skipping ticker filter: {e}")
                _universe_tickers = None  # fail open — don't filter if universe unknown

            merged = []
            for pos in self.positions:
                # Skip tickers that don't belong to this market's universe
                if _universe_tickers is not None and pos.ticker not in _universe_tickers:
                    logger.warning(
                        "_update_state_positions: %s is not in %s universe — "
                        "skipping state backfill (belongs to another universe)",
                        pos.ticker, self.market_id,
                    )
                    continue
                prev = existing.get(pos.ticker, {})
                merged.append({
                    "ticker": pos.ticker,
                    # Use position's enriched strategy (now comes from SQLite
                    # via _enrich_from_plans); only fall back to state file if
                    # still unknown after all enrichment sources.
                    "strategy": pos.strategy if pos.strategy not in ("unknown", "") else prev.get("strategy", pos.strategy),
                    "entry_date": prev.get("entry_date") or pos.entry_date,
                    "entry_price": pos.entry_price,
                    "shares": pos.shares,
                    # Always use live broker stop (more current than state)
                    "stop_price": pos.stop_price if pos.stop_price > 0 else prev.get("stop_price", 0),
                    "order_id": prev.get("order_id", getattr(pos, 'order_id', '')),
                    "stop_order_id": getattr(pos, 'stop_order_id', '') or prev.get("stop_order_id", ""),
                    "tp_order_id": getattr(pos, 'tp_order_id', '') or prev.get("tp_order_id", ""),
                })
            state["positions"] = merged
            with open(path, "w") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug("Could not update state positions: %s", e)

    def reconcile_broker_fills(self, trade_date: str) -> list[dict]:
        """Detect positions that were closed by broker-side orders (trailing stops, etc.)

        Compares local state positions against current broker positions.
        Any position in local state but NOT on broker was filled — look up
        the fill details and record as a closed trade.

        Returns list of newly recorded closed-trade dicts.
        """
        if not self._broker:
            return []

        # Load local state to get positions we THINK we have
        path = self._state_path()
        if not path.exists():
            return []

        try:
            with open(path) as f:
                state = json.load(f)
        except Exception as e:
            logger.warning("reconcile state file load failed: %s", e)
            return []

        local_tickers = {p.get("ticker") for p in state.get("positions", []) if p.get("ticker")}
        broker_tickers = {pos.ticker for pos in self.positions}

        # Positions in local state but NOT on broker = closed by broker
        vanished = local_tickers - broker_tickers
        if not vanished:
            return []

        logger.info("reconcile_broker_fills: %d position(s) vanished from broker: %s",
                    len(vanished), vanished)

        # Look up fill details from broker order history
        reconciled = []
        try:
            recent_orders = self._broker.get_history_orders(days=7)
        except Exception as e:
            logger.warning("reconcile_broker_fills: could not fetch order history: %s", e)
            recent_orders = []

        # Build fill lookup: ticker -> most recent SELL fill
        sell_fills = {}
        for order in recent_orders:
            if (order.ticker in vanished
                    and order.side.value.upper() == 'SELL'
                    and order.status.value.upper() == 'FILLED'):
                # Keep the most recent fill per ticker
                prev = sell_fills.get(order.ticker)
                if prev is None:
                    sell_fills[order.ticker] = order
                else:
                    # Compare filled_at timestamps from raw dict
                    prev_time = prev.raw.get('filled_at', '')
                    curr_time = order.raw.get('filled_at', '')
                    if curr_time > prev_time:
                        sell_fills[order.ticker] = order

        for ticker in vanished:
            # Find the local state position data
            local_pos = next((p for p in state.get("positions", []) if p.get("ticker") == ticker), {})
            entry_price = local_pos.get("entry_price", 0)
            shares = local_pos.get("shares", 0)
            strategy = local_pos.get("strategy", "unknown")
            entry_date = local_pos.get("entry_date", "")

            fill = sell_fills.get(ticker)
            if fill:
                exit_price = fill.fill_price
                order_type = fill.raw.get('order_type', 'unknown')
                filled_at = fill.raw.get('filled_at', trade_date)
                # Extract date from filled_at timestamp
                exit_date = filled_at[:10] if filled_at and len(filled_at) >= 10 else trade_date
                exit_reason = f"broker_{order_type}"  # e.g. "broker_trailing_stop"
            else:
                # No fill found — use closing price as estimate
                logger.warning("reconcile_broker_fills: no sell fill found for %s, using trade_date", ticker)
                exit_price = entry_price  # conservative: assume breakeven if no data
                exit_date = trade_date
                exit_reason = "broker_unknown"

            pnl = round((exit_price - entry_price) * shares, 2)

            trade_record = {
                "ticker": ticker,
                "strategy": strategy,
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_date": exit_date,
                "exit_price": exit_price,
                "shares": shares,
                "pnl": pnl,
                "pnl_pct": round((exit_price - entry_price) / entry_price * 100, 2) if entry_price else 0.0,
                "exit_type": exit_reason,
                "exit_reason": exit_reason,
                "reconciled": True,  # Flag that this was auto-detected, not locally triggered
            }

            # Don't double-record — check if already in closed_trades
            already_recorded = any(
                t.get("ticker") == ticker and t.get("exit_date") == exit_date
                for t in self.closed_trades
            )
            if already_recorded:
                logger.info("reconcile_broker_fills: %s already recorded as closed trade, skipping", ticker)
                continue

            self.record_closed_trade(trade_record)
            reconciled.append(trade_record)
            logger.info(
                "reconcile_broker_fills: RECORDED %s exit at $%.2f (%s) — PnL $%.2f",
                ticker, exit_price, exit_reason, pnl
            )

        return reconciled

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
            atlas_cash = starting_equity - sum(entry_costs) + total_realized_pnl

        This accounts for profits/losses from closed trades being returned
        to the cash pool, keeping Atlas equity continuous across trade exits.
        """
        atlas_pos = self.atlas_positions
        atlas_pos_value = sum(
            p.current_value(prices.get(p.ticker, p.entry_price) if prices else p.entry_price)
            for p in atlas_pos
        )
        # Infer cash: starting capital minus what is currently deployed,
        # plus realized P&L from closed trades.
        atlas_entry_cost = sum(p.entry_value for p in atlas_pos)
        total_realized_pnl = sum(t.get("pnl", 0) for t in self.closed_trades)
        atlas_cash = self.starting_equity - atlas_entry_cost + total_realized_pnl
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
            logger.warning(
                "SECTOR_CAP REJECTED: ticker=%s sector=%s current_count=%d cap=%d "
                "(existing positions path)",
                getattr(signal, "ticker", "?"), sector, sector_count, self.max_sector_conc,
            )

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

    # ── Per-market equity attribution ─────────────────────────────────────────

    def _get_per_market_equity(
        self,
        current_broker_eq: float,
        prices: dict[str, float] | None = None,
    ) -> float | None:
        """Return this market's estimated allocated equity using live cash attribution.

        Uses a two-component formula:
        - **Position MV**: sum of this market's current positions × current prices
          (from ``prices`` dict, falling back to ``entry_price``).
        - **Live cash share**: snapshot cash_attributed + realized cash flows on this
          market's symbols (FILL + DIV activities) since the snapshot_time.

        This replaces the stale-snap_cash proportional scaling formula which caused
        phantom drawdowns when positions exited intraday: the exit proceeds moved from
        position_mv → broker cash but snap_cash was locked to yesterday's value.
        (#FIX-PMEQ-001, 2026-05-01)

        Degraded mode: if the Alpaca activities API is unavailable, falls back to
        ``live_cash = snap_cash`` (frozen, no scaling).  ``self._per_market_equity_degraded``
        is set to True so ``check_daily_drawdown`` can suppress the kill switch below the
        catastrophic 20% threshold.

        Legacy fallback: if the snapshot row lacks ``position_mv`` / ``cash_attributed``
        columns (both zero), falls back to proportional scaling of ``allocated_equity``.
        Maintains backwards-compat with rows written before these columns existed.

        Returns ``None`` on DB failure, missing table, or stale snapshot (>3 days).
        """
        try:
            from db.atlas_db import get_db
            with get_db() as db:
                row = db.execute(
                    """
                    SELECT allocated_equity, broker_equity, date,
                           position_mv, cash_attributed, snapshot_time
                    FROM market_equity_history
                    WHERE market_id = ?
                    ORDER BY date DESC, created_at DESC
                    LIMIT 1
                    """,
                    (self.market_id,),
                ).fetchone()
        except Exception as exc:
            logger.debug("_get_per_market_equity DB read failed: %s", exc)
            return None

        if row is None:
            logger.debug(
                "_get_per_market_equity: no market_equity_history row for %s",
                self.market_id,
            )
            return None

        snap_alloc: float = row["allocated_equity"] or 0.0
        snap_broker: float = row["broker_equity"] or 0.0
        snap_pos_mv: float = row["position_mv"] or 0.0
        snap_cash: float = row["cash_attributed"] or 0.0
        snap_date: str = row["date"] or ""
        snap_time_str: str = row["snapshot_time"] or ""

        # Reject stale snapshot (>3 trading days old)
        try:
            from datetime import date as _date
            snap_days_old = (_date.today() - _date.fromisoformat(snap_date)).days
            if snap_days_old > 3:
                logger.debug(
                    "_get_per_market_equity: snapshot for %s is %d days old — too stale, "
                    "falling back to global broker equity",
                    self.market_id, snap_days_old,
                )
                return None
        except (ValueError, TypeError):
            pass  # Malformed date — just proceed

        if snap_broker <= 0 or current_broker_eq <= 0:
            return None

        cash_scale = current_broker_eq / snap_broker  # kept for legacy path only

        # ── New formula: position MV + live cash attribution ──────────────────
        if snap_pos_mv > 0 or snap_cash > 0:
            # Position MV: sum current prices × shares for this market's positions
            if self.positions:
                current_pos_mv = sum(
                    p.shares * ((prices or {}).get(p.ticker) or p.entry_price)
                    for p in self.positions
                )
            else:
                current_pos_mv = 0.0

            # ── Live cash attribution ─────────────────────────────────────────
            # Parse snapshot_time so we can fetch activities since that moment.
            snap_time: datetime | None = None
            if snap_time_str:
                try:
                    _ts_str = snap_time_str.replace("Z", "+00:00")
                    snap_time = datetime.fromisoformat(_ts_str)
                    if snap_time.tzinfo is None:
                        snap_time = snap_time.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pass  # Malformed snapshot_time — will degrade gracefully below

            if snap_time is not None and self._broker is not None:
                # Live path: fetch realized cash flows since the snapshot
                from portfolio.per_market_cash_flow import compute_realized_cash_flow_since
                # market_symbols keys define which markets we track;
                # values (ticker sets) are not used internally — derive_universe routes.
                market_symbols: dict[str, set[str]] = {
                    m: set() for m in _ALL_TRADED_MARKETS
                }
                cash_flows, degraded = compute_realized_cash_flow_since(
                    self._broker, snap_time, market_symbols
                )
                if degraded:
                    live_cash = snap_cash  # frozen: best we can do without activities
                    self._per_market_equity_degraded = True
                    logger.warning(
                        "_get_per_market_equity %s: activities API DEGRADED — "
                        "using frozen snap_cash=$%.2f (since=%s)",
                        self.market_id, snap_cash, snap_time_str,
                    )
                else:
                    live_cash = snap_cash + cash_flows.get(self.market_id, 0.0)
                    self._per_market_equity_degraded = False
                    logger.debug(
                        "_get_per_market_equity %s: snap_cash=$%.2f "
                        "+ cash_flow=$%.2f → live_cash=$%.2f",
                        self.market_id, snap_cash,
                        cash_flows.get(self.market_id, 0.0), live_cash,
                    )
            else:
                # No broker connection or no valid snapshot_time.
                # Use snap_cash as-is; mark degraded so kill switch is suppressed.
                live_cash = snap_cash
                self._per_market_equity_degraded = True
                logger.debug(
                    "_get_per_market_equity %s: no broker or no snap_time "
                    "(broker=%s snap_time=%s) — using frozen snap_cash=$%.2f",
                    self.market_id,
                    "connected" if self._broker is not None else "None",
                    snap_time_str or "missing",
                    snap_cash,
                )

            per_market_eq = current_pos_mv + live_cash
            logger.debug(
                "_get_per_market_equity %s (live-cash formula): "
                "pos_mv=$%.2f + live_cash=$%.2f (snap_cash=$%.2f + flow=$%.2f) "
                "→ per_market=$%.2f | degraded=%s",
                self.market_id, current_pos_mv, live_cash,
                snap_cash, live_cash - snap_cash, per_market_eq,
                self._per_market_equity_degraded,
            )

        else:
            # ── Legacy fallback: no position/cash breakdown in snapshot ───────
            # Proportional scaling of full allocated equity — less accurate but
            # safe for rows written before position_mv/cash_attributed columns existed.
            per_market_eq = snap_alloc * cash_scale
            self._per_market_equity_degraded = False
            logger.debug(
                "_get_per_market_equity %s (legacy proportional): "
                "snap_alloc=$%.2f snap_broker=$%.2f current_broker=$%.2f → per_market=$%.2f",
                self.market_id, snap_alloc, snap_broker, current_broker_eq, per_market_eq,
            )

        return per_market_eq

    def check_daily_drawdown(self, prices: dict[str, float] = None):
        """Check if daily drawdown limit breached.

        Uses per-market allocated equity (from ``market_equity_history``) as the
        primary equity measure so each market has an independent drawdown HWM.
        Falls back to global broker equity if attribution data is unavailable,
        then to internal equity() if broker returns 0.

        Per-market equity is scaled to the current total broker equity to stay
        current between EOD attribution snapshots.

        Resets daily_high_water at the start of each new calendar day so
        yesterday's high-water mark cannot trigger a false HALT today.
        """
        today_str = datetime.now().strftime("%Y-%m-%d")

        # Determine effective equity — prefer per-market attributed equity
        broker_eq = self.broker_equity()
        if broker_eq > 0:
            per_market_eq = self._get_per_market_equity(broker_eq, prices)
            if per_market_eq is not None:
                effective_eq = per_market_eq
                logger.debug(
                    "check_daily_drawdown %s: using per-market equity $%.2f "
                    "(global broker $%.2f)",
                    self.market_id, effective_eq, broker_eq,
                )
            else:
                # No per-market data → fall back to global broker equity
                effective_eq = broker_eq
                logger.debug(
                    "check_daily_drawdown %s: per-market equity unavailable, "
                    "using global broker equity $%.2f",
                    self.market_id, broker_eq,
                )
        else:
            effective_eq = self.equity(prices)
            logger.warning(
                "check_daily_drawdown: broker_equity() returned 0 — falling back "
                "to internal equity() (broker_data_valid=%s). "
                "Drawdown computed from potentially stale equity $%.2f.",
                self.broker_data_valid, effective_eq,
            )

        # SESSION RESET: new calendar day → reset HWM to today's broker equity
        if self.daily_high_water_date != today_str:
            old_hwm = self.daily_high_water
            old_date = self.daily_high_water_date
            self.daily_high_water = effective_eq
            self.daily_high_water_date = today_str
            self._hwm_reset_at = datetime.now()  # start 1h cooldown (see HALT guard below)
            logger.info(
                "Session HWM reset: $%.2f → $%.2f for %s (date %s → %s)",
                old_hwm, effective_eq, self.market_id, old_date, today_str,
            )
            # Best-effort Telegram notification — never blocks or raises
            try:
                from utils.telegram import send_message as _tg_send
                _tg_send(
                    f"📊 HWM reset for {self.market_id}: "
                    f"${old_hwm:.2f} → ${effective_eq:.2f} "
                    f"(date {old_date} → {today_str})"
                )
            except Exception as _tg_exc:
                logger.debug(
                    "HWM reset Telegram notification failed (non-fatal): %s", _tg_exc
                )
        else:
            # Same session — ratchet HWM up only if equity has grown
            self.daily_high_water = max(self.daily_high_water, effective_eq)

        # Compute drawdown against current session HWM
        dd = (
            (self.daily_high_water - effective_eq) / self.daily_high_water
            if self.daily_high_water > 0
            else 0.0
        )

        # _HALT_COOLDOWN: within 1h of HWM reset, suppress marginal false-positive halts.
        # The per-market equity formula stabilises over a few minutes after calibration.
        # Override threshold (>20%) still fires — this only protects against drift artefacts.
        _HALT_COOLDOWN_SECS = 3600        # 1 hour
        _HALT_COOLDOWN_OVERRIDE_DD = 0.20  # catastrophic drawdown always halts

        if dd >= self.max_daily_dd:
            _now_dt = datetime.now()
            if (
                self._hwm_reset_at is not None
                and dd < _HALT_COOLDOWN_OVERRIDE_DD
                and (_now_dt - self._hwm_reset_at).total_seconds() < _HALT_COOLDOWN_SECS
            ):
                logger.warning(
                    "HALT suppressed — within 1h of HWM reset "
                    "(would have halted at %.2f%% drawdown on %s)",
                    dd * 100, self.market_id,
                )
                return False, dd

            # Degraded-mode guard: if per-market cash flow could not be reconciled
            # (Alpaca activities API unavailable), do NOT trip the kill switch on a
            # stale snap_cash estimate.  The catastrophic 20% override still fires.
            if (
                getattr(self, "_per_market_equity_degraded", False)
                and dd < _HALT_COOLDOWN_OVERRIDE_DD
            ):
                logger.warning(
                    "HALT suppressed for %s — per-market cash flow attribution DEGRADED "
                    "(activities API unavailable). Drawdown=%.2f%% but cannot trust "
                    "snap_cash. Catastrophic override (20%%) still active.",
                    self.market_id, dd * 100,
                )
                return False, dd

            self.halted = True
            self.halt_reason = f"Daily drawdown {dd:.2%} >= {self.max_daily_dd:.2%}"
            logger.warning("HALT: %s", self.halt_reason)
            # Write kill_switch HALT file (belt-and-suspenders — also gates execute_approved)
            try:
                from brokers import kill_switch as _ks
                _ks.halt(f"daily_drawdown {dd:.2%} on {self.market_id}")
                logger.info("kill_switch HALT file written for %s", self.market_id)
            except Exception as _ks_exc:
                logger.warning(
                    "kill_switch.halt() failed during drawdown halt: %s", _ks_exc
                )
                try:
                    from utils.telegram import send_message as _tg_send
                    _tg_send(
                        f"⚠️ Drawdown HALT engaged but kill_switch file write FAILED: "
                        f"{_ks_exc}. market_state + JSON halt persists."
                    )
                except Exception as _tg_err:
                    logger.debug(
                        "Telegram notification failed during drawdown halt (non-fatal): %s",
                        _tg_err,
                    )
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
        """Append a closed trade and persist.

        Also triggers an asynchronous dashboard refresh so strategy
        performance metrics are always current after any position close.
        """
        # ── Validation: reject ghost trades (exit before entry) ──
        entry_date = str(trade_record.get("entry_date", ""))[:10]
        exit_date = str(trade_record.get("exit_date", ""))[:10]
        ticker = trade_record.get("ticker", "???")
        if entry_date and exit_date and exit_date < entry_date:
            logger.warning(
                "record_closed_trade: REJECTED ghost trade for %s — "
                "exit_date %s is before entry_date %s",
                ticker, exit_date, entry_date,
            )
            return

        # ── Validation: reject duplicate trades ──
        entry_price = trade_record.get("entry_price", 0)
        shares = trade_record.get("shares", 0)
        for existing in self.closed_trades:
            if (existing.get("ticker") == ticker
                    and str(existing.get("entry_date", ""))[:10] == entry_date
                    and existing.get("entry_price") == entry_price
                    and existing.get("shares") == shares
                    and str(existing.get("exit_date", ""))[:10] == exit_date):
                logger.warning(
                    "record_closed_trade: REJECTED duplicate trade for %s — "
                    "same ticker/entry_price/shares/exit_date already recorded",
                    ticker,
                )
                return

        self.closed_trades.append(trade_record)
        self.save_state()
        self._trigger_dashboard_refresh()

    # Class-level debounce: skip if another refresh was triggered within 60s
    _last_dashboard_trigger: float = 0.0

    def _trigger_dashboard_refresh(self):
        """Fire-and-forget dashboard data regeneration.

        Runs generate_data.py in a detached subprocess so the caller
        (execution, EOD settlement, etc.) is never blocked.  Debounced
        at 60 s so batch exits (e.g. 5 stops in one EOD run) only spawn
        one refresh.  Failures are logged but never propagated —
        dashboard staleness must not break the trading pipeline.
        """
        import subprocess
        import time

        now = time.monotonic()
        if now - LivePortfolio._last_dashboard_trigger < 60:
            logger.debug("Dashboard refresh skipped (debounce)")
            return
        LivePortfolio._last_dashboard_trigger = now

        try:
            project = Path(__file__).resolve().parent.parent
            subprocess.Popen(
                ["python3", str(project / "dashboard" / "generate_data.py")],
                cwd=str(project),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # fully detached
            )
            logger.debug("Dashboard refresh triggered (async)")
        except Exception as exc:
            logger.warning("Dashboard refresh trigger failed (non-fatal): %s", exc)
