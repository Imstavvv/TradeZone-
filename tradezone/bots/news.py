"""Reporter - reads world and crypto news so the team knows what is moving the market.

Every few minutes it reads public news feeds (crypto news, world news, business news - plain RSS,
no account needed) and the crypto Fear & Greed index. Each headline is scored with a transparent
word list (e.g. "hack", "lawsuit", "rate hike" negative; "ETF approval", "rate cut", "record high"
positive) and matched to the coins it mentions. Headlines without a coin feed a "market mood".

How the team uses it - always inside the hard safety limits:

* **Bad news on a coin** (a hack, a delisting, a lawsuit in the last 2 hours): no new buys of it.
* **Conviction nudge:** a trade with the news gets up to +0.1 conviction, against it up to -0.1.
* **Scary world news** (very negative market mood): new trades are sized at half.
* **Grok** (if connected) sees the recent headlines when it reviews a trade.

News can only make the team more careful or slightly more confident; it never moves a stop,
never opens a trade by itself and never overrides the daily-loss or drawdown limits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from email.utils import parsedate_to_datetime

import httpx

from ..core.models import now
from .base import Bot

log = logging.getLogger(__name__)

DEFAULT_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]
FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"

# word -> weight; matched as whole words / phrases, case-insensitive
POSITIVE = {
    "etf approval": 1.0, "approves": 0.6, "approved": 0.6, "inflows": 0.5, "record high": 0.8, "all-time high": 0.8,
    "rally": 0.6, "surge": 0.6, "soars": 0.7, "jumps": 0.4, "gains": 0.3, "bullish": 0.5, "adoption": 0.5,
    "partnership": 0.4, "rate cut": 0.7, "cuts rates": 0.7, "upgrade": 0.4, "listing": 0.4, "lists": 0.3,
    "rebound": 0.4, "recovery": 0.3, "beats expectations": 0.5, "stimulus": 0.4, "ceasefire": 0.5, "deal": 0.2,
    # company news (stocks)
    "beats estimates": 0.5, "tops estimates": 0.5, "raises guidance": 0.6, "record revenue": 0.6, "buyback": 0.4,
    "upgraded": 0.4, "price target raised": 0.4,
}
NEGATIVE = {
    "hack": 1.0, "hacked": 1.0, "exploit": 0.9, "stolen": 0.9, "drained": 0.9, "lawsuit": 0.7, "sues": 0.7,
    "charged": 0.6, "fraud": 0.9, "ban": 0.7, "bans": 0.7, "crackdown": 0.7, "delist": 1.0, "delisting": 1.0,
    "bankruptcy": 1.0, "insolvent": 1.0, "crash": 0.8, "plunge": 0.7, "plunges": 0.7, "tumbles": 0.6,
    "sell-off": 0.6, "selloff": 0.6, "liquidations": 0.5, "bearish": 0.5, "outage": 0.6, "halts": 0.6,
    "rate hike": 0.7, "raises rates": 0.7, "hotter-than-expected": 0.6, "inflation surges": 0.6, "recession": 0.6,
    "war": 0.6, "invasion": 0.8, "missile": 0.6, "sanctions": 0.5, "tariff": 0.4, "tariffs": 0.4, "default": 0.5,
    # company news (stocks)
    "recall": 0.6, "recalls": 0.6, "probe": 0.5, "investigation": 0.5, "misses estimates": 0.5, "cuts guidance": 0.6,
    "lowers guidance": 0.6, "downgrade": 0.4, "downgraded": 0.4, "layoffs": 0.3, "price target cut": 0.4,
}
COINS = {
    "BTC": r"bitcoin|btc", "ETH": r"ethereum|ether\b|eth", "SOL": r"solana|sol\b", "XRP": r"xrp|ripple",
    "AVAX": r"avalanche|avax", "LINK": r"chainlink|link\b", "DOGE": r"dogecoin|doge", "SHIB": r"shiba|shib",
    "PEPE": r"pepe", "BONK": r"bonk", "WIF": r"dogwifhat|\bwif\b", "FLOKI": r"floki", "ADA": r"cardano|ada\b",
    "LTC": r"litecoin|ltc", "DOT": r"polkadot|dot\b",
}
CRYPTO_WORDS = re.compile(r"crypto|bitcoin|ethereum|stablecoin|token|blockchain|defi|exchange", re.IGNORECASE)


def _words(table: dict[str, float]) -> list[tuple[re.Pattern, float]]:
    return [(re.compile(rf"(?<![\w-]){re.escape(k)}(?![\w-])", re.IGNORECASE), w) for k, w in table.items()]


_POS, _NEG = _words(POSITIVE), _words(NEGATIVE)
_COIN_RE = {c: re.compile(rf"(?<![\w$]){p}", re.IGNORECASE) for c, p in COINS.items()}


def score_headline(text: str) -> float:
    """-1 (very bad for prices) .. +1 (very good). Transparent word list, no black box."""
    pos = sum(w for rx, w in _POS if rx.search(text))
    neg = sum(w for rx, w in _NEG if rx.search(text))
    if pos == neg == 0:
        return 0.0
    return max(-1.0, min(1.0, pos - neg))


def coins_in(text: str) -> list[str]:
    return [c for c, rx in _COIN_RE.items() if rx.search(text)]


def parse_feed(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 or Atom -> [{title, link, ts, source}]. Never raises on odd feeds."""
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in list(root.iter("item")) + list(root.iter("{http://www.w3.org/2005/Atom}entry")):
        title = (item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or "").strip()
        if not title:
            continue
        link = item.findtext("link") or ""
        atom_link = item.find("{http://www.w3.org/2005/Atom}link")
        if not link and atom_link is not None:
            link = atom_link.get("href", "")
        when = item.findtext("pubDate") or item.findtext("{http://www.w3.org/2005/Atom}updated") or ""
        try:
            ts = parsedate_to_datetime(when).timestamp() if when and "," in when else now()
        except (TypeError, ValueError):
            ts = now()
        out.append({"title": re.sub(r"\s+", " ", title)[:240], "link": link.strip()[:500], "ts": ts, "source": source})
    return out[:40]


