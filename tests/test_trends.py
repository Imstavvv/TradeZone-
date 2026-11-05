"""Trend Scout: meme coins over $50k market cap with promising volume, and trending coins, plugged in."""

import httpx
import pytest

from tests.helpers import prime, run
from tradezone.config import Settings
from tradezone.core.models import Side, Signal
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.simulated import SimulatedFeed

MEMES = [  # CoinGecko /coins/markets?category=meme-token
    {"symbol": "pepe", "name": "Pepe", "market_cap": 3e9, "total_volume": 6e8, "price_change_percentage_24h": 4.0},
    {"symbol": "mog", "name": "Mog", "market_cap": 4e8, "total_volume": 3e7},  # promising
    {"symbol": "tiny", "name": "Tiny", "market_cap": 80_000, "total_volume": 20_000},  # over $50k, but $20k/day: no
    {"symbol": "dust", "name": "Dust", "market_cap": 30_000, "total_volume": 90_000},  # under $50k market cap: no
    {"symbol": "sleepy", "name": "Sleepy", "market_cap": 5e8, "total_volume": 2e6},  # 0.4% turnover: not promising
    {"symbol": "dexonly", "name": "DEX only", "market_cap": 9e6, "total_volume": 4e6},  # not listed on the venue
]
TRENDING = {"coins": [{"item": {"symbol": "SUI", "name": "Sui", "data": {"market_cap": "$12,000,000,000",
                                                                         "total_volume": "$900,000,000"}}},
                      {"item": {"symbol": "MOG", "name": "Mog", "data": {"market_cap": "$400,000,000"}}}]}
LISTED = {f"{c}-USD": {"pair": f"{c}USD"} for c in ("BTC", "PEPE", "MOG", "TINY", "DUST", "SLEEPY", "SUI")}


class KrakenLike(SimulatedFeed):
    name = "kraken"
    pairs = LISTED

    async def history(self, symbol, granularity, limit=300):
        return [{"ts": 1_700_000_000 + 60 * i, "open": 1, "high": 1.01, "low": 0.99, "close": 1 + i * 1e-4, "volume": 5}
                for i in range(60)]


def make(**kw):
    s = Settings(feed="coinbase", symbols=["BTC-USD"], **kw)
    s.risk.symbol_cooldown_s = 0
    desk = TradingDesk(s, feed=KrakenLike(s.symbols), journal=Journal(":memory:"))
    desk.bots["scout"].bar_seconds = 60

    import copy
    data = desk.fake_gecko = {"memes": copy.deepcopy(MEMES), "trending": copy.deepcopy(TRENDING), "pump": []}

    def handler(req):
        if req.url.path.endswith("/coins/markets"):
            if req.url.params.get("category") == "pump-fun":
                return httpx.Response(200, json=data["pump"])
            return httpx.Response(200, json=data["memes"] if req.url.params["page"] == "1" else [])
        return httpx.Response(200, json=data["trending"])
    desk.bots["trends"].client = httpx.AsyncClient(base_url="https://api.coingecko.com/api/v3",
                                                   transport=httpx.MockTransport(handler))
    return desk


def test_plugs_in_promising_meme_coins_and_trending_coins_the_venue_lists():
    desk = make()
    res = run(desk.bots["trends"].refresh())
    assert set(res["added"]) == {"PEPE-USD", "MOG-USD", "SUI-USD"}
    assert res["added"][0] in ("SUI-USD", "MOG-USD")  # trending coins first
    for sym in ("TINY-USD", "DUST-USD", "SLEEPY-USD", "DEXONLY-USD"):
        assert sym not in desk.settings.symbols  # thin volume, too small, dead volume, or not on the venue
    assert desk.settings.is_meme("MOG-USD") and desk.settings.is_meme("PEPE-USD")  # meme rules apply
    assert not desk.settings.is_meme("SUI-USD")  # trending but not a meme coin
    assert desk.bots["scout"].books["MOG-USD"].count == 60  # price history loaded: tradeable straight away
    t = desk.state()["trends"]
    assert [x["symbol"] for x in t["trending"]] == ["SUI-USD", "MOG-USD"] and t["qualified"] == 2


def test_trending_coins_get_a_small_conviction_boost():
    desk = make()
    run(desk.bots["trends"].refresh())
    assert desk.bots["trends"].nudge("MOG-USD", 1) == pytest.approx(0.05)
    assert desk.bots["trends"].nudge("PEPE-USD", 1) == 0.0  # promising, not trending
    assert desk.bots["trends"].nudge("MOG-USD", -1) == 0.0  # no boost for shorting it


