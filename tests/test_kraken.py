"""Kraken: the bots can trade and take profits; nothing can withdraw money or crypto."""

import base64
import hashlib
import hmac
import urllib.parse

import httpx
import pytest

from tests.helpers import capture, prime, run
from tradezone.brokers.kraken import ALLOWED_PRIVATE, KrakenBroker, KrakenError, sign
from tradezone.config import Settings
from tradezone.core.models import Order, OrderStatus, Side, Signal, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

SECRET = base64.b64encode(b"k" * 64).decode()


class FakeKraken:
    """A tiny Kraken: checks every signature, keeps balances, fills market orders at a set price."""

    def __init__(self, can_withdraw=False, can_trade=True, usd=500.0, held=None):
        self.can_withdraw, self.can_trade = can_withdraw, can_trade
        self.bal = {"ZUSD": usd, **(held or {})}
        self.price = {"XBTUSD": 60_000.0, "XDGUSD": 0.2}
        self.orders, self.calls = {}, []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/0/public/AssetPairs":
            return httpx.Response(200, json={"error": [], "result": {
                "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "lot_decimals": 8, "ordermin": "0.0001"},
                "XDGUSD": {"altname": "XDGUSD", "wsname": "XDG/USD", "base": "XXDG", "lot_decimals": 2, "ordermin": "20"},
                "XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT", "lot_decimals": 8, "ordermin": "0.0001"}}})
        data = dict(urllib.parse.parse_qsl(req.content.decode()))
        method = path.rsplit("/", 1)[-1]
        self.calls.append(method)
        want = base64.b64encode(hmac.new(base64.b64decode(SECRET), path.encode() + hashlib.sha256(
            (data["nonce"] + req.content.decode()).encode()).digest(), hashlib.sha512).digest()).decode()
        if req.headers.get("API-Sign") != want:
            return self.err("EAPI:Invalid signature")
        if method == "Balance":
            return self.ok({k: f"{v:.8f}" for k, v in self.bal.items()})
        if method == "TradeBalance":
            return self.ok({"eb": f"{self.bal['ZUSD'] + self.bal.get('XXBT', 0) * self.price['XBTUSD']:.2f}"})
        if method == "WithdrawInfo":
            return self.err("EFunding:Unknown withdraw key") if self.can_withdraw else self.err("EGeneral:Permission denied")
        if method == "AddOrder":
            if not self.can_trade:
                return self.err("EGeneral:Permission denied")
            if data.get("validate") == "true":
                return self.ok({"descr": {"order": "validated"}})
            vol, px = float(data["volume"]), self.price[data["pair"]]
            base = "XXBT" if data["pair"] == "XBTUSD" else "XXDG"
            cost, fee = vol * px, vol * px * 0.0026
            sgn = 1 if data["type"] == "buy" else -1
            self.bal[base] = self.bal.get(base, 0) + sgn * vol
            self.bal["ZUSD"] -= sgn * cost + fee
            txid = f"O{len(self.orders) + 1}"
            self.orders[txid] = {"status": "closed", "vol_exec": data["volume"], "cost": f"{cost:.8f}", "fee": f"{fee:.8f}"}
            return self.ok({"txid": [txid]})
        if method == "QueryOrders":
            return self.ok({data["txid"]: self.orders[data["txid"]]})
        return self.err(f"EGeneral:Unknown method {method}")

    @staticmethod
    def ok(result):
        return httpx.Response(200, json={"error": [], "result": result})

    @staticmethod
    def err(msg):
        return httpx.Response(200, json={"error": [msg], "result": {}})


def broker(fk: FakeKraken) -> KrakenBroker:
    return KrakenBroker("KEY", SECRET, client=httpx.AsyncClient(base_url="https://api.kraken.com",
                                                                transport=httpx.MockTransport(fk.handler)))


def test_signature_matches_krakens_documented_example():
    data = {"nonce": "1616492376594", "ordertype": "limit", "pair": "XBTUSD", "price": 37500, "type": "buy", "volume": 1.25}
    assert sign("/0/private/AddOrder", data, "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg==") \
        == "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="


def test_code_can_never_call_a_withdrawal_endpoint():
    fk = FakeKraken()
    b = broker(fk)
    for method in ("Withdraw", "WithdrawCancel", "WalletTransfer", "Stake", "DepositAddresses"):
        with pytest.raises(KrakenError, match="refused"):
            run(b._private(method, asset="XBT", amount="1"))
    assert fk.calls == []  # refused before any request left the server
    assert not any(m.startswith("Withdraw") and m != "WithdrawInfo" for m in ALLOWED_PRIVATE)


