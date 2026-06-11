"""Tests for the IB Web REST adapter (translation + order-reply-confirm loop, via a fake HTTP transport)."""
from atlas.brokers.base import OrderSide, OrderType
from atlas.brokers.ib_web.broker import IBWebBroker


class FakeHTTP:
    """In-memory IB Web API. Records calls; models the order-reply-confirm chain."""

    def __init__(self):
        self.calls = []
        self._order_seq = 0

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if path == "/iserver/accounts":
            return {"accounts": ["DU123"], "selectedAccount": "DU123"}
        if path == "/portfolio/accounts":
            return [{"accountId": "DU123"}]
        if path == "/trsrv/futures":
            sym = (params or {}).get("symbols")
            return {sym: [{"symbol": sym, "conid": 495512557 if sym == "MES" else 495512999, "expirationDate": 20251219}]}
        if path == "/portfolio2/DU123/positions":
            return [{"conid": 495512557, "position": 2, "avgPrice": 5000.0, "avgCost": 50000.0,
                     "description": "MESZ5", "currency": "USD", "marketPrice": 5001.0, "marketValue": 50010.0}]
        if path == "/iserver/account/DU123/summary":
            return {"netLiquidationValue": 15000, "buyingPower": 60000, "totalCashValue": 15000, "currency": "USD"}
        if path == "/iserver/marketdata/snapshot":
            return [{"conid": 495512557, "31": "5001.50"}]
        if path == "/iserver/account/orders":
            return {"orders": [{"orderId": 987, "ticker": "MES", "side": "BUY", "status": "Submitted", "filledQuantity": "0"}]}
        if path.startswith("/iserver/account/order/status/"):
            return {"order_status": "Filled", "cum_fill": "2", "average_price": "5000.5"}
        return {}

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if path == "/iserver/auth/status":
            return {"authenticated": True}
        if path == "/iserver/auth/ssodh/init":
            return {"authenticated": True}
        if path.endswith("/orders"):
            return [{"id": "reply-uuid-1", "message": ["You are submitting an order without market data..."],
                     "messageIds": ["o354"]}]                     # first response: a warning reply
        if path == "/iserver/reply/reply-uuid-1":
            self._order_seq += 1
            return [{"order_id": str(986 + self._order_seq), "order_status": "PreSubmitted", "encrypt_message": "1"}]
        return {}

    def delete(self, path):
        self.calls.append(("DELETE", path, None))
        return {"msg": "Request was submitted", "order_id": path.rsplit("/", 1)[-1]}


def _b():
    b = IBWebBroker({"trading": {"mode": "paper"}}, http=FakeHTTP())
    b.connect()
    return b


def test_connect_resolves_account():
    b = _b()
    assert b.is_connected and b.account_id == "DU123" and b.name == "ib_web"


def test_resolve_conid_caches():
    b = _b()
    assert b._conid("MES") == 495512557 and b._conid("MES") == 495512557


def test_get_account_info():
    ai = _b().get_account_info()
    assert ai.equity == 15000.0 and ai.buying_power == 60000.0


def test_get_positions():
    ps = _b().get_positions()
    assert len(ps) == 1 and ps[0].ticker == "MES" and ps[0].shares == 2 and ps[0].entry_price == 5000.0


def test_get_prices_parses_field31():
    px = _b().get_prices(["MES"])
    assert px["MES"] == 5001.5


def test_place_order_walks_reply_confirm_loop():
    b = _b()
    r = b.place_order("MES", OrderSide.BUY, 2, 5000.0, order_type=OrderType.MARKET)
    assert r.success and r.order_id == "987"
    # the warning reply was confirmed before the order went live
    assert any(c[0] == "POST" and c[1] == "/iserver/reply/reply-uuid-1" for c in b._http.calls)
    # the submitted order was a MARKET BUY of 2 contracts on the resolved conid
    order = [c for c in b._http.calls if c[1].endswith("/orders")][0][2]["orders"][0]
    assert order["side"] == "BUY" and order["quantity"] == 2 and order["orderType"] == "MKT" and order["conid"] == 495512557


def test_place_short_sell():
    b = _b()
    r = b.place_order("MNQ", OrderSide.SELL, 1, 18000.0, order_type=OrderType.LIMIT)
    assert r.success
    order = [c for c in b._http.calls if c[1].endswith("/orders")][0][2]["orders"][0]
    assert order["side"] == "SELL" and order["orderType"] == "LMT" and order["price"] == 18000.0


def test_order_error_returns_failure():
    b = _b()
    b._http.post = lambda path, json=None: {"error": "Order not confirmed"} if path.endswith("/orders") else {}
    r = b.place_order("MES", OrderSide.BUY, 1, 5000.0)
    assert not r.success and "Order not confirmed" in r.message


def test_get_order_status():
    r = _b().get_order_status("987")
    assert r.success and r.filled_qty == 2 and r.fill_price == 5000.5


def test_registry_returns_ib_web():
    from atlas.brokers.registry import get_live_broker
    br = get_live_broker({"trading": {"broker": "ib_web", "mode": "paper"}, "market": "boreas"})
    assert br is not None and br.name == "ib_web"
