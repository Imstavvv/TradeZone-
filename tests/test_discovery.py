"""Finding new coins, stocks and news on every account - not only Kraken ones.

* crypto: whatever this account's exchange lists (Kraken pairs, Coinbase products, Alpaca's coins) and the
  account itself can trade (e.g. Uphold, which trades on Coinbase prices);
* stocks (Alpaca): the day's busiest established stocks - $10+, $50M+ traded today, a main exchange,
  fractional shares, never a leveraged or inverse fund;
* news: Alpaca's headlines are tagged with the tickers they're about, so stocks get news too.
"""

import copy
import json
import time

import httpx

from tests.helpers import run
from tests.test_trends import MEMES, TRENDING
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.alpaca import AlpacaFeed
from tradezone.market.simulated import SimulatedFeed


def gecko(desk):
    data = {"memes": copy.deepcopy(MEMES), "trending": copy.deepcopy(TRENDING), "pump": []}

    def handler(req):
        if req.url.path.endswith("/coins/markets"):
            if req.url.params.get("category") == "pump-fun":
                return httpx.Response(200, json=data["pump"])
            return httpx.Response(200, json=data["memes"] if req.url.params["page"] == "1" else [])
        return httpx.Response(200, json=data["trending"])
    desk.bots["trends"].client = httpx.AsyncClient(base_url="https://api.coingecko.com/api/v3",
                                                   transport=httpx.MockTransport(handler))
    return data


class CoinbaseLike(SimulatedFeed):
    name = "coinbase"
    listed = frozenset({"BTC-USD", "PEPE-USD", "MOG-USD", "SUI-USD", "PEPE-EUR"})


def test_coin_discovery_works_on_coinbase_prices_too():
    s = Settings(feed="coinbase", symbols=["BTC-USD"])
    d = TradingDesk(s, feed=CoinbaseLike(s.symbols), journal=Journal(":memory:"))
    gecko(d)
    res = run(d.bots["trends"].refresh())
    assert set(res["added"]) == {"PEPE-USD", "MOG-USD", "SUI-USD"} and d.bots["trends"].last_error == ""


def test_only_coins_the_linked_account_can_trade():
    s = Settings(feed="coinbase", symbols=["BTC-USD"])
    uphold = type("Uphold", (), {"supports": lambda self, sym: sym != "MOG-USD", "symbols": []})()
    d = TradingDesk(s, feed=CoinbaseLike(s.symbols), journal=Journal(":memory:"), live_broker=uphold)
    gecko(d)
    assert "MOG-USD" not in run(d.bots["trends"].refresh())["added"]


# ------------------------------------------------------------------------------------- Alpaca
SCREEN = {
    "actives": ["AAPL", "TSLA", "SOXL", "PENNY", "OTCX", "WHOLE", "QUIET", "BRK.B"],
    "gainers": {"PLTR": 9.5, "PENNY": 40.0},
    "snap": {"TSLA": (250.0, 9e7), "SOXL": (30.0, 9e7), "PENNY": (2.5, 9e8), "OTCX": (20.0, 9e6), "WHOLE": (60.0, 3e6),
             "QUIET": (80.0, 1e5), "PLTR": (25.0, 8e7), "AAPL": (190.0, 6e7)},
    "assets": {"TSLA": ("Tesla, Inc. Common Stock", "NASDAQ", True), "PLTR": ("Palantir Technologies", "NASDAQ", True),
               "SOXL": ("Direxion Daily Semiconductor Bull 3X Shares", "ARCA", True),
               "PENNY": ("Penny Corp", "NASDAQ", True), "OTCX": ("Some OTC Co", "OTC", True),
               "WHOLE": ("Whole Shares Only Inc", "NYSE", False), "QUIET": ("Quiet Co", "NYSE", True),
               "AAPL": ("Apple Inc.", "NASDAQ", True)},
}


class AlpacaLike(SimulatedFeed):
    name = "alpaca"

    def __init__(self, symbols):
        super().__init__(symbols)
        self.screen = copy.deepcopy(SCREEN)

    async def most_actives(self, top=50):
        return list(self.screen["actives"])

    async def movers(self, top=20):
        return dict(self.screen["gainers"])

    async def snapshots(self, symbols):
        return {x: {"price": p, "volume": v / p, "dollar_volume": v} for x, (p, v) in self.screen["snap"].items() if x in symbols}

    async def history(self, symbol, granularity, limit=300):
        return [{"ts": 1_700_000_000 + 60 * i, "open": 1, "high": 1.01, "low": 0.99, "close": 100 + i * 0.01, "volume": 5}
                for i in range(60)]