class NewsBot(Bot):
    name = "news"
    title = "Reporter"
    role = "World & Market News"
    mission = "🧠 Learn the markets - know what the world is saying before the team trades"
    description = (
        "Reads crypto, world and business news plus the Fear & Greed index every few minutes, scores "
        "each headline and tells the team: no buying a coin hit by bad news, smaller trades when the "
        "world looks scary, and a small conviction nudge with or against the news."
    )

    FETCH_EVERY_S = 300
    MAX_BYTES = 2_000_000
    BREAKING_S = 2 * 3600  # bad news this recent blocks new trades the other way
    DECAY_S = 6 * 3600

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.headlines: deque[dict] = deque(maxlen=200)
        self._seen: set[str] = set()
        self.fear_greed: dict | None = None
        self._last_fetch = 0.0
        self._task: asyncio.Task | None = None
        self.client: httpx.AsyncClient | None = None
        self.last_error = ""

    # ------------------------------------------------------------- fetching
    def active(self) -> bool:
        s = self.settings
        return s.news and s.feed != "sim"  # synthetic prices have nothing to do with real news

    async def on_cycle(self) -> None:
        if not self.active() or (self._task and not self._task.done()):
            return
        if now() - self._last_fetch >= self.FETCH_EVERY_S:
            self._last_fetch = now()
            self._task = asyncio.get_running_loop().create_task(self.refresh())

    async def _get(self, url: str) -> str | None:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                            headers={"User-Agent": "TradeZone/1.0 (news reader)"})
        try:
            r = await self.client.get(url)
            r.raise_for_status()
            return r.text[: self.MAX_BYTES]
        except Exception as exc:  # one bad feed must not stop the others
            self.last_error = f"{url.split('/')[2]}: {type(exc).__name__}"
            return None

    async def refresh(self) -> int:
        """Read every feed once; returns how many new headlines arrived."""
        feeds = self.settings.news_feeds or DEFAULT_FEEDS
        texts = await asyncio.gather(*(self._get(u) for u in feeds))
        new = 0
        for url, text in zip(feeds, texts, strict=True):
            if text:
                new += self.add_items(parse_feed(text, url.split("/")[2].replace("www.", "")))
        feed = self.desk.feed
        if self.settings.news_alpaca and hasattr(feed, "news"):  # headlines tagged with the stocks/coins they're about
            try:
                new += self.add_items(await feed.news(list(self.settings.symbols)))
            except Exception as exc:  # the RSS news still counts
                self.last_error = f"alpaca news: {type(exc).__name__}"
        fg = await self._get(FEAR_GREED_URL)
        if fg:
            try:
                d = json.loads(fg)["data"][0]
                self.fear_greed = {"value": int(d["value"]), "label": d["value_classification"], "ts": now()}
            except (KeyError, IndexError, ValueError, TypeError):
                pass
        self.metrics.update({"headlines": len(self.headlines), "mood": round(self.market_mood(), 2)})
        if new:
            self.act(f"read {new} new headline{'s' if new > 1 else ''} - market mood {self.mood_word()}")
        return new

    def add_items(self, items: list[dict]) -> int:
        new = 0
        for it in sorted(items, key=lambda x: x["ts"]):
            key = it["title"].lower()
            if key in self._seen or now() - it["ts"] > 24 * 3600:
                continue
            self._seen.add(key)
            text = it["title"]
            score = score_headline(text)
            coins = list(dict.fromkeys([*coins_in(text), *self._tagged(it), *self._watched_in(text)]))
            crypto = bool(coins) or bool(CRYPTO_WORDS.search(text))
            self.headlines.append({**it, "score": score, "coins": coins, "crypto": crypto})
            new += 1
            if score <= -0.7 and coins:
                self.act(f"BAD NEWS for {', '.join(coins)}: {text[:90]}", "warning")
        if len(self._seen) > 5000:
            self._seen = {h["title"].lower() for h in self.headlines}
        return new

    def _tagged(self, it: dict) -> list[str]:
        """Tickers a news service tagged the headline with (BTCUSD -> BTC; stocks as they are), if watched."""
        watched = {x.upper().replace("/", "-").split("-")[0] for x in self.settings.symbols}
        out = []
        for t in it.get("symbols") or []:
            t = str(t).upper()
            coin = t[:-3] if t.endswith("USD") and t[:-3] in watched and t not in watched else t
            if coin in watched:
                out.append(coin)
        return out

    def _watched_in(self, text: str) -> list[str]:
        """Newly found coins (not in the built-in list) named in a headline: $TICKER, or the coin's full name."""
        trends = self.desk.bots.get("trends")
        names = {}
        for src in (getattr(trends, "qualified", {}), getattr(trends, "trending", {})):
            for sym, info in src.items():
                if "-" in sym:
                    names[sym.split("-")[0]] = str((info or {}).get("name") or "")
        out = []
        for coin, name in names.items():
            if coin in COINS:
                continue
            if re.search(rf"\${re.escape(coin)}\b", text, re.IGNORECASE) or \
                    (len(name) >= 4 and re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text)):
                out.append(coin)
        return out

    # ------------------------------------------------------------ the read-out
    def _weight(self, h: dict) -> float:
        return max(0.0, 1 - (now() - h["ts"]) / self.DECAY_S)

    def coin_score(self, symbol: str, within_s: float | None = None) -> float:
        """Recent news for one coin, -1..+1 (newer headlines count more)."""
        coin = symbol.upper().replace("/", "-").split("-")[0]
        total = wsum = 0.0
        for h in self.headlines:
            if coin not in h["coins"] or (within_s and now() - h["ts"] > within_s):
                continue
            w = self._weight(h)
            total += w * h["score"]
            wsum += w
        return max(-1.0, min(1.0, total / max(wsum, 1.0))) if wsum else 0.0

    def market_mood(self) -> float:
        """World + crypto news without a specific coin, blended with Fear & Greed: -1..+1."""
        total = wsum = 0.0
        for h in self.headlines:
            if h["coins"] or h["score"] == 0:
                continue
            w = self._weight(h) * (1.0 if h["crypto"] else 0.6)
            total += w * h["score"]
            wsum += w
        mood = total / max(wsum, 2.0) if wsum else 0.0
        if self.fear_greed:  # 0 = extreme fear, 100 = extreme greed
            mood = 0.7 * mood + 0.3 * (self.fear_greed["value"] - 50) / 50
        return max(-1.0, min(1.0, mood))

    def mood_word(self) -> str:
        m = self.market_mood()
        return "scary" if m <= -0.5 else "nervous" if m < -0.15 else "upbeat" if m >= 0.3 else "calm"

    # --------------------------------------------------- what the team asks for
    def veto(self, symbol: str, direction: int) -> str | None:
        """A reason to skip a NEW trade this way because of breaking news, or None."""
        if not self.enabled or not self.active():
            return None
        recent = self.coin_score(symbol, self.BREAKING_S)
        if direction > 0 and recent <= -0.5:
            return f"bad news on {symbol} in the last 2 hours"
        if direction < 0 and recent >= 0.5:
            return f"good news on {symbol} in the last 2 hours"
        return None

    def conviction_nudge(self, symbol: str, direction: int) -> float:
        if not self.enabled or not self.active():
            return 0.0
        return round(0.1 * self.coin_score(symbol) * direction, 3)

    def size_mult(self, direction: int) -> float:
        if not self.enabled or not self.active():
            return 1.0
        return 0.5 if self.market_mood() <= -0.5 and direction > 0 else 1.0

    def context_for(self, symbol: str, n: int = 6) -> list[str]:
        """Recent headlines about this coin (or the market) for Grok."""
        coin = symbol.upper().split("-")[0]
        rel = [h for h in self.headlines if coin in h["coins"]] or [h for h in self.headlines if h["crypto"]]
        return [f"{h['title']} ({h['source']})" for h in list(rel)[-n:]]

    def summary(self) -> dict:
        items = sorted(self.headlines, key=lambda h: -h["ts"])[:12]
        return {"active": self.active(), "mood": round(self.market_mood(), 2), "mood_word": self.mood_word(),
                "fear_greed": self.fear_greed, "error": self.last_error,
                "updated": self._last_fetch or None,
                "headlines": [{k: h[k] for k in ("title", "link", "ts", "source", "score", "coins")} for h in items]}

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.client is not None:
            await self.client.aclose()
