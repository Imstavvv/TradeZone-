"""Execution Trader - routes approved orders to the broker and tracks fills."""

from __future__ import annotations

from ..core.bus import Event
from ..core.models import Alert, Fill, Order, OrderRequest, OrderStatus, now
from .base import Bot


class ExecutionBot(Bot):
    name = "execution"
    title = "Executor"
    role = "Order Execution & Routing"
    description = (
        "Turns approved orders into broker orders, prevents duplicates, enforces reduce-only "
        "exits, tracks partial fills and cancels orders that go stale."
    )
    critical = True

    ORDER_TIMEOUT_S = 60.0

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.orders: dict[str, Order] = {}
        self.working: dict[str, Order] = {}
        self._submitted_at: dict[str, float] = {}

    def subscriptions(self):
        return {"order.approved": self.on_approved}

    def working_for(self, symbol: str) -> Order | None:
        return next((o for o in self.working.values() if o.symbol == symbol), None)

    async def on_approved(self, ev: Event) -> None:
        req: OrderRequest = ev.payload
        if (w := self.working_for(req.symbol)) is not None:
            self.bump("duplicates_blocked")
            self.act(f"blocked {req.side.value} {req.symbol}: order {w.id} still working", "warning")
            return
        qty = req.qty
        if req.reduce_only:
            pos = self.portfolio.position(req.symbol)
            qty = min(qty, abs(pos.qty))
            if qty <= 0 or (pos.qty > 0) == (req.side.sign > 0):
                self.act(f"dropped reduce-only {req.symbol}: no position to reduce", "warning")
                return
        order = Order(
            req.symbol, req.side, qty, req.id, reason=req.reason,
            reduce_only=req.reduce_only, stop_price=req.stop_price, take_profit=req.take_profit,
        )
        self.orders[order.id] = order
        fills = await self.desk.broker.submit(order)
        self.bump("orders")
        if order.status is OrderStatus.REJECTED:
            self.bump("broker_rejects")
            self.publish("order.update", order)
            self.publish("alert", Alert("warning", f"broker rejected {order.id}: {order.reason}", self.name))
            self.act(f"broker rejected {order.side.value} {order.symbol}: {order.reason}", "warning")
            return
        if not order.status.is_terminal:
            self.working[order.id] = order
            self._submitted_at[order.id] = now()
        self.publish("order.update", order)
        self._emit(order, fills)
        self.act(f"sent {order.side.value.upper()} {order.qty:g} {order.symbol} via {self.desk.broker.name} -> {order.status.value}")

    def _emit(self, order: Order, fills: list[Fill]) -> None:
        for f in fills:
            self.bump("fills")
            self.bump("notional", abs(f.qty * f.price))
            self.publish("order.filled", f)
            self.act(f"filled {f.side.value.upper()} {f.qty:g} {f.symbol} @ {f.price:,.2f}")

    async def on_cycle(self) -> None:
        for oid, order in list(self.working.items()):
            fills = await self.desk.broker.poll(order)
            self._emit(order, fills)
            if fills:
                self.publish("order.update", order)
            if order.status.is_terminal:
                self.working.pop(oid, None)
                self._submitted_at.pop(oid, None)
                if not fills:
                    self.publish("order.update", order)
            elif now() - self._submitted_at.get(oid, now()) > self.ORDER_TIMEOUT_S:
                await self.desk.broker.cancel(order)
                order.status = OrderStatus.CANCELED
                self.working.pop(oid, None)
                self.publish("order.update", order)
                self.act(f"canceled stale order {oid}", "warning")
        self.metrics["working"] = len(self.working)
