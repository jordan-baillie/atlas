"""End-to-end tests for the daily Paper Book loop (provider -> executor -> track -> record/approval)."""
import json

import atlas.execution.daily as daily
from atlas.brokers.base import AccountInfo, BrokerAdapter, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo
from atlas.execution.registry import DeployedStrategy, register_provider


class SimBroker(BrokerAdapter):
    def __init__(self, equity=10000.0, prices=None):
        super().__init__({}); self._equity = equity; self._prices = prices or {}; self._pos = {}; self.orders = []
        self._connected = True

    def connect(self): self._connected = True; return True
    def disconnect(self): self._connected = False
    def get_account_info(self): return AccountInfo(equity=self._equity, cash=self._equity, market_id="sim")
    def get_positions(self): return [PositionInfo(ticker=t, shares=q) for t, q in self._pos.items() if q]
    def get_prices(self, tickers): return {t: self._prices[t] for t in tickers if t in self._prices}

    def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET, stop_price=None, remark=""):
        self._pos[ticker] = self._pos.get(ticker, 0) + (qty if side == OrderSide.BUY else -qty)
        self.orders.append((ticker, side, qty))
        return OrderResult(success=True, ticker=ticker, side=side, status=OrderStatus.FILLED, filled_qty=qty, fill_price=price)

    def cancel_order(self, oid): return OrderResult(success=True)
    def cancel_all_orders(self): return []
    def get_open_orders(self): return []
    def get_order_status(self, oid): return OrderResult(success=True)


def _no_killswitch(monkeypatch):
    from atlas.execution import kill_switch as ks
    monkeypatch.setattr(ks, "check_all_layers", lambda **k: None)


def test_shadow_paper_trades(tmp_path, monkeypatch):
    """shadow = the Paper Book: places REAL paper orders on live data (not dry)."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("demo_a")(lambda asof: {"AAA": 0.5, "BBB": 0.5})
    s = DeployedStrategy(name="demo", provider="demo_a", state="shadow", capital=10000,
                         expectation={"daily_mean": 0.0008, "daily_std": 0.01, "sharpe": 1.0})
    b = SimBroker(prices={"AAA": 100.0, "BBB": 50.0})
    r = daily.run_strategy(s, "2026-06-09", mode="shadow", broker=b)
    assert r.error is None and not r.dry_run and r.n_orders == 2 and len(b.orders) == 2   # PLACED (paper)
    assert (tmp_path / "demo" / "runs.jsonl").exists()


def test_deploy_pass_registers_paper_and_provider_reads_target(tmp_path, monkeypatch):
    """deploy_pass() puts a forge PASS into the Paper Book; the file-provider reads its target.json."""
    import atlas.execution.providers as prov
    import atlas.execution.registry as reg
    monkeypatch.setattr(prov, "LIVE_DATA", tmp_path)
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "live_strategies.json")
    s = prov.deploy_pass("vmom_pass", capital=5000, strategy_path="/x/strat.py")
    assert s.state == "shadow" and s.provider == "vmom_pass" and not s.approved
    assert [d.name for d in reg.deployed()] == ["vmom_pass"]
    (tmp_path / "vmom_pass").mkdir(exist_ok=True)
    (tmp_path / "vmom_pass" / "target.json").write_text(json.dumps({"asof": "2026-06-09", "weights": {"AAA": 0.6}}))
    assert prov.forge_strategy_provider("vmom_pass")("2026-06-09") == {"AAA": 0.6}
    assert prov.forge_strategy_provider("missing")("2026-06-09") == {}   # no file -> safe no-op


def test_live_unapproved_is_held_and_flagged(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("demo_b")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="canary1", provider="demo_b", state="canary", approved=False, capital=250)
    b = SimBroker(prices={"AAA": 100.0})
    r = daily.run_strategy(s, "2026-06-09", mode="live", broker=b)
    assert r.awaiting_approval and r.dry_run and b.orders == []   # canary + unapproved -> held


def test_live_approved_executes(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("demo_c")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="live1", provider="demo_c", state="live", approved=True, capital=10000)
    b = SimBroker(prices={"AAA": 100.0})
    r = daily.run_strategy(s, "2026-06-09", mode="live", broker=b)
    assert not r.dry_run and r.executed == 1 and len(b.orders) == 1 and not r.awaiting_approval


def test_broker_unavailable_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    register_provider("demo_d")(lambda asof: {"AAA": 1.0})

    class Down(SimBroker):
        @property
        def is_connected(self): return False
    r = daily.run_strategy(DeployedStrategy(name="x", provider="demo_d", state="shadow"), "2026-06-09", broker=Down())
    assert r.error == "broker unavailable"


def test_run_daily_empty_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    assert daily.run_daily(mode="shadow", strategies=[], notify=False).n_strategies == 0


def test_registry_approve_and_state(tmp_path, monkeypatch):
    import atlas.execution.registry as reg
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "live_strategies.json")
    reg.upsert(DeployedStrategy(name="z", provider="demo_a", state="shadow"))
    assert reg.approve("z") and reg.deployed()[0].approved
    assert reg.set_state("z", "canary") and reg.deployed()[0].state == "canary"


def test_boreas_provider_is_safe_noop():
    import atlas.execution.providers as p
    assert p.boreas_carry_trend("2026-06-09") == {}   # stub until 2026-08-28 + productionization


# ── futures calendar rolls in the daily loop ─────────────────────


class RollSimBroker(SimBroker):
    """SimBroker with futures-style roll hooks (mimics IBBroker.check_rolls/roll_position)."""

    def __init__(self, rolls=None, roll_result=None, **kw):
        super().__init__(**kw)
        self._rolls = rolls or []
        self._roll_result = roll_result or {"closed": True, "reopened": True, "half_rolled": False}
        self.rolled = []

    def check_rolls(self):
        return list(self._rolls)

    def roll_position(self, roll):
        self.rolled.append(roll)
        return dict(self._roll_result, ticker=roll["ticker"], qty=roll["qty"])


_ROLL = {"ticker": "MES", "qty": 1, "held_conid": 111, "held_local": "MESM6",
         "front_conid": 222, "front_local": "MESU6"}


def test_daily_rolls_before_rebalance(tmp_path, monkeypatch):
    """A pending roll executes first; rebalance then proceeds normally."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("roll_a")(lambda asof: {"MES": 1.0})
    s = DeployedStrategy(name="rolldemo", provider="roll_a", state="shadow", capital=10000)
    b = RollSimBroker(rolls=[dict(_ROLL)], prices={"MES": 5000.0})
    r = daily.run_strategy(s, "2026-06-12", mode="shadow", broker=b)
    assert b.rolled == [dict(_ROLL)]                       # roll executed
    assert r.error is None                                  # rebalance proceeded


