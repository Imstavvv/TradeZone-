"""Kraken prices (public data, no key) and Kraken's list of tradeable USD pairs.

When Kraken is the real-money account, prices come from Kraken itself, so every coin the bots can
trade there (including meme coins Coinbase doesn't list) has a price, and practice matches real money.

* **Pairs:** ``AssetPairs`` - which coins trade against USD, their lot size, minimum order and status.
  Only pairs whose status is ``online`` (buying *and* selling allowed) are used.
* **Meme coins:** at start-up the feed looks for well-known meme coins Kraken lists against USD and adds
  the ones with enough trading (24h volume) that a position can always be sold again.
* **Prices:** ``Ticker`` for every symbol in one request, polled every couple of seconds (Kraken's
  public limit is about one request per second). Bid, ask and 24h volume are kept for the
  "can it be sold?" check.
* **History:** ``OHLC`` 1-minute candles so indicators are ready at start-up.
"""

from __future__ import annotations

import logging
import time

import httpx

from ..core.models import Tick
from .base import MarketFeed

log = logging.getLogger(__name__)

PUBLIC = "https://api.kraken.com"
ALIASES = {"BTC": "XBT", "DOGE": "XDG"}  # Kraken's names for some coins
OHLC_MINUTES = {60: 1, 300: 5, 900: 15, 1800: 30, 3600: 60, 14400: 240, 86400: 1440}


def ours(base: str) -> str:
    """Kraken's coin name -> ours (XBT -> BTC, XDG -> DOGE)."""
    return next((k for k, v in ALIASES.items() if v == base), base)


def parse_pairs(result: dict) -> dict[str, dict]:
    """``AssetPairs`` result -> {"BTC-USD": {pair, key, base, lot, ordermin, costmin}} for online USD pairs."""
    out: dict[str, dict] = {}
    for key, p in (result or {}).items():
        ws = p.get("wsname", "")
        if not ws.endswith("/USD") or p.get("status", "online") != "online":
            continue  # cancel_only / post_only / reduce_only pairs can't be bought and sold freely
        sym = f"{ours(ws.split('/')[0])}-USD"
        out[sym] = {"pair": p.get("altname") or key, "key": key, "base": p["base"], "alt": ws.split("/")[0],
                    "lot": int(p.get("lot_decimals", 8)), "price_dec": int(p.get("pair_decimals", 8)),
                    "ordermin": float(p.get("ordermin") or 0), "costmin": float(p.get("costmin") or 0)}
    return out


def parse_ticker(result: dict, by_key: dict[str, str]) -> dict[str, dict]:
    """``Ticker`` result -> {symbol: {last, bid, ask, vol_usd}} (24h volume valued at the 24h average price)."""
    out = {}
    for key, t in (result or {}).items():
        sym = by_key.get(key)
        if sym is None:
            continue
        try:
            last, bid, ask = float(t["c"][0]), float(t["b"][0]), float(t["a"][0])
            vol_usd = float(t["v"][1]) * float(t["p"][1])
            vol_today = float(t["v"][0])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        out[sym] = {"last": last, "bid": bid, "ask": ask, "vol_usd": vol_usd, "vol_today": vol_today, "ts": time.time()}
    return out


async def fetch_pairs(client: httpx.AsyncClient) -> dict[str, dict]:
    r = await client.get("/0/public/AssetPairs")
    r.raise_for_status()
    return parse_pairs(r.json().get("result") or {})


async def fetch_quotes(client: httpx.AsyncClient, pairs: dict[str, dict], symbols: list[str]) -> dict[str, dict]:
    want = [pairs[s]["pair"] for s in symbols if s in pairs]
    if not want:
        return {}
    r = await client.get("/0/public/Ticker", params={"pair": ",".join(want)})
    r.raise_for_status()
    body = r.json()
    if body.get("error") and not body.get("result"):
        # Kraken refuses the WHOLE request if one pair is unknown (e.g. just delisted). Find it, drop it,
        # and keep the rest priced - one bad coin must never leave every coin without a price.
        if len(want) == 1:
            raise ValueError("; ".join(body["error"]))
        out: dict[str, dict] = {}
        for s in [x for x in symbols if x in pairs]:
            try:
                out.update(await fetch_quotes(client, pairs, [s]))
            except ValueError as exc:
                log.warning("kraken won't quote %s (%s): dropped", s, exc)
                pairs.pop(s, None)
        return out
    by_key = {}
    for s in symbols:
        if s in pairs:
            by_key[pairs[s]["key"]] = by_key[pairs[s]["pair"]] = s
    return parse_ticker(body.get("result") or {}, by_key)


