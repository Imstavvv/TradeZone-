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

import asyncio
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


TOKENIZED = "tokenized_asset"  # Kraken's asset class for xStocks (tokenized stocks: AAPLx, TSLAx, ...)


def parse_pairs(result: dict, aclass: str = "currency") -> dict[str, dict]:
    """``AssetPairs`` result -> {"BTC-USD": {pair, key, base, lot, ordermin, costmin}} for online USD pairs."""
    out: dict[str, dict] = {}
    for key, p in (result or {}).items():
        ws = p.get("wsname", "")
        if not ws.endswith("/USD") or p.get("status", "online") != "online":
            continue  # cancel_only / post_only / reduce_only pairs can't be bought and sold freely
        sym = f"{ours(ws.split('/')[0])}-USD".upper()  # (Kraken's tokenized stocks are written AAPLx: one spelling everywhere)
        out[sym] = {"pair": p.get("altname") or key, "key": key, "base": p["base"], "alt": ws.split("/")[0],
                    "lot": int(p.get("lot_decimals", 8)), "price_dec": int(p.get("pair_decimals", 8)),
                    "ordermin": float(p.get("ordermin") or 0), "costmin": float(p.get("costmin") or 0),
                    "aclass": p.get("aclass_base") or aclass}
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


async def fetch_pairs(client: httpx.AsyncClient, stocks: bool = True) -> dict[str, dict]:
    """Kraken's USD pairs: the coins, and (where Kraken offers them to the account's region) its tokenized stocks,
    which its API only lists when asked for that asset class."""
    r = await client.get("/0/public/AssetPairs")
    r.raise_for_status()
    out = parse_pairs(r.json().get("result") or {})
    if stocks:
        try:
            rs = await client.get("/0/public/AssetPairs", params={"aclass_base": TOKENIZED})
            rs.raise_for_status()
            body = rs.json()
            for sym, p in parse_pairs(body.get("result") or {}, TOKENIZED).items():
                out.setdefault(sym, {**p, "aclass": TOKENIZED})  # (a coin of the same name stays the coin)
        except (httpx.HTTPError, ValueError, KeyError) as exc:  # (not offered here, or Kraken busy: coins only)
            log.info("kraken tokenized stocks unavailable: %s", exc)
    return out


def aclass_params(p: dict) -> dict:
    """The extra field Kraken needs on a request about a tokenized stock (none for a coin)."""
    return {"asset_class": TOKENIZED} if p.get("aclass") == TOKENIZED else {}


QUOTE_BATCH = 100  # pairs per Ticker request (hundreds in one URL can be refused)


