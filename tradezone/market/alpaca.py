"""Latest-trade prices for US equities (and crypto) from Alpaca's data API."""

from __future__ import annotations

import logging

import httpx

from ..core.models import Tick, now
from .base import MarketFeed

log = logging.getLogger(__name__)


class AlpacaFeed(MarketFeed):
    name = "alpaca"
    BASE = "https://data.alpaca.markets"

    def __init__(
        self, symbols: list[str], key: str, secret: str, client: httpx.AsyncClient | None = None
    ) -> None:
        self.stocks = [s for s in symbols if "-" not in s and "/" not in s]
        self.crypto = {s.replace("-", "/"): s for s in symbols if s not in self.stocks}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.client = client or httpx.AsyncClient(base_url=self.BASE, headers=headers, timeout=5.0)

    async def poll(self) -> list[Tick]:
        ticks: list[Tick] = []
        try:
            if self.stocks:
                r = await self.client.get(
                    "/v2/stocks/trades/latest", params={"symbols": ",".join(self.stocks)}
                )
                r.raise_for_status()
                for sym, t in r.json().get("trades", {}).items():
                    ticks.append(Tick(sym, float(t["p"]), float(t.get("s", 0)), now()))
            if self.crypto:
                r = await self.client.get(
                    "/v1beta3/crypto/us/latest/trades",
                    params={"symbols": ",".join(self.crypto)},
                )
                r.raise_for_status()
                for sym, t in r.json().get("trades", {}).items():
                    ticks.append(
                        Tick(self.crypto.get(sym, sym), float(t["p"]), float(t.get("s", 0)), now())
                    )
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("alpaca data poll failed: %s", exc)
        else:
            self.last_error = ""
        return ticks

    async def close(self) -> None:
        await self.client.aclose()
