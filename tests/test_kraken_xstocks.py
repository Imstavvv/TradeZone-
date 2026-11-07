"""Spot stocks through Kraken Pro (asked for): Kraken's tokenized stocks are their own asset class in its API."""

import urllib.parse

import httpx
import pytest

from tests.helpers import run
from tests.test_kraken import SECRET, FakeKraken
from tradezone.brokers.kraken import KrakenBroker
from tradezone.core.models import Order, OrderStatus, Side
from tradezone.market.kraken import KrakenFeed

STOCK = {"AAPLxUSD": {"altname": "AAPLxUSD", "wsname": "AAPLx/USD", "base": "AAPLx", "lot_decimals": 8, "ordermin": "0.01",
                      "aclass_base": "tokenized_asset"}}


def like_kraken(fk, offered=True, sent=None):
    """Kraken as its API behaves: xStocks listed, priced and ordered only with their asset class."""
    def handler(req):
        path, q = req.url.path, req.url.params
        if path.endswith("AssetPairs") and q.get("aclass_base") == "tokenized_asset":
            return httpx.Response(200, json={"error": [], "result": STOCK} if offered else {"error": ["EGeneral:Invalid arguments"]})
        if path.endswith("Ticker") and "AAPLxUSD" in q.get("pair", ""):
            if q.get("asset_class") != "tokenized_asset":
                return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"]})
            return httpx.Response(200, json={"error": [], "result": {"AAPLxUSD": {
                "c": ["230", "1"], "b": ["229.9", "1", "1"], "a": ["230.1", "1", "1"], "v": ["1", "20000"], "p": ["230", "230"]}}})
        if path.endswith("AddOrder"):
            d = dict(urllib.parse.parse_qsl(req.content.decode()))
            if d.get("pair") == "AAPLxUSD":
                if sent is not None:
                    sent.append(d)
                if d.get("asset_class") != "tokenized_asset":
                    return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"], "result": {}})
                txid = f"S{len(fk.orders) + 1}"
                px = 230.0
                fk.orders[txid] = {"status": "closed", "vol_exec": d["volume"], "cost": f"{float(d['volume']) * px:.8f}",
                                   "fee": f"{float(d['volume']) * px * 0.0026:.8f}"}
                return httpx.Response(200, json={"error": [], "result": {"txid": [txid]}})
        return fk.handler(req)
    return httpx.AsyncClient(base_url="https://api.kraken.com", transport=httpx.MockTransport(handler))


def test_xstocks_are_listed_priced_and_traded_with_their_asset_class():
    fk = FakeKraken(usd=1_000.0)
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=like_kraken(fk), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    assert "AAPLX-USD" in symbols and feed.pairs["AAPLX-USD"]["aclass"] == "tokenized_asset"
    feed._last_poll = 0
    ticks = {t.symbol: t.price for t in run(feed.poll())}
    assert ticks.get("AAPLX-USD") == 230.0 and "BTC-USD" in ticks  # (each asset class asked for on its own)
    run(feed.client.aclose())


def test_an_xstock_order_carries_the_asset_class_and_fills():
    fk, sent = FakeKraken(usd=1_000.0), []
    b = KrakenBroker("KEY", SECRET, client=like_kraken(fk, sent=sent))
    run(b.load_pairs())
    o = Order("AAPLX-USD", Side.BUY, 0.5, "x1")
    fills = run(b.submit(o))
    assert o.status is OrderStatus.FILLED and sum(f.qty for f in fills) == pytest.approx(0.5)
    assert sent and sent[-1]["asset_class"] == "tokenized_asset" and sent[-1]["pair"] == "AAPLxUSD"
    run(b.client.aclose())


def test_where_kraken_doesnt_offer_xstocks_the_coins_trade_as_before():
    fk = FakeKraken(usd=1_000.0)
    symbols = ["BTC-USD"]
    feed = KrakenFeed(symbols, client=like_kraken(fk, offered=False), min_meme_volume_usd=1e6, all_coins=True)
    run(feed.start())
    assert "AAPLX-USD" not in symbols and "BTC-USD" in feed.pairs
    run(feed.client.aclose())
