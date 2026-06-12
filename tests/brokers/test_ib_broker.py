"""Tests for the IB micro-futures adapter (translation logic, via an injected fake IB client)."""
from types import SimpleNamespace as NS

import pytest

from atlas.brokers.base import OrderSide, OrderType
from atlas.brokers.ib.broker import MICRO_FUTURES, IBBroker


class FakeIB:
    def __init__(self, positions=None, account=None, prices=None):
        self._positions, self._account, self._prices = positions or [], account or [], prices or {}
        self.placed = []

    def isConnected(self): return True
    def qualifyContracts(self, c): return (c,)
    def accountSummary(self): return self._account
    def positions(self): return self._positions

    def reqTickers(self, *cons):
        return [NS(contract=c, marketPrice=(lambda c=c: self._prices.get(getattr(c, "symbol", ""), float("nan"))))
                for c in cons]

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return NS(order=NS(orderId=1), contract=contract,
                  orderStatus=NS(status="Filled", filled=order.totalQuantity, avgFillPrice=100.0))

    def openTrades(self): return []
    def trades(self): return []
    def cancelOrder(self, o): pass


def _pos(sym, qty, avg_cost):
    return NS(contract=NS(symbol=sym, currency="USD", localSymbol=sym), position=qty, avgCost=avg_cost)


def test_multiplier_table():
    b = IBBroker({}, ib=FakeIB())
    assert b.multiplier("MES") == 5.0 and b.multiplier("MGC") == 10.0


def test_unknown_symbol_raises():
    with pytest.raises(ValueError):
        IBBroker({}, ib=FakeIB()).multiplier("ZZZ")


def test_get_positions_divides_avgcost_by_multiplier():
    # IB reports futures avgCost as per-contract NOTIONAL (price*multiplier): MES @5000 -> 25000
    b = IBBroker({}, ib=FakeIB(positions=[_pos("MES", 2, 25000.0)]))
    ps = b.get_positions()
    assert len(ps) == 1 and ps[0].ticker == "MES" and ps[0].shares == 2 and ps[0].entry_price == 5000.0


def test_place_market_buy():
    fake = FakeIB()
    r = IBBroker({}, ib=fake).place_order("MES", OrderSide.BUY, 2, 5000.0, order_type=OrderType.MARKET)
    assert r.success and r.filled_qty == 2
    _, order = fake.placed[0]
    assert order.action == "BUY" and order.totalQuantity == 2


def test_place_short_sell():
    fake = FakeIB()
    r = IBBroker({}, ib=fake).place_order("MNQ", OrderSide.SELL, 1, 18000.0)
    assert r.success
    assert fake.placed[0][1].action == "SELL"


def test_get_account_info():
    acct = [NS(tag="NetLiquidation", value="15000", currency="USD"),
            NS(tag="BuyingPower", value="60000", currency="USD")]
    ai = IBBroker({}, ib=FakeIB(account=acct)).get_account_info()
    assert ai.equity == 15000.0 and ai.buying_power == 60000.0


def test_get_prices():
    px = IBBroker({}, ib=FakeIB(prices={"MES": 5001.0})).get_prices(["MES"])
    assert px["MES"] == 5001.0


def test_paper_vs_live_port():
    assert IBBroker({"trading": {"mode": "paper"}}).port == 7497
    assert IBBroker({"trading": {"mode": "live"}}).port == 7496 and IBBroker({"trading": {"mode": "live"}}).is_live


def test_registry_returns_ib_broker():
    from atlas.brokers.registry import get_live_broker
    br = get_live_broker({"trading": {"broker": "ib", "mode": "paper"}, "market": "boreas"})
    assert br is not None and br.name == "ib"


# ── front-month resolution + roll detection ──────────────────────


class RollFakeIB(FakeIB):
    """Fake that simulates IB's two-step resolution: ContFuture -> conId -> concrete FUT."""

    def __init__(self, front=("MESU6", 620731015), **kw):
        super().__init__(**kw)
        self._front_local, self._front_conid = front

    def qualifyContracts(self, c):
        if type(c).__name__ == "ContFuture":
            c.conId = self._front_conid          # CONTFUT resolves to front-month conId
            return (c,)
        if getattr(c, "conId", 0) == self._front_conid:
            c.secType = "FUT"                    # concrete, orderable front month
            c.symbol, c.localSymbol = "MES", self._front_local
            c.lastTradeDateOrContractMonth = "20260918"
            return (c,)
        return (c,)


def test_contract_resolves_concrete_front_month():
    b = IBBroker({}, ib=RollFakeIB())
    c = b._contract("MES")
    assert getattr(c, "secType", "") == "FUT"          # orderable, not CONTFUT
    assert getattr(c, "localSymbol", "") == "MESU6"
    assert b._contract("MES") is c                      # cached within session


def test_check_rolls_flags_stale_holding():
    # held June contract (conId differs from current front month Sept)
    held = NS(contract=NS(symbol="MES", conId=111, localSymbol="MESM6", currency="USD"),
              position=2, avgCost=25000.0)
    b = IBBroker({}, ib=RollFakeIB(positions=[held]))
    rolls = b.check_rolls()
    assert len(rolls) == 1
    r = rolls[0]
    assert r["ticker"] == "MES" and r["held_local"] == "MESM6" and r["front_local"] == "MESU6"


def test_check_rolls_clean_when_holding_front_month():
    held = NS(contract=NS(symbol="MES", conId=620731015, localSymbol="MESU6", currency="USD"),
              position=2, avgCost=25000.0)
    b = IBBroker({}, ib=RollFakeIB(positions=[held]))
    assert b.check_rolls() == []


def test_check_rolls_ignores_non_futures_and_flat():
    held_flat = NS(contract=NS(symbol="MES", conId=111, localSymbol="MESM6", currency="USD"),
                   position=0, avgCost=0.0)
    equity = NS(contract=NS(symbol="AAPL", conId=999, localSymbol="AAPL", currency="USD"),
                position=10, avgCost=150.0)
    b = IBBroker({}, ib=RollFakeIB(positions=[held_flat, equity]))
    assert b.check_rolls() == []
