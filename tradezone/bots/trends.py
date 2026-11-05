"""Trend Scout - plugs the team into trending coins and every meme coin worth trading.

Every half hour it asks CoinGecko (free public market data, no account) for:

* **meme coins** with a market cap of at least **$50k** and **promising volume**: at least $100k traded
  in 24 h, and at least 5% of the market cap changing hands (real interest, not a dead coin);
* the coins **trending** on CoinGecko right now;
* coins **launched on pump.fun** (CoinGecko's pump.fun category) that pass the same bar: popular ($50k+ market
  cap) with plenty of volume. They're bought only once the exchange lists them - never through a wallet key, so
  nothing can ever be withdrawn - and they get the meme-coin rules.

pump.fun is full of copycat coins that reuse a famous ticker, so a discovered coin is only plugged in when its
CoinGecko price matches the exchange's price for that ticker (within 5%): the same coin, not a namesake.

Every one of them that the venue lists against USD (Kraken, with Kraken prices) is added to what the
bots watch and trade, with its recent price history loaded so the bots can act at once. Meme coins get
the meme-coin rules (smaller bets, wider stops). Trending coins get a small conviction boost. Coins that
stop qualifying are dropped again, but never while the bots hold them.

What stays the same: a coin is only bought if the "can it be sold?" checks pass (listed for buying and
selling, a tight bid/ask gap, enough daily trading for the position), so a thin coin is watched but not
bought. Coins that only trade on decentralised exchanges can't be bought here: TradeZone trades through
the exchange account, and DEX-only tokens carry a high risk of tokens that can't be sold.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..core.models import now
from .base import Bot

log = logging.getLogger(__name__)


def _money(v) -> float:
    """CoinGecko's trending list gives "$1,234,567" strings; the markets list gives numbers."""
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


