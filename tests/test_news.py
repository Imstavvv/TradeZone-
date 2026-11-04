"""Reporter: world & crypto news makes the team more careful (never less safe)."""

import time

import httpx

from tests.helpers import capture, prime, run
from tradezone.bots.news import coins_in, parse_feed, score_headline
from tradezone.core.models import Side, Signal

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Major exchange hacked, $200M in Solana drained</title><link>https://x/1</link>
  <pubDate>{d}</pubDate></item>
<item><title>SEC approves spot Ethereum ETF; ether rallies</title><link>https://x/2</link><pubDate>{d}</pubDate></item>
<item><title>Fed signals rate hike as inflation surges; war fears grow</title><link>https://x/3</link><pubDate>{d}</pubDate></item>
</channel></rss>"""


def rfc822(ts):
    return time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(ts))


def live(desk):
    desk.settings.feed = "coinbase"  # news only matters for real prices (off for synthetic ones)
    return desk.bots["news"]


def test_headlines_are_scored_and_matched_to_coins():
    assert score_headline("Exchange hacked, funds stolen") < -0.5
    assert score_headline("SEC approves Bitcoin ETF approval, record high") > 0.5
    assert score_headline("Bitcoin trades sideways") == 0.0
    assert coins_in("Solana and Dogecoin rally while BTC waits") == ["BTC", "SOL", "DOGE"]
    items = parse_feed(RSS.format(d=rfc822(time.time())), "example.com")
    assert len(items) == 3 and items[0]["link"] == "https://x/1"
    assert parse_feed("<not xml", "x") == []


def test_reads_feeds_and_fear_and_greed(desk):
    news = live(desk)

    def handler(req):
        if "alternative.me" in str(req.url):
            return httpx.Response(200, json={"data": [{"value": "12", "value_classification": "Extreme Fear"}]})
        if "bbci" in str(req.url):
            return httpx.Response(500)  # one broken feed doesn't stop the rest
        return httpx.Response(200, text=RSS.format(d=rfc822(time.time())))
    news.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    desk.settings.news_feeds = ["https://coindesk.com/rss", "https://feeds.bbci.co.uk/news/world/rss.xml"]
    assert run(news.refresh()) == 3
    assert news.fear_greed["value"] == 12
    assert news.coin_score("SOL-USD") < 0 < news.coin_score("ETH-USD")
    assert news.market_mood() < 0 and news.last_error
    s = desk.state()["news"]
    assert s["headlines"][0]["title"] and s["mood_word"] in ("scary", "nervous")


def test_bad_news_blocks_buys_good_news_blocks_shorts(desk):
    news = live(desk)
    news.add_items(parse_feed(RSS.format(d=rfc822(time.time())), "x"))
    rejected = capture(desk, "signal.rejected")
    prime(desk, "SOL-USD", 100.0)
    desk.settings.symbols.append("SOL-USD")
    desk.bus.publish("signal.proposed", Signal("SOL-USD", Side.BUY, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("SOL-USD").is_flat and "bad news" in rejected[-1]["reason"]
    prime(desk, "ETH-USD", 1000.0)
    desk.settings.symbols.append("ETH-USD")
    desk.bus.publish("signal.proposed", Signal("ETH-USD", Side.SELL, 0.9, "t", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("ETH-USD").is_flat and "good news" in rejected[-1]["reason"]
    # old news no longer blocks
    for h in news.headlines:
        h["ts"] -= 3 * 3600
    desk.bus.publish("signal.proposed", Signal("SOL-USD", Side.BUY, 0.9, "t2", "quant"))
    run(desk.bus.drain())
    assert desk.portfolio.position("SOL-USD").qty > 0


def test_scary_world_news_halves_new_buys(desk):
    news = live(desk)
    assert news.size_mult(1) == 1.0
    news.fear_greed = {"value": 5, "label": "Extreme Fear", "ts": time.time()}
    news.add_items([{"title": f"War: missile strikes, markets crash {i}", "link": "", "ts": time.time(), "source": "x"}
                    for i in range(4)])
    assert news.mood_word() == "scary" and news.size_mult(1) == 0.5 and news.size_mult(-1) == 1.0


def test_news_is_off_for_synthetic_prices_and_grok_sees_headlines(desk):
    news = desk.bots["news"]
    assert not news.active() and news.veto("SOL-USD", 1) is None
    run(news.on_cycle())
    assert news._task is None  # no network calls in practice-on-synthetic mode
    live(desk).add_items(parse_feed(RSS.format(d=rfc822(time.time())), "x"))
    prime(desk, "SOL-USD", 100.0)
    prompt = desk.bots["grok"].build_prompt(Signal("SOL-USD", Side.BUY, 0.9, "t", "quant"))
    assert "drained" in prompt and "market_mood" in prompt
