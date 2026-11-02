"""Portfolio Manager - owns the book, marks positions and manages exits."""

from __future__ import annotations

from ..core.bus import Event
from ..core.models import Alert, Fill, OrderStatus, Side, Signal, SignalKind, Tick
from .base import Bot


class PortfolioBot(Bot):
    name = "portfolio"
    title = "Portfolio Manager"
    role = "Positions, P&L & Exit Management"
    description = (
        "Books every fill, marks positions to market, and protects open trades with "
        "stop-loss, take-profit and trailing stops - issuing exit orders when they trigger."
    )
    critical = True

    RECONCILE_EVERY = 30

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.trail_dist: dict[str, float] = {}
        self.exiting: set[str] = set()
        self._cycles = 0

    def subscriptions(self):
        return {
            "market.tick": self.on_tick,
            "order.filled": self.on_fill,
            "order.update": self.on_order_update,
        }

    def on_fill(self, ev: Event) -> None:
        f: Fill = ev.payload
        p = self.portfolio
        was_flat = p.position(f.symbol).is_flat
        closed = p.apply_fill(f)
        pos = p.position(f.symbol)
        exe = self.desk.bots.get("execution")
        order = exe.orders.get(f.order_id) if exe else None
        if pos.is_flat:
            self.exiting.discard(f.symbol)
            self.trail_dist.pop(f.symbol, None)
        elif (was_flat or closed) and order and order.stop_price:
            pos.stop_price = order.stop_price
            pos.take_profit = order.take_profit
            self.trail_dist[f.symbol] = abs(pos.avg_price - order.stop_price)
        if closed:
            self.bump("closed_trades")
            self.bump("wins" if closed.pnl > 0 else "losses")
            self.publish("trade.closed", closed)
            self.act(f"closed {closed.side} {closed.qty:g} {closed.symbol} P&L {closed.pnl:+,.2f}")
        else:
            self.act(f"opened/added {f.symbol} now {pos.qty:g} @ {pos.avg_price:,.2f}")
        self.publish("portfolio.update", p.to_dict())

    def on_order_update(self, ev: Event) -> None:
        o = ev.payload
        if o.reduce_only and o.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            self.exiting.discard(o.symbol)  # allow the exit to be retried

    def _exit(self, symbol: str, reason: str) -> None:
        pos = self.portfolio.position(symbol)
        if pos.is_flat or symbol in self.exiting:
            return
        self.exiting.add(symbol)
        side = Side.SELL if pos.qty > 0 else Side.BUY
        self.publish("signal.proposed", Signal(symbol, side, 1.0, reason, self.name, SignalKind.EXIT))
        self.bump("exits_triggered")
        self.act(f"EXIT {symbol}: {reason}", "warning")

    def on_tick(self, ev: Event) -> None:
        t: Tick = ev.payload
        p = self.portfolio
        p.mark(t.symbol, t.price)
        pos = p.position(t.symbol)
        if pos.is_flat:
            return
        long = pos.qty > 0
        if self.settings.trailing_stop and pos.stop_price is not None and t.symbol in self.trail_dist:
            hw = pos.high_water if pos.high_water is not None else t.price
            hw = max(hw, t.price) if long else min(hw, t.price)
            pos.high_water = hw
            trail = hw - self.trail_dist[t.symbol] if long else hw + self.trail_dist[t.symbol]
            pos.stop_price = max(pos.stop_price, trail) if long else min(pos.stop_price, trail)
        if pos.stop_price is not None and ((long and t.price <= pos.stop_price) or (not long and t.price >= pos.stop_price)):
            self._exit(t.symbol, f"stop hit at {t.price:,.2f} (stop {pos.stop_price:,.2f})")
        elif pos.take_profit is not None and ((long and t.price >= pos.take_profit) or (not long and t.price <= pos.take_profit)):
            self._exit(t.symbol, f"take-profit hit at {t.price:,.2f}")

    def flatten_all(self, reason: str) -> int:
        n = 0
        for pos in list(self.portfolio.open_positions):
            self.exiting.discard(pos.symbol)
            self._exit(pos.symbol, reason)
            n += 1
        return n

    async def reconcile_positions(self) -> list[str]:
        """Compare ledger positions with the broker's. Mismatch pauses trading."""
        exe = self.desk.bots.get("execution")
        if exe is not None and exe.working:
            return []  # in-flight orders legitimately cause temporary differences
        broker_pos = await self.desk.broker.positions()
        if broker_pos is None:
            return []
        norm = lambda s: s.replace("-", "").replace("/", "").upper()  # noqa: E731
        theirs = {norm(k): v for k, v in broker_pos.items()}
        ours = {norm(p.symbol): p.qty for p in self.portfolio.positions.values() if not p.is_flat}
        problems = [
            f"{sym}: ledger {ours.get(sym, 0.0):g} vs broker {theirs.get(sym, 0.0):g}"
            for sym in set(ours) | set(theirs)
            if abs(ours.get(sym, 0.0) - theirs.get(sym, 0.0)) > 1e-6 * max(1.0, abs(theirs.get(sym, 0.0)))
        ]
        self.metrics["positions_reconciled"] = not problems
        if problems:
            msg = "; ".join(problems)
            self.act(f"POSITION MISMATCH: {msg}", "critical")
            self.publish("alert", Alert("critical", f"Broker position mismatch - trading paused: {msg}", self.name))
            self.desk.pause(f"position mismatch: {msg}")
        return problems

    async def on_cycle(self) -> None:
        p = self.portfolio
        p.update_watermarks()
        self._cycles += 1
        self.metrics.update(
            {
                "equity": round(p.equity, 2),
                "open_positions": len(p.open_positions),
                "realized_pnl": round(p.realized_pnl, 2),
                "unrealized_pnl": round(p.unrealized_pnl, 2),
            }
        )
        self.publish("portfolio.update", p.to_dict())
        if self._cycles % self.RECONCILE_EVERY == 0:
            await self.reconcile_positions()
            acct = await self.desk.broker.account()
            if acct and acct.get("equity"):
                diff = abs(acct["equity"] - p.equity) / max(acct["equity"], 1)
                self.metrics["broker_equity"] = acct["equity"]
                if diff > 0.02:
                    self.publish(
                        "alert",
                        Alert("warning", f"ledger equity differs from broker by {diff:.1%}", self.name),
                    )
