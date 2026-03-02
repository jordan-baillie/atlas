"""Strategy Allocation Pools — per-strategy position slot management.

Prevents high-volume strategies (e.g. momentum_breakout) from monopolising
all available position slots when multiple strategies compete for the same
portfolio capacity.

Config schema (under config["allocation"]):
    {
        "enabled": true,
        "mode": "hard_pool",       # "hard_pool" | "soft_pool"
        "overflow_enabled": true,  # soft_pool: can borrow from _other pool
        "pools": {
            "trend_following":  {"max_positions": 5},
            "mean_reversion":   {"max_positions": 5},
            "opening_gap":      {"max_positions": 3},
            "_other":           {"max_positions": 2}
        }
    }

Modes:
    hard_pool — strategy is hard-capped at its pool limit.
    soft_pool — strategy can borrow from the _other (overflow) pool if its
                own pool is full but overflow slots are available.

The special "_other" key acts as a shared overflow pool for:
  1. Any strategy not explicitly named in pools.
  2. Overflow borrowing in soft_pool mode.

All config is optional — when "enabled" is false or the section is missing
entirely, the pools are a no-op and existing behavior is preserved.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StrategyAllocationPool:
    """Manages per-strategy position slot budgets.

    Instantiate once per backtest run or per plan generation call.
    Pass ``open_positions`` to each check so counts stay accurate.

    Args:
        config: Full Atlas config dict.  The ``allocation`` sub-section
                is read from this dict.
    """

    def __init__(self, config: Dict[str, Any]):
        alloc_cfg = config.get("allocation", {})
        self.enabled: bool = alloc_cfg.get("enabled", False)
        self.mode: str = alloc_cfg.get("mode", "hard_pool")
        self.overflow_enabled: bool = alloc_cfg.get("overflow_enabled", True)

        # pools: strategy_name -> max_positions
        raw_pools = alloc_cfg.get("pools", {})
        self.pools: Dict[str, int] = {
            k: v.get("max_positions", 999)
            for k, v in raw_pools.items()
        }
        self._other_cap: int = self.pools.get("_other", 0)
        if self.enabled:
            logger.debug(
                "StrategyAllocationPool: mode=%s, pools=%s",
                self.mode, self.pools,
            )

    # ── Public API ────────────────────────────────────────────

    def is_enabled(self) -> bool:
        return self.enabled

    def can_accept(
        self,
        strategy_name: str,
        open_positions: List[Dict[str, Any]],
    ) -> tuple[bool, str]:
        """Check whether a signal from *strategy_name* can be accepted.

        Args:
            strategy_name: Name of the strategy (e.g. 'trend_following').
            open_positions: Current list of open position dicts.  Each dict
                            must have a ``strategy`` key.

        Returns:
            (ok, reason) — ok=True if the signal may be accepted.
        """
        if not self.enabled:
            return True, "allocation disabled"

        strategy_count = self.count_by_strategy(strategy_name, open_positions)
        pool_cap = self._get_pool_cap(strategy_name)

        # ── Hard-pool check ───────────────────────────────────
        if self.mode == "hard_pool":
            if strategy_count >= pool_cap:
                return False, (
                    f"Allocation pool '{strategy_name}' full "
                    f"({strategy_count}/{pool_cap})"
                )
            return True, "within pool"

        # ── Soft-pool check ───────────────────────────────────
        # Own pool has room → accept
        if strategy_count < pool_cap:
            return True, "within pool"

        # Own pool full → check overflow pool
        if self.overflow_enabled and self._other_cap > 0:
            overflow_used = self._count_overflow_usage(open_positions)
            if overflow_used < self._other_cap:
                return True, "using overflow pool"
            return False, (
                f"Allocation pool '{strategy_name}' full "
                f"({strategy_count}/{pool_cap}) and overflow full "
                f"({overflow_used}/{self._other_cap})"
            )

        return False, (
            f"Allocation pool '{strategy_name}' full "
            f"({strategy_count}/{pool_cap})"
        )

    def count_by_strategy(
        self,
        strategy_name: str,
        open_positions: List[Dict[str, Any]],
    ) -> int:
        """Count open positions belonging to *strategy_name*."""
        return sum(
            1 for p in open_positions
            if p.get("strategy") == strategy_name
        )

    def counts_summary(
        self,
        open_positions: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, int]]:
        """Return a summary dict of used/cap for each pool.

        Useful for diagnostics and plan formatting.
        """
        if not self.enabled:
            return {}
        summary = {}
        strategies_seen = set()
        for p in open_positions:
            s = p.get("strategy", "_other")
            strategies_seen.add(s)
        # Include all configured pools
        for strategy_name in self.pools:
            if strategy_name == "_other":
                continue
            used = self.count_by_strategy(strategy_name, open_positions)
            cap = self.pools[strategy_name]
            summary[strategy_name] = {"used": used, "cap": cap}
        # Overflow pool
        if self._other_cap > 0:
            overflow_used = self._count_overflow_usage(open_positions)
            summary["_other"] = {"used": overflow_used, "cap": self._other_cap}
        return summary

    # ── Internal helpers ──────────────────────────────────────

    def _get_pool_cap(self, strategy_name: str) -> int:
        """Return pool cap for a strategy.  Falls back to _other cap."""
        if strategy_name in self.pools:
            return self.pools[strategy_name]
        # Strategy not explicitly configured — use _other cap if defined
        if "_other" in self.pools:
            return self.pools["_other"]
        # No limit configured
        return 999

    def _count_overflow_usage(
        self, open_positions: List[Dict[str, Any]]
    ) -> int:
        """Count positions that are using the overflow (_other) pool.

        A position uses the overflow pool when its strategy is either:
        - Explicitly mapped to '_other', or
        - Exceeds its own pool cap (soft-pool overflow borrowers).
        """
        # Count positions whose strategy is not explicitly in pools (or maps to _other)
        unnamed_count = sum(
            1 for p in open_positions
            if p.get("strategy") not in self.pools
            or p.get("strategy") == "_other"
        )
        # Count positions from named strategies that exceed their own pool cap
        overflow_from_named = 0
        if self.mode == "soft_pool":
            per_strat: Dict[str, int] = {}
            for p in open_positions:
                s = p.get("strategy", "_other")
                per_strat[s] = per_strat.get(s, 0) + 1
            for strat, cnt in per_strat.items():
                if strat in self.pools and strat != "_other":
                    cap = self.pools[strat]
                    overflow_from_named += max(0, cnt - cap)
        return unnamed_count + overflow_from_named


def build_allocation_pool(config: Dict[str, Any]) -> StrategyAllocationPool:
    """Factory helper — creates a StrategyAllocationPool from a config dict."""
    return StrategyAllocationPool(config)
