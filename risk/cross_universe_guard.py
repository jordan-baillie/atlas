"""Cross-universe position & buying-power guard for entry orders.

Apr 24 lesson: each universe sizes positions against FULL equity, ignoring
positions in OTHER universes. Result: 1.75× effective leverage and -$4063 cash.

This module enforces:
  1. Hard cap on total simultaneous positions across all universes
  2. Positive buying-power requirement at order submission time

Risk-reducing orders (exits, stops, TPs) are NEVER gated — the guard is invoked
ONLY from the entry path (_execute_entry in live_executor.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_GLOBAL_RISK_CONFIG = Path("/root/atlas/config/global_risk.json")

# Defensive defaults: guard ON even if config file missing/malformed
_DEFAULT_MAX_POSITIONS = 8
_DEFAULT_REQUIRE_POSITIVE_CASH = True
_DEFAULT_ENABLED = True


@dataclass
class GuardConfig:
    enabled: bool
    global_max_positions: int
    require_positive_cash: bool


@dataclass
class GuardDecision:
    allowed: bool
    reason: str = ""
    positions_count: Optional[int] = None
    positions_cap: Optional[int] = None
    buying_power: Optional[float] = None
    estimated_order_cost: Optional[float] = None


def load_guard_config() -> GuardConfig:
    """Load guard config from config/global_risk.json. Defaults are ON."""
    if not _GLOBAL_RISK_CONFIG.exists():
        logger.info(
            "cross_universe_guard: config not found at %s — using defaults (enabled=True, max=%d)",
            _GLOBAL_RISK_CONFIG, _DEFAULT_MAX_POSITIONS,
        )
        return GuardConfig(
            enabled=_DEFAULT_ENABLED,
            global_max_positions=_DEFAULT_MAX_POSITIONS,
            require_positive_cash=_DEFAULT_REQUIRE_POSITIVE_CASH,
        )
    try:
        with _GLOBAL_RISK_CONFIG.open() as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(
            "cross_universe_guard: failed to load %s (%s) — using defaults",
            _GLOBAL_RISK_CONFIG, e,
        )
        return GuardConfig(
            enabled=_DEFAULT_ENABLED,
            global_max_positions=_DEFAULT_MAX_POSITIONS,
            require_positive_cash=_DEFAULT_REQUIRE_POSITIVE_CASH,
        )
    cug = data.get("cross_universe_guard", {})
    return GuardConfig(
        enabled=bool(cug.get("enabled", _DEFAULT_ENABLED)),
        global_max_positions=int(cug.get("global_max_positions", _DEFAULT_MAX_POSITIONS)),
        require_positive_cash=bool(cug.get("require_positive_cash", _DEFAULT_REQUIRE_POSITIVE_CASH)),
    )


def count_open_positions_all_universes() -> int:
    """Count open positions across ALL universes/markets in trades table."""
    try:
        from db.atlas_db import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM trades WHERE status='open'"
            ).fetchone()
            return int(row[0] if row else 0)
    except Exception as e:
        logger.error("cross_universe_guard: failed to count positions: %s", e)
        # Defensive: return very high count to BLOCK on DB error rather than allow unbounded
        return 9999


def available_buying_power(broker) -> float:
    """Query broker for cash + margin headroom, clamp to >= 0.

    Tries broker.get_account() first (Alpaca trade_client style), then falls
    back to broker.get_account_info() (AlpacaBroker adapter style which returns
    an AccountInfo dataclass with a .buying_power attribute).

    Returns 0.0 if broker is None or all queries fail.
    """
    try:
        if broker is None:
            return 0.0
        # Path 1: broker.get_account() — returns dict-like (e.g. raw Alpaca trade_client)
        if hasattr(broker, "get_account"):
            acct = broker.get_account()
            if acct is not None:
                for key in ("buying_power", "non_marginable_buying_power", "cash"):
                    v = acct.get(key) if isinstance(acct, dict) else getattr(acct, key, None)
                    if v is not None:
                        return max(0.0, float(v))
        # Path 2: broker.get_account_info() — AlpacaBroker adapter, returns AccountInfo dataclass
        if hasattr(broker, "get_account_info"):
            acct_info = broker.get_account_info()
            if acct_info is not None:
                bp = getattr(acct_info, "buying_power", None)
                if bp is not None:
                    return max(0.0, float(bp))
        return 0.0
    except Exception as e:
        logger.error("cross_universe_guard: failed to read buying power: %s", e)
        return 0.0


def check_entry(
    *,
    ticker: str,
    universe: str,
    qty: int,
    price: float,
    broker=None,
    config: Optional[GuardConfig] = None,
) -> GuardDecision:
    """Evaluate whether an entry order should be allowed.

    Args:
        ticker:   The symbol being entered.
        universe: 'sp500' / 'sector_etfs' / 'commodity_etfs' / etc.
        qty:      Proposed share count.
        price:    Proposed entry price (used for cost calculation).
        broker:   Broker handle (must expose .get_account() or .get_account_info()).
        config:   Optional override; loads from disk if None.

    Returns:
        GuardDecision with allowed=True/False + reason + diagnostics.
    """
    cfg = config if config is not None else load_guard_config()

    if not cfg.enabled:
        return GuardDecision(allowed=True, reason="guard disabled in config")

    # 1. Position count check
    n_open = count_open_positions_all_universes()
    if n_open >= cfg.global_max_positions:
        reason = (
            f"position cap reached: {n_open}/{cfg.global_max_positions} "
            f"open positions across all universes"
        )
        return GuardDecision(
            allowed=False,
            reason=reason,
            positions_count=n_open,
            positions_cap=cfg.global_max_positions,
        )

    # 2. Buying power check
    estimated_cost = float(qty) * float(price) if qty and price else 0.0
    if cfg.require_positive_cash:
        bp = available_buying_power(broker)
        if bp <= 0:
            return GuardDecision(
                allowed=False,
                reason=f"no buying power: ${bp:.2f}",
                positions_count=n_open,
                positions_cap=cfg.global_max_positions,
                buying_power=bp,
                estimated_order_cost=estimated_cost,
            )
        if estimated_cost > bp:
            return GuardDecision(
                allowed=False,
                reason=f"order cost ${estimated_cost:.2f} exceeds buying power ${bp:.2f}",
                positions_count=n_open,
                positions_cap=cfg.global_max_positions,
                buying_power=bp,
                estimated_order_cost=estimated_cost,
            )
        return GuardDecision(
            allowed=True,
            reason=f"ok ({n_open}/{cfg.global_max_positions} positions, bp=${bp:.2f})",
            positions_count=n_open,
            positions_cap=cfg.global_max_positions,
            buying_power=bp,
            estimated_order_cost=estimated_cost,
        )

    return GuardDecision(
        allowed=True,
        reason=f"ok ({n_open}/{cfg.global_max_positions} positions)",
        positions_count=n_open,
        positions_cap=cfg.global_max_positions,
    )


def telegram_alert(ticker: str, universe: str, decision: GuardDecision) -> None:
    """Best-effort Telegram alert when guard rejects an entry."""
    try:
        from utils.telegram import send_message, tg_escape
        msg = (
            f"🛡️ [guard] entry rejected ticker={ticker} universe={universe} "
            f"reason={decision.reason}"
        )
        if decision.positions_count is not None:
            msg += f" (positions={decision.positions_count}/{decision.positions_cap}"
            if decision.buying_power is not None:
                msg += f", cash=${decision.buying_power:.2f}"
            msg += ")"
        send_message(tg_escape(msg))
    except Exception as e:
        logger.warning("cross_universe_guard: telegram alert failed: %s", e)
