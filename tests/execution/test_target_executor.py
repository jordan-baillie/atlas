"""Tests for the target-weight executor (the new forge->live productionization bridge)."""
import pytest

from atlas.brokers.base import AccountInfo, BrokerAdapter, OrderResult, OrderSide, OrderStatus, OrderType, PositionInfo
from atlas.execution.target_executor import ContractSpec, TargetExecutor


class SimBroker(BrokerAdapter):
    """Minimal in-memory BrokerAdapter for testing (long-short positions, instant fills)."""

    def __init__(self, equity=10000.0, prices=None, positions=None):
        super().__init__({})
        self._equity = equity
        self._prices = prices or {}
        self._pos = dict(positions or {})   # ticker -> signed shares
        self.orders = []

    def connect(self): self._connected = True; return True
    def disconnect(self): self._connected = False
    def get_account_info(self): return AccountInfo(equity=self._equity, cash=self._equity, market_id="sim")

    def get_positions(self):
        return [PositionInfo(ticker=t, shares=q, current_price=self._prices.get(t, 0.0))
                for t, q in self._pos.items() if q != 0]

    def get_prices(self, tickers): return {t: self._prices[t] for t in tickers if t in self._prices}

    def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET, stop_price=None, remark=""):
        self._pos[ticker] = self._pos.get(ticker, 0) + (qty if side == OrderSide.BUY else -qty)
        self.orders.append((ticker, side, qty, price))
        return OrderResult(success=True, ticker=ticker, side=side, status=OrderStatus.FILLED,
                           requested_qty=qty, filled_qty=qty, fill_price=price)

    def cancel_order(self, order_id): return OrderResult(success=True)
    def cancel_all_orders(self): return []
    def get_open_orders(self): return []
    def get_order_status(self, order_id): return OrderResult(success=True)


def test_long_only_rebalance_from_flat():
    b = SimBroker(equity=10000, prices={"AAA": 100.0, "BBB": 50.0})
    rep = TargetExecutor(b).rebalance({"AAA": 0.5, "BBB": 0.5}, dry_run=False, check_kill_switch=False)
    assert rep.target_qty["AAA"] == 50 and rep.target_qty["BBB"] == 100   # 0.5*10000/100 ; 0.5*10000/50
    assert len(rep.executed) == 2 and b._pos["AAA"] == 50 and b._pos["BBB"] == 100


def test_short_open_via_negative_weight():
    b = SimBroker(equity=10000, prices={"AAA": 100.0})
    rep = TargetExecutor(b).rebalance({"AAA": -0.5}, dry_run=False, check_kill_switch=False)
    assert rep.target_qty["AAA"] == -50 and rep.orders[0].side == OrderSide.SELL
    assert b._pos["AAA"] == -50   # short opened


def test_exit_held_name_absent_from_targets():
    b = SimBroker(equity=10000, prices={"AAA": 100.0, "BBB": 50.0}, positions={"BBB": 100})
    rep = TargetExecutor(b).rebalance({"AAA": 1.0}, dry_run=False, check_kill_switch=False)
    assert rep.target_qty["BBB"] == 0 and b._pos.get("BBB", 0) == 0 and b._pos["AAA"] == 100


def test_dust_delta_skipped():
    b = SimBroker(positions={"AAA": 50})
    ex = TargetExecutor(b, min_delta_notional=1000.0)
    rep = ex.rebalance({"AAA": 0.51}, prices={"AAA": 100.0}, deployable_equity=10000,
                       dry_run=False, check_kill_switch=False)
    assert rep.n_orders == 0   # delta 1 share * $100 = $100 < $1000 floor


def test_dry_run_places_nothing():
    b = SimBroker(equity=10000, prices={"AAA": 100.0})
    rep = TargetExecutor(b).rebalance({"AAA": 1.0}, dry_run=True, check_kill_switch=False)
    assert rep.n_orders == 1 and rep.results == [] and b.orders == []


def test_kill_switch_blocks_execution(monkeypatch):
    b = SimBroker(equity=10000, prices={"AAA": 100.0})
    from atlas.execution import kill_switch as ks

    class _BR:
        layer, reason = "L3", "trading halt"
    monkeypatch.setattr(ks, "check_all_layers", lambda **k: _BR())
    rep = TargetExecutor(b).rebalance({"AAA": 1.0}, dry_run=False)
    assert rep.blocked and "L3" in rep.blocked and b.orders == []   # computed but NOT placed


def test_kill_switch_failclosed_on_error(monkeypatch):
    b = SimBroker(equity=10000, prices={"AAA": 100.0})
    from atlas.execution import kill_switch as ks

    def _boom(**k): raise RuntimeError("db down")
    monkeypatch.setattr(ks, "check_all_layers", _boom)
    rep = TargetExecutor(b).rebalance({"AAA": 1.0}, dry_run=False)
    assert rep.blocked and b.orders == []   # fail-CLOSED


def test_futures_multiplier_sizing():
    b = SimBroker(equity=50000, prices={"MES": 5000.0})
    ex = TargetExecutor(b, specs={"MES": ContractSpec(multiplier=5.0)})
    rep = ex.rebalance({"MES": 1.0}, dry_run=False, check_kill_switch=False)
    assert rep.target_qty["MES"] == 2 and b._pos["MES"] == 2   # 50000/(5000*5)=2 contracts


