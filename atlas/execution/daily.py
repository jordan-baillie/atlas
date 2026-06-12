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
    # join broker results back onto orders by (ticker, side): order_id enables next-day
    # fill reconciliation (record_fills.py -> slippage/broker-error go-live gates G6/G7);
    # ok=False rows feed the broker-error rate.
    res = {(getattr(r, "ticker", None), getattr(r, "side", None)): r for r in report.results}
    def _o(o):
        r = res.get((o.ticker, o.side))
        row = {"ticker": o.ticker, "side": o.side.value, "qty": o.qty, "px": o.ref_price,
               "order_id": (getattr(r, "order_id", "") or "") if r else "",
               "ok": bool(getattr(r, "success", False)) if r else None}
        # persist the broker rejection reason (task #19): without it G7 is a bare count that
        # can't distinguish operator bugs from market frictions (HTB/halt/wash-trade/inactive)
        if r is not None and not getattr(r, "success", False) and getattr(r, "message", None):
            row["err"] = str(r.message)[:160]
        fb = (getattr(r, "raw", None) or {}).get("fallback") if r is not None else None
        if fb:
            row["fallback"] = fb
        return row
    rec = {"date": asof, "state": s.state, "dry_run": report.dry_run, "n_orders": report.n_orders,
           "turnover": round(report.turnover_notional, 2), "blocked": report.blocked,
           "track": (track.status if track else None),
           "orders": [_o(o) for o in report.orders]}
    with (d / "runs.jsonl").open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _handle_rolls(s: DeployedStrategy, broker, dry: bool) -> Optional[str]:
    """Futures calendar rolls, BEFORE the rebalance (pre-registered policy 2026-06-12,
    atlas tasks/IB_MICRO_ADAPTER_PLAN.md): trigger = IB's CONTFUT front-month switch
    (check_rolls), execution = paired close+reopen market orders, half-roll = STOP and
    page — never blind-retry. No-op for brokers without check_rolls (equities).

    Returns an error string if the book is half-rolled (callers must abort the
    rebalance: TargetExecutor diffs by symbol and would double-trade a half-rolled book).
    """
    if not hasattr(broker, "check_rolls"):
        return None
    try:
        rolls = broker.check_rolls()
    except Exception as e:
        logger.warning("check_rolls failed for %s: %s", s.name, e)
        return None
    if not rolls:
        return None
    if dry:
        logger.info("%s: %d roll(s) needed (dry run — reporting only): %s", s.name, len(rolls),
                    [f"{r['held_local']}->{r['front_local']}" for r in rolls])
        return None
    for r in rolls:
        res = broker.roll_position(r)
        if res.get("half_rolled"):
            return (f"HALF-ROLLED {res['ticker']} {res.get('qty', 0):+d} "
                    f"({r.get('held_local', '?')} closed, reopen failed): {res.get('error', '')}")
        if not res.get("reopened"):
            logger.warning("%s: roll skipped for %s: %s", s.name, res.get("ticker"), res.get("error"))
    return None


def run_strategy(s: DeployedStrategy, asof: str, mode: str = "shadow", broker=None) -> StrategyRunResult:
    try:
        broker = broker or _build_broker(s)
        if broker is None or not getattr(broker, "is_connected", False):
            return StrategyRunResult(s.name, s.state, s.broker, error="broker unavailable")
        weights = s.target_portfolio(asof)
        specs = {k: ContractSpec(**v) for k, v in (s.specs or {}).items()}
        ex = TargetExecutor(broker, specs=specs, tif=(s.tif or None))
        # shadow = Paper Book: place REAL paper orders on live data (the forward-paper gate).
        # canary/live = real capital: held (dry) unless human-approved AND invoked in live mode.
        dry = s.state in ("canary", "live") and (not s.approved or mode != "live")
        # futures calendar rolls FIRST — a half-rolled book aborts the rebalance (critical)
        roll_err = _handle_rolls(s, broker, dry)
        if roll_err:
            return StrategyRunResult(s.name, s.state, s.broker, error=roll_err)
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
    # lifecycle 'retired' (human-confirmed exit, pre-reg 2026-06-12) stops order placement;
    # closing positions stays a manual broker action (no auto-liquidation, board policy).
    strategies = [s for s in strategies if getattr(s, "lifecycle", "shadow") != "retired"]
    report = DailyReport(asof, mode, [run_strategy(s, asof, mode) for s in strategies])
    if not strategies:
        logger.info("daily(%s): no deployed strategies — nothing to do", mode)
    out = LIVE_DATA / "daily"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{asof}.json").write_text(json.dumps(
        {"date": asof, "mode": mode, "results": [asdict(r) for r in report.results]}, indent=2))
    # monitoring: Telegram is CRITICAL-only (operator directive 2026-06-12) — halt,
    # error, divergence, or orders held for human approval. Routine order flow is NOT
    # critical; the crucible morning report covers the book daily.
    if notify and (strategies and any(r.blocked or r.awaiting_approval or r.error or
                                      r.track_status == "diverging" for r in report.results)):
        _send_telegram("\U0001f6a8 " + _digest(report))
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
