"""The Alpaca real-money connection, request by request (orders, fills, shorting, balances, errors)."""

import httpx
import pytest

from tests.helpers import run
from tradezone.brokers.alpaca import AlpacaBroker
from tradezone.core.models import Order, OrderStatus, Side


class FakeAlpaca:
    def __init__(self):
        self.orders, self.sent, self.fail = {}, [], set()
        self.account = {"cash": "1500.50", "equity": "2600.00", "shorting_enabled": True}
        self.assets = {"TSLA": {"tradable": True, "shortable": True, "easy_to_borrow": True},
                       "GME": {"tradable": True, "shortable": True, "easy_to_borrow": False}}
        self.positions = [{"symbol": "BTCUSD", "qty": "0.01", "side": "long"},
                          {"symbol": "TSLA", "qty": "3", "side": "short"}]

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        if path in self.fail:
            raise httpx.ConnectError("network down")
        if path == "/v2/orders" and m == "POST":
            import json
            body = json.loads(req.content)
            self.sent.append(body)
            if float(body["qty"]) > 1000:
                return httpx.Response(403, json={"message": "insufficient buying power"})
            oid = f"A{len(self.orders) + 1}"
            self.orders[oid] = {"id": oid, "status": "new", "filled_qty": "0", "filled_avg_price": None}
            return httpx.Response(200, json=self.orders[oid])
        if path.startswith("/v2/orders/") and m == "GET":
            return httpx.Response(200, json=self.orders[path.rsplit("/", 1)[1]])
        if path.startswith("/v2/orders/") and m == "DELETE":
            self.orders[path.rsplit("/", 1)[1]]["status"] = "canceled"
            return httpx.Response(204)
        if path == "/v2/account":
            return httpx.Response(200, json=self.account)
        if path.startswith("/v2/assets/"):
            a = self.assets.get(path.rsplit("/", 1)[1])
            return httpx.Response(200, json=a) if a else httpx.Response(404)
        if path == "/v2/positions":
            return httpx.Response(200, json=self.positions)
        if path.startswith("/v2/positions/"):
            sym = path.rsplit("/", 1)[1]
            p = next((x for x in self.positions if x["symbol"] == sym), None)
            return httpx.Response(200, json=p) if p else httpx.Response(404)
        return httpx.Response(404)


def broker(fa):
    return AlpacaBroker("k", "s", paper=False, client=httpx.AsyncClient(base_url="https://api.alpaca.markets",
                                                                        transport=httpx.MockTransport(fa.handler)))


def test_orders_are_sent_in_alpacas_format():
    fa = FakeAlpaca()
    b = broker(fa)
    run(b.submit(Order("BTC-USD", Side.BUY, 0.0125, "r1")))
    run(b.submit(Order("TSLA", Side.SELL, 2, "r2")))
    assert fa.sent[0]["symbol"] == "BTC/USD" and fa.sent[0]["time_in_force"] == "gtc" and fa.sent[0]["qty"] == "0.0125"
    assert fa.sent[1]["symbol"] == "TSLA" and fa.sent[1]["time_in_force"] == "day" and fa.sent[1]["side"] == "sell"


def test_partial_fills_are_reported_once_each_at_the_right_price():
    fa = FakeAlpaca()
    b = broker(fa)
    o = Order("BTC-USD", Side.BUY, 0.02, "r1")
    assert run(b.submit(o)) == [] and o.status is OrderStatus.SUBMITTED
    fa.orders[o.broker_id].update(status="partially_filled", filled_qty="0.01", filled_avg_price="60000")
    f1 = run(b.poll(o))
    fa.orders[o.broker_id].update(status="filled", filled_qty="0.02", filled_avg_price="60500")
    f2 = run(b.poll(o))
    assert f1[0].qty == pytest.approx(0.01) and f1[0].price == pytest.approx(60_000)
    assert f2[0].qty == pytest.approx(0.01) and f2[0].price == pytest.approx(61_000)  # the second half's own price
    assert run(b.poll(o)) == [] and o.status is OrderStatus.FILLED  # nothing reported twice


def test_a_refused_order_and_a_network_error_are_rejections_not_crashes():
    fa = FakeAlpaca()
    b = broker(fa)
    o = Order("BTC-USD", Side.BUY, 5000, "r1")
    run(b.submit(o))
    assert o.status is OrderStatus.REJECTED and "insufficient buying power" in o.reason
    fa.fail.add("/v2/orders")
    o2 = Order("BTC-USD", Side.BUY, 0.01, "r2")
    run(b.submit(o2))
    assert o2.status is OrderStatus.REJECTED and "transport error" in o2.reason


def test_cancel_and_poll_survive_a_network_blip():
    fa = FakeAlpaca()
    b = broker(fa)
    o = Order("BTC-USD", Side.BUY, 0.01, "r1")
    run(b.submit(o))
    fa.fail.add(f"/v2/orders/{o.broker_id}")
    run(b.cancel(o))  # logged, not raised
    assert run(b.poll(o)) == []
    fa.fail.clear()
    run(b.cancel(o))
    assert fa.orders[o.broker_id]["status"] == "canceled"


def test_only_easy_to_borrow_stocks_on_a_margin_account_can_be_shorted_never_crypto():
    fa = FakeAlpaca()
    b = broker(fa)
    b.symbols = ["TSLA", "GME", "BTC-USD"]
    run(b.refresh_market())
    assert b.can_short("TSLA") and not b.can_short("GME") and not b.can_short("BTC-USD")
    fa.account["equity"] = "1500"  # under $2,000: no shorting at all
    b._short_at = 0
    run(b.refresh_market())
    assert not b.can_short("TSLA")
    fa.fail.add("/v2/account")  # unknown = not allowed
    b._short_at = 0
    run(b.refresh_market())
    assert not b.shorting_enabled


def test_balances_and_holdings():
    fa = FakeAlpaca()
    b = broker(fa)
    assert run(b.account()) == {"cash": 1500.5, "equity": 2600.0, "daytrade_count": 0, "pattern_day_trader": False}
    assert run(b.positions()) == {"BTCUSD": 0.01, "TSLA": -3.0}
    assert run(b.position_qty("BTC-USD")) == pytest.approx(0.01)
    assert run(b.position_qty("TSLA")) == -3.0 and run(b.position_qty("ETH-USD")) == 0.0
    fa.fail.update({"/v2/account", "/v2/positions", "/v2/positions/BTCUSD"})
    assert run(b.account()) is None and run(b.positions()) is None and run(b.position_qty("BTC-USD")) is None