async def fetch_quotes(client: httpx.AsyncClient, pairs: dict[str, dict], symbols: list[str]) -> dict[str, dict]:
    want = [pairs[s]["pair"] for s in symbols if s in pairs]
    if not want:
        return {}
    listed = [s for s in symbols if s in pairs]
    classes = {pairs[s].get("aclass", "currency") for s in listed}
    if len(classes) > 1:  # coins and tokenized stocks are asked for separately (Kraken's asset_class field)
        out: dict[str, dict] = {}
        for c in sorted(classes):
            out.update(await fetch_quotes(client, pairs, [s for s in listed if pairs[s].get("aclass", "currency") == c]))
        return out
    if len(want) > QUOTE_BATCH:
        if pairs[listed[0]].get("aclass", "currency") == "currency":
            # every coin on Kraken: ONE request for all of them (Ticker with no pair returns every pair). Batches
            # of 100 meant 6 requests every 2 s for 600 coins - three times Kraken's ~1 a second public limit.
            r = await client.get("/0/public/Ticker")
            r.raise_for_status()
            body = r.json()
            if not body.get("error") or body.get("result"):
                keys = {}
                for s in listed:
                    keys[pairs[s]["key"]] = keys[pairs[s]["pair"]] = s
                return parse_ticker(body.get("result") or {}, keys)
        out = {}
        for i in range(0, len(listed), QUOTE_BATCH):  # (tokenized stocks, or if the all-pairs answer failed)
            out.update(await fetch_quotes(client, pairs, listed[i:i + QUOTE_BATCH]))
        return out
    r = await client.get("/0/public/Ticker", params={"pair": ",".join(want), **aclass_params(pairs[listed[0]])})
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
    HISTORY_GAP_S = 1.05  # between price-history requests

    async def last_prices(self, symbols: list[str]) -> dict[str, float]:
        """Kraken's last price for each symbol it lists (to check a discovered coin is really the same coin)."""
        if not self.pairs:
            return {}
        quotes = await fetch_quotes(self.client, self.pairs, [s for s in symbols if s in self.pairs])
        return {s: q["last"] for s, q in quotes.items() if q.get("last")}

    def __init__(self, symbols: list[str], client: httpx.AsyncClient | None = None,
                 meme_candidates: list[str] | None = None, meme_coins: list[str] | None = None,
                 min_meme_volume_usd: float = 1_000_000.0, max_memes: int = 12,
                 small_candidates: list[str] | None = None, max_small: int = 8,
                 all_coins: bool = False, max_all: int = 400, min_all_volume_usd: float | None = None) -> None:
        self.all_coins, self.max_all = all_coins, max_all
        self.min_all_volume_usd = min_all_volume_usd
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
        self._history_lock = asyncio.Lock()
        self._history_at = 0.0
        self._vol_today: dict[str, float] = {}
        self.waiting: dict[str, float] = {}  # pairs listed since start-up, not yet trading enough -> since when
        self.new_listings: list[str] = []  # coins Kraken listed after start-up that joined the watch list

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
        await self.discover_all()

    async def rescan(self) -> list[str]:
        """Kraken's pair list again: tokens it listed since start-up (new DEX-born tokens land on Kraken often)
        join the watch list - under the same rules as every coin (enough daily trading to be sold again)."""
        try:
            pairs = await fetch_pairs(self.client)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.last_error = f"pairs: {type(exc).__name__}: {exc}"[:200]
            return []
        t = time.monotonic()
        for p in pairs:
            if p not in self.pairs:
                self.waiting[p] = t  # (new on Kraken: checked again each scan for a day, until it trades enough)
        self.pairs.update(pairs)
        for p in [p for p, at in self.waiting.items() if p in self.symbols or t - at > 86_400]:
            self.waiting.pop(p, None)
        if not self.waiting:
            return []
        before = set(self.symbols)
        await self.discover_memes()
        await self.discover_small()
        await self.discover_all()
        joined = [x for x in self.symbols if x not in before]
        self.new_listings.extend(joined)
        for x in joined:
            self.waiting.pop(x, None)
        if joined:
            log.info("kraken: %d newly listed coins joined: %s", len(joined), ", ".join(joined[:10]))
        return joined

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

    #: not crypto to trade: stablecoins and money (they don't move like coins), and tokens tied to them
    NOT_COINS = frozenset({"USDC", "USDT", "PYUSD", "USDG", "DAI", "USDS", "TUSD", "USDE", "FDUSD", "RLUSD", "USD1", "USDQ", "USDR",
                 "EURC", "EURQ", "EURR", "EUROP", "EUR", "GBP", "AUD", "CAD", "CHF", "JPY", "AED", "ZUSD", "USD"})

    async def discover_all(self) -> list[str]:
        """Every coin Kraken lists against USD that trades enough to be sold again (busiest first)."""
        if not self.all_coins:
            return []
        fresh = [s for s in self.pairs if s not in self.symbols and s.split("-")[0] not in self.NOT_COINS]
        if not fresh:
            return []
        try:
            quotes = await fetch_quotes(self.client, self.pairs, fresh)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("kraken all-coins volume check failed: %s", exc)
            return []
        bar = self.min_all_volume_usd if self.min_all_volume_usd is not None else self.min_meme_volume_usd
        ranked = sorted((s for s in fresh if quotes.get(s, {}).get("vol_usd", 0) >= bar),
                        key=lambda s: -quotes[s]["vol_usd"])[: max(0, self.max_all - len(self.symbols))]
        self.symbols.extend(ranked)
        self.added.extend(ranked)
        for s in ranked:  # a known meme coin under the meme bar still gets the meme rules (smaller, capped)
            coin = s.split("-")[0]
            if coin in {c.upper() for c in self.meme_candidates} and coin not in self.meme_coins:
                self.meme_coins.append(coin)
        self.quotes.update({s: quotes[s] for s in ranked})
        if ranked:
            log.info("kraken: trading every coin that trades enough - %d added", len(ranked))
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
        result: dict = {}
        for attempt in range(4):
            async with self._history_lock:  # one at a time, ~a second apart (Kraken's public limit)
                wait = self.HISTORY_GAP_S - (time.monotonic() - self._history_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    r = await self.client.get("/0/public/OHLC", params={"pair": p["pair"], "interval": minutes,
                                                                        **aclass_params(p)})
                    r.raise_for_status()
                    body = r.json()
                except (httpx.HTTPError, ValueError) as exc:
                    self.last_error = f"history {symbol}: {type(exc).__name__}: {exc}"[:200]
                    return None
                finally:
                    self._history_at = time.monotonic()
            errors = body.get("error") or []
            if not errors:
                result = body.get("result") or {}
                break
            if any("Too many requests" in e or "Rate limit" in e for e in errors) and attempt < 3:
                await asyncio.sleep(1.5 * (attempt + 1))  # (Kraken said slow down: wait a little longer each time)
                continue
            self.last_error = f"history {symbol}: {'; '.join(errors)}"[:200]  # (said, not silently "no history")
            return None
        rows = next((v for k, v in result.items() if k != "last" and isinstance(v, list)), [])
        candles = [{"ts": float(row[0]), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]),
                    "close": float(row[4]), "volume": float(row[6])} for row in rows[:-1]]  # last one is still forming
        return candles[-limit:]

    async def trades(self, symbol: str, count: int = 500) -> list[dict] | None:
        """The exchange's most recent trades for one coin (the tape): price, size, time and which side took it
        (``buy`` = a buyer paid the ask). Shares the history requests' pacing (Kraken's public limit)."""
        p = self.pairs.get(symbol)
        if p is None:
            return None
        async with self._history_lock:
            wait = self.HISTORY_GAP_S - (time.monotonic() - self._history_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                r = await self.client.get("/0/public/Trades", params={"pair": p["pair"], "count": count,
                                                                       **aclass_params(p)})
                r.raise_for_status()
                body = r.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.last_error = f"trades {symbol}: {type(exc).__name__}: {exc}"[:200]
                return None
            finally:
                self._history_at = time.monotonic()
        if body.get("error"):
            self.last_error = f"trades {symbol}: {'; '.join(body['error'])}"[:200]
            return None
        rows = next((v for k, v in (body.get("result") or {}).items() if k != "last" and isinstance(v, list)), [])
        out = []
        for row in rows:
            try:
                out.append({"price": float(row[0]), "qty": float(row[1]), "ts": float(row[2]),
                            "side": "buy" if row[3] == "b" else "sell"})
            except (IndexError, TypeError, ValueError):
                continue
        return out

    async def close(self) -> None:
        await self.client.aclose()