class KrakenFeed(MarketFeed):
    name = "kraken"
    POLL_EVERY_S = 2.0

    def __init__(self, symbols: list[str], client: httpx.AsyncClient | None = None,
                 meme_candidates: list[str] | None = None, meme_coins: list[str] | None = None,
                 min_meme_volume_usd: float = 1_000_000.0, max_memes: int = 12,
                 small_candidates: list[str] | None = None, max_small: int = 8) -> None:
        self.symbols = symbols  # the same list object as settings.symbols: added / dropped coins show everywhere
        self.client = client or httpx.AsyncClient(base_url=PUBLIC, timeout=8.0, headers={"User-Agent": "TradeZone/1.0"})
        self.meme_candidates = meme_candidates or []
        self.meme_coins = meme_coins if meme_coins is not None else []
        self.min_meme_volume_usd = min_meme_volume_usd
        self.max_memes = max_memes
        self.small_candidates = small_candidates or []
        self.max_small = max_small
        self.pairs: dict[str, dict] = {}
        self.quotes: dict[str, dict] = {}
        self.dropped: list[str] = []
        self.added: list[str] = []
        self.last_error = ""
        self.mode = "poll"
        self._last_poll = 0.0
        self._vol_today: dict[str, float] = {}

    async def start(self) -> None:
        try:
            self.pairs = await fetch_pairs(self.client)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.last_error = f"pairs: {type(exc).__name__}: {exc}"[:200]
            log.warning("kraken pair list unavailable (%s); keeping all symbols", exc)
            return
        listed = [s for s in self.symbols if s in self.pairs]
        if listed:  # drop what Kraken doesn't trade against USD (never all of them)
            self.dropped = [s for s in self.symbols if s not in self.pairs]
            self.symbols[:] = listed
        await self.discover_memes()
        await self.discover_small()

    async def discover_small(self) -> list[str]:
        """Add smaller altcoins Kraken lists against USD that trade enough to be sold again (busiest first)."""
        fresh = [f"{c}-USD" for c in self.small_candidates if f"{c}-USD" in self.pairs and f"{c}-USD" not in self.symbols]
        if not fresh or self.max_small <= 0:
            return []
        try:
            quotes = await fetch_quotes(self.client, self.pairs, fresh)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("kraken small-coin volume check failed: %s", exc)
            return []
        ranked = sorted((s for s in fresh if quotes.get(s, {}).get("vol_usd", 0) >= self.min_meme_volume_usd),
                        key=lambda s: -quotes[s]["vol_usd"])[: self.max_small]
        self.symbols.extend(ranked)
        self.added.extend(ranked)
        self.quotes.update({s: quotes[s] for s in ranked})
        return ranked

    async def discover_memes(self) -> list[str]:
        """Add Kraken-listed meme coins with enough daily trading that a position can be sold again."""
        fresh = [f"{c}-USD" for c in self.meme_candidates if f"{c}-USD" in self.pairs and f"{c}-USD" not in self.symbols]
        if not fresh:
            return []
        try:
            quotes = await fetch_quotes(self.client, self.pairs, fresh)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("kraken meme volume check failed: %s", exc)
            return []
        ranked = sorted((s for s in fresh if quotes.get(s, {}).get("vol_usd", 0) >= self.min_meme_volume_usd),
                        key=lambda s: -quotes[s]["vol_usd"])
        have = sum(1 for s in self.symbols if s.split("-")[0] in self.meme_coins)
        self.added = ranked[:max(0, self.max_memes - have)]
        for s in self.added:
            self.symbols.append(s)
            coin = s.split("-")[0]
            if coin not in self.meme_coins:
                self.meme_coins.append(coin)
        self.quotes.update({s: quotes[s] for s in self.added})
        if self.added:
            log.info("kraken meme coins added: %s", ", ".join(self.added))
        return self.added

    async def poll(self) -> list[Tick]:
        t = time.monotonic()
        if t - self._last_poll < self.POLL_EVERY_S:
            return []
        self._last_poll = t
        if not self.pairs:
            await self.start()
            if not self.pairs:
                return []
        try:
            quotes = await fetch_quotes(self.client, self.pairs, self.symbols)
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"ticker: {type(exc).__name__}: {exc}"[:200]
            log.warning("kraken ticker failed: %s", exc)
            return []
        self.quotes.update(quotes)
        self.last_error = ""
        ticks = []
        for s, q in quotes.items():
            if q["last"] <= 0:
                continue
            today, prev = q.get("vol_today", 0.0), self._vol_today.get(s)
            self._vol_today[s] = today
            traded = today - prev if prev is not None and today >= prev else 0.0  # resets at the UTC day: skip
            ticks.append(Tick(s, q["last"], traded, q["ts"]))
        return ticks

    async def history(self, symbol: str, granularity: int, limit: int = 300) -> list[dict] | None:
        minutes = OHLC_MINUTES.get(granularity)
        p = self.pairs.get(symbol)
        if minutes is None or p is None:
            return None
        try:
            r = await self.client.get("/0/public/OHLC", params={"pair": p["pair"], "interval": minutes})
            r.raise_for_status()
            result = r.json().get("result") or {}
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"history {symbol}: {type(exc).__name__}: {exc}"[:200]
            return None
        rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), [])
        candles = [{"ts": float(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                    "close": float(row[4]), "volume": float(row[6])} for row in rows[:-1]]  # last one is still forming
        return candles[-limit:]

    async def close(self) -> None:
        await self.client.aclose()
