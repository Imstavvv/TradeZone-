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
from tradezone.core.models import Order, OrderStatus, Side, Signal, SignalKind, Tick
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
        self.spread = {"XBTUSD": 0.0002, "XDGUSD": 0.001}  # bid/ask gap
        self.volume = {"XBTUSD": 2_000.0, "XDGUSD": 50_000_000.0}  # coins traded in 24 h
        self.orders, self.calls, self.oflags = {}, [], []

    def handler(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/0/public/AssetPairs":
            return httpx.Response(200, json={"error": [], "result": {
                "XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT", "lot_decimals": 8, "ordermin": "0.0001"},
                "XDGUSD": {"altname": "XDGUSD", "wsname": "XDG/USD", "base": "XXDG", "lot_decimals": 2, "ordermin": "20"},
                "XXBTZEUR": {"altname": "XBTEUR", "wsname": "XBT/EUR", "base": "XXBT", "lot_decimals": 8, "ordermin": "0.0001"}}})
        if path == "/0/public/Ticker":
            keys = {"XBTUSD": "XXBTZUSD", "XDGUSD": "XDGUSD"}
            out = {}
            for alt in req.url.params["pair"].split(","):
                px, gap = self.price[alt], self.spread[alt]
                out[keys[alt]] = {"c": [str(px), "1"], "b": [str(px * (1 - gap / 2)), "1", "1"],
                                  "a": [str(px * (1 + gap / 2)), "1", "1"], "v": ["1", str(self.volume[alt])],
                                  "p": [str(px), str(px)]}
            return self.ok(out)
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
            self.oflags.append(data.get("oflags"))
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
    s = Settings(feed="coinbase", live_venue="kraken", kraken_key="KEY", kraken_secret=SECRET,
                 symbols=["BTC-USD", "DOGE-USD"], withdrawal_cooldown_h=0)
    s.risk.symbol_cooldown_s = 0
    return TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fk))


