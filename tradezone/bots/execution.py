"""Execution Trader - routes approved orders to the broker and tracks fills."""

from __future__ import annotations

import math

from ..core.bus import Event
from ..core.models import Alert, Fill, Order, OrderRequest, OrderStatus, Side, now
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
        self._processed_requests: dict[str, bool] = {}  # (ordered, so the oldest can be pruned)
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
            cap = self.portfolio.equity * self.settings.limits(self.desk.is_live).max_position_pct * 1.05
            if req.qty * px > cap:
                return f"fat-finger guard: notional {req.qty * px:,.0f} exceeds cap {cap:,.0f}"
        return None

    PLAIN = (("Insufficient funds", "not enough free cash or coins on the exchange (some may be held by an open order "
                                    "or a hold-stack buy) - the next idea is sized to what's free"),
             ("Invalid nonce", "the exchange saw a newer request from this API key - is the same key used by another app "
                               "or a second TradeZone? Give each its own key"),
             ("Rate limit", "too many requests at once - retried a moment later"),
             ("minimum", "under the exchange's minimum order"),
             ("Permission denied", "the API key isn't allowed to trade - turn on 'Create & modify orders' for it"),
             ("Unknown asset pair", "the exchange doesn't trade this pair right now"),
             ("Market in post_only", "the market is in post-only mode - only resting orders are taken for now"),
             ("Service", "the exchange is busy or down for a moment"))

    @classmethod
    def plain_refusal(cls, reason: str | None) -> str:
        why = next((v for k, v in cls.PLAIN if k.lower() in (reason or "").lower()), None)
        return f" ({why})" if why else ""

    def cash_room(self, symbol: str) -> float:
        """How much of ``symbol`` the cash still free can buy: cash minus buys already working or filled but not
        yet booked. Two buys approved in the same moment were each sized against the whole cash, and together
        could ask for more than the account had."""
        px = self.desk.prices.get(symbol) or 0.0
        if px <= 0:
            return 0.0
        ask = (getattr(self.desk.broker, "quotes", {}).get(symbol) or {}).get("ask")
        if ask and ask > px and ask < px * 1.05:
            px = ask  # (a market buy pays the ask, not the last price)
        prices, fee = self.desk.prices, (lambda sym: 1 + self.settings.fee_bps(sym) / 10_000)  # (fees paid too)
        committed = sum(max(0.0, o.qty - o.filled_qty) * (o.limit_price or prices.get(o.symbol) or 0.0) * fee(o.symbol)
                        for o in self.working.values() if o.side is Side.BUY and not o.reduce_only)
        committed += sum(q * (prices.get(s) or 0.0) * fee(s) for s, q in self.unbooked.items() if q > 0)
        free = self.desk.free_cash() - committed
        return max(0.0, free * 0.995 / (px * (1 + self.settings.fee_bps(symbol) / 10_000)))

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
        self._processed_requests[req.id] = True
        if (w := self.working_for(req.symbol)) is not None and w.maker and req.reduce_only and not req.calm:
            await self._cancel_for_urgent(w)  # a stop / cut / flatten never waits behind an order resting on the book
            w = self.working_for(req.symbol)
        if w is not None:
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
        elif req.side is Side.BUY:
            room = self.cash_room(req.symbol)
            if room <= 0:
                self.bump("cash_blocks")
                self.act(f"blocked buy {req.symbol}: the cash is already committed to other buys", "warning")
                return
            if qty > room:  # (sized against the same cash as a buy approved just before it: trimmed to what's left)
                self.act(f"{req.symbol}: buy trimmed {qty:g} -> {room:g} to the cash still free")
                qty = room
            px = self.desk.prices.get(req.symbol) or 0.0
            least = self.desk.broker.min_qty(req.symbol, px) if px > 0 else 0.0
            if qty < least:  # (what's left is under the exchange's minimum order: it could only be refused)
                self.bump("cash_blocks")
                self.act(f"skipped buy {req.symbol}: the cash still free buys {qty:g}, under the exchange's minimum "
                         f"order ({least:g}) - waits for cash")
                return
        order = Order(
            req.symbol, req.side, qty, req.id, reason=req.reason,
            reduce_only=req.reduce_only, stop_price=req.stop_price, take_profit=req.take_profit,
        )
        self.orders[order.id] = order
        if self.desk.is_live and getattr(self.desk.broker, "name", "") == "kraken" and (
                (not req.reduce_only and self.settings.maker_entries)
                or (req.reduce_only and req.calm and self.settings.maker_exits)):
            # a limit at the bid (a profit-taking sell: at the ask) first, for the maker fee; the rest at market
            # after maker_wait_s. Stops and cuts are never calm: they always go straight to market.
            order.maker = True
        if self.desk.is_live:
            await self.desk.broker.fresh_quote(req.symbol)  # the venue's bid/ask right now, for the fill check
        self.ref_price[order.id] = self.reference_price(req.symbol, req.side)
        fills = await self.desk.broker.submit(order)
        self.bump("orders")
        if order.status is OrderStatus.REJECTED:
            self.bump("broker_rejects")
            self.publish("order.update", order)
            self.publish("alert", Alert("warning", f"broker rejected {order.side.value} {order.symbol}: {order.reason}"
                                                   f"{self.plain_refusal(order.reason)}", self.name))
            self.act(f"broker rejected {order.side.value} {order.symbol}: {order.reason}", "warning")
            return
        if not order.status.is_terminal:
            self.working[order.id] = order
            self._submitted_at[order.id] = now()
        self.publish("order.update", order)
        self._emit(order, fills)
        if order.maker and order.status.is_terminal and order.status is not OrderStatus.FILLED:
            await self._maker_fallback(order)  # post-only refused (it would have taken liquidity): go market
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
                if order.maker and order.status is not OrderStatus.FILLED:
                    await self._maker_fallback(order)
            elif order.maker and now() - self._submitted_at.get(oid, now()) > self.settings.maker_wait_s:
                await self.desk.broker.cancel(order)  # waited at the bid long enough: the rest goes market
                late = await self.desk.broker.poll(order)
                self._emit(order, late)
                self.working.pop(oid, None)
                self._submitted_at.pop(oid, None)
                if order.status is not OrderStatus.FILLED:
                    order.status = OrderStatus.CANCELED
                    self.publish("order.update", order)
                    await self._maker_fallback(order)
            elif not order.maker and now() - self._submitted_at.get(oid, now()) > self.ORDER_TIMEOUT_S:
                # (a resting maker order is never just dropped here: its own wait above hands the rest to market)
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
        self._prune()

    async def _cancel_for_urgent(self, order: Order) -> None:
        """Take a resting maker order off the book now (keeping anything it already filled) - no market fallback:
        the urgent exit that replaces it sells whatever is held."""
        await self.desk.broker.cancel(order)
        late = await self.desk.broker.poll(order)
        self._emit(order, late)
        self.working.pop(order.id, None)
        self._submitted_at.pop(order.id, None)
        if order.status is not OrderStatus.FILLED:
            order.status = OrderStatus.CANCELED
        self.publish("order.update", order)
        self.bump("maker_cancelled_for_urgent")
        self.act(f"took the resting {order.side.value} {order.symbol} off the book: an urgent exit goes first")

    async def _maker_fallback(self, order: Order) -> None:
        """Buy (or, for a profit-taking exit, sell) whatever the limit didn't get, at market (never missed)."""
        rest = order.qty - order.filled_qty
        if order.reduce_only:  # never sell more than is still held
            rest = min(rest, abs(self.effective_qty(order.symbol)))
        if rest <= max(self.desk.broker.min_qty(order.symbol, self.desk.prices.get(order.symbol, 0) or 1), 1e-12):
            return
        child = Order(order.symbol, order.side, rest, order.request_id, reason=order.reason,
                      reduce_only=order.reduce_only, stop_price=order.stop_price, take_profit=order.take_profit)
        self.orders[child.id] = child
        self.ref_price[child.id] = self.reference_price(order.symbol, order.side)
        fills = await self.desk.broker.submit(child)
        if child.status is OrderStatus.REJECTED:
            self.publish("order.update", child)
            self.act(f"market rest of {order.symbol} rejected: {child.reason}", "warning")
            return
        if not child.status.is_terminal:
            self.working[child.id] = child
            self._submitted_at[child.id] = now()
        self.publish("order.update", child)
        self._emit(child, fills)
        self.bump("maker_fallbacks")

    KEEP_ORDERS = 3000

    def _prune(self) -> None:
        """Keep memory flat on a desk that runs for weeks: forget the oldest finished orders
        (the journal keeps every fill permanently)."""
        if len(self.orders) > self.KEEP_ORDERS:
            for oid in list(self.orders)[: len(self.orders) - self.KEEP_ORDERS + 500]:
                if oid not in self.working:
                    self.orders.pop(oid, None)
                    self.ref_price.pop(oid, None)
                    self._cum_filled.pop(oid, None)
        if len(self._processed_requests) > self.KEEP_ORDERS:
            for rid in list(self._processed_requests)[:500]:
                self._processed_requests.pop(rid, None)
