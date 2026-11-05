"""The Stacker: slowly builds a long-term hold of a few coins on the side.

* **Hold stack, not trades.** Coins it buys go into a separate hold stack. They are never part of the trading
  book, so no bot ever sells them and they have **no stop-loss**: they are yours to keep (and to sell yourself,
  if you ever want to). The trading balance and its profit/loss don't include them; the stack has its own value.
* **Slowly.** A small budget builds up over time (1% of the trading balance a day by default) plus a share of
  every winning trade's profit. When the budget is big enough for the venue's smallest order, it buys one coin:
  the one furthest below its share of the stack, preferably while that coin isn't running hot (RSI under 55).
* **Never at the trading team's expense.** It only spends free cash, keeps at least a quarter of the trading
  balance free, stops growing the stack past half of everything, and waits while trading is paused or stopped.
"""

from __future__ import annotations

import os
import time

from ..core.bus import Event
from ..core.models import Order, OrderStatus, Side, now
from ..core.util import norm_symbol
from .base import Bot


class StackerBot(Bot):
    name = "stacker"
    title = "Stacker"
    role = "Long-Term Holds"
    mission = "🪙 Stack coins slowly on the side - held for the long run, never sold by the bots, no stop-loss"
    description = ("Slowly buys SOL, XRP, DOGE and ETH into a separate hold stack: a small daily budget plus a share "
                   "of winning trades' profit. Held coins are never sold by any bot and have no stop-loss.")

    WAIT_FOR_DIP_S = 6 * 3600  # budget ready but every coin running hot this long: buy anyway
    MAX_POLLS = 20

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.books: dict[str, dict[str, dict]] = {"paper": {}, "live": {}}  # mode -> symbol -> {qty, cost, buys}
        self.budget: dict[str, float] = {"paper": 0.0, "live": 0.0}
        self._clock: float | None = None
        self._accrued_at: dict[str, float] = {}
        self._ready_since: float | None = None
        self._measure: tuple | None = None
        self._measure_at = 0.0
        self.pending: dict | None = None  # the order in flight: {order, mode, before, ours_before, fills}
        self.history: list[dict] = []

    def subscriptions(self):
        return {"market.tick": self.on_tick, "trade.log": self.on_trade}

    # -- what to stack --------------------------------------------------------------
    @property
    def mode(self) -> str:
        return "live" if self.desk.is_live else "paper"

    @property
    def book(self) -> dict[str, dict]:
        return self.books[self.mode]

    def coins(self) -> list[str]:
        want = [c.strip().upper() for c in self.settings.stack_coins if c.strip()]
        syms = []
        for c in want:
            sym = c if "-" in c else f"{c}-USD"
            if sym in self.settings.symbols and self.desk.broker.supports(sym) is not False:
                syms.append(sym)
        return syms

    def value(self, mode: str | None = None) -> float:
        book = self.books[mode or self.mode]
        return sum(h["qty"] * (self.desk.prices.get(s) or h["cost"] / max(h["qty"], 1e-12)) for s, h in book.items())

    # -- budget -------------------------------------------------------------------
    def on_tick(self, ev: Event) -> None:
        ts = ev.payload.ts or now()
        self._clock = max(self._clock or ts, ts)

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        if self.settings.stack and (t.get("pnl") or 0) > 0 and t.get("mode", self.desk.mode) == self.desk.mode:
            self.budget[self.mode] += t["pnl"] * self.settings.stack_profit_share

    def _accrue(self) -> None:
        mode, clock = self.mode, self._clock or now()
        last = self._accrued_at.get(mode)
        self._accrued_at[mode] = clock
        if last is None or clock <= last:
            return
        rate = self.settings.stack_daily_usd or self.settings.stack_daily_pct * max(self.portfolio.equity, 0.0)
        self.budget[mode] += rate * min(clock - last, 86_400) / 86_400  # a long gap catches up at most a day
        cap = 3 * rate + 50  # never a big catch-up purchase
        self.budget[mode] = min(self.budget[mode], cap)

    # -- buying -------------------------------------------------------------------
    def _blocked(self) -> str | None:
        d = self.desk
        if d.halted:
            return "trading is stopped"
        if d.paused:
            return "trading is paused"
        if d.feed_down:
            return "no fresh prices"
        return None

    def min_buy(self, sym: str, price: float) -> float:
        least = self.desk.broker.min_qty(sym, price) * price * 1.05
        return max(self.settings.stack_min_buy_usd, least)

    def pick(self) -> tuple[str, float] | None:
        """The coin furthest below its share of the stack (preferably one that isn't running hot), and how much to buy."""
        coins = [s for s in self.coins() if self.desk.prices.get(s)]
        if not coins:
            return None
        total = self.value() + self.budget[self.mode]
        target = total / len(coins)

        def short(s):
            h = self.book.get(s)
            return target - (h["qty"] * self.desk.prices[s] if h else 0.0)

        snaps = self.desk.bots["risk"].snapshots
        cool = [s for s in coins if (snaps.get(s) and snaps[s].indicators.get("rsi", 50) <= self.settings.stack_max_rsi)]
        clock = self._clock or now()
        pool = cool
        if not pool:
            self._ready_since = self._ready_since or clock
            if clock - self._ready_since < self.WAIT_FOR_DIP_S:
                return None
            pool = coins  # every coin running hot for hours: buy anyway (slowly is the point, not timing)
        sym = max(pool, key=short)
        return sym, self.min_buy(sym, self.desk.prices[sym])

    async def on_cycle(self) -> None:
        if self.pending is not None:
            await self._follow()
            return
        if getattr(self, "_measure", None) and now() - self._measure_at > 10:
            await self.settle_live_qty()
        if not self.settings.stack:
            return
        self._accrue()
        self.metrics["budget"] = round(self.budget[self.mode], 2)
        self.metrics["value"] = round(self.value(), 2)
        if self._blocked():
            return
        choice = self.pick()
        if choice is None:
            return
        sym, usd = choice
        if self.budget[self.mode] < usd:
            return
        usd = max(usd, min(self.budget[self.mode], self.settings.stack_max_buy_usd))  # up to the largest single buy
        free = self.desk.free_cash()
        equity = max(self.portfolio.equity, 0.0)
        if free - usd < self.settings.stack_keep_cash * equity:
            self.metrics["waiting"] = "keeping cash free for trading"
            return
        if self.value() + usd > self.settings.stack_max_share * (equity + self.value()):
            self.metrics["waiting"] = "the stack is at its maximum share"
            return
        exe = self.desk.bots.get("execution")
        if exe is not None and (exe.working_for(sym) is not None or abs(exe.unbooked.get(sym, 0.0)) > 1e-12):
            return  # the trading team has an order on this coin: next time
        await self._buy(sym, usd)

    async def _buy(self, sym: str, usd: float) -> None:
        price = self.desk.prices[sym]
        qty = usd / (price * (1 + (self.settings.fee_bps(sym) + self.settings.slippage_bps) / 10_000))
        broker = self.desk.broker
        before = await broker.position_qty(sym) if self.desk.is_live else None
        order = Order(sym, Side.BUY, qty, f"stack-{int(time.time() * 1000)}", reason="stacker: long-term hold")
        fills = await broker.submit(order)
        if order.status is OrderStatus.REJECTED:
            self.bump("rejected")
            self.act(f"couldn't stack {sym}: {order.reason}", "warning")
            self._ready_since = None
            return
        self.pending = {"order": order, "mode": self.mode, "before": before, "fills": list(fills), "polls": 0,
                        "ours_before": self.portfolio.position(sym).qty}
        await self._follow(first=True)

    async def _follow(self, first: bool = False) -> None:
        p = self.pending
        order = p["order"]
        if not first:
            p["fills"] += await self.desk.broker.poll(order)
            p["polls"] += 1
        if not order.status.is_terminal and p["polls"] < self.MAX_POLLS:
            return
        if not order.status.is_terminal:  # still not done: cancel the rest, keep what filled
            await self.desk.broker.cancel(order)
            p["fills"] += await self.desk.broker.poll(order)
        self.pending = None
        self._finish(order, p)

    def _finish(self, order: Order, p: dict) -> None:
        fills = p["fills"]
        qty = sum(f.qty for f in fills)
        if qty <= 0:
            self.act(f"stack order for {order.symbol} didn't fill", "warning")
            return
        cost = sum(f.qty * f.price + f.commission for f in fills)
        sym, mode = order.symbol, p["mode"]
        got = qty
        if p["before"] is not None:  # real money: check what really arrived (a fee taken in the coin, too)
            self._measure, self._measure_at = (sym, p, qty), now()
        pm = self.desk.bots["portfolio"]
        pm.last_fill_ts = now()  # type: ignore[attr-defined]  # let the account's balances settle before comparing
        self.portfolio.apply_flow(-cost)  # money moved from the trading balance into the hold stack
        self.publish("funds.withdrawal", {"usd_value": cost, "source": "stack", "asset": sym,
                                          "note": "moved into the hold stack"})
        h = self.books[mode].setdefault(sym, {"qty": 0.0, "cost": 0.0, "buys": 0})
        h["qty"] += got
        h["cost"] += cost
        h["buys"] += 1
        self.budget[mode] = max(0.0, self.budget[mode] - cost)
        if mode == "live":  # the account holds these now: the records' check must leave them alone
            pm.outside[norm_symbol(sym)] = pm.outside.get(norm_symbol(sym), 0.0) + got  # type: ignore[attr-defined]
        self._ready_since = None
        px = cost / got
        self.history.append({"ts": now(), "mode": mode, "symbol": sym, "qty": got, "usd": cost, "price": px})
        del self.history[:-200]
        self.desk.journal.record(now(), "stack.buy", self.name, self.history[-1], mode)
        self.bump("buys")
        self.act(f"stacked {got:.8g} {sym.split('-')[0]} for ${cost:,.2f} (at {px:,.6g}) - held, never sold by the bots")
        self.save()

    async def settle_live_qty(self) -> None:
        """After a real-money buy, correct the stack to what the account really received (fees in the coin)."""
        m = getattr(self, "_measure", None)
        if not m:
            return
        self._measure = None
        sym, p, qty = m
        after = await self.desk.broker.position_qty(sym)
        if after is None or p["before"] is None:
            return
        ours_change = self.portfolio.position(sym).qty - p["ours_before"]
        got = after - p["before"] - ours_change
        if 0 < got < qty:  # a little less arrived (fee in the coin): hold what's really there
            h = self.books["live"][sym]
            h["qty"] -= qty - got
            pm = self.desk.bots["portfolio"]
            pm.outside[norm_symbol(sym)] = pm.outside.get(norm_symbol(sym), 0.0) - (qty - got)  # type: ignore[attr-defined]

    # -- views / persistence ------------------------------------------------------
    def summary(self) -> dict:
        mode = self.mode
        rows = []
        for sym, h in sorted(self.books[mode].items()):
            px = self.desk.prices.get(sym)
            val = h["qty"] * px if px else None
            rows.append({"symbol": sym, "qty": h["qty"], "cost": h["cost"], "avg": h["cost"] / h["qty"] if h["qty"] else 0,
                         "value": val, "pnl": (val - h["cost"]) if val is not None else None, "buys": h["buys"]})
        cost = sum(r["cost"] for r in rows)
        value = sum(r["value"] or r["cost"] for r in rows)
        nxt = None
        if self.settings.stack:
            choice = None
            coins = [s for s in self.coins() if self.desk.prices.get(s)]
            if coins:
                try:
                    choice = self.pick()
                except Exception:
                    choice = None
            if choice:
                nxt = {"symbol": choice[0], "at": choice[1]}
        return {"on": bool(self.settings.stack), "mode": mode, "coins": [c.split("-")[0] for c in self.coins()],
                "holdings": rows, "cost": cost, "value": value, "pnl": value - cost, "budget": self.budget[mode],
                "daily": self.settings.stack_daily_usd or self.settings.stack_daily_pct * max(self.portfolio.equity, 0.0),
                "profit_share": self.settings.stack_profit_share, "next": nxt, "blocked": self._blocked(),
                "recent": [x for x in self.history if x["mode"] == mode][-8:]}

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("stack", {"books": self.books, "budget": self.budget, "history": self.history[-200:],
                                 "on": self.settings.stack})

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("stack") if store is not None else None
        if not data:
            return False
        for mode in ("paper", "live"):
            self.books[mode] = {k: v for k, v in (data.get("books") or {}).get(mode, {}).items()
                                if isinstance(v, dict) and v.get("qty", 0) > 0}
            self.budget[mode] = float((data.get("budget") or {}).get(mode, 0.0))
        self.history = list(data.get("history") or [])
        if "on" in data and "TZ_STACK" not in os.environ:  # paused from the dashboard: stays paused after a restart
            self.settings.stack = bool(data["on"])
        return True

    def held(self, mode: str = "live") -> dict[str, float]:
        """Coins in the hold stack (by normalised symbol)."""
        return {norm_symbol(s): h["qty"] for s, h in self.books[mode].items() if h["qty"] > 0}
