"""Real-time crypto prices from Coinbase (public data, no account needed).

* **Primary:** the WebSocket ``ticker`` channel streams every trade as it happens.
* **Fallback:** if the stream is down or silent, the public REST ticker is polled (a few
  symbols per cycle, to stay under Coinbase's public rate limit) until the stream recovers.
* **History:** ``history()`` loads recent 1-minute candles so indicators are ready at start-up.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from datetime import datetime

import httpx

from ..core.models import Tick, now
from .base import MarketFeed

log = logging.getLogger(__name__)

CANDLE_GRANULARITIES = (60, 300, 900, 3600, 21600, 86400)


def _parse_time(s: str | None) -> float:
    if not s:
        return now()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return now()


class CoinbaseFeed(MarketFeed):
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"
    WS_URL = "wss://ws-feed.exchange.coinbase.com"
    REST_PER_CYCLE = 5  # fallback polling: symbols per cycle (public limit is ~10 req/s)
    WS_STALE_S = 15.0  # no stream message for this long -> fall back to REST

    def __init__(self, symbols: list[str], client: httpx.AsyncClient | None = None,
                 ws_url: str | None = None, use_ws: bool = True) -> None:
        self.symbols = symbols
        self.client = client or httpx.AsyncClient(
            base_url=self.BASE, timeout=5.0, headers={"User-Agent": "TradeZone/1.0"}
        )
        self.ws_url = ws_url or self.WS_URL
        self.use_ws = use_ws
        self.last_error = ""
        self._backoff_until = 0.0
        self._buffer: deque[Tick] = deque(maxlen=20_000)
        self._ws_task: asyncio.Task | None = None
        self._ws_last_msg = 0.0
        self.ws_connected = False
        self._rr = 0  # round-robin index for REST fallback
        self.mode = "rest"
        self.dropped: list[str] = []

    # ---------------------------------------------------------------- stream
    async def check_products(self) -> list[str]:
        """Drop symbols Coinbase doesn't list (one unknown coin would break the whole stream).

        Returns the dropped symbols. If the product list can't be loaded, nothing is dropped.
        """
        try:
            r = await self.client.get("/products")
            r.raise_for_status()
            listed = {p["id"] for p in r.json() if isinstance(p, dict) and not p.get("trading_disabled")
                      and p.get("status", "online") == "online"}
        except Exception as exc:
            log.info("coinbase product list unavailable (%s); keeping all symbols", exc)
            return []
        if not listed:
            return []
        dropped = [sym for sym in self.symbols if sym not in listed]
        if dropped and len(dropped) < len(self.symbols):
            self.symbols[:] = [sym for sym in self.symbols if sym in listed]  # same list as settings.symbols
            log.warning("not listed on Coinbase, skipped: %s", ", ".join(dropped))
            return dropped
        return []

    async def start(self) -> None:
        self.dropped = await self.check_products()
        if self.use_ws and self._ws_task is None:
            self._ws_task = asyncio.get_running_loop().create_task(self._ws_loop())

    async def _ws_loop(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # pragma: no cover - websockets ships with uvicorn[standard]
            self.last_error = "websockets package missing; using REST polling"
            return
        delay = 1.0
        while True:
            try:
                async with connect(self.ws_url, open_timeout=10, ping_interval=20, max_size=2**22) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe", "product_ids": self.symbols, "channels": ["ticker"],
                    }))
                    self.ws_connected = True
                    delay = 1.0
                    async for raw in ws:
                        self._ws_last_msg = time.monotonic()
                        msg = json.loads(raw)
                        kind = msg.get("type")
                        if kind == "ticker" and msg.get("price"):
                            self._buffer.append(Tick(
                                msg["product_id"], float(msg["price"]),
                                float(msg.get("last_size") or 0.0), _parse_time(msg.get("time")),
                            ))
                        elif kind == "error":
                            self.last_error = f"stream error: {msg.get('message')} {msg.get('reason', '')}".strip()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"stream: {type(exc).__name__}: {exc}"[:200]
                log.warning("coinbase stream dropped: %s (retry in %.0fs)", exc, delay)
            self.ws_connected = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    def stream_healthy(self) -> bool:
        return self.ws_connected and time.monotonic() - self._ws_last_msg < self.WS_STALE_S

    # ------------------------------------------------------------------ REST
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

    async def _poll_rest(self) -> list[Tick]:
        if time.monotonic() < self._backoff_until:
            return []
        n = min(self.REST_PER_CYCLE, len(self.symbols))
        batch = [self.symbols[(self._rr + i) % len(self.symbols)] for i in range(n)]
        self._rr = (self._rr + n) % len(self.symbols)
        results = await asyncio.gather(*(self._one(s) for s in batch))
        ticks = [t for t in results if t is not None]
        if len(ticks) == len(batch) and not self.use_ws:
            self.last_error = ""
        return ticks

    async def poll(self) -> list[Tick]:
        if self.stream_healthy() or (self._buffer and self.ws_connected):
            self.mode = "stream"
            ticks = list(self._buffer)
            self._buffer.clear()
            if ticks:
                self.last_error = ""
            return ticks
        self.mode = "rest"
        self._buffer.clear()
        return await self._poll_rest()

    # --------------------------------------------------------------- history
    async def history(self, symbol: str, granularity: int, limit: int = 300) -> list[dict] | None:
        """Recent completed candles, oldest first: dicts with ts, low, high, open, close, volume."""
        if granularity not in CANDLE_GRANULARITIES:
            return None
        try:
            r = await self.client.get(f"/products/{symbol}/candles", params={"granularity": granularity})
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:
            self.last_error = f"history {symbol}: {type(exc).__name__}: {exc}"[:200]
            log.warning("coinbase candles %s failed: %s", symbol, exc)
            return None
        cutoff = time.time() - granularity  # drop the still-forming candle
        candles = [
            {"ts": float(t), "low": float(lo), "high": float(hi), "open": float(op), "close": float(cl),
             "volume": float(v)}
            for t, lo, hi, op, cl, v in rows if float(t) <= cutoff
        ]
        candles.sort(key=lambda c: c["ts"])
        return candles[-limit:]

    async def close(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # it's being shut down either way
                await self._ws_task
        await self.client.aclose()