class AlpacaAccount:
    symbols = ()
    label = "Alpaca"

    def __init__(self, feed):
        self.feed = feed

    def supports(self, sym):
        return True

    async def asset(self, sym):
        a = self.feed.screen["assets"].get(sym)
        return {"name": a[0], "exchange": a[1], "fractionable": a[2], "tradable": True, "status": "active"} if a else None


def alpaca_desk(cash=5_000.0):
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", starting_cash=cash, live_venue="alpaca",
                 symbols=["BTC-USD", "SPY", "AAPL"])
    feed = AlpacaLike(s.symbols)
    d = TradingDesk(s, feed=feed, journal=Journal(":memory:"), live_broker=AlpacaAccount(feed))
    d.bots["scout"].bar_seconds = 60
    gecko(d)
    return d


def test_the_alpaca_team_finds_established_busy_stocks_and_skips_risky_ones():
    d = alpaca_desk()
    res = run(d.bots["trends"].refresh())
    assert {"TSLA", "PLTR"} <= set(res["added"])
    for bad, why in (("SOXL", "a 3x leveraged fund"), ("PENNY", "under $10"), ("OTCX", "not a main exchange"),
                     ("WHOLE", "no fractional shares"), ("QUIET", "too little traded"), ("BRK.B", "odd ticker")):
        assert bad not in d.settings.symbols, why
    assert d.settings.symbols.count("AAPL") == 1  # already watched: not added twice
    assert d.bots["scout"].books["TSLA"].count == 60  # price history loaded: tradeable at once
    assert d.bots["trends"].nudge("PLTR", 1) > 0 and d.bots["trends"].nudge("TSLA", 1) == 0  # a big riser today
    assert [x["symbol"] for x in d.state()["trends"]["stocks"]] == ["TSLA", "PLTR"]  # busiest first


def test_its_coins_are_the_ones_alpaca_carries():
    d = alpaca_desk()
    res = run(d.bots["trends"].refresh())
    assert "PEPE-USD" in res["added"] and "MOG-USD" not in res["added"] and "SUI-USD" not in res["added"]


def test_stocks_that_stop_qualifying_are_dropped_unless_held():
    from tradezone.core.portfolio import Position
    d = alpaca_desk()
    t = d.bots["trends"]
    run(t.refresh())
    d.portfolio.positions["PLTR"] = Position(symbol="PLTR", qty=2, avg_price=25, last_price=25)
    d.feed.screen.update(actives=[], gainers={})
    d.feed.screen["snap"]["TSLA"] = (250.0, 1e6)  # hardly traded any more
    d.feed.screen["snap"]["PLTR"] = (25.0, 1e6)
    res = run(t.refresh())
    assert "TSLA" in res["dropped"] and "TSLA" not in d.settings.symbols
    assert "PLTR" in d.settings.symbols  # held: still watched


def test_no_stock_search_under_1000_or_without_the_account_linked():
    small = alpaca_desk(cash=500.0)  # the growth ladder: stocks join from $1,000
    assert not [x for x in run(small.bots["trends"].refresh())["added"] if "-" not in x]
    s = Settings(feed="alpaca", alpaca_key="k", alpaca_secret="s", symbols=["BTC-USD", "SPY"], starting_cash=5_000.0)
    d = TradingDesk(s, feed=AlpacaLike(s.symbols), journal=Journal(":memory:"))  # practice data keys only
    gecko(d)
    run(d.bots["trends"].refresh())
    assert "Alpaca account linked" in d.bots["trends"].summary()["error"]


