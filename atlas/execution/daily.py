"""live/daily.py — the daily forge->live ops loop (shadow-first).

For each DEPLOYED strategy: produce today's target book -> diff via TargetExecutor -> track-vs-expectation ->
record. **shadow** = the Paper Book: places REAL paper orders on live data (the board's forward-paper gate, $0
real). **canary/live** = real capital: held (dry) unless human-approved AND invoked in live mode. Kill-switch is
enforced INSIDE TargetExecutor (fail-closed). Real-money execution stays human-gated (board 2026-06-09).

Run: ``python3 -m live.daily [--mode shadow|live] [--date YYYY-MM-DD]``
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atlas.execution.target_executor import ContractSpec, TargetExecutor
from atlas.execution import registry
from atlas.execution.registry import DeployedStrategy
from atlas.kernel.paths import LIVE_DATA_DIR, PROJECT_ROOT

logger = logging.getLogger("atlas.live.daily")
LIVE_DATA = LIVE_DATA_DIR


@dataclass
class StrategyRunResult:
    name: str
    state: str = "shadow"
    broker: str = ""
    n_orders: int = 0
    turnover: float = 0.0
    executed: int = 0
    dry_run: bool = True
    track_status: Optional[str] = None
    blocked: Optional[str] = None
    awaiting_approval: bool = False
    error: Optional[str] = None


@dataclass
class DailyReport:
    date: str
    mode: str
    results: list = field(default_factory=list)   # list[StrategyRunResult]

    @property
    def n_strategies(self) -> int:
        return len(self.results)


def _build_broker(s: DeployedStrategy):
    from atlas.brokers.registry import get_live_broker
    # REAL capital only for an approved canary/live strategy; everything else (shadow=Paper Book) is paper.
    real = s.state in ("canary", "live") and s.approved
    cfg = {"trading": {"broker": s.broker, "mode": "live" if real else "paper"}, "market": s.name}
    br = get_live_broker(cfg)
    if br is not None and not br.is_connected:
        try:
            br.connect()
        except Exception as e:
            logger.warning("broker connect failed for %s: %s", s.name, e)
    return br


def _realized_returns(name: str) -> list:
    f = LIVE_DATA / name / "returns.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text().splitlines():
        try:
            out.append(float(json.loads(line)["ret"]))
        except Exception:
            continue
    return out


def _record_run(s: DeployedStrategy, asof: str, report, track) -> None:
    d = LIVE_DATA / s.name
    d.mkdir(parents=True, exist_ok=True)
    rec = {"date": asof, "state": s.state, "dry_run": report.dry_run, "n_orders": report.n_orders,
           "turnover": round(report.turnover_notional, 2), "blocked": report.blocked,
           "track": (track.status if track else None),
           "orders": [{"ticker": o.ticker, "side": o.side.value, "qty": o.qty, "px": o.ref_price} for o in report.orders]}
    with (d / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def run_strategy(s: DeployedStrategy, asof: str, mode: str = "shadow", broker=None) -> StrategyRunResult:
    try:
        broker = broker or _build_broker(s)
        if broker is None or not getattr(broker, "is_connected", False):
            return StrategyRunResult(s.name, s.state, s.broker, error="broker unavailable")
        weights = s.target_portfolio(asof)
        specs = {k: ContractSpec(**v) for k, v in (s.specs or {}).items()}
        ex = TargetExecutor(broker, specs=specs)
        # shadow = Paper Book: place REAL paper orders on live data (the forward-paper gate).
        # canary/live = real capital: held (dry) unless human-approved AND invoked in live mode.
        dry = s.state in ("canary", "live") and (not s.approved or mode != "live")
        # VIRTUAL SUB-BOOK (shadow only): N strategies share one paper account, so each diffs against
        # its OWN book — never the account's blended positions (live/virtual_book.py). Canary/live
        # strategies run on dedicated real accounts and keep diffing against true account positions.
        book = None
        if s.state == "shadow":
            from atlas.execution.virtual_book import VirtualBook
            book = VirtualBook(s.name, capital_base=(s.capital or 0.0))
        rep = ex.rebalance(weights, deployable_equity=(s.capital or None), dry_run=dry,
                           current_qty=(book.current_qty() if book is not None else None))
        if book is not None and not rep.dry_run and not rep.blocked:
            # Apply this strategy's OWN successful fills to its book at the reference price.
            filled = {(getattr(r, "ticker", None), getattr(r, "side", None))
                      for r in rep.results if getattr(r, "success", False)}
            for o in rep.orders:
                if (o.ticker, o.side) in filled:
                    book.apply_fill(o.ticker, o.side.value, o.qty, o.ref_price, ex._spec(o.ticker).multiplier)
            book.save()

        track = None
        if s.expectation:
            from atlas.execution.track_expectation import Expectation, evaluate
            track = evaluate(_realized_returns(s.name), Expectation(**s.expectation))

        _record_run(s, asof, rep, track)
        # a canary/live strategy with orders but no human approval is held (executed as dry_run) -> flag it
        awaiting = s.state in ("canary", "live") and not s.approved and rep.n_orders > 0
        return StrategyRunResult(s.name, s.state, s.broker, rep.n_orders, rep.turnover_notional,
                                 len(rep.executed), rep.dry_run, (track.status if track else None),
                                 rep.blocked, awaiting)
    except Exception as e:
        logger.exception("run_strategy %s failed", s.name)
        return StrategyRunResult(s.name, s.state, s.broker, error=str(e))


def _send_telegram(text: str) -> None:
    """Best-effort Telegram digest via the kernel notifier (no-op without creds)."""
    try:
        from atlas.kernel.notify import send_message
        send_message(text)
    except Exception as e:
        logger.debug("telegram digest skipped: %s", e)


def _digest(report: DailyReport) -> str:
    lines = [f"\U0001f9ed <b>Live {report.mode} {report.date}</b> — {report.n_strategies} strateg" +
             ("y" if report.n_strategies == 1 else "ies")]
    for r in report.results:
        tag = "⛔HALT" if r.blocked else ("⚠️DIVERGING" if r.track_status == "diverging" else "✅")
        appr = " \U0001f7e1AWAITING APPROVAL" if r.awaiting_approval else ""
        err = f" err={r.error}" if r.error else ""
        lines.append(f"• {r.name} [{r.state}/{r.broker}] {tag} orders={r.n_orders} exec={r.executed} "
                     f"dry={r.dry_run} track={r.track_status}{appr}{err}")
    return "\n".join(lines)


def run_daily(mode: str = "shadow", asof: Optional[str] = None, strategies=None, notify: bool = True) -> DailyReport:
    import atlas.execution.providers  # noqa: F401  (register target-portfolio providers)
    asof = asof or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    strategies = strategies if strategies is not None else registry.deployed()
    report = DailyReport(asof, mode, [run_strategy(s, asof, mode) for s in strategies])
    if not strategies:
        logger.info("daily(%s): no deployed strategies — nothing to do", mode)
    out = LIVE_DATA / "daily"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{asof}.json").write_text(json.dumps(
        {"date": asof, "mode": mode, "results": [asdict(r) for r in report.results]}, indent=2))
    # monitoring: digest only when there's something to report (a strategy ran, halted, diverged, or awaits approval)
    if notify and (strategies and any(r.n_orders or r.blocked or r.awaiting_approval or r.error or
                                      r.track_status == "diverging" for r in report.results)):
        _send_telegram(_digest(report))
    return report


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="shadow", choices=["shadow", "live"])
    ap.add_argument("--date", default=None)
    a = ap.parse_args()
    rep = run_daily(mode=a.mode, asof=a.date)
    print(f"daily({rep.mode}) {rep.date}: {rep.n_strategies} strategies")
    for r in rep.results:
        print(f"  {r.name} [{r.state}/{r.broker}] orders={r.n_orders} exec={r.executed} "
              f"dry={r.dry_run} track={r.track_status} blocked={r.blocked} err={r.error}")
