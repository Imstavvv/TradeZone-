"""Portfolio Manager - owns the book, marks positions and manages exits."""

from __future__ import annotations

from ..core.bus import Event
from ..core.models import Alert, Fill, OrderStatus, Side, Signal, SignalKind, Tick, now
from ..core.util import norm_symbol, position_mismatches
from .base import Bot


class PortfolioBot(Bot):
    name = "portfolio"
    title = "Portfolio Manager"
    role = "Positions, P&L & Exit Management"
    mission = "🌱 Grow - protect profits and let winners compound"
    description = (
        "Books every fill, marks positions to market, and protects open trades with "
        "stop-loss, take-profit and trailing stops - issuing exit orders when they trigger."
    )
    critical = True

    RECONCILE_EVERY = 30
    FEE_TOLERANCE = 0.01  # differences up to 1% (fee-sized) on crypto are the broker's fee, not an error

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.trail_dist: dict[str, float] = {}  # initial risk distance (1R) per symbol
        self.exiting: set[str] = set()
        self.protected: set[str] = set()  # profit protection armed (stop at/above break-even)
        self.scaled_out: set[str] = set()
        self.tick_move: dict[str, float] = {}  # EWMA of absolute tick-to-tick price change
        self._last_px: dict[str, float] = {}
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
        was_protected = f.symbol in self.protected
        if pos.is_flat:
            self.exiting.discard(f.symbol)
            self.trail_dist.pop(f.symbol, None)
            self.protected.discard(f.symbol)
            self.scaled_out.discard(f.symbol)
        elif order is not None and order.reduce_only:
            self.exiting.discard(f.symbol)  # partial exit done; full exits may fire again
        if (was_flat or (closed and not pos.is_flat and not (order and order.reduce_only))) \
                and not pos.is_flat and order and order.stop_price:
            pos.stop_price = order.stop_price
            pos.take_profit = order.take_profit
            self.trail_dist[f.symbol] = abs(pos.avg_price - order.stop_price)
            self.protected.discard(f.symbol)
            self.scaled_out.discard(f.symbol)
        pos_after = pos.qty
        if was_flat:
            action = "open"
        elif pos.is_flat:
            action = "close"
        elif closed and (pos_after > 0) != (closed.side == "long"):
            action = "flip"
        elif closed:
            action = "partial" if order is not None and order.reduce_only else "reduce"
        else:
            action = "add"
        self.publish("trade.log", {
            "ts": f.ts, "mode": self.desk.mode, "symbol": f.symbol, "side": f.side.value, "action": action,
            "qty": f.qty, "price": f.price, "notional": f.qty * f.price, "fee": f.commission,
            "pnl": closed.pnl if closed else None, "position_after": pos_after,
            "avg_price": pos.avg_price if not pos.is_flat else None,
            "reason": order.reason if order else "", "order_id": f.order_id, "fill_id": f.id,
            "broker": self.desk.broker.name,
        })
        if closed:
            self.bump("closed_trades")
            self.bump("wins" if closed.pnl > 0 else "losses")
            if was_protected:
                self.bump("protected_exits")
                if closed.pnl < 0:  # only possible through a price gap or bad slippage
                    self.bump("winners_turned_losers")
                    self.publish("alert", Alert(
                        "warning", f"{closed.symbol} was in profit but closed at {closed.pnl:+,.2f} "
                        "(price gapped through the protective stop)", self.name))
            self.publish("trade.closed", closed)
            self.act(f"closed {closed.side} {closed.qty:g} {closed.symbol} P&L {closed.pnl:+,.2f}")
        else:
            self.act(f"opened/added {f.symbol} now {pos.qty:g} @ {pos.avg_price:,.2f}")
        self.publish("portfolio.update", p.to_dict())

    def on_order_update(self, ev: Event) -> None:
        o = ev.payload
        if o.reduce_only and o.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            self.exiting.discard(o.symbol)  # allow the exit to be retried

    def _exit(self, symbol: str, reason: str, fraction: float = 1.0) -> None:
        pos = self.portfolio.position(symbol)
        if pos.is_flat or symbol in self.exiting:
            return
        if fraction >= 1.0:
            self.exiting.add(symbol)
        side = Side.SELL if pos.qty > 0 else Side.BUY
        self.publish("signal.proposed",
                     Signal(symbol, side, 1.0, reason, self.name, SignalKind.EXIT, fraction=fraction))
        self.bump("exits_triggered")
        self.act(f"EXIT {symbol}: {reason}", "warning")

    def on_tick(self, ev: Event) -> None:
        t: Tick = ev.payload
        p = self.portfolio
        last = self._last_px.get(t.symbol)
        if last is not None:
            move = abs(t.price - last)
            prev = self.tick_move.get(t.symbol, move)
            self.tick_move[t.symbol] = 0.95 * prev + 0.05 * move
        self._last_px[t.symbol] = t.price
        p.mark(t.symbol, t.price)
        pos = p.position(t.symbol)
        if pos.is_flat:
            return
        long = pos.qty > 0
        d = 1 if long else -1
        hw = pos.high_water if pos.high_water is not None else t.price
        hw = max(hw, t.price) if long else min(hw, t.price)
        pos.high_water = hw
        tighten = (lambda a, b: max(a, b)) if long else (lambda a, b: min(a, b))
        r = self.trail_dist.get(t.symbol)
        if self.settings.trailing_stop and pos.stop_price is not None and r:
            pos.stop_price = tighten(pos.stop_price, hw - d * r)
        if r and pos.stop_price is not None:
            self._protect_profit(t.symbol, pos, t.price, hw, r, d, tighten)
        if pos.stop_price is not None and ((long and t.price <= pos.stop_price) or (not long and t.price >= pos.stop_price)):
            self._exit(t.symbol, f"stop hit at {t.price:,.2f} (stop {pos.stop_price:,.2f})")
        elif pos.take_profit is not None and ((long and t.price >= pos.take_profit) or (not long and t.price <= pos.take_profit)):
            if self._runner_mode(t.symbol) and t.symbol not in self.scaled_out:
                self._start_runner(t.symbol, pos, t.price, d, tighten)
            else:
                self._exit(t.symbol, f"take-profit hit at {t.price:,.6g}")

    def _is_skim(self, symbol: str) -> bool:
        sk = self.desk.bots.get("skimmer")
        return bool(sk and sk.owns(symbol))

    def _runner_mode(self, symbol: str | None = None) -> bool:
        s = self.settings
        if symbol is not None and self._is_skim(symbol):
            return False  # a skim is a quick in-and-out: the whole thing goes at its small target
        return s.runners and s.stop_method == "percent" and s.runner_sell_fraction < 1

    def _start_runner(self, sym, pos, price, d, tighten) -> None:
        """Target reached: bank part of it, move the stop into profit and let the rest run."""
        s = self.settings
        tp_pct = (pos.take_profit / pos.avg_price - 1) * d if pos.take_profit and pos.avg_price else s.plan_pcts(sym, self.quick)[1]
        lock = tp_pct * s.runner_lock_share
        self.scaled_out.add(sym)
        self.protected.add(sym)
        pos.take_profit = None  # the rest has no ceiling: the trailing stop decides
        pos.stop_price = tighten(pos.stop_price, pos.avg_price * (1 + d * lock))
        self.bump("runners")
        self.bump("partial_profits")
        self.act(f"{sym} hit +{tp_pct:.0%}: banking {s.runner_sell_fraction:.0%}, runner stop "
                 f"now {pos.stop_price:,.6g} (locks +{lock:.1%})")
        self._exit(sym, f"take-profit +{tp_pct:.0%} at {price:,.6g} - letting the rest run",
                   s.runner_sell_fraction)

    def round_trip_cost(self, price: float, symbol: str = "") -> float:
        """Per-unit cost of getting in and out (fees + slippage both ways), with a small margin."""
        return self.settings.round_trip_cost(symbol, price)

    def _protect_profit(self, sym, pos, price, hw, r, d, tighten) -> None:
        s = self.settings
        entry = pos.avg_price
        cost = self.round_trip_cost(entry, sym)
        # cushion so one ordinary tick can't jump from above the stop to below break-even
        cushion = cost + 2.0 * self.tick_move.get(sym, 0.0)
        peak_gain = (hw - entry) * d
        # 1) once comfortably green, the stop can never again sit below break-even + costs
        trigger_r = s.flip_breakeven_r if self.quick else s.breakeven_trigger_r
        if peak_gain >= max(trigger_r * r, 2 * cushion):
            floor = entry + d * max(cushion, s.profit_lock_pct * peak_gain)
            before = pos.stop_price
            pos.stop_price = tighten(pos.stop_price, floor)
            if sym not in self.protected:
                self.protected.add(sym)
                self.bump("profit_locks")
                self.act(f"{sym} in profit: stop raised to {pos.stop_price:,.4g} (locks gain)")
            elif pos.stop_price != before:
                self.metrics["last_lock"] = sym
        # 2) bank part of the winner at +scale_out_r (in runner mode this happens at the target)
        if (s.scale_out_fraction > 0 and not self._runner_mode() and not self._is_skim(sym) and sym not in self.scaled_out
                and (price - entry) * d >= s.scale_out_r * r + cost):
            self.scaled_out.add(sym)
            self.bump("partial_profits")
            self._exit(sym, f"partial take-profit +{s.scale_out_r:g}R at {price:,.4g}", s.scale_out_fraction)

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
        ours = {p.symbol: p.qty for p in self.portfolio.open_positions}
        problems = []
        for sym, (a, b) in position_mismatches(ours, broker_pos).items():
            pos = next((p for p in self.portfolio.open_positions if norm_symbol(p.symbol) == sym), None)
            # fee taken in the coin: the broker holds slightly less than we bought -> adopt its number
            if pos is not None and self.settings.is_crypto(pos.symbol) and 0 < abs(b) < abs(a) \
                    and (abs(a) - abs(b)) <= abs(a) * self.FEE_TOLERANCE and (a > 0) == (b > 0):
                delta = b - a
                pos.qty = b
                self.publish("position.adjusted", {"symbol": pos.symbol, "delta": delta,
                                                   "reason": "broker fee taken in coin"})
                self.act(f"{pos.symbol}: adopted broker quantity {b:g} (fee in coin {delta:+g})")
                continue
            problems.append(f"{sym}: ledger {a:g} vs broker {b:g}")
        self.metrics["positions_reconciled"] = not problems
        if not problems and self.desk.paused and self.desk.pause_reason.startswith("position mismatch"):
            self.desk.resume()  # it was only a mismatch, and the books match the broker again
            self.publish("alert", Alert("info", "Positions match the broker again - trading resumed", self.name))
            self.act("positions match the broker again - trading resumed")
        if problems:
            msg = "; ".join(problems)
            self.act(f"POSITION MISMATCH: {msg}", "critical")
            self.publish("alert", Alert("critical", f"Broker position mismatch - trading paused: {msg}", self.name))
            self.desk.pause(f"position mismatch: {msg}")
        return problems

    def _free_stale_slots(self) -> None:
        """Quick flips: a trade that went nowhere for hours but is in profit after fees is closed,
        so its slot can go to a better chance. Losers stay with their stop; runners keep running."""
        s = self.settings
        t = now()
        for pos in list(self.portfolio.open_positions):
            sym = pos.symbol
            if sym in self.exiting or sym in self.scaled_out or not pos.opened_ts or self._is_skim(sym):
                continue
            if self.quick:
                hours = s.flip_meme_max_hold_hours if s.is_meme(sym) else s.flip_max_hold_hours
            else:
                hours = s.meme_max_hold_hours if s.is_meme(sym) else s.max_hold_hours
            if hours <= 0 or t - pos.opened_ts < hours * 3600 or pos.last_price is None:
                continue
            net = s.net_if_sold(pos, pos.last_price)  # after both fees
            if net > 0 and net >= self.desk.min_take():  # ...and worth its share of a transfer fee
                self.bump("stale_flips")
                self._exit(sym, f"quick flip: +{net:,.2f} after {hours:g} h without reaching the target - freeing the slot")

    async def on_cycle(self) -> None:
        p = self.portfolio
        p.update_watermarks()
        self._cycles += 1
        if self._cycles % 30 == 0:
            self._free_stale_slots()
        self.metrics.update(
            {
                "protected_now": len(self.protected),
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
            capped = self.desk.is_live and self.settings.live_max_capital > 0
            if acct and acct.get("equity") and not capped:  # a capital cap makes equities differ by design
                diff = abs(acct["equity"] - p.equity) / max(acct["equity"], 1)
                self.metrics["broker_equity"] = acct["equity"]
                if diff > 0.02:
                    self.publish(
                        "alert",
                        Alert("warning", f"ledger equity differs from broker by {diff:.1%}", self.name),
                    )
