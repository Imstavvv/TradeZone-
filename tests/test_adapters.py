import asyncio

import httpx
import pytest

from tradezone.brokers.alpaca import AlpacaBroker
from tradezone.brokers.paper import PaperBroker
from tradezone.config import LIVE_CONFIRM_PHRASE, Settings
from tradezone.core.models import Order, OrderStatus, Side, Tick
from tradezone.market.coinbase import CoinbaseFeed


def test_paper_broker_slippage_and_commission():
    b = PaperBroker(commission_bps=10, slippage_bps=10)
    b.on_prices([Tick("X", 100.0)])
    o = Order("X", Side.BUY, 10, "r")
    fills = asyncio.run(b.submit(o))
    assert o.status is OrderStatus.FILLED
    assert fills[0].price == pytest.approx(100.1)
    assert fills[0].commission == pytest.approx(1001 * 0.001)
    o2 = Order("Y", Side.BUY, 1, "r")
    assert asyncio.run(b.submit(o2)) == [] and o2.status is OrderStatus.REJECTED


def test_alpaca_broker_submit_and_incremental_fills():
    calls = []
    state = {"n": 0}

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "POST":
            return httpx.Response(200, json={"id": "abc", "status": "accepted", "filled_qty": "0"})
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"id": "abc", "status": "partially_filled",
                                             "filled_qty": "4", "filled_avg_price": "10"})
        return httpx.Response(200, json={"id": "abc", "status": "filled",
                                         "filled_qty": "10", "filled_avg_price": "11.2"})

    client = httpx.AsyncClient(base_url="https://paper-api.alpaca.markets", transport=httpx.MockTransport(handler))
    b = AlpacaBroker("k", "s", paper=True, client=client)
    o = Order("AAPL", Side.BUY, 10, "r")

    async def go():
        assert await b.submit(o) == []
        f1 = await b.poll(o)
        f2 = await b.poll(o)
        return f1, f2

    f1, f2 = asyncio.run(go())
    assert f1[0].qty == 4 and f1[0].price == 10
    assert f2[0].qty == pytest.approx(6) and f2[0].price == pytest.approx(12.0)
    assert o.status is OrderStatus.FILLED
    assert calls[0] == ("POST", "/v2/orders")


def test_alpaca_reject_is_reported():
    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"message": "insufficient buying power"})))
    o = Order("AAPL", Side.BUY, 1, "r")
    asyncio.run(AlpacaBroker("k", "s", client=client).submit(o))
    assert o.status is OrderStatus.REJECTED and "403" in o.reason


def test_coinbase_feed_parses_and_skips_failures():
    def handler(req):
        if "BTC" in req.url.path:
            return httpx.Response(200, json={"price": "65000.5", "volume": "12"})
        return httpx.Response(500)

    client = httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))
    ticks = asyncio.run(CoinbaseFeed(["BTC-USD", "ETH-USD"], client=client).poll())
    assert [(t.symbol, t.price) for t in ticks] == [("BTC-USD", 65000.5)]


def test_live_mode_requires_explicit_confirmation():
    live = {"feed": "alpaca", "alpaca_live_key": "k", "alpaca_live_secret": "s"}
    s = Settings(mode="live", **live)
    with pytest.raises(ValueError, match="TZ_LIVE_CONFIRM"):
        s.validate()
    Settings(mode="live", live_confirm=LIVE_CONFIRM_PHRASE, **live).validate()
    with pytest.raises(ValueError, match="simulated"):
        Settings(mode="live", feed="sim", alpaca_live_key="k", alpaca_live_secret="s",
                 live_confirm=LIVE_CONFIRM_PHRASE).validate()
    with pytest.raises(ValueError, match="LIVE_KEY"):
        Settings(mode="live", feed="coinbase", live_confirm=LIVE_CONFIRM_PHRASE).validate()


def test_live_availability_reported():
    assert Settings(feed="sim").live_blockers()  # synthetic prices can't go live
    assert Settings(feed="coinbase", alpaca_live_key="k", alpaca_live_secret="s").live_blockers() == []


def test_paper_defaults_to_real_market_data():
    s = Settings()
    s.validate()
    assert s.feed == "coinbase" and all(x.endswith("-USD") for x in s.symbols)
    s = Settings(alpaca_key="k", alpaca_secret="s")
    s.validate()
    from tradezone.config import ALPACA_STOCKS
    assert s.feed == "alpaca" and "PEPE-USD" in s.symbols
    assert [x for x in s.symbols if "-" not in x] == ALPACA_STOCKS  # crypto plus the tested stocks
    with pytest.raises(ValueError, match="crypto"):
        Settings(feed="coinbase", symbols=["AAPL"]).validate()


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("TZ_SYMBOLS", "AAA, BBB")
    monkeypatch.setenv("TZ_MAX_DAILY_LOSS_PCT", "0.02")
    s = Settings.from_env()
    assert s.symbols == ["AAA", "BBB"] and s.risk.max_daily_loss_pct == 0.02
