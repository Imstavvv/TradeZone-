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
            await self.client.delete(f"/v2/orders/{order.broker_id}")

    async def account(self) -> dict | None:
        try:
            r = await self.client.get("/v2/account")
            r.raise_for_status()
            a = r.json()
            return {"cash": float(a["cash"]), "equity": float(a["equity"])}
        except Exception as exc:
            log.warning("alpaca account fetch failed: %s", exc)
            return None

    async def close(self) -> None:
        await self.client.aclose()