def test_bots_trade_and_take_profit_on_kraken_but_cannot_withdraw():
    fk = FakeKraken(usd=5_000.0, held={"XXBT": 0.2})
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())  # pairs + bid / ask / volume for the "can it be sold?" check
    run(desk.switch_mode("live"))
    assert desk.is_live and desk.broker.name == "kraken" and desk.portfolio.cash == 5_000.0
    prime(desk, "BTC-USD", 60_000.0, atr=300.0)
    desk.bus.publish("signal.proposed", Signal("BTC-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    pos = desk.portfolio.position("BTC-USD")
    assert pos.qty > 0 and "AddOrder" in fk.calls
    assert fk.oflags[-1] == "fciq"  # fee paid in USD: every coin bought arrives and can be sold again
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
    assert desk.wallets.withdraw(w.id, "BTC", 0.0003).status == "manual_required"  # ~$19 (test-send cap $25)
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
        run(desk.link_broker("KEY", SECRET, venue="kraken"))
    fk.can_withdraw = False
    assert run(desk.link_broker("KEY", SECRET, venue="kraken"))["cash"] == 500.0
    info = desk.broker_info()
    assert info["linked"] and info["venue"] == "kraken" and info["label"] == "Kraken" and info["ready"]
    again = TradingDesk(Settings(feed="coinbase"), journal=Journal(db))
    assert again.load_linked_broker() and again.live_broker.name == "kraken"


def test_never_buys_a_coin_it_cannot_sell_again():
    fk = FakeKraken(usd=5_000.0)
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())
    run(desk.switch_mode("live"))
    rejected = capture(desk, "signal.rejected")

    def try_buy(sym, price):
        desk.settings.symbols.append(sym) if sym not in desk.settings.symbols else None
        prime(desk, sym, price, atr=price * 0.005)
        desk.bus.publish("signal.proposed", Signal(sym, Side.BUY, 0.9, "t", "quant"))
        run(desk.bus.drain())
        return desk.portfolio.position(sym).qty

    assert try_buy("PEPE-USD", 0.00001) == 0  # Kraken doesn't list it here: can't be sold there
    assert "can't be traded on your real-money Kraken" in rejected[-1]["reason"]
    assert "can't be bought and sold on Kraken" in desk.live_broker.sell_check("PEPE-USD", 1e7, 0.00001)
    fk.spread["XDGUSD"] = 0.05  # 5% bid/ask gap: selling would lose 5% straight away
    run(desk.live_broker.refresh_market())
    assert try_buy("DOGE-USD", 0.2) == 0 and "too thin to sell" in rejected[-1]["reason"]
    fk.spread["XDGUSD"], fk.volume["XDGUSD"] = 0.001, 10_000.0  # only $2k traded a day: no one to sell to
    run(desk.live_broker.refresh_market())
    assert try_buy("DOGE-USD", 0.2) == 0 and "too little trading" in rejected[-1]["reason"]
    fk.volume["XDGUSD"] = 50_000_000.0
    run(desk.live_broker.refresh_market())
    desk.bots["risk"].last_trade.clear()
    assert try_buy("DOGE-USD", 0.2) > 0  # liquid, listed, big enough: fine
    b = desk.live_broker
    assert "too small to sell" in b.sell_check("DOGE-USD", 15, 0.2)  # under Kraken's 20 DOGE minimum
    assert b.sell_check("DOGE-USD", 30, 0.2) is None  # partial profit-takes sell it all rather than leave dust
    b.quotes["BTC-USD"]["ts"] -= 600  # stale market data: real money refuses rather than guesses
    assert "can't confirm" in b.sell_check("BTC-USD", 0.01, 60_000)


def test_partial_profit_takes_never_leave_an_unsellable_remainder():
    fk = FakeKraken(usd=5_000.0)
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())
    run(desk.switch_mode("live"))
    desk.portfolio.position("DOGE-USD")
    prime(desk, "DOGE-USD", 0.2, atr=0.001)
    pos = desk.portfolio.position("DOGE-USD")
    pos.qty, pos.avg_price, pos.last_price, pos.opened_ts = 30.0, 0.18, 0.2, 1.0  # 30 DOGE; Kraken's minimum is 20
    fk.bal["XXDG"] = 30.0
    approved = capture(desk, "order.approved")
    desk.bus.publish("signal.proposed", Signal("DOGE-USD", Side.SELL, 1.0, "bank half", "skimmer",
                                               kind=SignalKind.EXIT, fraction=0.5))
    run(desk.bus.drain())
    assert approved and approved[-1].qty == pytest.approx(30.0)  # 15 + 15 would both be under 20: sells all 30


