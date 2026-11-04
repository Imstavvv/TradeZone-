"""Uphold: real-money trading through card-to-card transactions."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.helpers import ORIGIN, capture, run
from tradezone.brokers.uphold import UpholdBroker, create_personal_access_token
from tradezone.config import Settings
from tradezone.core.models import Order, OrderStatus, Side, Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.server import create_app

TOKEN = "upholdtoken-0123456789abcdef"


def card(cid, cur, bal, usd=None):
    return {"id": cid, "currency": cur, "balance": str(bal), "available": str(bal),
            "normalized": [{"currency": "USD", "balance": str(bal if usd is None else usd),
                            "available": str(bal if usd is None else usd)}]}


class FakeUphold:
    """Just enough of api.uphold.com to trade USD <-> coins."""

    def __init__(self, cards=None, rate=None, spread=0.01):
        self.cards = cards if cards is not None else [card("usd1", "USD", 50)]
        self.rate = rate or {"DOGE": 0.20, "BTC": 60_000.0}
        self.spread, self.calls, self.status = spread, [], "completed"

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path, dict(req.url.params)))
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        path = req.url.path
        if path == "/v0/assets":
            return httpx.Response(200, json=[{"code": c, "status": "open"} for c in ("USD", "BTC", "DOGE", "PEPE")])
        if path == "/v0/me/cards" and req.method == "GET":
            return httpx.Response(200, json=self.cards)
        if path == "/v0/me/cards" and req.method == "POST":
            body = json.loads(req.content)
            c = card(f"{body['currency'].lower()}1", body["currency"], 0)
            self.cards.append(c)
            return httpx.Response(200, json=c)
        if path.endswith("/transactions") and req.method == "POST":
            assert req.url.params["commit"] == "true"
            body = json.loads(req.content)
            origin = next(c for c in self.cards if c["id"] == path.split("/")[4])
            dest = next(c for c in self.cards if c["id"] == body["destination"])
            coin, qty = body["denomination"]["currency"], float(body["denomination"]["amount"])
            if origin["currency"] == "USD":  # buy: pay the ask
                usd = qty * self.rate[coin] * (1 + self.spread)
                if usd > float(origin["available"]):
                    return httpx.Response(400, json={"code": "validation_failed", "errors": "insufficient funds"})
                o_amt, d_amt = usd, qty
            else:  # sell: receive the bid
                o_amt, d_amt = qty, qty * self.rate[coin] * (1 - self.spread)
            for c, delta in ((origin, -o_amt), (dest, d_amt)):
                c["balance"] = c["available"] = str(float(c["balance"]) + delta)
            return httpx.Response(200, json={
                "id": "tx-1", "status": self.status,
                "origin": {"amount": str(o_amt), "currency": origin["currency"]},
                "destination": {"amount": str(d_amt), "currency": dest["currency"]}})
        if path.startswith("/v0/me/transactions/"):
            return httpx.Response(200, json={"id": "tx-1", "status": "completed",
                                             "origin": {"amount": "10.1"}, "destination": {"amount": "50"}})
        return httpx.Response(404, json={"error": "not found"})


def broker(fake, **kw):
    client = httpx.AsyncClient(base_url=UpholdBroker.BASE, transport=httpx.MockTransport(fake),
                               headers={"Authorization": f"Bearer {TOKEN}"})
    b = UpholdBroker(TOKEN, client=client, **kw)
    b.on_prices([Tick("DOGE-USD", 0.20), Tick("BTC-USD", 60_000.0)])
    return b


def test_buy_opens_a_coin_card_and_books_the_spread_as_cost():
    fake = FakeUphold()
    b = broker(fake)
    o = Order("DOGE-USD", Side.BUY, 100, "r")
    fills = run(b.submit(o))
    assert o.status is OrderStatus.FILLED and o.broker_id == "tx-1"
    assert ("POST", "/v0/me/cards", {}) in fake.calls  # first DOGE trade: card created
    f = fills[0]
    assert f.qty == 100 and f.price == pytest.approx(0.20)  # market price: no fake "slippage"
    assert f.commission == pytest.approx(0.20)  # the 1% spread on $20, counted as a cost
    assert f.qty * f.price + f.commission == pytest.approx(20.2)  # every cent paid is accounted for


def test_sell_goes_back_to_usd():
    fake = FakeUphold(cards=[card("usd1", "USD", 0), card("doge1", "DOGE", 100, usd=20)])
    b = broker(fake)
    o = Order("DOGE-USD", Side.SELL, 100, "r")
    f = run(b.submit(o))[0]
    assert f.price == pytest.approx(0.20) and f.commission == pytest.approx(0.20)
    assert float(fake.cards[0]["balance"]) == pytest.approx(19.8)
    assert run(b.poll(o)) == []  # already filled: nothing reported twice


def test_insufficient_funds_and_unknown_pairs_are_rejected():
    b = broker(FakeUphold())
    o = Order("BTC-USD", Side.BUY, 1, "r")
    assert run(b.submit(o)) == [] and o.status is OrderStatus.REJECTED and "400" in o.reason
    o2 = Order("DOGE-EUR", Side.BUY, 1, "r")
    assert run(b.submit(o2)) == [] and o2.status is OrderStatus.REJECTED
    o3 = Order("DOGE-USD", Side.SELL, 1, "r")  # no DOGE card at all
    assert run(b.submit(o3)) == [] and "no DOGE balance" in o3.reason


def test_pending_transaction_fills_on_poll():
    fake = FakeUphold()
    fake.status = "pending"
    b = broker(fake)
    o = Order("DOGE-USD", Side.BUY, 50, "r")
    assert run(b.submit(o)) == [] and o.status is OrderStatus.SUBMITTED
    f = run(b.poll(o))[0]
    assert o.status is OrderStatus.FILLED and f.qty == 50 and f.price == pytest.approx(0.20)


def test_account_positions_and_supported_coins():
    fake = FakeUphold(cards=[card("usd1", "USD", 50), card("doge1", "DOGE", 100, usd=20),
                             card("eth1", "ETH", 1, usd=3000), card("btc1", "BTC", 1e-9, usd=0.00006),
                             card("eur1", "EUR", 10, usd=11)])
    b = broker(fake, symbols=["DOGE-USD", "BTC-USD"])
    assert run(b.account()) == {"cash": 50.0, "equity": pytest.approx(3081.00006)}
    # your ETH isn't something the bots trade, BTC dust and EUR cash aren't positions
    assert run(b.positions()) == {"DOGE-USD": 100.0}
    assert b.supports("DOGE-USD") is None  # unknown until the asset list loads
    run(b.load_assets())
    assert b.supports("DOGE-USD") and b.supports("BONK-USD") is False and b.supports("AAPL") is False


def test_personal_access_token_flow():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, req.headers.get("OTP-Token")))
        assert req.headers["Authorization"].startswith("Basic ")
        if req.url.path == "/v0/me/authentication_methods":
            return httpx.Response(200, json=[{"id": "m1", "default": True, "verified": True}])
        return httpx.Response(200, json={"accessToken": "pat-123"})

    client = httpx.AsyncClient(base_url=UpholdBroker.BASE, transport=httpx.MockTransport(handler))
    assert run(create_personal_access_token("me@x.com", "pw", "123456", client=client)) == "pat-123"
    assert seen[-1] == ("POST", "/v0/me/tokens", "123456")


def test_settings_pick_uphold_and_practise_with_its_costs(monkeypatch):
    monkeypatch.delenv("TZ_CRYPTO_FEE_BPS", raising=False)
    s = Settings(feed="coinbase", uphold_token=TOKEN)
    s.validate()
    assert s.venue() == "uphold" and s.live_blockers() == [] and s.crypto_fee_bps == 100
    s2 = Settings(feed="coinbase", live_venue="uphold")
    s2.validate()
    assert "Uphold" in s2.live_blockers()[0]
    monkeypatch.setenv("TZ_CRYPTO_FEE_BPS", "60")  # an explicit setting wins
    s3 = Settings(feed="coinbase", uphold_token=TOKEN, crypto_fee_bps=60)
    s3.validate()
    assert s3.crypto_fee_bps == 60
    with pytest.raises(ValueError):
        Settings(feed="coinbase", live_venue="kraken").validate()


@pytest.fixture
def site(tmp_path, monkeypatch):
    fake = FakeUphold()
    real_init = UpholdBroker.__init__

    def init(self, token, client=None, **kw):
        client = httpx.AsyncClient(base_url=UpholdBroker.BASE, headers={"Authorization": f"Bearer {token}"},
                                   transport=httpx.MockTransport(fake))
        real_init(self, token, client=client, **kw)
    monkeypatch.setattr(UpholdBroker, "__init__", init)
    desk = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=Journal(str(tmp_path / "tz.db")))
    with TestClient(create_app(desk, run_desk=False), base_url=ORIGIN) as c:
        c.desk = desk
        c.post("/api/signin/code", json={"code": desk.security.recovery_code})
        yield c


def test_link_uphold_from_the_app(site):
    r = site.post("/api/accounts/broker", json={"key": "short", "venue": "uphold"})
    assert r.status_code == 400 and "whole Uphold access token" in r.json()["detail"]
    r = site.post("/api/accounts/broker", json={"key": f"Bearer {TOKEN}", "venue": "uphold"}).json()
    assert r["ok"] and r["venue"] == "uphold" and r["label"] == "Uphold" and r["account"]["cash"] == 50
    assert "BONK-USD" in r["practice_only"] or "WIF-USD" in r["practice_only"]
    assert TOKEN not in site.get("/api/accounts").text  # never sent back
    assert site.desk.settings.crypto_fee_bps >= 100  # practice now costs what Uphold costs
    chk = site.get("/api/mode").json()
    assert chk["blockers"] == [] and chk["account"]["trading_capital"] == 50
    again = TradingDesk(Settings(feed="coinbase", host="0.0.0.0"), journal=site.desk.journal)
    assert again.load_linked_broker() and again.live_broker.name == "uphold"  # remembered across restarts
    assert site.delete("/api/accounts/broker").status_code == 200
    assert site.get("/api/accounts").json()["broker"]["linked"] is False


def test_live_team_trades_on_uphold_and_counts_the_spread():
    from tests.test_meme_runners import buy, tick
    from tradezone.market.simulated import SimulatedFeed

    fake = FakeUphold(cards=[card("usd1", "USD", 50)])
    s = Settings(feed="coinbase", uphold_token=TOKEN, symbols=["DOGE-USD", "BONK-USD"])
    s.risk.symbol_cooldown_s = 0
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"), live_broker=broker(fake))
    run(desk.live_broker.load_assets())
    run(desk.switch_mode("live"))
    assert desk.is_live and desk.portfolio.cash == 50
    rejected = capture(desk, "signal.rejected")
    assert buy(desk, "BONK-USD", 0.00003).is_flat and "practice only" in rejected[-1]["reason"]
    pos = buy(desk, "DOGE-USD", 0.20)
    assert not pos.is_flat and not desk.paused, desk.pause_reason  # Uphold's spread isn't "slippage"
    spent = 50 - float(fake.cards[0]["balance"])
    assert desk.portfolio.cash == pytest.approx(50 - spent)  # our book matches the Uphold account
    tick(desk, "DOGE-USD", 0.20)
    assert desk.portfolio.equity < 50  # the spread is a real cost, already counted
