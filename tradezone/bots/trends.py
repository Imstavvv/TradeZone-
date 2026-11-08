"""Trend Scout - plugs the team into trending coins and every meme coin worth trading.

Every half hour it asks CoinGecko (free public market data, no account) for:

* **meme coins** of any market cap (no minimum, asked for) with **promising volume**: at least $100k traded
  in 24 h, and at least 5% of the market cap changing hands (real interest, not a dead coin);
* the coins **trending** on CoinGecko right now;
* coins **launched on pump.fun** (CoinGecko's pump.fun category) that pass the same bar: popular (any market
  cap) with plenty of volume. They're bought only once the exchange lists them - never through a wallet key, so
  nothing can ever be withdrawn - and they get the meme-coin rules.

pump.fun is full of copycat coins that reuse a famous ticker, so a discovered coin is only plugged in when its
CoinGecko price matches the exchange's price for that ticker (within 5%): the same coin, not a namesake.

Every one of them that this account's exchange lists against USD (Kraken's pairs, Coinbase's products,
Alpaca's coins - and that the account itself can trade) is added to what the bots watch and trade, with its recent price history loaded so the bots can act at once. Meme coins get
the meme-coin rules (smaller bets, wider stops). Trending coins get a small conviction boost. Coins that
stop qualifying are dropped again, but never while the bots hold them.

What stays the same: a coin is only bought if the "can it be sold?" checks pass (listed for buying and
selling, a tight bid/ask gap, enough daily trading for the position), so a thin coin is watched but not
bought. Coins that only trade on decentralised exchanges can't be bought here: TradeZone trades through
the exchange account, and DEX-only tokens carry a high risk of tokens that can't be sold.

**Stocks (Alpaca accounts):** every half hour it also reads Alpaca's screener - the day's most-traded US
stocks and biggest risers - and adds the established ones: a share price of at least $10, at least $50M
traded today, listed on a main US exchange, fractional shares allowed, and never a leveraged or inverse
fund (those can lose most of their value in days). Big risers get the same small boost as trending coins.
Stocks that stop qualifying are dropped again, never while held.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..core.models import now
from .base import Bot

_pause = asyncio.sleep  # (between history requests; tests skip the wait)
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
        "Scans CoinGecko for meme coins of any market cap with promising volume and for trending coins, "
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
        self.stocks_added: dict[str, float] = {}  # stocks this bot added -> when
        self.stock_info: dict[str, dict] = {}  # why each qualifies: price, dollar volume, % up today
        self.stock_error = ""
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
        pairs = self.listed()
        stocks = await self.refresh_stocks()
        if not pairs:
            if not stocks["checked"]:
                self.last_error = "this account's prices don't say which coins the exchange lists yet"
            return {"added": stocks["added"], "dropped": stocks["dropped"]}
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
            if sym in qualified or sym in trend or self._busy(sym):
                continue  # still qualifies, or the bots hold it (or an order / the hold stack): keep watching
            if sym in s.symbols:
                s.symbols.remove(sym)
            del self.added[sym]
            dropped.append(sym)
        self.trending.update(stocks["trend"])
        added, dropped = added + stocks["added"], dropped + stocks["dropped"]
        if added:
            await self._load_history([x for x in added if x not in stocks["added"]])
            self.act(f"plugged in {len(added)} coin(s) / stock(s): {', '.join(added)}")
        if dropped:
            self.act(f"dropped {', '.join(dropped)} (no longer trending / below the volume bar)")
        self.bump("added", len(added))
        self.bump("dropped", len(dropped))
        self.metrics["trending"] = len(trend)
        return {"added": added, "dropped": dropped}

    def _busy(self, sym: str) -> bool:
        """Still needed: held, an order on it still filling, or in the hold stack (its value needs the price)."""
        if not self.portfolio.position(sym).is_flat:
            return True
        exe = self.desk.bots.get("execution")
        if exe is not None and (exe.working_for(sym) is not None or abs(exe.unbooked.get(sym, 0.0)) > 1e-12):
            return True
        st = self.desk.bots.get("stacker")
        return st is not None and any(sym in book for book in st.books.values())

    def listed(self) -> set[str]:
        """The coins this account's exchange lists against USD, and the account can trade."""
        from ..config import ALPACA_CRYPTO
        feed = self.desk.feed
        if feed.name == "kraken":
            out = set(getattr(feed, "pairs", None) or {})
        elif feed.name == "coinbase":
            out = {x for x in getattr(feed, "listed", set()) if x.endswith("-USD")}
        elif feed.name == "alpaca":
            out = {f"{c}-USD" for c in ALPACA_CRYPTO}
        else:
            return set()
        broker = self.desk.live_broker  # e.g. Uphold trades on Coinbase prices: only what Uphold also lists
        if broker is not None:
            out = {x for x in out if broker.supports(x) is not False}
        return out

    LEVERAGED = ("ultra", "2x", "3x", "-1x", "-2x", "-3x", "leveraged", "inverse", "bear ", " bull ", "short ")
    MAIN_EXCHANGES = frozenset({"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS", "NYSEARCA"})

    async def refresh_stocks(self) -> dict:
        """Alpaca accounts: find established stocks that are busy today; drop the ones that stopped qualifying."""
        s, feed = self.settings, self.desk.feed
        out: dict = {"added": [], "dropped": [], "trend": {}, "checked": False}
        self.stock_error = ""
        broker = self.desk.live_broker
        if not (s.discover_stocks and s.alpaca_stocks and hasattr(feed, "most_actives")):
            return out
        if broker is None or not hasattr(broker, "asset"):
            self.stock_error = "finding stocks needs the Alpaca account linked (to check each one can be traded)"
            return out
        if self.desk.in_focus(s.alpaca_stocks[0]):
            return out  # (the growth ladder: stocks join from $1,000)
        try:
            actives, gainers = await feed.most_actives(50), await feed.movers(20)
            base = set(s.symbols) - set(self.stocks_added)
            cands = [x for x in dict.fromkeys([*actives, *gainers, *self.stocks_added])
                     if x.isalpha() and len(x) <= 5 and x not in base]
            snaps = await feed.snapshots(cands)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.stock_error = f"Alpaca screener: {type(exc).__name__}: {exc}"[:200]
            log.warning("stock scan failed: %s", exc)
            return out
        out["checked"] = True
        ok: dict[str, dict] = {}
        for sym in cands:
            sn = snaps.get(sym)
            if not sn or sn["price"] < s.stock_min_price or sn["dollar_volume"] < s.stock_min_dollar_volume:
                continue
            info = await broker.asset(sym)
            name = str((info or {}).get("name") or "")
            if not info or not info.get("tradable") or not info.get("fractionable") or info.get("status", "active") != "active" \
                    or str(info.get("exchange", "")).upper() not in self.MAIN_EXCHANGES \
                    or any(w in f" {name.lower()} " for w in self.LEVERAGED):
                continue
            ok[sym] = {"name": name, "price": sn["price"], "dollar_volume": sn["dollar_volume"],
                       "up_today": gainers.get(sym)}
        self.stock_info = ok
        keep = sorted(ok, key=lambda x: -ok[x]["dollar_volume"])[:s.max_discovered_stocks]
        for sym in keep:
            if sym not in s.symbols:
                s.symbols.append(sym)  # the same list the feed, broker and bots use
                self.stocks_added[sym] = now()
                out["added"].append(sym)
        for sym in list(self.stocks_added):
            if sym in keep or self._busy(sym):
                continue  # still qualifies, or the bots hold it (or an order on it is still filling)
            if sym in s.symbols:
                s.symbols.remove(sym)
            del self.stocks_added[sym]
            out["dropped"].append(sym)
        risers = sorted((x for x in keep if ok[x]["up_today"]), key=lambda x: -(ok[x]["up_today"] or 0))
        out["trend"] = {x: {"name": ok[x]["name"], "rank": 100 + i, "up_today": ok[x]["up_today"], "stock": True}
                        for i, x in enumerate(risers)}
        if out["added"]:
            await self._load_history(out["added"])
        return out

    async def _load_history(self, symbols: list[str]) -> None:
        scout = self.desk.bots.get("scout")
        gran = int(getattr(scout, "bar_seconds", 0) or 0)
        if scout is None or gran <= 0:
            return
        for n, sym in enumerate(symbols):
            if n:  # (many coins at once - up to 100 - stay within the exchange's request limit; this runs in the background)
                await _pause(self.settings.history_pause_s)
            try:
                candles = await self.desk.feed.history(sym, gran, 300)
            except Exception as exc:  # one coin's history must not stop the scan
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
                "qualified": len(self.qualified), "added": sorted(self.added), "error": self.last_error or self.stock_error,
                "pump": sorted(self.pump),
                "stocks": [{"symbol": k, **v} for k, v in sorted(self.stock_info.items(), key=lambda x: -x[1]["dollar_volume"])
                           if k in self.stocks_added],
                "min_market_cap": self.settings.discovery_min_mcap}

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.client is not None:
            await self.client.aclose()
