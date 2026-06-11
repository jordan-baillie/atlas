"""live/providers.py — target-portfolio PROVIDERS for the Paper Book / live pipeline.

A provider is ``asof_date -> {symbol: target_weight}``, registered by name; a DeployedStrategy references it by
name. Forge PASSes feed the Paper Book through a FILE contract (decouples Atlas from Hephaestus):

    data/live/<name>/target.json = {"asof": "YYYY-MM-DD", "weights": {symbol: w}, "strategy_path": "..."}

Hephaestus computes today's weights from the strategy's signal on live data and writes that file; Atlas's
``forge_strategy_provider`` just reads it. ``deploy_pass`` registers a PASS into the book (state='shadow' =
paper-on-live-data; autonomous, no real capital — real capital stays human-gated, board 2026-06-09).
"""
from __future__ import annotations

import json
from pathlib import Path

from atlas.execution.registry import PROVIDERS, DeployedStrategy, deployed, register_provider, upsert
from atlas.kernel.paths import LIVE_DATA_DIR

LIVE_DATA = LIVE_DATA_DIR


# ── static providers ──────────────────────────────────────────────────────────
@register_provider("boreas_carry_trend")
def boreas_carry_trend(asof_date) -> dict:
    """BOREAS carry+trend micro-futures book. Stub until the 2026-08-28 verdict + productionization."""
    return {}


def static_provider(weights: dict):
    def fn(asof_date) -> dict:
        return dict(weights)
    return fn


# ── forge PASS -> Paper Book bridge (generic, file-based) ──────────────────────
def forge_strategy_provider(name: str):
    """Provider that reads the daily target-weights file Hephaestus writes for a deployed PASS strategy.
    Returns {} if the file is missing (strategy flattens / no-op) — safe by default."""
    def fn(asof_date) -> dict:
        f = LIVE_DATA / name / "target.json"
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text()).get("weights", {}) or {}
        except Exception:
            return {}
    return fn


def deploy_pass(name: str, *, capital: float = 10000.0, broker: str = "alpaca",
                expectation: dict | None = None, strategy_path: str = "") -> DeployedStrategy:
    """Register a forge PASS into the Paper Book as a paper-traded (shadow) strategy.

    state='shadow' => the daily loop places REAL paper orders on live data (the forward-paper gate). No real
    capital, so no human approval needed here; promotion to canary/live (real capital) IS human-gated.
    """
    register_provider(name)(forge_strategy_provider(name))
    s = DeployedStrategy(name=name, provider=name, state="shadow", broker=broker,
                         capital=capital, approved=False, expectation=expectation or {})
    upsert(s)
    d = LIVE_DATA / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(
        {"name": name, "strategy_path": strategy_path, "deployed_at": __import__("datetime").date.today().isoformat()},
        indent=2))
    return s


# auto-register a file-provider for any deployed strategy not covered by a static provider,
# so the daily loop (a fresh process) resolves PROVIDERS[name] for every deployed PASS.
for _s in deployed():
    if _s.provider not in PROVIDERS:
        register_provider(_s.provider)(forge_strategy_provider(_s.name))