def test_daily_half_roll_aborts_rebalance(tmp_path, monkeypatch):
    """closed-but-not-reopened = position mismatch -> abort, surface as error (criticals page)."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("roll_b")(lambda asof: {"MES": 1.0})
    s = DeployedStrategy(name="halfroll", provider="roll_b", state="shadow", capital=10000)
    b = RollSimBroker(rolls=[dict(_ROLL)],
                      roll_result={"closed": True, "reopened": False, "half_rolled": True,
                                   "error": "reopen failed after close: outage"},
                      prices={"MES": 5000.0})
    r = daily.run_strategy(s, "2026-06-12", mode="shadow", broker=b)
    assert r.error and "HALF-ROLLED" in r.error
    assert b.orders == []                                   # NO rebalance orders placed


def test_daily_dry_mode_reports_rolls_without_trading(tmp_path, monkeypatch):
    """canary/unapproved (dry) only reports the needed roll — no roll orders placed."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("roll_c")(lambda asof: {"MES": 1.0})
    s = DeployedStrategy(name="dryroll", provider="roll_c", state="canary", capital=10000, approved=False)
    b = RollSimBroker(rolls=[dict(_ROLL)], prices={"MES": 5000.0})
    r = daily.run_strategy(s, "2026-06-12", mode="shadow", broker=b)
    assert b.rolled == []                                   # reported, not executed
    assert r.error is None and r.dry_run


