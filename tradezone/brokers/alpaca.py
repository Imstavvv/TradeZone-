"""Alpaca brokerage adapter (paper or live endpoint) via the REST v2 API."""

from __future__ import annotations

import logging

import httpx

from ..core.models import Fill, Order, OrderStatus, Side
from .base import Broker

log = logging.getLogger(__name__)

_STATUS = {
    "new": OrderStatus.SUBMITTED,
    "accepted": OrderStatus.SUBMITTED,
    "pending_new": OrderStatus.SUBMITTED,
    "accepted_for_bidding": OrderStatus.SUBMITTED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "done_for_day": OrderStatus.CANCELED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "suspended": OrderStatus.REJECTED,
}


def _is_crypto(sym: str) -> bool:
    return "-" in sym or "/" in sym


class AlpacaBroker(Broker):
    name = "alpaca"
    label = "Alpaca"
    PAPER_URL = "https://paper-api.alpaca.markets"
    LIVE_URL = "https://api.alpaca.markets"

    def __init__(
        self, key: str, secret: str, paper: bool = True, client: httpx.AsyncClient | None = None
    ) -> None:
        self.is_live = not paper
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        base = self.PAPER_URL if paper else self.LIVE_URL
        self.client = client or httpx.AsyncClient(base_url=base, headers=headers, timeout=10.0)
        self._reported: dict[str, tuple[float, float]] = {}  # order id -> (qty, avg)
        # Alpaca takes its crypto fee from what you RECEIVE: coins on a buy, dollars on a sale (stocks: none).
        # The fill is booked that way - fewer coins, fee as the commission - or the records drift above what
        # the account holds and a later "sell everything" asks for more than is there (and is refused).
        self.crypto_fee_bps = 25.0
        self.symbols: list[str] = []  # what the bots watch (set by the desk)
        self.day_trades: int | None = None  # Alpaca's own count of day trades in the last 5 trading days
        self.clock_open: bool | None = None  # Alpaca's market clock (holidays and early closes included)
        self.clock_at = 0.0

    def supports(self, symbol: str) -> bool | None:
        from ..config import Settings
        return Settings.live_tradeable(symbol)

    def _fills_from(self, order: Order, data: dict) -> list[Fill]:
        order.status = _STATUS.get(data.get("status", ""), order.status)
        filled = float(data.get("filled_qty") or 0)
        avg = float(data.get("filled_avg_price") or 0)
        prev_qty, prev_avg = self._reported.get(order.id, (0.0, 0.0))
        order.filled_qty, order.avg_fill_price = filled, avg
        if filled <= prev_qty + 1e-12:
            return []
        delta = filled - prev_qty
        px = (avg * filled - prev_avg * prev_qty) / delta
        self._reported[order.id] = (filled, avg)
        if not _is_crypto(order.symbol) or self.crypto_fee_bps <= 0:
            return [Fill(order.id, order.symbol, order.side, delta, px, 0.0)]
        fee = delta * px * self.crypto_fee_bps / 10_000  # (in dollars)
        if order.side is Side.BUY:  # the coins received are the fee short: same dollars paid, fee as commission
            return [Fill(order.id, order.symbol, order.side, delta * (1 - self.crypto_fee_bps / 10_000), px, fee)]
        return [Fill(order.id, order.symbol, order.side, delta, px, fee)]

    async def submit(self, order: Order) -> list[Fill]:
        crypto = _is_crypto(order.symbol)
        body = {
            "symbol": order.symbol.replace("-", "/") if crypto else order.symbol,
            "qty": f"{order.qty:.8f}".rstrip("0").rstrip("."),
            "side": order.side.value,
            "type": "market",
            "time_in_force": "gtc" if crypto else "day",
            "client_order_id": order.id,
        }
        try:
            r = await self.client.post("/v2/orders", json=body)
            if r.status_code >= 400:
                order.status = OrderStatus.REJECTED
                order.reason = f"alpaca {r.status_code}: {r.text[:200]}"
                return []
            data = r.json()
        except httpx.HTTPError as exc:
            order.status = OrderStatus.REJECTED
            order.reason = f"alpaca transport error: {exc}"
            return []
        order.broker_id = data.get("id")
        if order.status is OrderStatus.NEW:
            order.status = OrderStatus.SUBMITTED
        return self._fills_from(order, data)

    async def poll(self, order: Order) -> list[Fill]:
        if not order.broker_id:
            return []
        try:
            r = await self.client.get(f"/v2/orders/{order.broker_id}")
            r.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("alpaca poll %s failed: %s", order.id, exc)
            return []
        return self._fills_from(order, r.json())

    async def cancel(self, order: Order) -> None:
        if order.broker_id:
            try:
                await self.client.delete(f"/v2/orders/{order.broker_id}")
            except httpx.HTTPError as exc:  # a network blip must not become a bot error (the next poll settles it)
                log.warning("alpaca cancel %s failed: %s", order.id, exc)

    async def refresh_clock(self) -> None:
        """Is the US stock market open right now - by Alpaca's clock, which knows holidays and early closes."""
        import time as _t
        try:
            r = await self.client.get("/v2/clock")
            r.raise_for_status()
            self.clock_open, self.clock_at = bool(r.json().get("is_open")), _t.time()
        except Exception as exc:
            log.warning("alpaca clock fetch failed: %s", exc)

    async def asset(self, symbol: str) -> dict | None:
        """Alpaca's listing for a stock (tradable, fractionable, exchange, name) - cached for a day."""
        import time as _t
        cache = self.__dict__.setdefault("_assets", {})
        hit = cache.get(symbol)
        if hit and _t.time() - hit[0] < 86_400:
            return hit[1]
        try:
            r = await self.client.get(f"/v2/assets/{symbol}")
            if r.status_code == 404:
                info = None
            else:
                r.raise_for_status()
                info = r.json()
        except Exception as exc:
            log.warning("alpaca asset %s failed: %s", symbol, exc)
            return None
        cache[symbol] = (_t.time(), info)
        return info

    def market_open(self) -> bool | None:
        """Alpaca's answer if it's recent (10 minutes), else None (then the plain weekday 9:30-16:00 rule)."""
        import time as _t
        return self.clock_open if self.clock_open is not None and _t.time() - self.clock_at < 600 else None

    async def refresh_market(self) -> None:
        """The market clock (when stocks are watched). (No shorting: nothing else to check.)"""
        if any(not _is_crypto(s) for s in self.symbols):
            await self.refresh_clock()

    async def account(self) -> dict | None:
        try:
            r = await self.client.get("/v2/account")
            r.raise_for_status()
            a = r.json()
            self.day_trades = int(a.get("daytrade_count") or 0)  # day trades in the last 5 trading days
            # never margin funds (asked for): negative cash is a margin loan, and buying power includes borrowing -
            # the bots only ever use the cash the account really holds
            return {"cash": max(0.0, float(a["cash"])), "equity": float(a["equity"]), "daytrade_count": self.day_trades,
                    "pattern_day_trader": bool(a.get("pattern_day_trader"))}
        except Exception as exc:
            log.warning("alpaca account fetch failed: %s", exc)
            return None

    async def position_qty(self, symbol: str) -> float | None:
        sym = symbol.replace("-", "").replace("/", "")
        try:
            r = await self.client.get(f"/v2/positions/{sym}")
            if r.status_code == 404:
                return 0.0
            r.raise_for_status()
            p = r.json()
        except Exception as exc:
            log.warning("alpaca position %s fetch failed: %s", symbol, exc)
            return None
        qty = float(p["qty"])
        return -abs(qty) if p.get("side") == "short" else qty

    async def positions(self) -> dict[str, float] | None:
        try:
            r = await self.client.get("/v2/positions")
            r.raise_for_status()
        except Exception as exc:
            log.warning("alpaca positions fetch failed: %s", exc)
            return None
        out: dict[str, float] = {}
        for p in r.json():
            qty = float(p["qty"])
            if p.get("side") == "short" and qty > 0:
                qty = -qty
            out[p["symbol"]] = qty
        return out

    async def close(self) -> None:
        await self.client.aclose()
