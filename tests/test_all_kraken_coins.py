"""Everything on Kraken (asked for): every coin it lists against USD that trades enough to sell again is traded."""

import asyncio

import httpx

from tests.helpers import run
from tradezone.config import Settings
from tradezone.desk import TradingDesk
from tradezone.journal import Journal
from tradezone.market.kraken import KrakenFeed, fetch_quotes


def kraken(coins: dict[str, float], calls: list | None = None):
    """A public Kraken listing these coins against USD, each trading this many dollars a day."""
    pairs = {f"{c}USD": {"altname": f"{c}USD", "wsname": f"{c}/USD", "base": c} for c in coins}

    def handler(req):
        if req.url.path.endswith("AssetPairs"):
            return httpx.Response(200, json={"error": [], "result": pairs})
        if calls is not None:
            calls.append(req.url.params.get("pair", ""))
        res = {alt: {"c": ["1", "1"], "b": ["0.999", "1", "1"], "a": ["1", "1", "1"], "v": ["1", str(coins[alt[:-3]])],
                     "p": ["1", "1"]} for alt in req.url.params["pair"].split(",")}
        return httpx.Response(200, json={"error": [], "result": res})
    return httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler))


def test_every_coin_that_trades_enough_is_added_never_stablecoins_or_money():
    coins = {"BTC": 9e9, "AAVE": 5e7, "KAITO": 3e6, "TINY": 2e4, "USDC": 9e9, "USDT": 9e9, "EUR": 9e9, "GBP": 9e9}
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=kraken(coins), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    assert symbols == ["BTC-USD", "AAVE-USD", "KAITO-USD"]  # busiest first; TINY too thin; no stablecoins or money
    run(feed.client.aclose())


def test_off_it_adds_nothing_and_it_is_on_by_default():
    assert Settings().kraken_all_coins
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=kraken({"BTC": 9e9, "AAVE": 5e7}), min_meme_volume_usd=1e6, all_coins=False)
    run(feed.start())
    assert symbols == ["BTC-USD"]
    run(feed.client.aclose())


def test_hundreds_of_coins_are_priced_in_batches():
    coins = {f"C{i}": 5e6 for i in range(250)}
    calls: list = []
    client = kraken(coins, calls)
    pairs = {f"C{i}-USD": {"pair": f"C{i}USD", "key": f"C{i}USD"} for i in range(250)}
    quotes = run(fetch_quotes(client, pairs, list(pairs)))
    assert len(quotes) == 250 and len(calls) == 3 and max(len(c.split(",")) for c in calls) <= 100
    run(client.aclose())


def test_with_many_coins_trading_starts_after_the_first_40_and_the_rest_load_meanwhile():
    syms = [f"C{i}-USD" for i in range(60)]
    s = Settings(feed="sim", symbols=list(syms))
    asked = []

    class Feed:
        name = "test"

        async def history(self, symbol, granularity, limit=300):
            asked.append(symbol)
            await asyncio.sleep(0)
            return [{"ts": 1_700_000_000 + 60 * k, "open": 1, "high": 1.01, "low": 0.99, "close": 1, "volume": 10}
                    for k in range(5)]

        async def close(self):
            pass
    d = TradingDesk(s, feed=Feed(), journal=Journal(":memory:"))
    d.bots["scout"].bar_seconds = 60.0  # (one-minute bars, as with Kraken's prices)

    async def go():
        d.running = True
        await d.backfill()
        first = len(asked)
        await d._backfill_rest
        return first
    first = run(go())
    assert first < 60 and set(syms[:40]) <= set(asked[:first])  # trading started before all 60 had loaded...
    assert sorted(asked) == sorted(syms) and [x for x in asked if x in syms[40:]] == syms[40:]  # ...the other 20 after


def test_krakens_tokenized_stocks_are_traded_with_one_spelling():
    coins = {"BTC": 9e9, "AAPLx": 4e6, "TSLAx": 2e6, "NVDAx": 1e4}
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=kraken(coins), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    assert symbols == ["BTC-USD", "AAPLX-USD", "TSLAX-USD"]  # NVDAx trades too little to be sold again
    assert feed.pairs["AAPLX-USD"]["pair"] == "AAPLxUSD"  # (orders still use Kraken's own name)
    run(feed.client.aclose())
