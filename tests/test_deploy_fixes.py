"""Regressions found while checking the Render deployment."""

import asyncio

import httpx
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.helpers import capture, run
from tradezone.config import Settings
from tradezone.core.models import Tick
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.base import MarketFeed
from tradezone.market.coinbase import CoinbaseFeed
from tradezone.server import create_app


def test_ws_rejects_bad_token_with_4401_after_accept():
    desk = TradingDesk(Settings(feed="sim", api_token="s3cret"), journal=Journal(":memory:"))
    with TestClient(create_app(desk, run_desk=False)) as c:
        try:
            with c.websocket_connect("/ws") as ws:
                ws.send_json({"type": "auth", "token": "wrong"})
                ws.receive_json()
            raise AssertionError("expected close")
        except WebSocketDisconnect as e:
            assert e.code == 4401
        with c.websocket_connect("/ws?token=s3cret") as ws:  # tokens in URLs are no longer accepted
            ws.send_json({"type": "auth", "token": "s3cret"})  # ...but a first auth message is
            assert ws.receive_json()["type"] == "snapshot"
        assert c.get("/api/auth").status_code == 401
        assert c.get("/api/auth", headers={"Authorization": "Bearer s3cret"}).json()["ok"]
        assert c.get("/api/health").json() == {"ok": True}  # reveals nothing


def test_coinbase_backs_off_when_rate_limited():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(429, headers={"Retry-After": "30"})

    feed = CoinbaseFeed(["BTC-USD", "ETH-USD"],
                        client=httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler)))
    assert asyncio.run(feed.poll()) == []
    assert "rate limited" in feed.last_error
    n = len(calls)
    assert asyncio.run(feed.poll()) == [] and len(calls) == n  # paused: no new requests


def test_coinbase_reports_unknown_product():
    feed = CoinbaseFeed(["NOPE-USD"], client=httpx.AsyncClient(
        base_url="https://x", transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    asyncio.run(feed.poll())
    assert "not a Coinbase product" in feed.last_error


class FlakyFeed(MarketFeed):
    name = "flaky"

    def __init__(self):
        self.up = False

    async def poll(self):
        if not self.up:
            self.last_error = "connection refused"
            return []
        return [Tick("BTC-USD", 100.0)]


def test_feed_outage_alerts_and_recovers():
    feed = FlakyFeed()
    desk = TradingDesk(Settings(feed="sim", symbols=["BTC-USD"]), feed=feed, journal=Journal(":memory:"))
    alerts = capture(desk, "alert")

    async def go(n):
        for _ in range(n):
            await desk.step()

    run(go(desk.FEED_DOWN_AFTER))
    assert desk.feed_down and "connection refused" in alerts[-1].message
    assert desk.state()["feed_status"]["ok"] is False
    feed.up = True
    run(go(1))
    assert not desk.feed_down and "restored" in alerts[-1].message


def test_feed_exception_does_not_stop_desk():
    class Boom(MarketFeed):
        async def poll(self):
            raise RuntimeError("kaboom")

    desk = TradingDesk(Settings(feed="sim"), feed=Boom(), journal=Journal(":memory:"))
    run(desk.step())
    assert desk.cycles == 1 and "kaboom" in desk.feed.last_error


def test_warmup_status_counts_bars():
    s = Settings(feed="coinbase", bar_seconds=10)
    from tradezone.market.simulated import SimulatedFeed
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    w = desk.state()["feed_status"]["warming_up"]
    assert w["done"] == 0 and w["needed"] == 30 and w["unit"] == "bars"
