"""End-to-end tests for the daily shadow loop (provider -> executor -> track -> record/approval)."""
import live.daily as daily
from brokers.base import AccountInfo, BrokerAdapter, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo
from live.registry import DeployedStrategy, register_provider


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
    from core import remediation_kill_switch as ks
    monkeypatch.setattr(ks, "check_all_layers", lambda **k: None)


def test_shadow_computes_but_does_not_place(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "LIVE_DATA", tmp_path)
    register_provider("demo_a")(lambda asof: {"AAA": 0.5, "BBB": 0.5})
    s = DeployedStrategy(name="demo", provider="demo_a", state="shadow", capital=10000,
                         expectation={"daily_mean": 0.0008, "daily_std": 0.01, "sharpe": 1.0})
    b = SimBroker(prices={"AAA": 100.0, "BBB": 50.0})
    r = daily.run_strategy(s, "2026-06-09", mode="shadow", broker=b)
    assert r.error is None and r.dry_run and r.n_orders == 2 and b.orders == []   # computed, NOT placed
    assert (tmp_path / "demo" / "runs.jsonl").exists()


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
    import live.registry as reg
    monkeypatch.setattr(reg, "REGISTRY_PATH", tmp_path / "live_strategies.json")
    reg.upsert(DeployedStrategy(name="z", provider="demo_a", state="shadow"))
    assert reg.approve("z") and reg.deployed()[0].approved
    assert reg.set_state("z", "canary") and reg.deployed()[0].state == "canary"


def test_boreas_provider_is_safe_noop():
    import live.providers as p
    assert p.boreas_carry_trend("2026-06-09") == {}   # stub until 2026-08-28 + productionization
