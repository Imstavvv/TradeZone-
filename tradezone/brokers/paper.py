"""Paper broker: instant market fills at the last price with slippage and fees."""

from __future__ import annotations

from typing import Callable

from ..core.models import Fill, Order, OrderStatus, Tick
from .base import Broker


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, commission_bps: float | Callable[[str], float] = 1.0, slippage_bps: float = 2.0) -> None:
        # a number, or a function symbol -> fee in bps (crypto and stocks are charged differently)
        self.fee = commission_bps if callable(commission_bps) else (lambda _s, c=commission_bps: c)
        self.slippage_bps = slippage_bps
        self.prices: dict[str, float] = {}
        self._seq = 0

    def on_prices(self, ticks: list[Tick]) -> None:
        for t in ticks:
            self.prices[t.symbol] = t.price

    async def submit(self, order: Order) -> list[Fill]:
        px = self.prices.get(order.symbol)
        self._seq += 1
        order.broker_id = f"paper-{self._seq}"
        if px is None or px <= 0 or order.qty <= 0:
            order.status = OrderStatus.REJECTED
            order.reason = "no market price" if px is None else "invalid quantity"
            return []
        slip = px * self.slippage_bps / 10_000 * order.side.sign
        fill_px = px + slip
        commission = abs(fill_px * order.qty) * self.fee(order.symbol) / 10_000
        order.status = OrderStatus.FILLED
        order.filled_qty = order.qty
        order.avg_fill_price = fill_px
        return [Fill(order.id, order.symbol, order.side, order.qty, fill_px, commission)]