def test_daily_equity_broker_unaffected(tmp_path, monkeypatch):
    """Brokers without check_rolls (equities/alpaca) skip the roll path entirely."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path); _no_killswitch(monkeypatch)
    register_provider("roll_d")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="equitydemo", provider="roll_d", state="shadow", capital=10000)
    b = SimBroker(prices={"AAA": 100.0})
    r = daily.run_strategy(s, "2026-06-12", mode="shadow", broker=b)
    assert r.error is None and r.n_orders == 1


def test_prefilter_tradable_drops_doomed_orders(monkeypatch):
    """task #37: pre-filter drops non-tradable (any side) + non-shortable (shorts only) BEFORE
    placement, records them as skipped, never redistributes the dropped weight, and is a no-op
    for non-Alpaca books."""
    import atlas.brokers.alpaca.tradable_assets as ta
    from atlas.execution.registry import DeployedStrategy

    monkeypatch.setattr(ta, "is_tradable", lambda t: t not in {"BRKL", "KALV"})
    monkeypatch.setattr(ta, "is_shortable", lambda t: t not in {"CGBD", "NMFC"})

    s = DeployedStrategy(name="x", provider="p", broker="alpaca")
    weights = {"AAPL": 0.3, "BRKL": -0.1, "CGBD": -0.1, "NMFC": 0.1, "MSFT": -0.2}
    kept, skipped = daily._prefilter_tradable(s, weights)

    assert "BRKL" in skipped and skipped["BRKL"] == "not_tradable"     # non-tradable dropped (short)
    assert "CGBD" in skipped and skipped["CGBD"] == "not_shortable"    # non-shortable SHORT dropped
    assert "NMFC" not in skipped and kept["NMFC"] == 0.1               # non-shortable but LONG -> kept
    assert "KALV" not in weights                                        # (sanity: not requested)
    assert kept == {"AAPL": 0.3, "NMFC": 0.1, "MSFT": -0.2}            # gap NOT redistributed
    assert all(kept[k] == weights[k] for k in kept)                    # each kept weight unchanged

    # non-Alpaca (futures/IB) book: never filtered (a root is not in Alpaca's set)
    fut = DeployedStrategy(name="f", provider="p", broker="ib")
    kept2, skipped2 = daily._prefilter_tradable(fut, {"ES": -0.5, "CL": 0.5})
    assert kept2 == {"ES": -0.5, "CL": 0.5} and skipped2 == {}


# ── Layer 1: track-before-placement (orphan-window elimination) ────────────────


def test_track_computed_before_placement_no_orphan(tmp_path, monkeypatch):
    """Layer 1: if track evaluation throws, it must happen BEFORE rebalance so no orders are placed
    (no orphan order_ids are left at the broker without a runs.jsonl record)."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    _no_killswitch(monkeypatch)

    def _bad_returns(name):
        raise RuntimeError("disk io error")
    monkeypatch.setattr(daily, "_realized_returns", _bad_returns)

    register_provider("demo_track_order")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="trackorder", provider="demo_track_order", state="shadow",
                         capital=10000, expectation={"daily_mean": 0.0008, "daily_std": 0.01, "sharpe": 1.0})
    b = SimBroker(prices={"AAA": 100.0})
    r = daily.run_strategy(s, "2026-06-26", mode="shadow", broker=b)
    assert r.error is not None   # throw propagated and caught by outer except
    assert b.orders == []        # NO orders placed — throw happened BEFORE rebalance


# ── Layer 2: submitted.jsonl write-ahead log ────────────────────────────────


class _IDedSimBroker(SimBroker):
    """SimBroker that stamps a non-empty order_id on each successful placement."""
    def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET,
                    stop_price=None, remark=""):
        res = super().place_order(ticker, side, qty, price, order_type, stop_price, remark)
        res.order_id = f"live-{ticker}"
        return res


def test_submitted_jsonl_written_with_correct_shape(tmp_path, monkeypatch):
    """Layer 2: run_strategy writes submitted.jsonl with {date,ticker,side,qty,px,order_id} per order."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    _no_killswitch(monkeypatch)
    register_provider("demo_subm")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="subm_test", provider="demo_subm", state="live",
                         approved=True, capital=10000)
    b = _IDedSimBroker(prices={"AAA": 100.0})
    r = daily.run_strategy(s, "2026-06-26", mode="live", broker=b)
    assert r.error is None and not r.dry_run

    subm = tmp_path / "subm_test" / "submitted.jsonl"
    assert subm.exists(), "submitted.jsonl must be written for live (non-dry) runs"
    rows = [json.loads(line) for line in subm.read_text().splitlines() if line]
    assert len(rows) == 1
    row = rows[0]
    assert row["date"] == "2026-06-26"
    assert row["ticker"] == "AAA"
    assert row["side"] == "BUY"
    assert row["qty"] == 100
    assert row["order_id"] == "live-AAA"
    assert "px" in row


def test_submitted_jsonl_not_written_for_dry_run(tmp_path, monkeypatch):
    """Layer 2: submitted.jsonl must NOT be written for dry runs (nothing is placed at the broker)."""
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    _no_killswitch(monkeypatch)
    register_provider("demo_dry_subm")(lambda asof: {"AAA": 1.0})
    s = DeployedStrategy(name="dry_subm", provider="demo_dry_subm", state="canary",
                         approved=False, capital=10000)
    b = _IDedSimBroker(prices={"AAA": 100.0})
    daily.run_strategy(s, "2026-06-26", mode="shadow", broker=b)
    subm = tmp_path / "dry_subm" / "submitted.jsonl"
    # File may not exist, or if it does exist it must be empty (no rows written for dry run)
    if subm.exists():
        rows = [l for l in subm.read_text().splitlines() if l.strip()]
        assert rows == [], "no submitted.jsonl rows for a dry run"
