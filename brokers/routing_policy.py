"""brokers/routing_policy.py — Broker-routing policy for a (config, universe) tuple.

Encapsulates every routing decision needed by ops scripts and the live executor:
when to skip a universe, when to run a paper-pass alongside the live pass, how
to derive the paper-mode config, how to split plan entries by lifecycle state,
and which DB tables to target for the current mode.

Replaces 5x duplicated routing boilerplate (reconcile_ledger, sync_protective_orders,
intraday_monitor, eod_settlement, execute_approved) and 3x forked write sites
inside brokers/live_executor.py.

Usage
-----
    from brokers.routing_policy import BrokerRoutingPolicy

    policy = BrokerRoutingPolicy(config, market_id="sp500")

    if policy.should_skip():
        return  # universe disabled

    # Live pass
    run_live(policy)

    # Paper pass (only if there are open paper trades for this universe)
    if policy.needs_paper_pass():
        run_paper(policy.for_paper())
"""
from __future__ import annotations

import functools
import logging
from typing import Iterable

logger = logging.getLogger("atlas.broker_routing")


class BrokerRoutingPolicy:
    """Resolves broker-routing decisions for one (config, universe) tuple.

    Construction is cheap (two dict lookups). DB-backed methods
    (`needs_paper_pass`, `split_entries_by_lifecycle`) lazy-load and memoize.

    A policy is *immutable* once constructed. To switch to paper mode, call
    `for_paper()` — returns a new policy; never mutates the original.

    Parameters
    ----------
    config : dict
        Active config dict (output of `utils.config.get_active_config`).
    market_id : str
        Universe / market identifier (e.g. `"sp500"`, `"commodity_etfs"`).
    """

    def __init__(self, config: dict, market_id: str):
        self.config: dict = config
        self.market_id: str = market_id
        trading = config.get("trading", {})
        self.mode: str = trading.get("mode", "live")
        self.live_enabled: bool = bool(trading.get("live_enabled", False))

    # ── Mode predicates ───────────────────────────────────────────────────

    @property
    def is_paper(self) -> bool:
        """True if `trading.mode == "paper"`."""
        return self.mode == "paper"

    @property
    def is_live(self) -> bool:
        """True if `trading.mode == "live"`."""
        return self.mode == "live"

    @property
    def is_passive(self) -> bool:
        """True if `trading.mode == "passive"`."""
        return self.mode == "passive"

    # ── Decisions ─────────────────────────────────────────────────────────

    def should_skip(self) -> bool:
        """True if this universe is disabled and ops should bail out.

        A universe is skipped when:
          - mode == "passive", OR
          - mode == "live" AND live_enabled is False (safety gate).

        Paper mode runs regardless of `live_enabled` (paper account, no
        real capital at risk).
        """
        if self.is_passive:
            return True
        if self.is_live and not self.live_enabled:
            return True
        return False

    @functools.cached_property
    def _has_open_paper_trades(self) -> bool:
        """DB-backed: any open paper trade for this universe? Memoized."""
        try:
            from db.atlas_db import get_open_paper_trades
            return any(r.get("universe") == self.market_id for r in get_open_paper_trades())
        except Exception as exc:  # noqa: BLE001 — DB read; non-fatal
            logger.debug("needs_paper_pass DB error (non-fatal): %s", exc)
            return False

    def needs_paper_pass(self) -> bool:
        """True if a paper-mode pass should run *in addition* to the live pass.

        Returns True iff:
          - this is NOT already a paper-mode policy (`is_paper == False`), AND
          - there is at least one open paper trade for this universe in the DB.

        Result is memoized for the policy's lifetime.
        """
        if self.is_paper:
            return False
        return self._has_open_paper_trades

    # ── Derived configs / policies ────────────────────────────────────────

    @property
    def paper_config(self) -> dict:
        """The paper-mode config patch.

        Equivalent to `{**self.config, "trading": {**trading, "mode": "paper"}}`.
        Used by callers that need to construct paper executors / brokers
        directly (e.g. `LivePortfolio(policy.paper_config, market_id=...)`).
        """
        trading = self.config.get("trading", {})
        return {**self.config, "trading": {**trading, "mode": "paper"}}

    def for_paper(self) -> "BrokerRoutingPolicy":
        """Return a derived policy with mode forced to `"paper"`.

        Used between live and paper passes:

            run_live(policy)
            if policy.needs_paper_pass():
                run_paper(policy.for_paper())
        """
        return BrokerRoutingPolicy(self.paper_config, self.market_id)

    # ── Per-entry routing ─────────────────────────────────────────────────

    def split_entries_by_lifecycle(self, entries: Iterable[dict]) -> tuple[list, list]:
        """Split entry/exit records into `(live, paper)` by strategy lifecycle.

        Entries whose `strategy` is in PAPER lifecycle state for this universe
        route to paper. All others (LIVE / RESEARCH / RETIRED / missing
        strategy) route to live.

        On import failure of `monitor.strategy_lifecycle`, returns
        `(list(entries), [])` — safe fallback (everything stays live).

        Delegates to `monitor.strategy_lifecycle.split_trades_by_lifecycle`,
        which is the canonical implementation. This method exists so callers
        don't need to know the canonical location.
        """
        try:
            from monitor.strategy_lifecycle import split_trades_by_lifecycle
        except ImportError as exc:
            logger.warning(
                "split_entries_by_lifecycle: strategy_lifecycle import failed (%s) — "
                "routing all entries to live (safe fallback)",
                exc,
            )
            return list(entries), []
        return split_trades_by_lifecycle(list(entries), self.market_id)

    # ── DB target resolution ──────────────────────────────────────────────

    def trade_table(self) -> str:
        """Return the trades table name for the current mode.

        `"paper_trades"` if paper, `"trades"` otherwise.
        """
        return "paper_trades" if self.is_paper else "trades"

    def protective_table(self) -> str:
        """Return the position_protective_orders table name for the current mode."""
        return "paper_position_protective_orders" if self.is_paper else "position_protective_orders"
