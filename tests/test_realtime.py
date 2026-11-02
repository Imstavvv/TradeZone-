"""Real-time Coinbase stream, REST fallback and history back-fill."""

import asyncio
import json
import time

import httpx
import pytest
from websockets.asyncio.server import serve

from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.coinbase import CoinbaseFeed
from tests.helpers import capture


def rest_client(handler):
    return httpx.AsyncClient(base_url="https://x", transport=httpx.MockTransport(handler))


def test_stream_subscribes_and_delivers_trades():
    async def go():
        subs = []

        async def handler(ws):
            subs.append(json.loads(await ws.recv()))
            for px in ("65000.10", "65001.50"):
                await ws.send(json.dumps({"type": "ticker", "product_id": "BTC-USD", "price": px,
                                          "last_size": "0.01", "time": "2026-11-02T20:00:00.123Z"}))
            await asyncio.sleep(0.5)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            feed = CoinbaseFeed(["BTC-USD", "ETH-USD"], ws_url=f"ws://127.0.0.1:{port}",
                                client=rest_client(lambda r: httpx.Response(500)))
            await feed.start()
            for _ in range(50):
                await asyncio.sleep(0.05)
                if len(feed._buffer) >= 2:
                    break
            ticks = await feed.poll()
            await feed.close()
            return subs, ticks, feed

    subs, ticks, feed = asyncio.run(go())
    assert subs[0] == {"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"], "channels": ["ticker"]}
    assert [t.price for t in ticks] == [65000.10, 65001.50] and feed.mode == "stream"
    assert ticks[0].ts == pytest.approx(1793649600.123, abs=1)


def test_falls_back_to_rest_when_stream_is_down():
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, json={"price": "100.5", "volume": "1"})

    async def go():
        feed = CoinbaseFeed([f"S{i}-USD" for i in range(8)], ws_url="ws://127.0.0.1:9", client=rest_client(handler))
        await feed.start()
        await asyncio.sleep(0.2)
        t1 = await feed.poll()
        t2 = await feed.poll()
        await feed.close()
        return feed, t1, t2

    feed, t1, t2 = asyncio.run(go())
    assert feed.mode == "rest" and "stream" in feed.last_error
    assert len(t1) == 5 and len(t2) == 5  # round-robin batches, under the public rate limit
    assert {t.symbol for t in t1 + t2} == {f"S{i}-USD" for i in range(8)}


def test_history_parses_candles_and_drops_open_candle():
    now = int(time.time()) // 60 * 60
    rows = [[now, 1, 2, 1.5, 1.6, 10]] + [[now - 60 * i, 99, 101, 100, 100 + i / 100, 5] for i in range(1, 6)]

    def handler(req):
        assert req.url.params["granularity"] == "60"
        return httpx.Response(200, json=rows)

    feed = CoinbaseFeed(["BTC-USD"], client=rest_client(handler), use_ws=False)
    candles = asyncio.run(feed.history("BTC-USD", 60))
    assert len(candles) == 5 and candles[0]["ts"] < candles[-1]["ts"]  # oldest first, open candle dropped
    assert asyncio.run(feed.history("BTC-USD", 15)) is None  # unsupported granularity


def test_backfill_warms_up_without_trading():
    now = int(time.time()) // 60 * 60

    def handler(req):
        if "candles" in req.url.path:
            # 300 one-minute candles trending up hard (would trigger buys if traded live)
            return httpx.Response(200, json=[[now - 60 * i, 99 + (300 - i) * 0.2, 101 + (300 - i) * 0.2,
                                              100 + (300 - i) * 0.2, 100 + (300 - i) * 0.2, 5] for i in range(1, 301)])
        return httpx.Response(500)

    s = Settings(feed="coinbase", symbols=["BTC-USD"])
    feed = CoinbaseFeed(s.symbols, client=rest_client(handler), use_ws=False)
    desk = TradingDesk(s, feed=feed, journal=Journal(":memory:"))
    signals = capture(desk, "signal.proposed")
    asyncio.run(desk.start())
    scout = desk.bots["scout"]
    assert scout.metrics["backfilled_bars"] == 300
    assert desk.state()["feed_status"]["warming_up"] is None  # ready immediately
    assert desk.bots["strategist"].regimes["BTC-USD"].regime.value == "trending"
    assert signals == []  # no trades on historical data
    assert desk.prices["BTC-USD"] == pytest.approx(100 + 299 * 0.2)
