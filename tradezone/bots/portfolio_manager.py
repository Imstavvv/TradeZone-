"""Portfolio Manager - owns the book, marks positions and manages exits."""

from __future__ import annotations

from collections import deque

from ..core.bus import Event
from ..core.models import Alert, Fill, OrderStatus, Side, Signal, SignalKind, Tick, now
from ..core.util import norm_symbol
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
    EXIT_RETRY_S = 30.0  # a refused exit is tried again after this, not on every tick
    SETTLE_S = 20.0  # wait this long after a fill before comparing with the broker
    FIXES_BEFORE_PAUSE = 3  # record corrections within an hour that mean something is really wrong
    FEE_TOLERANCE = 0.01  # differences up to 1% (fee-sized) on crypto are the broker's fee, not an error

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.trail_dist: dict[str, float] = {}  # initial risk distance (1R) per symbol
        self.exiting: set[str] = set()
        self.calm_exits: set[str] = set()  # full exits that are only taking profit (an urgent one may replace them)
        self.protected: set[str] = set()  # profit protection armed (stop at/above break-even)
        self.scaled_out: set[str] = set()
        self.tick_move: dict[str, float] = {}  # EWMA of absolute tick-to-tick price change
        self._last_px: dict[str, float] = {}
        self._cycles = 0
        self.outside: dict[str, float] = {}  # coins in the account that are the owner's, not the bots' (by norm symbol)
        self._suspect: dict[str, int] = {}  # mismatches seen on consecutive checks
        self._noted: dict[str, int] = {}
        self._fixes: deque[float] = deque()
        self.last_fill_ts = 0.0
        self.patient: set[str] = set()  # real-money trades with patient exits
        self.tp_steps: dict[str, int] = {}  # symbol -> how many +10% steps the take-profit has moved
        self.watch: dict[str, float] = {}  # symbol -> since when it's been down past its usual stop
        self._held_note: dict[str, float] = {}
        self._retry_at: dict[str, float] = {}  # symbol -> don't resend a refused exit before
        self._cash_seen: float | None = None  # a cash difference seen on the last check (must repeat to count)
        self._equity_note = 0.0

    def subscriptions(self):
        return {
            "market.tick": self.on_tick,
            "market.snapshot": self.on_snapshot,
            "order.filled": self.on_fill,
            "order.update": self.on_order_update,
            "signal.rejected": self.on_rejected,
        }

    def on_rejected(self, ev: Event) -> None:
        """One of our exits was turned down (e.g. its profit doesn't beat the fees yet): it may be tried again
        later - the trade isn't left marked as 'on its way out', which would hold back its other exits."""
        d = ev.payload
        sig = d.get("signal") if isinstance(d, dict) else None
        if not isinstance(sig, dict) or sig.get("source") != self.name or sig.get("kind") != SignalKind.EXIT.value:
            return
        sym = sig.get("symbol")
        self.exiting.discard(sym)
        self.calm_exits.discard(sym)
        self._retry_at[sym] = now() + self.EXIT_RETRY_S

    def on_fill(self, ev: Event) -> None:
        f: Fill = ev.payload
        self.last_fill_ts = now()
        p = self.portfolio
        was_flat = p.position(f.symbol).is_flat
        closed = p.apply_fill(f)
        pos = p.position(f.symbol)
        exe = self.desk.bots.get("execution")
        order = exe.orders.get(f.order_id) if exe else None
        was_protected = f.symbol in self.protected
        if was_flat and not pos.is_flat and self.settings.patient(self.desk.is_live) and not self._is_skim(f.symbol):
            self.patient.add(f.symbol)
        if pos.is_flat or was_flat:
            self.tp_steps.pop(f.symbol, None)  # a new trade starts counting steps from its own buy price
        if pos.is_flat:
            self.patient.discard(f.symbol)
            self.watch.pop(f.symbol, None)
            self.exiting.discard(f.symbol)
            self.calm_exits.discard(f.symbol)
            self.trail_dist.pop(f.symbol, None)
            self.protected.discard(f.symbol)
            self.scaled_out.discard(f.symbol)
        elif order is not None and order.reduce_only:
            self.exiting.discard(f.symbol)  # partial exit done; full exits may fire again
        if (was_flat or (closed and not pos.is_flat and not (order and order.reduce_only))) \
                and not pos.is_flat and order and order.stop_price:
            pos.stop_price = order.stop_price
            pos.take_profit = order.take_profit
            # trailing stop / break-even lock work from the normal plan's stop, not a wide -33% safety stop
            dist = abs(pos.avg_price - order.stop_price)
            if self.settings.wide_stop:
                dist = min(dist, pos.avg_price * self.settings.plan_pcts(f.symbol, self.quick)[0])
            self.trail_dist[f.symbol] = dist
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
        if o.reduce_only and o.maker and o.status is OrderStatus.CANCELED:
            return  # a profit-taking sell that waited at the ask: the rest is sold at market (or an urgent exit took over)
        if o.reduce_only and o.status in (OrderStatus.REJECTED, OrderStatus.CANCELED):
            self.exiting.discard(o.symbol)  # allow the exit to be retried...
            self._retry_at[o.symbol] = now() + self.EXIT_RETRY_S  # ...but not on every single tick
            if o.status is OrderStatus.REJECTED and "minimum" in (o.reason or ""):
                self._write_off_dust(o.symbol)

    def _write_off_dust(self, symbol: str) -> None:
        """A leftover too small for the venue to sell isn't a trade: take it off the bots' books, so it can't
        block new trades or fill the open-position slots. (It stays in your account, counted as yours.)"""
        pos = self.portfolio.position(symbol)
        price = self.desk.prices.get(symbol) or pos.last_price or pos.avg_price
        least = max(self.desk.broker.min_qty(symbol, price), 1.0 / price if price > 0 else 0.0)
        if pos.is_flat or abs(pos.qty) >= least:
            return
        qty = pos.qty
        pos.qty, pos.avg_price, pos.entry_fees = 0.0, 0.0, 0.0
        pos.stop_price = pos.take_profit = pos.high_water = pos.opened_ts = None
        for book in (self.trail_dist, self._retry_at):
            book.pop(symbol, None)
        self.exiting.discard(symbol)
        self.protected.discard(symbol)
        self.scaled_out.discard(symbol)
        self.publish("position.adjusted", {"symbol": symbol, "delta": -qty, "reason": "dust below the venue's minimum"})
        self.act(f"{symbol}: {qty:.8g} left over is below the venue's minimum sale - taken off the books (it's yours)")

    def _exit(self, symbol: str, reason: str, fraction: float = 1.0, calm: bool = False, force: bool = False) -> None:
        pos = self.portfolio.position(symbol)
        if pos.is_flat or now() < self._retry_at.get(symbol, 0.0):
            return
        if not force and self.holding_loser(pos):
            return  # never sell at a loss: held until it recovers
        if symbol in self.exiting and (calm or symbol not in self.calm_exits):
            return  # already on its way out (an urgent exit may still replace one that's only taking profit)
        if fraction >= 1.0:
            self.exiting.add(symbol)
            (self.calm_exits.add if calm else self.calm_exits.discard)(symbol)
        side = Side.SELL if pos.qty > 0 else Side.BUY
        self.publish("signal.proposed",
                     Signal(symbol, side, 1.0, reason, self.name, SignalKind.EXIT, fraction=fraction, calm=calm,
                            force=force))
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
        if self.settings.trailing_stop and pos.stop_price is not None and r \
                and (not self.settings.wide_stop or (hw - pos.avg_price) * d >= r):  # wide stop: trail once up 1R
            pos.stop_price = tighten(pos.stop_price, hw - d * r)
        if self.settings.fee_rule_stops and pos.stop_price is not None and (pos.stop_price - pos.avg_price) * d > 0:
            # a stop already in profit never sits where selling would keep less than the fees: up to the fee floor
            floor = self.fee_floor(t.symbol, pos.avg_price, d)
            if (floor - pos.stop_price) * d > 0 and (t.price - floor) * d > 0:
                pos.stop_price = floor
        if r and pos.stop_price is not None:
            self._protect_profit(t.symbol, pos, t.price, hw, r, d, tighten)
        self._step_take_profit(t.symbol, pos, t.price, d, tighten)
        if pos.stop_price is not None and ((long and t.price <= pos.stop_price) or (not long and t.price >= pos.stop_price)):
            self._exit(t.symbol, f"stop hit at {t.price:,.2f} (stop {pos.stop_price:,.2f})")
        elif pos.take_profit is not None and ((long and t.price >= pos.take_profit) or (not long and t.price <= pos.take_profit)):
            if self._runner_mode(t.symbol) and t.symbol not in self.scaled_out:
                self._start_runner(t.symbol, pos, t.price, d, tighten)
            else:
                self._exit(t.symbol, f"take-profit hit at {t.price:,.6g}", calm=True)

    def _step_take_profit(self, sym, pos, price, d, tighten) -> None:
        """Past +10% from the buy price (and every further 10%), move the take-profit another step out and
        lock in half of the step with the stop - the trade keeps going instead of being sold at the old target."""
        s = self.settings
        if not s.tp_step or s.tp_step_pct <= 0 or self._is_skim(sym) or not pos.avg_price:
            return
        gain = (price / pos.avg_price - 1) * d
        n = int(gain / s.tp_step_pct + 1e-9)
        if n < 1 or n <= self.tp_steps.get(sym, 0):
            return
        self.tp_steps[sym] = n
        entry, cost = pos.avg_price, self.round_trip_cost(pos.avg_price, sym)
        lock = entry * (1 + d * s.tp_step_pct * n * s.tp_step_lock)
        floor = self.fee_floor(sym, entry, d)
        if (floor - lock) * d > 0 and (price - floor) * d > 0:  # (never at or past the current price)
            lock = floor
        pos.stop_price = lock if pos.stop_price is None else tighten(pos.stop_price, lock)
        self.protected.add(sym)
        moved = ""
        if pos.take_profit is not None:  # a runner has no ceiling: only its stop steps up
            new_tp = entry * (1 + d * s.tp_step_pct * (n + 1)) + d * cost
            pos.take_profit = max(pos.take_profit, new_tp) if d > 0 else min(pos.take_profit, new_tp)
            moved = f", take-profit moved to {pos.take_profit:,.6g} (+{s.tp_step_pct * (n + 1):.0%})"
        self.bump("tp_steps")
        self.act(f"{sym} is +{gain:.1%} past the buy price: stop locks +{s.tp_step_pct * n * s.tp_step_lock:.0%}{moved}")

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
        lock_px = pos.avg_price * (1 + d * lock)
        floor = self.fee_floor(sym, pos.avg_price, d)  # the runner's stop keeps more profit than the fees
        lock_px = max(lock_px, floor) if d > 0 else min(lock_px, floor)
        if (price - lock_px) * d > 0:  # (never at or past the current price)
            pos.stop_price = tighten(pos.stop_price, lock_px)
        self.bump("runners")
        self.bump("partial_profits")
        self.act(f"{sym} hit +{tp_pct:.0%}: banking {s.runner_sell_fraction:.0%}, runner stop "
                 f"now {pos.stop_price:,.6g} (locks +{lock:.1%})")
        self._exit(sym, f"take-profit +{tp_pct:.0%} at {price:,.6g} - letting the rest run",
                   s.runner_sell_fraction, calm=True)

    def on_snapshot(self, ev: Event) -> None:
        """Patient exits, once per bar: a trade that's down past its usual stop is held for a comeback -
        unless momentum and volume both say it isn't coming back. It's closed as soon as it's back in profit."""
        snap = ev.payload
        sym = snap.symbol
        if snap.historical or sym not in self.patient or sym in self.exiting:
            return
        pos = self.portfolio.position(sym)
        if pos.is_flat:
            self.patient.discard(sym)
            self.watch.pop(sym, None)
            return
        s, price, ind = self.settings, snap.price, snap.indicators
        d = 1 if pos.qty > 0 else -1
        loss = (pos.avg_price - price) / pos.avg_price * d  # > 0: losing
        usual = s.plan_pcts(sym, self.quick)[0]  # where the usual tight stop would have been
        if loss >= usual and sym not in self.watch:
            self.watch[sym] = now()
            self.act(f"{sym} is down {loss:.1%}: holding for a comeback (cut only if momentum and volume say it won't)")
        if sym not in self.watch:
            return
        from .quant import momentum_score
        mom = momentum_score(snap) * d  # < 0: momentum against the trade
        vr = ind.get("volume_ratio")
        if vr is not None:
            heavy, vol_txt = vr >= s.no_return_volume, f"volume {vr:.1f}x usual"
        else:  # the feed doesn't report volume: a break of the recent swing level stands in for it
            heavy = price < ind.get("swing_low", 0) if d > 0 else price > ind.get("swing_high", float("inf"))
            vol_txt = "breaking the recent low" if d > 0 else "breaking the recent high"
        net = s.net_if_sold(pos, price)
        net_pct = net / (pos.avg_price * abs(pos.qty))
        if net_pct >= s.patient_rescue_pct and (not s.fee_rule_rescue or s.beats_fees(pos, price)):  # beats its fees
            self.watch.pop(sym, None)
            self.bump("patient_rescues")
            return self._exit(sym, f"came back: closed at +{net:,.2f} after fees after being down")
        if net > 0:  # back in profit after fees, just not beating them yet: never cut - wait for it (its stop protects it)
            return
        if self.holding_loser(pos):
            return  # never sell at a loss: held until it comes back, whatever momentum says
        if mom <= s.no_return_momentum and heavy:
            self.bump("patient_cuts")
            return self._exit(sym, f"no comeback in sight: momentum {mom:+.2f} against it, {vol_txt} - cutting at -{loss:.1%}")
        waited = now() - self.watch[sym]
        if waited > s.patient_max_hours * 3600 and mom <= 0:
            self.bump("patient_cuts")
            return self._exit(sym, f"waited {waited / 3600:.0f} h without a comeback - letting it go at -{loss:.1%}")
        if now() - self._held_note.get(sym, 0) > 3600:
            self._held_note[sym] = now()
            self.act(f"holding {sym} at -{loss:.1%}: momentum {mom:+.2f}, {vol_txt} - no sign it won't come back")

    def fee_floor(self, sym: str, entry: float, d: int) -> float:
        """The lowest a profit-locking stop may sit: selling there still keeps more profit than the fees."""
        keep = 1.0 + (max(0.0, self.settings.profit_over_fees) if self.settings.fee_rule_stops else 0.0)
        return entry + d * (keep * self.round_trip_cost(entry, sym) + 2.0 * self.tick_move.get(sym, 0.0))

    def round_trip_cost(self, price: float, symbol: str = "") -> float:
        """Per-unit cost of getting in and out (fees + slippage both ways), with a small margin."""
        return self.settings.round_trip_cost(symbol, price)

    def _protect_profit(self, sym, pos, price, hw, r, d, tighten) -> None:
        s = self.settings
        entry = pos.avg_price
        cost = self.round_trip_cost(entry, sym)
        # cushion so one ordinary tick can't jump from above the stop to below break-even
        # the locked stop keeps more profit than the fees: break-even + both fees, and the fees again on top
        cushion = abs(self.fee_floor(sym, entry, d) - entry)
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
                and (price - entry) * d >= s.scale_out_r * r + cost and s.beats_fees(pos, price)):
            self.scaled_out.add(sym)
            self.bump("partial_profits")
            self._exit(sym, f"partial take-profit +{s.scale_out_r:g}R at {price:,.4g}", s.scale_out_fraction, calm=True)

    def holding_loser(self, pos) -> bool:
        """Never sell at a loss (when on): a trade below break-even after fees is held, and said so once an hour."""
        s = self.settings
        price = self.desk.prices.get(pos.symbol) or pos.last_price
        if not s.never_sell_at_loss or not price or s.net_if_sold(pos, price) >= 0:
            return False
        if now() - self._held_note.get(f"loss|{pos.symbol}", 0) > 3600:  # (said - and counted - once an hour)
            self._held_note[f"loss|{pos.symbol}"] = now()
            self.act(f"holding {pos.symbol}: it's below break-even after fees, and the bots never sell at a loss")
            self.bump("held_not_sold")
        return True

    def flatten_all(self, reason: str, force: bool = True) -> int:
        n = 0
        for pos in list(self.portfolio.open_positions):
            self.exiting.discard(pos.symbol)
            self._exit(pos.symbol, reason, force=force)
            n += 1
        return n

    async def reconcile_positions(self) -> list[str]:
        """Compare the bots' records with what the broker really holds, and fix the records instead of
        stopping trading. Returns what had to be corrected.

        * A difference must show up on two checks in a row (balances can lag a fill by a few seconds),
          and nothing may be in flight.
        * **More** in the account than the bots hold (your own coins, Kraken Rewards interest, a deposit):
          noted as yours and left alone.
        * **Less** than the bots' records (sold or moved outside TradeZone): the records are updated as a sale
          at the market price, so the books match the account again.
        * Trading only pauses if the records needed correcting 3+ times in an hour (something is wrong)."""
        exe = self.desk.bots.get("execution")
        if exe is not None and (exe.working or any(abs(v) > 1e-12 for v in exe.unbooked.values())):
            return []  # in-flight orders legitimately cause temporary differences
        if getattr(self.desk.bots.get("stacker"), "pending", None) is not None:
            return []  # a hold-stack purchase is in flight
        if now() - self.last_fill_ts < self.SETTLE_S:
            return []  # let the broker's balances catch up with the latest fill
        broker_pos = await self.desk.broker.positions()
        if broker_pos is None:
            return []
        ours = {norm_symbol(p.symbol): p for p in self.portfolio.open_positions}
        theirs = {norm_symbol(k): v for k, v in broker_pos.items()}
        diff = {}
        for sym in set(ours) | set(theirs) | set(self.outside):
            a = ours[sym].qty if sym in ours else 0.0
            b = theirs.get(sym, 0.0) - self.outside.get(sym, 0.0)
            if abs(a - b) > 1e-6 * max(1.0, abs(b)):
                diff[sym] = (a, b)
        for sym in list(self._suspect):
            if sym not in diff:
                del self._suspect[sym]
        fixed = []
        for sym, (a, b) in diff.items():
            self._suspect[sym] = self._suspect.get(sym, 0) + 1
            if self._suspect[sym] < 2:
                continue  # seen once: wait for the next check before believing it
            del self._suspect[sym]
            pos = ours.get(sym)
            if pos is not None and self.settings.is_crypto(pos.symbol) and 0 < abs(b) < abs(a) \
                    and (abs(a) - abs(b)) <= abs(a) * self.FEE_TOLERANCE and (a > 0) == (b > 0):
                delta = b - a  # fee taken in the coin: the broker holds slightly less than we bought
                pos.qty = b
                self.publish("position.adjusted", {"symbol": pos.symbol, "delta": delta, "reason": "broker fee taken in coin"})
                self.act(f"{pos.symbol}: adopted broker quantity {b:g} (fee in coin {delta:+g})")
                continue
            if (a >= 0 and b > a) or (a <= 0 and b < a):  # more in the account than the bots hold: yours
                self.outside[sym] = self.outside.get(sym, 0.0) + (b - a)
                if abs(self.outside[sym]) < 1e-12:
                    del self.outside[sym]
                day = int(now() // 86_400)
                if self._noted.get(sym) != day:
                    self._noted[sym] = day
                    self.publish("alert", Alert("info", f"Your account holds {abs(b - a):.8g} more {sym} than the bots "
                                                        "bought (your own coins, rewards or a deposit) - left alone", self.name))
                self.act(f"{sym}: {b - a:+.8g} in the account isn't the bots' - left alone")
                continue
            fixed.append(self._external_change(pos, a, b))
        if fixed:
            self._fixes.extend([now()] * len(fixed))
            while self._fixes and now() - self._fixes[0] > 3600:
                self._fixes.popleft()
            msg = "; ".join(fixed)
            if len(self._fixes) >= self.FIXES_BEFORE_PAUSE:
                self.publish("alert", Alert("critical", f"The records had to be corrected {len(self._fixes)} times in "
                                                        f"an hour ({msg}) - trading paused so you can check", self.name))
                self.desk.pause(f"position mismatch: corrected {len(self._fixes)} times in an hour")
            else:
                self.publish("alert", Alert("warning", f"Records updated to match your account: {msg}. "
                                                       "Trading continues.", self.name))
        self.metrics["positions_reconciled"] = not diff
        if not diff and self.desk.paused and self.desk.pause_reason.startswith("position mismatch") \
                and len(self._fixes) < self.FIXES_BEFORE_PAUSE:
            self.desk.resume()  # it was only a mismatch, and the books match the broker again
            self.publish("alert", Alert("info", "Positions match the broker again - trading resumed", self.name))
            self.act("positions match the broker again - trading resumed")
        return fixed

    CASH_TOLERANCE_USD = 1.0  # cash differences smaller than this (and 0.5% of the balance) are rounding

    def sync_cash(self, acct: dict) -> float:
        """Keep the bots' real-money balance in step with the account: money you add or take out at the exchange
        is picked up (as a deposit / withdrawal, so it isn't counted as profit or loss). The difference must show
        up on two checks in a row with nothing in flight. With a capital cap, only money taken out is followed
        (the bots must never count on cash that's gone). Returns the amount applied."""
        p = self.portfolio
        exe = self.desk.bots.get("execution")
        if exe is not None and (exe.working or any(abs(v) > 1e-12 for v in exe.unbooked.values())) \
                or getattr(self.desk.bots.get("stacker"), "pending", None) is not None:
            self._cash_seen = None
            return 0.0
        if now() - self.last_fill_ts < self.SETTLE_S or acct.get("cash") is None:
            return 0.0
        cash = float(acct["cash"])
        gap = cash - p.cash
        if self.settings.live_max_capital > 0:
            gap = min(gap, 0.0)  # capped: deposits beyond the cap aren't the bots' to use
        if abs(gap) < max(self.CASH_TOLERANCE_USD, 0.005 * max(p.equity, 0.0)):
            # small and steady: the venue's own fees and rounding (Alpaca takes its crypto fee out of what a trade
            # receives, so it never shows up as a fee on the fill). Book it as a trading cost - it's not a transfer -
            # before it can add up to look like one.
            if abs(gap) >= 0.01 and self._cash_seen is not None and abs(gap - self._cash_seen) < 0.01:
                p.cash += gap
                if gap < 0:
                    p.commissions -= gap
                self.publish("cash.adjusted", {"delta": gap, "reason": "venue fees / rounding"})
                self.act(f"cash matched to the account: {gap:+,.2f} (venue fees / rounding)")
                self.bump("fee_corrections")
                self._cash_seen = None
                return 0.0
            self._cash_seen = gap if abs(gap) >= 0.01 else None
            return 0.0
        if self._cash_seen is None or abs(gap - self._cash_seen) > max(self.CASH_TOLERANCE_USD, 0.005 * abs(gap)):
            self._cash_seen = gap  # seen once (or still moving): wait for the next check
            return 0.0
        self._cash_seen = None
        p.apply_flow(gap)
        topic = "funds.deposit" if gap > 0 else "funds.withdrawal"
        self.publish(topic, {"usd_value": abs(gap), "source": "exchange", "asset": "USD",
                             "note": "seen in your exchange account"})
        venue = getattr(self.desk.broker, "label", "exchange")
        what = (f"${gap:,.2f} added to your {venue} account" if gap > 0
                else f"${-gap:,.2f} taken out of your {venue} account")
        self.publish("alert", Alert("info" if gap > 0 else "warning",
                                    f"{what} - the bots' balance is now ${p.cash:,.2f} cash "
                                    "(not counted as profit or loss)", self.name))
        self.act(f"cash synced with the account: {gap:+,.2f}")
        self.bump("cash_syncs")
        return gap

    def _external_change(self, pos, a: float, b: float) -> str:
        """The account holds less than the bots' records: book the difference as a trade at the market price."""
        sym = pos.symbol
        change = b - a  # negative: coins sold / moved out (a long); positive: a short bought back
        price = self.desk.prices.get(sym) or pos.last_price or pos.avg_price
        side = Side.BUY if change > 0 else Side.SELL
        exe = self.desk.bots.get("execution")
        if exe is not None:  # the ledger books it like any fill (and the audit follows)
            exe.unbooked[sym] = exe.unbooked.get(sym, 0.0) + change
        self.publish("order.filled", Fill("external", sym, side, abs(change), price, 0.0))
        self.act(f"{sym}: account holds {b:g}, records said {a:g} - booked {change:+g} at {price:,.6g} "
                 "(sold or moved outside TradeZone)", "warning")
        return f"{sym} {a:g} -> {b:g}"

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
            # more profit than the fees it cost, and worth its share of a transfer fee
            if s.beats_fees(pos, pos.last_price) and net >= self.desk.min_take():
                self.bump("stale_flips")
                self._exit(sym, f"quick flip: +{net:,.2f} after {hours:g} h without reaching the target - freeing the slot",
                           calm=True)

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
            if acct and self.desk.is_live:
                self.sync_cash(acct)
            capped = self.desk.is_live and self.settings.live_max_capital > 0
            kraken = hasattr(self.desk.broker, "capture_baseline")  # its total includes your own coins, by design
            if acct and acct.get("equity") and not capped and not kraken:
                diff = abs(acct["equity"] - p.equity) / max(acct["equity"], 1)
                self.metrics["broker_equity"] = acct["equity"]
                if diff > 0.02 and now() - self._equity_note > 6 * 3600:  # a note, not an alarm every half minute
                    self._equity_note = now()
                    self.publish(
                        "alert",
                        Alert("warning", f"ledger equity differs from broker by {diff:.1%}", self.name),
                    )
