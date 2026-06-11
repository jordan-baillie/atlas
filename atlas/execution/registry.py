"""live/registry.py — registry of DEPLOYED strategies for the forge->live pipeline.

Each entry is a target-weight book + its lifecycle state (shadow -> canary -> live) + capital slice + broker +
modeled expectation. Backed by ``config/live_strategies.json``. Starts EMPTY — a strategy enters only after a
stage-2 PASS + human approval (board 2026-06-09). A book produces ``{symbol: weight}`` via a named PROVIDER
(registered in code), so the JSON stays declarative and no callables are serialized.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional
from atlas.kernel.paths import CONFIG_DIR, PROJECT_ROOT


REGISTRY_PATH = PROJECT_ROOT / "config" / "live_strategies.json"

# name -> callable(asof_date) -> {symbol: target_weight}.  Providers are registered in code (BOREAS, a frozen
# forge spec, etc.); the registry JSON references them by name.
PROVIDERS: dict[str, Callable] = {}


def register_provider(name: str):
    def deco(fn: Callable) -> Callable:
        PROVIDERS[name] = fn
        return fn
    return deco


@dataclass
class DeployedStrategy:
    name: str
    provider: str                       # key into PROVIDERS
    state: str = "shadow"               # shadow | canary | live
    broker: str = "alpaca"             # registry broker name (alpaca | ib)
    capital: float = 0.0               # deployable equity slice (USD); canary <= 250 per board
    approved: bool = False             # human-approved for real-money execution
    specs: dict = field(default_factory=dict)        # {symbol: {multiplier, lot, min_notional}}
    expectation: dict = field(default_factory=dict)  # {daily_mean, daily_std, sharpe} (modeled backtest)

    def target_portfolio(self, asof_date) -> dict:
        fn = PROVIDERS.get(self.provider)
        if fn is None:
            return {}
        return fn(asof_date) or {}


def load() -> list[DeployedStrategy]:
    if not REGISTRY_PATH.exists():
        return []
    try:
        rows = json.loads(REGISTRY_PATH.read_text()) or []
        return [DeployedStrategy(**r) for r in rows]
    except Exception:
        return []


def save(strategies: list[DeployedStrategy]) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps([asdict(s) for s in strategies], indent=2))


def deployed(state: Optional[str] = None) -> list[DeployedStrategy]:
    out = load()
    return [s for s in out if state is None or s.state == state]


# ── lifecycle mutations (human-gated; board 2026-06-09) ───────────────────────
def upsert(strategy: DeployedStrategy) -> None:
    items = [s for s in load() if s.name != strategy.name] + [strategy]
    save(items)


def update(name: str, **changes) -> bool:
    items, found = load(), False
    for s in items:
        if s.name == name:
            for k, v in changes.items():
                setattr(s, k, v)
            found = True
    if found:
        save(items)
    return found


def approve(name: str) -> bool:
    """Human-approve a strategy for real-money execution (board: approval on every go-live/scale-up)."""
    return update(name, approved=True)


def set_state(name: str, state: str) -> bool:
    """Move a strategy through shadow -> canary -> live."""
    assert state in ("shadow", "canary", "live")
    return update(name, state=state)


if __name__ == "__main__":   # tiny CLI: python3 -m live.registry [list|approve NAME|state NAME shadow|canary|live]
    import sys
    a = sys.argv[1:]
    if not a or a[0] == "list":
        for s in load():
            print(f"  {s.name:24s} state={s.state:7s} approved={s.approved} broker={s.broker} cap=${s.capital:.0f} provider={s.provider}")
    elif a[0] == "approve" and len(a) == 2:
        print("approved" if approve(a[1]) else "not found")
    elif a[0] == "state" and len(a) == 3:
        print("updated" if set_state(a[1], a[2]) else "not found")
    else:
        print("usage: python3 -m live.registry [list | approve NAME | state NAME shadow|canary|live]")
