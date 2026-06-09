"""live/daily.py — the daily forge->live ops loop (shadow-first).

For each DEPLOYED strategy: produce today's target book -> diff via TargetExecutor (dry_run in shadow) ->
track-vs-expectation -> record. In shadow: compute + record only ($0). In live (state=='live' AND approved):
execute, then reconcile (Atlas's existing reconcile/kill-switch run on their own timers). Kill-switch is enforced
INSIDE TargetExecutor (fail-closed). Human approval gates real-money execution (board 2026-06-09).

Run: ``python3 -m live.daily [--mode shadow|live] [--date YYYY-MM-DD]``
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from brokers.target_executor import ContractSpec, TargetExecutor
from live import registry
from live.registry import DeployedStrategy

logger = logging.getLogger("atlas.live.daily")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIVE_DATA = PROJECT_ROOT / "data" / "live"


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
    from brokers.registry import get_live_broker
    live = s.state == "live" and s.approved
    cfg = {"trading": {"broker": s.broker, "mode": "live" if live else "paper"}, "market": s.name}
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
        # execute for real ONLY when mode=live AND the strategy is live AND human-approved
        dry = not (mode == "live" and s.state == "live" and s.approved)
        rep = ex.rebalance(weights, deployable_equity=(s.capital or None), dry_run=dry)

        track = None
        if s.expectation:
            from live.track_expectation import Expectation, evaluate
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
    """Best-effort Telegram digest (no-op if secrets/requests unavailable)."""
    try:
        import requests
        sec = json.loads((Path.home() / ".atlas-secrets.json").read_text())
        tok, chat = sec.get("telegram_bot_token"), sec.get("telegram_chat_id")
        if tok and chat:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML"}, timeout=10)
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
    import live.providers  # noqa: F401  (register target-portfolio providers)
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