def test_feed_finds_kraken_meme_coins_that_trade_enough():
    from tradezone.market.kraken import KrakenFeed

    pairs = {"XXBTZUSD": {"altname": "XBTUSD", "wsname": "XBT/USD", "base": "XXBT"},
             "XDGUSD": {"altname": "XDGUSD", "wsname": "XDG/USD", "base": "XXDG"},
             "BONKUSD": {"altname": "BONKUSD", "wsname": "BONK/USD", "base": "BONK"},
             "PENGUUSD": {"altname": "PENGUUSD", "wsname": "PENGU/USD", "base": "PENGU"},
             "TINYUSD": {"altname": "TINYUSD", "wsname": "TINY/USD", "base": "TINY"},
             "HALTUSD": {"altname": "HALTUSD", "wsname": "HALT/USD", "base": "HALT", "status": "cancel_only"}}
    vol = {"PENGUUSD": 9e9, "TINYUSD": 10.0, "XBTUSD": 1e3, "XDGUSD": 1e9, "BONKUSD": 1e12}

    def handler(req):
        if req.url.path.endswith("AssetPairs"):
            return httpx.Response(200, json={"error": [], "result": pairs})
        if req.url.path.endswith("Ticker"):
            res = {}
            for alt in req.url.params["pair"].split(","):
                res[alt] = {"c": ["0.01", "1"], "b": ["0.00999", "1", "1"], "a": ["0.01", "1", "1"],
                            "v": ["1", str(vol[alt])], "p": ["0.01", "0.01"]}
            return httpx.Response(200, json={"error": [], "result": res})
        rows = [[1700000000 + 60 * i, "1", "2", "0.5", "1.5", "1", "10", 5] for i in range(4)]
        return httpx.Response(200, json={"error": [], "result": {"XDGUSD": rows, "last": 1}})

    symbols, memes = ["BTC-USD", "DOGE-USD", "PEPE-USD"], ["DOGE", "PEPE"]
    feed = KrakenFeed(symbols, client=httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)),
                      meme_candidates=["PENGU", "TINY", "HALT", "NOPE"], meme_coins=memes, min_meme_volume_usd=1e6)
    run(feed.start())
    assert feed.dropped == ["PEPE-USD"]  # not on Kraken in this list
    assert symbols == ["BTC-USD", "DOGE-USD", "PENGU-USD"]  # PENGU trades enough; TINY doesn't; HALT can't be sold
    assert "PENGU" in memes  # treated as a meme coin (smaller bets, meme stops)
    feed._last_poll = 0
    ticks = run(feed.poll())
    assert {t.symbol for t in ticks} == set(symbols) and feed.quotes["DOGE-USD"]["vol_usd"] > 0
    candles = run(feed.history("DOGE-USD", 60))
    assert len(candles) == 3 and candles[0]["close"] == 1.5  # the still-forming candle is left out


def test_linking_kraken_in_the_app_switches_to_krakens_prices():
    desk = TradingDesk(Settings(), journal=Journal(":memory:"))  # feed=auto, nothing linked: Coinbase prices
    assert desk.feed.name == "coinbase" and desk.settings.feed_choice == "auto"
    desk.settings.kraken_key, desk.settings.kraken_secret, desk.settings.live_venue = "KEY", SECRET, "kraken"
    desk.live_broker = KrakenBroker("KEY", SECRET)
    run(desk._venue_feed())
    assert desk.feed.name == "kraken" and desk.settings.feed == "kraken"
    run(desk.feed.close())
    run(desk.live_broker.close())


def test_kraken_rewards_balances_count_as_yours():
    fk = FakeKraken(usd=0.0, held={"XBT.F": 0.25, "USD.F": 120.0})  # auto-earn: Kraken books coins as XBT.F
    b = broker(fk)
    run(b.load_pairs())
    assert run(b.account())["cash"] == 120.0
    assert run(b.positions()) == {"BTC-USD": pytest.approx(0.25)}  # not a "mismatch" that pauses trading


def test_a_stablecoin_only_account_gets_a_clear_message():
    fk = FakeKraken(usd=0.0, held={"USDC": 75.0})
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())
    chk = run(desk.mode_check())
    assert any("no US dollars" in b and "75.00 USDC" in b for b in chk["blockers"])


def test_small_accounts_round_up_to_krakens_minimum_order():
    fk = FakeKraken(usd=50.0)  # $50: a normal DOGE trade would be under Kraken's 20 DOGE minimum
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())
    run(desk.switch_mode("live"))
    prime(desk, "DOGE-USD", 0.2, atr=0.001)
    desk.bus.publish("signal.proposed", Signal("DOGE-USD", Side.BUY, 0.5, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("DOGE-USD").qty >= 20  # rounded up to a sellable size (risk stays small)


def test_a_mismatch_pause_clears_itself_once_the_books_match_again():
    fk = FakeKraken(usd=5_000.0)
    desk = kraken_desk(fk)
    run(desk.live_broker.refresh_market())
    run(desk.switch_mode("live"))
    fk.bal["XXBT"] = 0.01  # something appears that the bots didn't buy
    pm = desk.bots["portfolio"]
    assert run(pm.reconcile_positions()) and desk.paused
    fk.bal["XXBT"] = 0.0  # sorted out
    assert run(pm.reconcile_positions()) == [] and not desk.paused