def test_max_order_notional_cap():
    b = SimBroker(equity=100000, prices={"AAA": 100.0})
    rep = TargetExecutor(b, max_order_notional=2000.0).rebalance(
        {"AAA": 1.0}, dry_run=False, check_kill_switch=False)
    assert rep.orders[0].qty == 20   # capped at 2000/100; full target was 1000


def test_long_short_book_nets_correctly():
    b = SimBroker(equity=10000, prices={"AAA": 100.0, "BBB": 100.0})
    rep = TargetExecutor(b).rebalance({"AAA": 0.5, "BBB": -0.5}, dry_run=False, check_kill_switch=False)
    assert b._pos["AAA"] == 50 and b._pos["BBB"] == -50 and rep.turnover_notional == pytest.approx(10000.0)


class HTBBroker(SimBroker):
    """Rejects non-DAY orders on HTB names with the real Alpaca error; accepts DAY LIMIT retries."""

    HTB = {"WEN"}

    def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET,
                    stop_price=None, remark="", **kwargs):
        tif = kwargs.get("tif", "")
        if ticker in self.HTB and (tif or "").lower() != "day":
            return OrderResult(success=False, ticker=ticker, side=side, status=OrderStatus.FAILED,
                               message='{"code":42210000,"message":"only day orders are allowed '
                                       'for hard-to-borrow asset \\"%s\\""}' % ticker)
        self.orders.append((ticker, side, qty, price, order_type, tif, remark))
        self._pos[ticker] = self._pos.get(ticker, 0) + (qty if side == OrderSide.BUY else -qty)
        return OrderResult(success=True, ticker=ticker, side=side, status=OrderStatus.FILLED,
                           requested_qty=qty, filled_qty=qty, fill_price=price)


def test_htb_fallback_retries_as_banded_day_limit():
    """HTB rejection on an OPG short -> ONE retry as DAY LIMIT at ref*(1-30bps), marked in raw."""
    b = HTBBroker(equity=10000, prices={"WEN": 10.0, "AAA": 100.0})
    ex = TargetExecutor(b, tif="opg")
    rep = ex.rebalance({"WEN": -0.5, "AAA": 0.5}, dry_run=False, check_kill_switch=False)
    assert len(rep.executed) == 2                      # both succeeded (WEN via fallback)
    wen = next(r for r in rep.results if r.ticker == "WEN")
    assert wen.success and wen.raw.get("fallback") == "htb_day_limit"
    t, side, qty, px, otype, tif, remark = next(o for o in b.orders if o[0] == "WEN")
    assert otype == OrderType.LIMIT and tif == "day" and remark == "htb_fallbk"
    assert px == pytest.approx(10.0 * (1 - 0.0030))    # SELL: band BELOW reference
    aaa = next(r for r in rep.results if r.ticker == "AAA")
    assert "fallback" not in (aaa.raw or {})           # non-HTB path untouched


def test_htb_fallback_buy_bands_above_and_double_failure_keeps_context():
    """BUY-to-cover bands ABOVE ref; if the DAY LIMIT retry ALSO fails, both errors survive."""
    class DoubleFail(HTBBroker):
        def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET,
                        stop_price=None, remark="", **kwargs):
            if ticker in self.HTB and (kwargs.get("tif") or "").lower() == "day":
                self.orders.append((ticker, side, qty, price, order_type, kwargs.get("tif"), remark))
                return OrderResult(success=False, ticker=ticker, side=side,
                                   status=OrderStatus.FAILED, message="halted")
            return super().place_order(ticker, side, qty, price, order_type, stop_price, remark, **kwargs)

    b = DoubleFail(equity=10000, prices={"WEN": 10.0}, positions={"WEN": -100})
    rep = TargetExecutor(b, tif="opg").rebalance({}, dry_run=False, check_kill_switch=False)
    wen = next(r for r in rep.results if r.ticker == "WEN")   # cover BUY of the held short
    assert not wen.success and wen.raw.get("fallback") == "htb_day_limit"
    assert "halted" in wen.message and "42210000" in wen.message   # retry error keeps orig context
    retry = next(o for o in b.orders if o[5] == "day")
    assert retry[3] == pytest.approx(10.0 * (1 + 0.0030))     # BUY: band ABOVE reference


def test_non_htb_failure_not_retried():
    """A generic failure (e.g. buying power) must NOT trigger the HTB fallback."""
    class BPFail(SimBroker):
        def place_order(self, ticker, side, qty, price, order_type=OrderType.MARKET,
                        stop_price=None, remark="", **kwargs):
            self.orders.append((ticker, side, qty, price))
            return OrderResult(success=False, ticker=ticker, side=side,
                               status=OrderStatus.FAILED, message="insufficient buying power")

    b = BPFail(equity=10000, prices={"AAA": 100.0})
    rep = TargetExecutor(b).rebalance({"AAA": 0.5}, dry_run=False, check_kill_switch=False)
    assert len(b.orders) == 1                                  # no retry
    assert not rep.results[0].success
    assert "fallback" not in (rep.results[0].raw or {})
