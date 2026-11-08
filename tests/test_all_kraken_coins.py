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
    def handler(req):
        if req.url.path.endswith("AssetPairs"):  # (read live: a coin added to ``coins`` is a new listing)
            pairs = {f"{c}USD": {"altname": f"{c}USD", "wsname": f"{c}/USD", "base": c} for c in coins}
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


def test_hundreds_of_coins_are_priced_in_one_request():
    """Kraken's public limit is about one request a second: 600 coins in batches of 100 every 2 s broke it."""
    coins = {f"C{i}": 5e6 for i in range(250)}
    calls: list = []

    def handler(req):
        calls.append(req.url.params.get("pair", "ALL"))
        names = coins if "pair" not in req.url.params else [p[:-3] for p in req.url.params["pair"].split(",")]
        res = {f"{c}USD": {"c": ["1", "1"], "b": ["0.999", "1", "1"], "a": ["1", "1", "1"], "v": ["1", "5e6"], "p": ["1", "1"]}
               for c in names}
        res["OTHERUSD"] = res[next(iter(res))]  # (pairs nobody watches come back too: ignored)
        return httpx.Response(200, json={"error": [], "result": res})
    client = httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler))
    pairs = {f"C{i}-USD": {"pair": f"C{i}USD", "key": f"C{i}USD"} for i in range(250)}
    quotes = run(fetch_quotes(client, pairs, list(pairs)))
    assert len(quotes) == 250 and calls == ["ALL"]
    small = {k: pairs[k] for k in list(pairs)[:10]}
    calls.clear()
    assert len(run(fetch_quotes(client, small, list(small)))) == 10 and calls != ["ALL"]  # (a few coins: named)
    run(client.aclose())


def test_with_many_coins_trading_starts_after_the_first_40_and_the_rest_load_meanwhile():
    syms = [f"C{i}-USD" for i in range(60)]
    s = Settings(feed="sim", symbols=list(syms), timeframe_history=False)  # (only the 1-minute history here)
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


def test_small_caps_down_to_the_sell_checks_bar_are_traded_and_memes_keep_the_meme_rules():
    coins = {"BTC": 9e9, "SMALL": 400_000.0, "PNUT": 300_000.0, "DUST": 100_000.0}
    symbols, memes = ["BTC-USD"], ["DOGE"]
    s = Settings()
    assert s.min_coin_volume_usd == s.sell_min_volume_usd == 250_000.0
    feed = KrakenFeed(symbols, client=kraken(coins), meme_candidates=["PNUT"], meme_coins=memes, min_meme_volume_usd=1e6,
                      all_coins=True, min_all_volume_usd=s.min_coin_volume_usd)
    run(feed.start())
    assert symbols == ["BTC-USD", "SMALL-USD", "PNUT-USD"]  # (DUST trades too little to sell again)
    assert "PNUT" in memes and "SMALL" not in memes  # the meme coin is still treated as one
    run(feed.client.aclose())


def test_tokens_kraken_lists_after_start_up_join_on_the_next_scan():
    """New DEX-born tokens land on Kraken all the time: the pair list is read again every 30 minutes."""
    coins = {"BTC": 9e9, "AAVE": 5e7}
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=kraken(coins), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    assert symbols == ["BTC-USD", "AAVE-USD"]
    coins.update({"NEWDEX": 4e6, "THINDEX": 5e3})  # (Kraken lists two more; one barely trades)
    joined = run(feed.rescan())
    assert joined == ["NEWDEX-USD"] and symbols[-1] == "NEWDEX-USD" and feed.new_listings == ["NEWDEX-USD"]
    assert run(feed.rescan()) == []  # (nothing new: nothing added)
    run(feed.client.aclose())


def test_the_desk_says_when_new_listings_join():
    from tradezone.market.simulated import SimulatedFeed
    s = Settings(feed="sim", symbols=["BTC-USD"])
    d = TradingDesk(s, feed=SimulatedFeed(s.symbols), journal=Journal(":memory:"))
    d.feed.new_listings = ["NEWDEX-USD"]
    got = []
    d.bus.subscribe("alert", lambda e: got.append(e.payload.message), owner="t")
    run(d.step())
    run(d.bus.drain())
    assert any("Newly listed on Kraken" in m and "NEWDEX" in m for m in got) and d.feed.new_listings == []


def test_up_to_a_thousand_kraken_coins():
    assert Settings().max_kraken_coins == 1000
