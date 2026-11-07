"""Prices for US stocks and crypto from Alpaca's data API - and what the bots use to find new ones.

* latest trades for what the bots watch (the SAME list as the settings: coins and stocks the scouts add are
  priced from the next poll on);
* recent 1-minute bars, so a newly found stock or coin can be traded at once instead of warming up for 30 min;
* the stock screener (the day's most-traded stocks and biggest movers) and snapshots (price, day volume);
* news, tagged with the tickers each headline is about.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from ..core.models import Tick, now
from .base import MarketFeed

log = logging.getLogger(__name__)


def _mid(q: dict | None) -> float | None:
    """The middle of a quote's bid and ask (None if either side is missing or crossed)."""
    try:
        bid, ask = float(q["bp"]), float(q["ap"])  # type: ignore[index]
    except (TypeError, KeyError, ValueError):
        return None
    return (bid + ask) / 2 if 0 < bid <= ask else None


def _stock(s: str) -> bool:
    return "-" not in s and "/" not in s


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


class AlpacaFeed(MarketFeed):
    name = "alpaca"
    BASE = "https://data.alpaca.markets"

    def __init__(
        self, symbols: list[str], key: str, secret: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.symbols = symbols  # the settings' own list (not a copy)
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.client = client or httpx.AsyncClient(base_url=self.BASE, headers=headers, timeout=5.0)
        self.last_error = ""
        # what was already counted, so a quiet coin's last trade isn't counted again every second
        self._seen_trade: dict[str, str] = {}  # stock -> timestamp of the trade already counted
        self._bar_vol: dict[str, tuple[str, float]] = {}  # crypto -> (minute bar, its volume already counted)

    @property
    def stocks(self) -> list[str]:
        return [s for s in self.symbols if _stock(s)]

    @property
    def crypto(self) -> dict[str, str]:
        return {s.replace("-", "/"): s for s in self.symbols if not _stock(s)}

    async def poll(self) -> list[Tick]:
        ticks: list[Tick] = []
        stocks, crypto = self.stocks, self.crypto
        try:
            if stocks:
                r = await self.client.get("/v2/stocks/trades/latest", params={"symbols": ",".join(stocks)})
                r.raise_for_status()
                for sym, t in r.json().get("trades", {}).items():
                    new = t.get("t") is None or self._seen_trade.get(sym) != t.get("t")
                    if t.get("t") is not None:
                        self._seen_trade[sym] = t["t"]
                    ticks.append(Tick(sym, float(t["p"]), float(t.get("s", 0)) if new else 0.0, now()))
            if crypto:
                ticks += await self._crypto_ticks(crypto)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("alpaca data poll failed: %s", exc)
        else:
            self.last_error = ""
        return ticks

    async def _crypto_ticks(self, crypto: dict[str, str]) -> list[Tick]:
        """Alpaca's crypto venue is thin: a coin can go minutes between trades, and its "latest trade" is then
        the same old one every second - a frozen price (nothing for the indicators to see, so no buy ideas) and
        its size counted again every second. So the price is the middle of the current bid / ask (quotes move
        all the time), and the volume is what the current 1-minute bar gained since the last look."""
        pairs = ",".join(crypto)
        q = await self.client.get("/v1beta3/crypto/us/latest/quotes", params={"symbols": pairs})
        q.raise_for_status()
        quotes = q.json().get("quotes", {}) or {}
        try:
            b = await self.client.get("/v1beta3/crypto/us/latest/bars", params={"symbols": pairs})
            b.raise_for_status()
            bars = b.json().get("bars", {}) or {}
        except Exception as exc:  # volume is a nice-to-have; prices are not
            log.warning("alpaca crypto bars failed: %s", exc)
            bars = {}
        trades: dict = {}
        missing = [p for p in crypto if not _mid(quotes.get(p))]
        if missing:  # (no usable quote for some: their last trade, as before)
            t = await self.client.get("/v1beta3/crypto/us/latest/trades", params={"symbols": ",".join(missing)})
            t.raise_for_status()
            trades = t.json().get("trades", {}) or {}
        out = []
        for pair, sym in crypto.items():
            price = _mid(quotes.get(pair)) or (float(trades[pair]["p"]) if pair in trades else None)
            if not price:
                continue
            vol = 0.0
            bar = bars.get(pair)
            if bar and bar.get("t") is not None:
                seen_t, seen_v = self._bar_vol.get(pair, (None, 0.0))
                v = float(bar.get("v") or 0.0)
                vol = max(0.0, v - seen_v) if seen_t == bar["t"] else v  # (a new minute: all of it is new)
                self._bar_vol[pair] = (bar["t"], v)
            out.append(Tick(sym, price, vol, now()))
        return out

    async def history(self, symbol: str, granularity: int, limit: int = 300) -> list[dict] | None:
        """Recent completed 1-minute bars, oldest first (dicts with ts, low, high, open, close, volume)."""
        if granularity != 60:
            return None
        try:
            if _stock(symbol):
                r = await self.client.get(f"/v2/stocks/{symbol}/bars",
                                          params={"timeframe": "1Min", "limit": limit, "sort": "desc"})
                r.raise_for_status()
                rows = r.json().get("bars") or []
            else:
                pair = symbol.replace("-", "/")
                r = await self.client.get("/v1beta3/crypto/us/bars",
                                          params={"symbols": pair, "timeframe": "1Min", "limit": limit, "sort": "desc"})
                r.raise_for_status()
                rows = (r.json().get("bars") or {}).get(pair) or []
            candles = [{"ts": _ts(b["t"]), "open": float(b["o"]), "high": float(b["h"]), "low": float(b["l"]),
                        "close": float(b["c"]), "volume": float(b.get("v") or 0)} for b in rows]
        except Exception as exc:
            log.warning("alpaca bars %s failed: %s", symbol, exc)
            return None
        cutoff = now() - granularity  # drop the still-forming bar
        candles = sorted((c for c in candles if c["ts"] <= cutoff), key=lambda c: c["ts"])
        return candles[-limit:]

    async def last_prices(self, symbols: list[str]) -> dict[str, float]:
        """The latest price of each (stocks and crypto) - the scouts' same-coin check uses it."""
        out: dict[str, float] = {}
        stocks = [s for s in symbols if _stock(s)]
        crypto = {s.replace("-", "/"): s for s in symbols if not _stock(s)}
        if stocks:
            r = await self.client.get("/v2/stocks/trades/latest", params={"symbols": ",".join(stocks)})
            r.raise_for_status()
            out.update({s: float(t["p"]) for s, t in r.json().get("trades", {}).items()})
        if crypto:
            r = await self.client.get("/v1beta3/crypto/us/latest/trades", params={"symbols": ",".join(crypto)})
            r.raise_for_status()
            out.update({crypto.get(s, s): float(t["p"]) for s, t in r.json().get("trades", {}).items()})
        return out

    # ------------------------------------------------------------- finding new stocks
    async def most_actives(self, top: int = 50) -> list[str]:
        """Today's most-traded US stocks (by share volume)."""
        r = await self.client.get("/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": top})
        r.raise_for_status()
        return [str(x["symbol"]).upper() for x in r.json().get("most_actives") or [] if x.get("symbol")]

    async def movers(self, top: int = 20) -> dict[str, float]:
        """Today's biggest risers: symbol -> % change."""
        r = await self.client.get("/v1beta1/screener/stocks/movers", params={"top": top})
        r.raise_for_status()
        return {str(x["symbol"]).upper(): float(x.get("percent_change") or 0) for x in r.json().get("gainers") or []
                if x.get("symbol")}

    async def snapshots(self, symbols: list[str]) -> dict[str, dict]:
        """symbol -> {price, volume, dollar_volume}: the bigger of today's and yesterday's volume (early in the
        day hardly anything has traded yet - a busy stock shouldn't look quiet every morning)."""
        if not symbols:
            return {}
        r = await self.client.get("/v2/stocks/snapshots", params={"symbols": ",".join(symbols)})
        r.raise_for_status()
        out = {}
        for sym, snap in (r.json() or {}).items():
            if not isinstance(snap, dict):
                continue
            px = float(((snap.get("latestTrade") or {}).get("p")) or ((snap.get("dailyBar") or {}).get("c")) or 0)
            vol = max(float((snap.get("dailyBar") or {}).get("v") or 0), float((snap.get("prevDailyBar") or {}).get("v") or 0))
            out[sym.upper()] = {"price": px, "volume": vol, "dollar_volume": px * vol}
        return out

    async def news(self, symbols: list[str], limit: int = 50) -> list[dict]:
        """Recent headlines about these stocks / coins: [{title, link, ts, source, symbols}]."""
        tags = [s if _stock(s) else s.replace("-", "").replace("/", "") for s in symbols]
        r = await self.client.get("/v1beta1/news", params={"symbols": ",".join(tags[:50]), "limit": limit})
        r.raise_for_status()
        out = []
        for n in r.json().get("news") or []:
            try:
                ts = _ts(n.get("created_at") or n.get("updated_at"))
            except (TypeError, ValueError):
                continue
            out.append({"title": str(n.get("headline") or "").strip(), "link": n.get("url") or "", "ts": ts,
                        "source": f"alpaca/{n.get('source') or 'news'}",
                        "symbols": [str(x).upper() for x in n.get("symbols") or []]})
        return [x for x in out if x["title"]]

    async def close(self) -> None:
        await self.client.aclose()
