"""New Listings: reads Kraken's pair list every minute, so a token Kraken lists (new DEX-born tokens land there
often) joins the watch list within a minute - under the same rules as every coin: enough daily trading that it
can be sold again ($250k+ by default), never stablecoins or money. Nothing is bought just for being new: a new
coin trades only when the team's usual signals and checks say so."""

from __future__ import annotations

import asyncio
import time
from collections import deque

from ..core.models import Alert
from .base import Bot


class ListingsBot(Bot):
    name = "listings"
    title = "New Listings"
    role = "New Token Scanner"
    mission = "🆕 Be there on day one - every token Kraken lists joins the watch list within a minute"
    description = ("Reads Kraken's list of tradeable pairs every minute. A token Kraken has just listed (DEX-born "
                   "tokens included) joins the watch list straight away if it trades enough to be sold again, and "
                   "an alert names it. New coins are traded under exactly the same rules as every other coin.")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self._task: asyncio.Task | None = None
        self._at = time.monotonic()  # (start-up already read the list: the first rescan is a minute later)
        self.found: deque[dict] = deque(maxlen=50)

    def ready(self) -> bool:
        f = self.desk.feed
        return hasattr(f, "rescan") and bool(getattr(f, "pairs", None)) and bool(getattr(self.settings, "kraken_all_coins", True))

    async def on_cycle(self) -> None:
        if not self.__dict__.get("_loaded"):  # (the coins listed this week, kept across restarts)
            self._loaded = True
            store = getattr(self.desk, "store", None)
            for coin, at in ((store.load("new_coins") if store is not None else None) or {}).items():
                self.settings.new_coins.setdefault(coin, float(at))
        t = time.monotonic()
        if not self.ready() or t - self._at < float(getattr(self.settings, "listing_scan_s", 60.0)):
            return
        if self._task is not None and not self._task.done():
            return
        self._at = t
        self._task = asyncio.ensure_future(self._scan())  # (in the background: never slows the cycle)

    async def _scan(self) -> None:
        feed = self.desk.feed
        known = len(feed.pairs)
        try:
            joined = await feed.rescan()
        except Exception as exc:  # (a failed read: tried again next minute)
            self.bump("scan_errors")
            self.metrics["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            return
        self.bump("scans")
        self.metrics["pairs"] = len(feed.pairs)
        self.metrics["last_scan"] = time.time()
        self.metrics["waiting"] = len(getattr(feed, "waiting", {}))  # (listed, not yet trading enough)
        listed = len(feed.pairs) - known
        if listed:
            self.bump("listed_seen", listed)
        feed.new_listings.clear()  # (this bot announces them)
        if not joined:
            if listed:
                self.act(f"{listed} new pair(s) on Kraken, not trading enough yet to be sold again - checked each minute")
            return
        for sym in joined:
            self.found.append({"symbol": sym, "ts": time.time()})
            coin = sym.split("-")[0]
            self.settings.new_coins.setdefault(coin, time.time())  # (a new coin for a week: buys focus on it)
            if coin not in self.settings.dex_coins:  # (what Kraken lists now is mostly DEX-born: a quick-flip coin)
                self.settings.dex_coins.append(coin)
        self.bump("joined", len(joined))
        if (store := getattr(self.desk, "store", None)) is not None:
            week = time.time() - 8 * 86_400
            store.save("new_coins", {c: at for c, at in self.settings.new_coins.items() if at >= week})
        coins = ", ".join(x.split("-")[0] for x in joined[:8]) + (f" and {len(joined) - 8} more" if len(joined) > 8 else "")
        self.act(f"newly listed on Kraken, now watched: {coins}")
        self.publish("alert", Alert("info", f"🆕 Newly listed on Kraken, now watched and tradeable: {coins}", self.name))
