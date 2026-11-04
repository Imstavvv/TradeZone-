"""Execution Trader - routes approved orders to the broker and tracks fills."""

from __future__ import annotations

import math

from ..core.bus import Event
from ..core.models import Alert, Fill, Order, OrderRequest, OrderStatus, now
from .base import Bot


class ExecutionBot(Bot):
    name = "execution"
    title = "Executor"
    role = "Order Execution & Routing"
    mission = "💰 Make money - fill at a fair price, no mistakes"
    description = (
        "Turns approved orders into broker orders, prevents duplicates, enforces reduce-only "
        "exits, tracks partial fills and cancels orders that go stale."
    )
    critical = True

    ORDER_TIMEOUT_S = 60.0
    SLIPPAGE_COOLOFF_S = 1800.0

    def cooling(self, symbol: str) -> float:
        """Seconds left before ``symbol`` may be traded again after a bad fill (0 = fine)."""
        return max(0.0, self.cool_until.get(symbol, 0.0) - now())

    def reference_price(self, symbol: str, side) -> float:
        """What a fair fill looks like: the ask for a buy, the bid for a sell when the venue's quote is fresh
        (a market order always crosses the spread), else the last price."""
        price = self.desk.prices[symbol]
        q = getattr(self.desk.broker, "quotes", {}).get(symbol)
        if q and now() - q.get("ts", 0) < 10 and q.get("bid", 0) > 0 and q.get("ask", 0) > q["bid"]:
            return q["ask"] if side.sign > 0 else q["bid"]  # fresh: exactly what a market order should get
        if q and now() - q.get("ts", 0) < 180 and q.get("bid", 0) > 0 and q.get("ask", 0) > q["bid"]:
            # the quote can be up to half a minute old: use its bid/ask GAP around the latest price
            half = (q["ask"] - q["bid"]) / (q["ask"] + q["bid"])
            return price * (1 + half * side.sign)
        return price

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.orders: dict[str, Order] = {}
        self.working: dict[str, Order] = {}
        self._submitted_at: dict[str, float] = {}
        self.ref_price: dict[str, float] = {}
        self._cum_filled: dict[str, float] = {}
        self._processed_requests: set[str] = set()
        # fills emitted but not yet booked by the ledger (they sit in the event queue)
        self.unbooked: dict[str, float] = {}
        self.cool_until: dict[str, float] = {}  # symbol -> no new trades before (after a bad fill)
        self.metrics.update({"fills_verified": 0, "integrity_violations": 0, "pretrade_blocks": 0})

    def subscriptions(self):
        # only orders the Executive has authorised are executed
        return {"order.authorized": self.on_approved, "order.filled": self.on_booked}

    def on_booked(self, ev: Event) -> None:
        f: Fill = ev.payload
        left = self.unbooked.get(f.symbol, 0.0) - f.qty * f.side.sign
        if abs(left) < 1e-12:
            self.unbooked.pop(f.symbol, None)
        else:
            self.unbooked[f.symbol] = left

    def effective_qty(self, symbol: str) -> float:
        """Position including fills that are still on their way to the ledger."""
        return self.portfolio.position(symbol).qty + self.unbooked.get(symbol, 0.0)

    def working_for(self, symbol: str) -> Order | None:
        return next((o for o in self.working.values() if o.symbol == symbol), None)

    # -- pre-trade validation ------------------------------------------------
    def pretrade_check(self, req: OrderRequest) -> str | None:
        """Return a reason to block the order, or None if it is safe to send."""
        if req.id in self._processed_requests:
            return "duplicate order request (idempotency)"
        if req.symbol not in self.settings.symbols:
            return f"{req.symbol} is not on the approved symbol list"
        if not (math.isfinite(req.qty) and req.qty > 0):
            return f"invalid quantity {req.qty}"
        px = self.desk.prices.get(req.symbol)
        if not px or not math.isfinite(px) or px <= 0:
            return "no valid reference price"
        if not req.reduce_only:
            cap = self.portfolio.equity * self.settings.risk.max_position_pct * 1.05
            if req.qty * px > cap:
                return f"fat-finger guard: notional {req.qty * px:,.0f} exceeds cap {cap:,.0f}"
        return None

    def integrity_violation(self, message: str) -> None:
        self.bump("integrity_violations")
        self.act(f"INTEGRITY: {message}", "critical")
        self.publish("alert", Alert("critical", f"Execution integrity: {message} - trading paused", self.name))
        self.desk.pause(f"execution integrity: {message}")

    def verify_fill(self, order: Order, f: Fill) -> bool:
        problems = []
        if f.symbol != order.symbol:
            problems.append(f"symbol {f.symbol} != {order.symbol}")
        if f.side is not order.side:
            problems.append(f"side {f.side.value} != {order.side.value}")
        if not (math.isfinite(f.qty) and f.qty > 0 and math.isfinite(f.price) and f.price > 0):
            problems.append(f"invalid fill qty/price {f.qty}@{f.price}")
        cum = self._cum_filled.get(order.id, 0.0) + f.qty
        self._cum_filled[order.id] = cum
        if cum > order.qty * (1 + 1e-9) + 1e-12:
            problems.append(f"overfill {cum:g} > ordered {order.qty:g}")
        ref = self.ref_price.get(order.id)
        if ref and math.isfinite(f.price):
            adverse_bps = (f.price - ref) / ref * 10_000 * order.side.sign
            if adverse_bps > self.settings.max_slippage_bps and not order.reduce_only:
                # a worse-than-expected fill isn't a broken system: cool this coin off instead of stopping everything
                self.cool_until[f.symbol] = now() + self.SLIPPAGE_COOLOFF_S
                self.bump("bad_fills")
                self.publish("alert", Alert("warning", f"{f.symbol} filled {adverse_bps:.0f}bps worse than expected - "
                                                       f"no new {f.symbol} trades for {self.SLIPPAGE_COOLOFF_S / 60:.0f} min", self.name))
                self.act(f"bad fill on {f.symbol}: {adverse_bps:.0f}bps slippage - cooling it off", "warning")
        if problems:
            self.integrity_violation(f"{order.id} {'; '.join(problems)}")
            return False
        self.bump("fills_verified")
        return True

    @property
    def accuracy(self) -> float:
        total = self.metrics.get("fills", 0)
        return 1.0 if not total else self.metrics.get("fills_verified", 0) / total

    async def on_approved(self, ev: Event) -> None:
        req: OrderRequest = ev.payload
        if (why := self.pretrade_check(req)) is not None:
            self.bump("pretrade_blocks")
            self.act(f"blocked {req.side.value} {req.symbol}: {why}", "warning")
            self.publish("alert", Alert("warning", f"pre-trade check blocked {req.symbol}: {why}", self.name))
            return
        self._processed_requests.add(req.id)
        if (w := self.working_for(req.symbol)) is not None:
            self.bump("duplicates_blocked")
            self.act(f"blocked {req.side.value} {req.symbol}: order {w.id} still working", "warning")
            return
        qty = req.qty
        if req.reduce_only:
            held = self.effective_qty(req.symbol)
            qty = min(qty, abs(held))
            if self.desk.broker.name in ("alpaca", "uphold"):
                # real venues can take fees out of the coins received, so the broker can hold slightly
                # less than our book: never try to sell more than the broker actually has
                actual = await self.desk.broker.position_qty(req.symbol)
                if actual is not None and 0 < abs(actual) < qty:
                    self.act(f"{req.symbol}: selling broker's actual {abs(actual):g} (book {qty:g}; fee taken in coin)")
                    qty = abs(actual)
            if qty <= 1e-12 or (held > 0) == (req.side.sign > 0):
                self.act(f"dropped reduce-only {req.symbol}: no position to reduce", "warning")
                return
        order = Order(
            req.symbol, req.side, qty, req.id, reason=req.reason,
            reduce_only=req.reduce_only, stop_price=req.stop_price, take_profit=req.take_profit,
        )
        self.orders[order.id] = order
        if self.desk.is_live:
            await self.desk.broker.fresh_quote(req.symbol)  # the venue's bid/ask right now, for the fill check
        self.ref_price[order.id] = self.reference_price(req.symbol, req.side)
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
            self.unbooked[f.symbol] = self.unbooked.get(f.symbol, 0.0) + f.qty * f.side.sign
            self.verify_fill(order, f)  # violations pause the desk but the fill is still booked
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
                late = await self.desk.broker.poll(order)  # it may have filled while we waited: never lose a fill
                self._emit(order, late)
                if not order.status.is_terminal or order.status is not OrderStatus.FILLED:
                    order.status = OrderStatus.CANCELED
                self.working.pop(oid, None)
                self._submitted_at.pop(oid, None)
                self.publish("order.update", order)
                self.act(f"{'filled late' if late else 'canceled stale'} order {oid}", "warning")
        self.metrics["working"] = len(self.working)
        self.metrics["accuracy"] = round(self.accuracy, 6)
