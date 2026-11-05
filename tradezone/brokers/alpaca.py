"""Alpaca brokerage adapter (paper or live endpoint) via the REST v2 API."""

from __future__ import annotations

import logging

import httpx

from ..core.models import Fill, Order, OrderStatus
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
        self.symbols: list[str] = []  # what the bots watch (set by the desk)
        self.shorting_enabled = False  # margin account with shorting switched on (and $2,000+ equity)
        self.shortable: set[str] = set()  # stocks Alpaca says are shortable AND easy to borrow right now
        self._short_at = 0.0

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
        return [Fill(order.id, order.symbol, order.side, delta, px, 0.0)]

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

    async def refresh_market(self) -> None:
        """Which stocks can be shorted (every 10 min): a margin account and easy-to-borrow shares only."""
        import time as _t
        if _t.time() - self._short_at < 600:
            return
        self._short_at = _t.time()
        try:
            r = await self.client.get("/v2/account")
            r.raise_for_status()
            a = r.json()
            self.shorting_enabled = bool(a.get("shorting_enabled")) and float(a.get("equity") or 0) >= 2_000
            ok = set()
            if self.shorting_enabled:
                for sym in [s for s in self.symbols if not _is_crypto(s)]:
                    ra = await self.client.get(f"/v2/assets/{sym}")
                    if ra.status_code == 200:
                        d = ra.json()
                        if d.get("tradable") and d.get("shortable") and d.get("easy_to_borrow"):
                            ok.add(sym)
            self.shortable = ok
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            self.shorting_enabled, self.shortable = False, set()  # unknown = not allowed
            log.warning("alpaca shortability check failed: %s", exc)

    def can_short(self, symbol: str) -> bool:
        return not _is_crypto(symbol) and self.shorting_enabled and symbol in self.shortable

    async def account(self) -> dict | None:
        try:
            r = await self.client.get("/v2/account")
            r.raise_for_status()
            a = r.json()
            return {"cash": float(a["cash"]), "equity": float(a["equity"])}
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