def test_a_key_that_can_withdraw_is_refused():
    with pytest.raises(ValueError, match="Withdraw Funds"):
        run(broker(FakeKraken(can_withdraw=True)).verify())


def test_a_trade_only_key_is_accepted_and_a_view_only_key_explained():
    acct = run(broker(FakeKraken()).verify())
    assert acct["cash"] == 500.0
    with pytest.raises(ValueError, match="Create & Modify Orders"):
        run(broker(FakeKraken(can_trade=False)).verify())


def test_market_orders_round_to_lot_size_and_report_fees():
    fk = FakeKraken()
    b = broker(fk)
    o = Order("BTC-USD", Side.BUY, 0.001234567891, request_id="r1")
    fills = run(b.submit(o))
    assert o.status is OrderStatus.FILLED and len(fills) == 1
    f = fills[0]
    assert f.qty == pytest.approx(0.00123456) and f.price == pytest.approx(60_000) and f.commission > 0
    tiny = Order("DOGE-USD", Side.BUY, 5, request_id="r2")  # below Kraken's 20 DOGE minimum
    assert run(b.submit(tiny)) == [] and "minimum" in tiny.reason
    nope = Order("PEPE-USD", Side.BUY, 1e6, request_id="r3")
    assert run(b.submit(nope)) == [] and "isn't traded on Kraken" in nope.reason


def test_coins_you_already_hold_are_never_counted_or_sold():
    fk = FakeKraken(held={"XXBT": 0.5})
    b = broker(fk)
    run(b.load_pairs())
    run(b.capture_baseline())
    assert run(b.positions()) == {}  # your 0.5 BTC isn't the bots' to trade
    run(b.submit(Order("BTC-USD", Side.BUY, 0.01, request_id="r")))
    assert run(b.positions()) == {"BTC-USD": pytest.approx(0.01)}  # only what the bots bought


def kraken_desk(fk):
    s = Settings(feed="coinbase", live_platform="kraken", kraken_key="KEY", kraken_secret=SECRET,
                 symbols=["BTC-USD", "DOGE-USD"], withdrawal_cooldown_h=0)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fk))


def test_bots_trade_and_take_profit_on_kraken_but_cannot_withdraw():
    fk = FakeKraken(usd=5_000.0, held={"XXBT": 0.2})
    desk = kraken_desk(fk)
    run(desk.live_broker.load_pairs())
    run(desk.switch_mode("live"))
    assert desk.is_live and desk.broker.name == "kraken" and desk.portfolio.cash == 5_000.0
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("BTC-USD")
    assert pos.qty > 0 and "AddOrder" in fk.calls
    # price rises to the +6% target: the bots take profit (half, then the runner rides)
    fk.price["XBTUSD"] = 63_700.0
    trades = capture(desk, "trade.closed")
    desk.prices["BTC-USD"] = 63_700.0
    desk.bus.publish("market.tick", Tick("BTC-USD", 63_700.0))
    run(desk.bus.drain())
    assert trades and trades[-1].pnl > 0
    assert run(desk.bots["portfolio"].reconcile_positions()) == []  # books match Kraken (your 0.2 BTC ignored)
    # withdrawals: never sent from TradeZone
    w = desk.wallets.link("BTC", "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq")
    assert desk.wallets.withdraw(w.id, "BTC", 0.0001).status == "manual_required"
    assert not any(m.startswith("Withdraw") and m != "WithdrawInfo" for m in fk.calls)
    assert desk.broker_info()["can_withdraw"] is False


def test_link_kraken_from_the_app_and_remember_it(monkeypatch, tmp_path):
    fk = FakeKraken()
    import tradezone.brokers.kraken as kmod
    real = httpx.AsyncClient
    monkeypatch.setattr(kmod.httpx, "AsyncClient",
                        lambda **kw: real(base_url="https://api.kraken.com", transport=httpx.MockTransport(fk.handler)))
    db = str(tmp_path / "tz.db")
    desk = TradingDesk(Settings(feed="coinbase"), journal=Journal(db))
    with pytest.raises(ValueError, match="Withdraw Funds"):
        fk.can_withdraw = True
        run(desk.link_broker("KEY", SECRET, "kraken"))
    fk.can_withdraw = False
    assert run(desk.link_broker("KEY", SECRET, "kraken"))["cash"] == 500.0
    info = desk.broker_info()
    assert info["linked"] and info["platform"] == "kraken" and info["ready"]
    again = TradingDesk(Settings(feed="coinbase"), journal=Journal(db))
    assert again.load_linked_broker() and again.live_broker.name == "kraken"
