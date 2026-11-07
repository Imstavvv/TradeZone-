"""Kraken price history: paced to Kraken's public limit, "Too many requests" recognised and retried (it arrives as an
ordinary HTTP 200 with an error list), other errors reported - and the start-up backfill keeps every coin's history
that arrived, even when some coins are slow."""

import asyncio
import time

import httpx

from tests.helpers import run
from tradezone.market.kraken import KrakenFeed

PAIRS = {"BTC-USD": {"pair": "XBTUSD", "key": "XXBTZUSD", "base": "XXBT"},
         "DOGE-USD": {"pair": "XDGUSD", "key": "XDGUSD", "base": "XXDG"}}


def candles(n=5):
    t0 = int(time.time()) // 60 * 60 - 60 * n
    return [[t0 + 60 * i, "1", "2", "0.5", "1.5", "1.4", "10", 3] for i in range(n + 1)]


def feed(handler, gap=0.0):
    f = KrakenFeed(list(PAIRS), client=httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler)))
    f.pairs = dict(PAIRS)
    f.HISTORY_GAP_S = gap
    return f


def test_too_many_requests_is_retried_not_taken_as_no_history(monkeypatch):
    import tradezone.market.kraken as K
    naps = []

    async def nap(s):
        naps.append(s)
    monkeypatch.setattr(K.asyncio, "sleep", nap)
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 2:  # Kraken: slow down (HTTP 200, an error list, no result)
            return httpx.Response(200, json={"error": ["EGeneral:Too many requests"], "result": {}})
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": candles(), "last": 1}})
    out = run(feed(handler).history("BTC-USD", 60))
    assert out and len(out) == 5 and calls["n"] == 3
    assert len([n for n in naps if n >= 1.5]) == 2  # (waited a little longer each time)


def test_another_kraken_error_is_reported_not_silent():
    def handler(req):
        return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})
    f = feed(handler)
    assert run(f.history("BTC-USD", 60)) is None and "Unknown asset pair" in f.last_error


def test_history_requests_are_spaced_out():
    times = []

    def handler(req):
        times.append(time.monotonic())
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": candles(), "last": 1}})
    f = feed(handler, gap=0.05)

    async def both():
        return await asyncio.gather(f.history("BTC-USD", 60), f.history("DOGE-USD", 60))
    run(both())
    assert len(times) == 2 and times[1] - times[0] >= 0.045  # one at a time, apart (not both at once)


def test_the_backfill_keeps_what_arrived_when_one_coin_is_slow():
    from tradezone.config import Settings
    from tradezone.desk import TradingDesk
    from tradezone.journal import Journal
    from tradezone.market.simulated import SimulatedFeed
    s = Settings(feed="sim", symbols=["BTC-USD", "DOGE-USD"])
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    d.bots["scout"].bar_seconds = 60
    bars = [{"ts": time.time() - 60 * (40 - i), "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0 + i * 0.001, "volume": 5.0}
            for i in range(40)]

    async def history(sym, gran, n):
        if sym == "DOGE-USD":
            await asyncio.sleep(10)  # a coin that never answers in time
        return bars
    d.feed.history = history
    got = run(d.backfill(timeout=0.2))
    assert got == 40  # BTC's history was kept (it used to be thrown away with the slow coin's)