# --------------------------------------------------------- the Alpaca data API, request by request
def fake_alpaca_data():
    seen = []
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))

    def handler(req):
        p = req.url.path
        seen.append((p, dict(req.url.params)))
        if p == "/v2/stocks/trades/latest":
            return httpx.Response(200, json={"trades": {s: {"p": 100.0, "s": 5} for s in req.url.params["symbols"].split(",")}})
        if p == "/v1beta3/crypto/us/latest/trades":
            return httpx.Response(200, json={"trades": {s: {"p": 60000.0} for s in req.url.params["symbols"].split(",")}})
        if p.startswith("/v2/stocks/") and p.endswith("/bars"):
            return httpx.Response(200, json={"bars": [{"t": now_iso, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]})
        if p == "/v1beta3/crypto/us/bars":
            return httpx.Response(200, json={"bars": {"BTC/USD": [{"t": now_iso, "o": 1, "h": 2, "l": 0.5, "c": 1.6, "v": 3}]}})
        if p == "/v2/stocks/snapshots":
            return httpx.Response(200, json={"TSLA": {"latestTrade": {"p": 250.0}, "dailyBar": {"c": 249.0, "v": 1000},
                                                      "prevDailyBar": {"c": 248.0, "v": 400000}}})
        if p == "/v1beta1/screener/stocks/most-actives":
            return httpx.Response(200, json={"most_actives": [{"symbol": "TSLA", "volume": 1}, {"symbol": "NVDA"}]})
        if p == "/v1beta1/screener/stocks/movers":
            return httpx.Response(200, json={"gainers": [{"symbol": "PLTR", "percent_change": 9.5}], "losers": []})
        if p == "/v1beta1/news":
            return httpx.Response(200, json={"news": [{"headline": "Tesla recalls cars after probe", "created_at": now_iso,
                                                       "url": "u", "source": "benzinga", "symbols": ["TSLA"]},
                                                      {"headline": "Bitcoin ETF sees record inflows", "created_at": now_iso,
                                                       "source": "benzinga", "symbols": ["BTCUSD"]}]})
        return httpx.Response(404)
    return handler, seen


def test_the_alpaca_feed_prices_what_the_scouts_add_and_reads_history_screener_and_news():
    handler, seen = fake_alpaca_data()
    syms = ["BTC-USD", "SPY"]
    f = AlpacaFeed(syms, "k", "s", client=httpx.AsyncClient(base_url="https://data.alpaca.markets",
                                                            transport=httpx.MockTransport(handler)))
    syms.append("TSLA")  # added by the Trend Scout after start-up (the settings' own list)
    assert {t.symbol for t in run(f.poll())} == {"BTC-USD", "SPY", "TSLA"}
    assert run(f.history("TSLA", 60))[0]["close"] == 1.5 and run(f.history("BTC-USD", 60))[0]["close"] == 1.6
    assert run(f.history("TSLA", 300)) is None  # only 1-minute bars
    assert run(f.most_actives()) == ["TSLA", "NVDA"] and run(f.movers()) == {"PLTR": 9.5}
    assert run(f.snapshots(["TSLA"]))["TSLA"] == {"price": 250.0, "volume": 400000.0, "dollar_volume": 1e8}
    news = run(f.news(["TSLA", "BTC-USD"]))
    assert [n["symbols"] for n in news] == [["TSLA"], ["BTCUSD"]]
    assert ("/v1beta1/news", {"symbols": "TSLA,BTCUSD", "limit": "50"}) in seen
    assert run(f.last_prices(["TSLA", "BTC-USD"])) == {"TSLA": 100.0, "BTC-USD": 60000.0}


def test_the_reporter_reads_alpaca_news_for_stocks_and_coins():
    handler, _ = fake_alpaca_data()
    s = Settings(feed="alpaca", alpaca_live_key="k", alpaca_live_secret="s", symbols=["BTC-USD", "TSLA"])
    feed = AlpacaFeed(s.symbols, "k", "s", client=httpx.AsyncClient(base_url="https://data.alpaca.markets",
                                                                    transport=httpx.MockTransport(handler)))
    d = TradingDesk(s, feed=feed, journal=Journal(":memory:"))
    news = d.bots["news"]
    news.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))  # (no RSS here)
    run(news.refresh())
    assert news.coin_score("TSLA") < 0 and news.coin_score("BTC-USD") > 0  # a recall; record inflows
    assert any(h["coins"] == ["TSLA"] for h in news.headlines)
    assert json.dumps(news.summary())  # shown on the dashboard


def test_a_stock_with_an_order_still_filling_or_in_the_hold_stack_is_not_dropped():
    d = alpaca_desk()
    t = d.bots["trends"]
    run(t.refresh())
    d.bots["stacker"].books["paper"]["TSLA"] = {"qty": 0.1, "cost": 25.0, "buys": 1}  # moved into the hold stack
    d.bots["execution"].unbooked["PLTR"] = 2.0  # a fill not booked yet
    d.feed.screen.update(actives=[], gainers={})
    d.feed.screen["snap"].update(TSLA=(250.0, 1e6), PLTR=(25.0, 1e6))
    assert run(t.refresh())["dropped"] == [] and {"TSLA", "PLTR"} <= set(d.settings.symbols)


def test_up_to_100_found_coins_and_history_requests_are_spaced_out(_no_history_pause):
    import asyncio

    from tradezone.config import Settings
    from tradezone.desk import TradingDesk
    from tradezone.journal import Journal
    from tradezone.market.simulated import SimulatedFeed
    s = Settings(feed="sim", symbols=["BTC-USD"])
    assert s.max_discovered == 100
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    asked = []

    async def history(sym, gran, n):
        asked.append(sym)
        return []
    d.feed.history = history
    d.bots["scout"].bar_seconds = 60
    asyncio.run(d.bots["trends"]._load_history(["A-USD", "B-USD", "C-USD"]))
    assert asked == ["A-USD", "B-USD", "C-USD"]
    assert _no_history_pause == [s.history_pause_s] * 2  # a pause between requests, not before the first