class TrendBot(Bot):
    name = "trends"
    title = "Trend Scout"
    role = "Trending & Meme Coin Discovery"
    mission = "🔥 Take trades where the action is - trending coins and every meme coin with real volume"
    description = (
        "Scans CoinGecko for meme coins over $50k market cap with promising volume and for trending coins, "
        "and plugs the ones the exchange lists into the team's watchlist."
    )

    API = "https://api.coingecko.com/api/v3"

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.client: httpx.AsyncClient | None = None
        self.trending: dict[str, dict] = {}  # "PEPE-USD" -> {name, market_cap, volume, rank}
        self.qualified: dict[str, dict] = {}  # meme coins that pass the market-cap / volume bar (and are listed)
        self.added: dict[str, float] = {}  # symbols this bot added to the watchlist -> when
        self.pump: set[str] = set()  # of those, coins launched on pump.fun
        self.last_refresh = 0.0
        self.last_error = ""
        self._task: asyncio.Task | None = None
        self.metrics.update({"scans": 0, "added": 0, "dropped": 0, "trending": 0})

    # --------------------------------------------------------------- fetch
    def _http(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(base_url=self.API, timeout=15.0, headers={"User-Agent": "TradeZone/1.0"})
        return self.client

    async def fetch_memes(self) -> list[dict]:
        out = []
        for page in (1, 2):  # the busiest 500 meme coins
            r = await self._http().get("/coins/markets", params={
                "vs_currency": "usd", "category": "meme-token", "order": "volume_desc", "per_page": 250, "page": page})
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list) or not rows:
                break
            out += rows
        return out

    async def fetch_pump(self) -> list[dict]:
        """The busiest coins launched on pump.fun (CoinGecko's pump.fun category)."""
        r = await self._http().get("/coins/markets", params={
            "vs_currency": "usd", "category": "pump-fun", "order": "volume_desc", "per_page": 250, "page": 1})
        r.raise_for_status()
        rows = r.json()
        return rows if isinstance(rows, list) else []

    async def same_coin(self, prices: dict[str, float | None], strict: set[str]) -> set[str]:
        """The candidates whose CoinGecko price matches the exchange's (a namesake copycat coin doesn't).
        A coin in ``strict`` must be checked; others pass when a price is missing (as before)."""
        feed = self.desk.feed
        if not hasattr(feed, "last_prices"):
            return {s for s in prices if s not in strict}
        try:
            venue = await feed.last_prices([s for s, p in prices.items() if p])
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("same-coin price check failed: %s", exc)
            return {s for s in prices if s not in strict and not prices[s]}
        ok, tol = set(), self.settings.same_coin_tolerance
        for s, p in prices.items():
            v = venue.get(s)
            if p and v:
                if abs(p / v - 1) <= tol:
                    ok.add(s)
                else:
                    self.bump("namesakes_skipped")
                    self.act(f"skipped {s}: CoinGecko's coin is at {p:,.6g} but the exchange's {s} is at {v:,.6g} - "
                             "a different coin with the same ticker")
            elif s not in strict:
                ok.add(s)
        return ok

    async def fetch_trending(self) -> list[dict]:
        r = await self._http().get("/search/trending")
        r.raise_for_status()
        return [c.get("item", {}) for c in (r.json().get("coins") or [])]

    def promising(self, mcap: float, volume: float) -> bool:
        s = self.settings
        return mcap >= s.discovery_min_mcap and volume >= s.discovery_min_volume \
            and volume >= s.discovery_min_turnover * mcap

    # ------------------------------------------------------------- refresh
    async def refresh(self) -> dict:
        """One scan: find qualifying meme coins and trending coins, plug in the listed ones, drop stale ones."""
        s = self.settings
        self.last_refresh = now()
        self.bump("scans")
        pairs = getattr(self.desk.feed, "pairs", None) or {}
        if self.desk.feed.name != "kraken" or not pairs:
            self.last_error = "needs the exchange's own prices (Kraken) to know which coins can be traded"
            return {"added": [], "dropped": []}
        try:
            memes, trending = await self.fetch_memes(), await self.fetch_trending()
            pump = await self.fetch_pump() if s.discover_pump else []
        except (httpx.HTTPError, ValueError) as exc:
            self.last_error = f"CoinGecko: {type(exc).__name__}: {exc}"[:200]
            log.warning("trend scan failed: %s", exc)
            return {"added": [], "dropped": []}
        self.last_error = ""
        qualified, trend, prices, pumped = {}, {}, {}, set()
        for c, from_pump in [(c, False) for c in memes] + [(c, True) for c in pump]:
            sym = f"{str(c.get('symbol', '')).upper()}-USD"
            mcap, vol = _money(c.get("market_cap")), _money(c.get("total_volume"))
            if sym in pairs and self.promising(mcap, vol):
                qualified.setdefault(sym, {"name": c.get("name", ""), "market_cap": mcap, "volume": vol,
                                           "change_24h": c.get("price_change_percentage_24h")})
                prices.setdefault(sym, _money(c.get("current_price")) or None)
                if from_pump:
                    pumped.add(sym)
                    qualified[sym]["pump"] = True
        for rank, c in enumerate(trending, 1):
            sym = f"{str(c.get('symbol', '')).upper()}-USD"
            data = c.get("data") or {}
            mcap, vol = _money(data.get("market_cap")), _money(data.get("total_volume"))
            if sym in pairs and mcap >= s.discovery_min_mcap:
                trend[sym] = {"name": c.get("name", ""), "market_cap": mcap, "volume": vol, "rank": rank}
                prices.setdefault(sym, _money(data.get("price")) or None)
        # only the same coin: a namesake (a copycat reusing a famous ticker) is never traded as the real one
        new = {k: v for k, v in prices.items() if k not in s.symbols}
        if new:
            ok = await self.same_coin(new, strict=pumped)
            for k in new:
                if k not in ok:
                    qualified.pop(k, None)
                    trend.pop(k, None)
        self.pump = {k for k in pumped if k in qualified}
        self.metrics["pump_coins"] = len(self.pump)
        self.qualified, self.trending = qualified, trend
        wanted = list(trend) + sorted(qualified, key=lambda x: -qualified[x]["volume"])  # trending first, then busiest
        added = []
        for sym in wanted:
            if sym in s.symbols or len(self.added) >= s.max_discovered:
                continue
            s.symbols.append(sym)  # the same list the feed, broker and bots use
            coin = sym.split("-")[0]
            if sym in qualified and coin not in s.meme_coins:
                s.meme_coins.append(coin)
            self.added[sym] = now()
            added.append(sym)
        dropped = []
        for sym in list(self.added):
            if sym in qualified or sym in trend or not self.portfolio.position(sym).is_flat:
                continue  # still qualifies, or the bots hold it: keep watching
            if sym in s.symbols:
                s.symbols.remove(sym)
            del self.added[sym]
            dropped.append(sym)
        if added:
            await self._load_history(added)
            self.act(f"plugged in {len(added)} coin(s): {', '.join(added)}")
        if dropped:
            self.act(f"dropped {', '.join(dropped)} (no longer trending / below the volume bar)")
        self.bump("added", len(added))
        self.bump("dropped", len(dropped))
        self.metrics["trending"] = len(trend)
        return {"added": added, "dropped": dropped}

    async def _load_history(self, symbols: list[str]) -> None:
        scout = self.desk.bots.get("scout")
        gran = int(getattr(scout, "bar_seconds", 0) or 0)
        if scout is None or gran <= 0:
            return
        for sym in symbols:
            try:
                candles = await self.desk.feed.history(sym, gran, 300)
            except Exception as exc:  # noqa: BLE001 - one coin's history must not stop the scan
                log.warning("history %s failed: %s", sym, exc)
                continue
            if candles:
                scout.backfill(sym, candles)  # type: ignore[attr-defined]

    # ---------------------------------------------------------------- team
    def nudge(self, symbol: str, direction: int) -> float:
        """A small conviction boost for buying a coin that's trending right now."""
        if not self.enabled or direction <= 0 or symbol not in self.trending:
            return 0.0
        return self.settings.trending_nudge

    async def on_cycle(self) -> None:
        s = self.settings
        if not s.discover_coins or not self.enabled or (self._task is not None and not self._task.done()):
            return
        if now() - self.last_refresh >= s.discovery_every_min * 60:
            self.last_refresh = now()
            self._task = asyncio.get_running_loop().create_task(self.refresh())

    def summary(self) -> dict:
        return {"trending": [{"symbol": k, **v} for k, v in sorted(self.trending.items(), key=lambda x: x[1]["rank"])],
                "qualified": len(self.qualified), "added": sorted(self.added), "error": self.last_error,
                "pump": sorted(self.pump),
                "min_market_cap": self.settings.discovery_min_mcap}

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.client is not None:
            await self.client.aclose()
