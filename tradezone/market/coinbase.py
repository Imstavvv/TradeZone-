"""Real-time crypto prices from Coinbase's public (no-key) REST API."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..core.models import Tick, now
from .base import MarketFeed

log = logging.getLogger(__name__)


class CoinbaseFeed(MarketFeed):
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"

    def __init__(self, symbols: list[str], client: httpx.AsyncClient | None = None) -> None:
        self.symbols = symbols
        self.client = client or httpx.AsyncClient(
            base_url=self.BASE, timeout=5.0, headers={"User-Agent": "TradeZone/1.0"}
        )
        self.last_error = ""
        self._backoff_until = 0.0

    async def _one(self, sym: str) -> Tick | None:
        try:
            r = await self.client.get(f"/products/{sym}/ticker")
            if r.status_code == 429:  # rate limited: pause polling instead of hammering
                wait = float(r.headers.get("Retry-After", 5) or 5)
                self._backoff_until = time.monotonic() + min(max(wait, 1.0), 60.0)
                self.last_error = f"rate limited by Coinbase (pausing {wait:g}s)"
                return None
            if r.status_code == 404:
                self.last_error = f"{sym} is not a Coinbase product"
                return None
            r.raise_for_status()
            data = r.json()
            return Tick(sym, float(data["price"]), float(data.get("volume", 0.0)), now())
        except Exception as exc:
            self.last_error = f"{sym}: {type(exc).__name__}: {exc}"[:200]
            log.warning("coinbase ticker %s failed: %s", sym, exc)
            return None

    async def poll(self) -> list[Tick]:
        if time.monotonic() < self._backoff_until:
            return []
        results = await asyncio.gather(*(self._one(s) for s in self.symbols))
        ticks = [t for t in results if t is not None]
        if len(ticks) == len(self.symbols):
            self.last_error = ""
        return ticks

    async def close(self) -> None:
        await self.client.aclose()