def test_coins_that_stop_qualifying_are_dropped_unless_held():
    desk = make()
    t = desk.bots["trends"]
    run(t.refresh())
    prime(desk, "PEPE-USD", 0.00001, atr=0.0000001)
    desk.bus.publish("signal.proposed", Signal("PEPE-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    held = desk.portfolio.position("PEPE-USD").qty > 0
    desk.fake_gecko["memes"] = []  # nothing qualifies any more
    desk.fake_gecko["trending"] = {"coins": []}
    res = run(t.refresh())
    assert "MOG-USD" in res["dropped"] and "SUI-USD" in res["dropped"]
    assert ("PEPE-USD" in desk.settings.symbols) == held  # still watched while the bots hold it


def test_it_needs_the_exchanges_own_prices():
    s = Settings(feed="coinbase", symbols=["BTC-USD"])
    desk = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    assert run(desk.bots["trends"].refresh()) == {"added": [], "dropped": []}
    assert "Kraken" in desk.bots["trends"].last_error


PUMP = [  # CoinGecko /coins/markets?category=pump-fun
    {"symbol": "fartcoin", "name": "Fartcoin", "market_cap": 9e8, "total_volume": 2e8, "current_price": 0.90},
    {"symbol": "pnut", "name": "Peanut", "market_cap": 2e8, "total_volume": 4e7, "current_price": 0.20},
    {"symbol": "pepe", "name": "PEPE (pump copycat)", "market_cap": 70_000, "total_volume": 150_000,
     "current_price": 0.0007},  # reuses a famous ticker: not the real PEPE
    {"symbol": "rugged", "name": "Rugged", "market_cap": 60_000, "total_volume": 2_000, "current_price": 0.01},
    {"symbol": "nokraken", "name": "DEX only", "market_cap": 5e6, "total_volume": 3e6, "current_price": 0.5},
]


def pump_desk(prices):
    desk = make()
    desk.fake_gecko["memes"] = []
    desk.fake_gecko["pump"] = PUMP
    desk.fake_gecko["trending"] = {"coins": []}
    desk.feed.pairs = {**LISTED, **{f"{c}-USD": {"pair": f"{c}USD"} for c in ("FARTCOIN", "PNUT", "RUGGED")}}

    async def last_prices(symbols):
        return {s: prices[s] for s in symbols if s in prices}
    desk.feed.last_prices = last_prices
    return desk


def test_pump_fun_coins_over_50k_with_plenty_of_volume_are_plugged_in():
    desk = pump_desk({"FARTCOIN-USD": 0.91, "PNUT-USD": 0.2, "PEPE-USD": 0.00001, "RUGGED-USD": 0.01})
    res = run(desk.bots["trends"].refresh())
    assert set(res["added"]) == {"FARTCOIN-USD", "PNUT-USD"}
    assert desk.settings.is_meme("FARTCOIN-USD")  # meme-coin rules: smaller bets, wider stops
    assert desk.state()["trends"]["pump"] == ["FARTCOIN-USD", "PNUT-USD"]
    assert "NOKRAKEN-USD" not in desk.settings.symbols and "RUGGED-USD" not in desk.settings.symbols  # not listed / dead


def test_a_copycat_reusing_a_famous_ticker_is_never_traded_as_the_real_coin():
    desk = pump_desk({"FARTCOIN-USD": 0.91, "PNUT-USD": 0.2, "PEPE-USD": 0.00001})
    run(desk.bots["trends"].refresh())
    assert "PEPE-USD" not in desk.settings.symbols  # the copycat's $0.0007 isn't Kraken's PEPE at $0.00001
    assert desk.bots["trends"].metrics["namesakes_skipped"] == 1


def test_a_pump_coin_whose_price_cant_be_checked_is_skipped():
    desk = pump_desk({"FARTCOIN-USD": 0.91})  # no Kraken price for PNUT right now
    res = run(desk.bots["trends"].refresh())
    assert res["added"] == ["FARTCOIN-USD"]


def test_pump_fun_discovery_can_be_switched_off():
    desk = pump_desk({"FARTCOIN-USD": 0.91, "PNUT-USD": 0.2})
    desk.settings.discover_pump = False
    assert run(desk.bots["trends"].refresh())["added"] == []


def test_kraken_feed_gives_last_prices_for_the_same_coin_check():
    from tests.test_kraken import FakeKraken
    from tradezone.market.kraken import KrakenFeed, fetch_pairs
    fk = FakeKraken()
    feed = KrakenFeed(["BTC-USD", "DOGE-USD"], client=httpx.AsyncClient(base_url="https://api.kraken.com",
                                                                        transport=httpx.MockTransport(fk.handler)))

    async def go():
        feed.pairs = await fetch_pairs(feed.client)
        return await feed.last_prices(["BTC-USD", "DOGE-USD", "NOPE-USD"])
    assert run(go()) == {"BTC-USD": 60_000.0, "DOGE-USD": 0.2}
