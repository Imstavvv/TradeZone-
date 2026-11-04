"""Risk Manager - the gatekeeper between ideas and orders."""

from __future__ import annotations

from collections import deque

from ..config import AGGRESSIVE
from ..core.bus import Event
from ..core.models import (
    Alert,
    OrderRequest,
    Regime,
    Side,
    Signal,
    SignalKind,
    Snapshot,
    now,
)
from ..core.stops import StopRejected, plan_stop
from ..core.util import is_closing, round_qty
from .base import Bot


class RiskBot(Bot):
    name = "risk"
    title = "Risk Manager"
    role = "Risk Control & Position Sizing"
    mission = "💰 Make money - only trades whose reward beats the risk and fees"
    description = (
        "Vets every signal against exposure, concentration, cooldown and rate limits, sizes "
        "positions by volatility, and trips the kill switch on excessive loss or drawdown."
    )
    critical = True

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.snapshots: dict[str, Snapshot] = {}
        self.order_times: deque[float] = deque()
        self.last_trade: dict[str, float] = {}
        self.seen_signals: set[str] = set()
        self.strength_by_signal: dict[str, float] = {}  # for the Executive's defensive stance
        self._peak_key: tuple | None = None  # (mode, day, book) the day's best equity belongs to
        self.day_peak = 0.0

    # ---- the goal: grow fast, lose little --------------------------------------------
    def _track_day_peak(self) -> float:
        p = self.portfolio
        key = (self.desk.mode, self.desk._day, id(p))
        if key != self._peak_key:
            self._peak_key, self.day_peak = key, max(p.day_start_equity, p.equity)
        self.day_peak = max(self.day_peak, p.equity)
        return self.day_peak

    def day_guard(self) -> dict:
        """Today's gains lock and the losing-day brake."""
        p, s = self.portfolio, self.settings
        start, peak = p.day_start_equity, self._track_day_peak()
        best = peak - start
        agg = s.aggressive(self.desk.is_live)
        trigger = AGGRESSIVE["day_lock_trigger"] if agg else s.day_lock_trigger_pct
        giveback = AGGRESSIVE["day_giveback"] if agg else s.day_giveback_pct
        locked = start > 0 and best >= trigger * start and p.equity <= start + best * (1 - giveback)
        goal = self.desk.daily_goal()
        goal_day = goal > 0 and best >= goal  # a goal day: protect it more tightly
        if goal_day and p.equity <= start + best * (1 - s.daily_goal_giveback):
            locked, giveback = True, s.daily_goal_giveback
        lim = s.limits(self.desk.is_live).max_daily_loss_pct
        if self.desk.is_live:
            lim = min(lim, s.live_limits()[0])
        return {"locked": bool(locked), "goal_day": bool(goal_day), "best_today": best, "best_today_pct": best / start if start > 0 else 0.0,
                "brake": p.daily_pnl_pct <= -lim / 2, "keep_at_least": start + best * (1 - giveback)}

    def subscriptions(self):
        return {
            "market.snapshot": self.on_snapshot,
            "signal.proposed": self.on_signal,
            "signal.reviewed": self.on_reviewed,
        }

    def on_snapshot(self, ev: Event) -> None:
        self.snapshots[ev.payload.symbol] = ev.payload

    # ------------------------------------------------------------------
    def reject(self, sig: Signal, reason: str) -> None:
        self.bump("rejected")
        self.publish("signal.rejected", {"signal": sig.to_dict(), "reason": reason})
        self.act(f"REJECTED {sig.side.value} {sig.symbol}: {reason}", "warning")

    def approve(self, req: OrderRequest) -> None:
        self.bump("approved")
        self.order_times.append(now())
        self.last_trade[req.symbol] = now()
        self.publish("order.approved", req)
        tag = " (reduce-only)" if req.reduce_only else ""
        self.act(f"APPROVED {req.side.value} {req.qty:g} {req.symbol}{tag}")

    def _rate_limited(self) -> bool:
        cutoff = now() - 60
        while self.order_times and self.order_times[0] < cutoff:
            self.order_times.popleft()
        return len(self.order_times) >= self.settings.risk.max_orders_per_minute

    def _round_qty(self, qty: float, price: float) -> float:
        return round_qty(qty, price, self.settings.risk.fractional)

    def on_signal(self, ev: Event) -> None:
        sig: Signal = ev.payload
        grok = self.desk.bots.get("grok")
        if grok is not None and grok.gates(sig):
            return  # waits for Grok's verdict (signal.reviewed)
        self.evaluate(sig)

    def on_reviewed(self, ev: Event) -> None:
        r = ev.payload
        sig: Signal = r["signal"]
        if not r["approved"]:
            return self.reject(sig, f"Grok veto: {r['rationale']}")
        # blend the model's conviction with Grok's confidence
        sig.strength = round((sig.strength + r["confidence"]) / 2, 3)
        self.evaluate(sig)

    def evaluate(self, sig: Signal) -> None:
        if sig.id in self.seen_signals:
            return self.reject(sig, "duplicate signal id")
        self.seen_signals.add(sig.id)
        if len(self.seen_signals) > 50_000:
            self.seen_signals.clear()
        limits = self.settings.limits(self.desk.is_live)  # Aggressive (real money) allows bigger bets
        pos = self.portfolio.position(sig.symbol)
        price = self.desk.prices.get(sig.symbol)
        if not price:
            return self.reject(sig, "no market price")

        closing = is_closing(pos.qty, sig.side)
        if sig.kind is SignalKind.EXIT or closing:
            if pos.is_flat:
                return self.reject(sig, "nothing to close")
            if not closing:
                return self.reject(sig, "exit signal on wrong side")
            pm = self.desk.bots.get("portfolio")
            if sig.kind is SignalKind.ENTRY and pm is not None and sig.symbol in pm.scaled_out \
                    and self.settings.runners and self.settings.stop_method == "percent":
                return self.reject(sig, "holding the runner - its trailing stop decides the exit")
            if sig.kind is SignalKind.ENTRY and self.settings.exit_only_in_profit:
                # discretionary (strategy) exit: only to bank a net profit; losers are left to the stop
                net = self.settings.net_if_sold(pos, price)  # both fees counted
                if net <= 0:
                    return self.reject(sig, f"strategy exit skipped: not in profit ({net:+,.2f}); stop-loss still protects")
                if net < self.desk.min_take():
                    return self.reject(sig, f"strategy exit skipped: +{net:,.2f} isn't worth its share of the "
                                            f"~${self.desk.transfer_fee():,.2f} transfer fee yet")
            qty = abs(pos.qty)
            if sig.fraction < 1.0:
                part = self._round_qty(qty * sig.fraction, price)
                least = max(self.desk.broker.min_qty(sig.symbol, price), 1.0 / price)  # the venue's smallest sale
                if part >= least and qty - part >= least:  # both the part and what's left can still be sold
                    qty = part
            # reduce-only exits are always allowed: they lower risk
            return self.approve(
                OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, reduce_only=True)
            )

        # ---- new risk from here on ----
        if self.desk.halted:
            return self.reject(sig, f"kill switch active ({self.desk.halt_reason})")
        if self.desk.paused:
            return self.reject(sig, "trading paused")
        if not pos.is_flat:
            return self.reject(sig, "already positioned in this direction")
        guard = self.day_guard()
        if guard["locked"]:  # minimal losses: a good day isn't handed back
            return self.reject(sig, f"locking in today's gains: up {guard['best_today_pct']:.1%} at best and too much "
                                    "of it given back - no new trades until tomorrow")
        if sig.side is Side.SELL and (why := self.desk.short_block(sig.symbol)):
            return self.reject(sig, f"no short: {why}")
        s0 = self.settings
        news = self.desk.bots.get("news")
        if news is not None:  # 📰 breaking news: don't buy into a hack
            if (why := news.veto(sig.symbol, sig.side.sign)) is not None:
                return self.reject(sig, why)
            sig.strength = round(max(0.0, min(1.0, sig.strength + news.conviction_nudge(sig.symbol, sig.side.sign))), 3)
        exe = self.desk.bots.get("execution")
        if exe is not None and (left := exe.cooling(sig.symbol)) > 0:
            return self.reject(sig, f"{sig.symbol} is cooling off after a bad fill ({left / 60:.0f} min left)")
        coach = self.desk.bots.get("coach")
        quick = self.quick
        bar = coach.bar() if coach is not None else (s0.flip_min_strength if quick else limits.min_signal_strength)
        if sig.strength < bar:
            return self.reject(sig, f"conviction {sig.strength:.2f} below threshold")
        if coach is not None and (left := coach.benched(sig.symbol)) > 0:
            return self.reject(sig, f"{sig.symbol} is benched by the Coach after repeated losses ({left / 60:.0f} min left)")
        if (why := self.desk.in_focus(sig.symbol)):
            return self.reject(sig, why)
        focus_max = self.desk.focus_status()["max_positions"]
        most = min(limits.max_open_positions, focus_max) if focus_max else limits.max_open_positions
        if len(self.portfolio.open_positions) >= most:
            return self.reject(sig, "max open positions reached" + (" for this balance (spread wider as it grows)" if focus_max else ""))
        meme = self.settings.is_meme(sig.symbol)
        if meme and sum(self.settings.is_meme(p.symbol) for p in self.portfolio.open_positions) \
                >= self.settings.max_meme_positions + (AGGRESSIVE["extra_memes"] if s0.aggressive(self.desk.is_live) else 0):
            return self.reject(sig, "already holding the most meme coins allowed at once")
        if self.desk.is_live and self.desk.broker.supports(sig.symbol) is False:
            return self.reject(sig, f"{sig.symbol} can't be traded on your real-money {self.desk.broker.label} "
                                    "account (practice only)")
        since = now() - self.last_trade.get(sig.symbol, 0)
        if since < limits.symbol_cooldown_s:
            return self.reject(sig, f"symbol cooldown ({limits.symbol_cooldown_s - since:.0f}s left)")
        if self._rate_limited():
            return self.reject(sig, "order rate limit")

        snap = self.snapshots.get(sig.symbol)
        if snap is None:
            return self.reject(sig, "no indicator data")
        ind = snap.indicators
        atr = max(ind["atr"], price * 0.001)
        s = self.settings
        direction = sig.side.sign
        cost = s.round_trip_cost(sig.symbol, price)  # buy + sell fees and slippage, per unit
        if sig.source == "skimmer":  # a quick day-trade skim: small target, tight stop
            stop_dist = price * s.skim_stop_pct
            stop = price - stop_dist * direction
            skim = s.flip_skim_target_pct if quick else s.skim_target_pct
            tp = price * (1 + skim * direction) + cost * direction  # the target is after fees
            how = f"skim -{s.skim_stop_pct:.1%} / +{skim:.1%} after fees"
        elif s.stop_method == "percent":
            sl_pct, tp_pct = s.plan_pcts(sig.symbol, quick)
            if s.patient(self.desk.is_live):  # a wider safety stop: losing trades get room to come back
                sl_pct = s.patient_meme_stop_pct if meme else s.patient_stop_pct
            stop_dist = price * sl_pct
            stop = price - stop_dist * direction
            tp = price * (1 + tp_pct * direction) + cost * direction  # the target is after fees
            how = f"-{sl_pct:.0%} / +{tp_pct:.0%} after fees" + (" (quick flip)" if quick else "") \
                + (", patient" if s.patient(self.desk.is_live) else "")
        elif s.stop_method == "structure" and "swing_low" in ind:
            try:
                plan = plan_stop(
                    direction, price, atr, ind.get("swing_low"), ind.get("swing_high"),
                    ind.get("donchian_high"), ind.get("donchian_low"),
                    buffer_atr=s.stop_buffer_atr, min_stop_atr=s.min_stop_atr, max_stop_atr=s.max_stop_atr,
                    reward_risk=s.reward_risk, min_room_r=s.min_room_r,
                )
            except StopRejected as exc:
                self.bump("rejected_stop_placement")
                return self.reject(sig, str(exc))
            stop, tp, stop_dist, how = plan.stop, plan.target, plan.distance, plan.method
        else:
            stop_dist = s.stop_atr_mult * atr
            stop = price - stop_dist * direction
            tp = price + s.take_profit_atr_mult * atr * direction
            how = f"{s.stop_atr_mult:g}x ATR"

        # accuracy: only take trades whose target clearly pays for the fees
        edge = s.flip_min_edge if quick else s.min_edge_cost_multiple
        reward = abs(tp - price)
        if reward < edge * cost:
            self.bump("rejected_fees")
            return self.reject(sig, f"target +{reward / price:.2%} doesn't clear fees "
                                    f"({cost / price:.2%} round trip x{edge:g})")

        equity = self.portfolio.equity
        risk_budget = equity * limits.risk_per_trade * (0.5 + 0.5 * sig.strength)
        strategist = self.desk.bots.get("strategist")
        if strategist and strategist.regime(sig.symbol) is Regime.VOLATILE:
            risk_budget *= 0.5
        if coach is not None:  # 🌱 grow: size up slowly after good results, down after bad ones
            risk_budget *= coach.risk_mult() * coach.symbol_mult(sig.symbol)
        if meme:  # meme coins swing hard: smaller bets
            risk_budget *= AGGRESSIVE["meme_risk_mult"] if s.aggressive(self.desk.is_live) else s.meme_risk_mult
        if news is not None:  # scary world news: half-size new buys
            risk_budget *= news.size_mult(direction)
        if guard["brake"]:  # a losing day: smaller trades until it turns
            risk_budget *= s.losing_day_size
        qty = risk_budget / (stop_dist + cost)  # a stopped-out trade also pays fees
        qty = min(qty, equity * limits.max_position_pct / price)
        room = equity * limits.max_gross_exposure - self.portfolio.gross_exposure
        qty = min(qty, max(room, 0) / price)
        # buys need cash; a short needs the same amount set aside (no borrowing to make bets bigger)
        qty = min(qty, self.desk.free_cash() * 0.995 / price)
        # portfolio heat: total money at risk to all stops stays under the cap
        heat_room = equity * limits.max_portfolio_heat - self.open_risk()
        if heat_room <= 0:
            return self.reject(sig, f"portfolio heat at limit ({limits.max_portfolio_heat:.0%} of equity at risk)")
        qty = min(qty, heat_room / (stop_dist + cost))
        qty = self._round_qty(qty, price)
        if sig.side is Side.SELL and not s.is_crypto(sig.symbol):
            qty = float(int(qty))  # shares can only be shorted whole
        if qty <= 0:
            return self.reject(sig, "insufficient capital / exposure room")

        # small accounts: a buy just under the venue's minimum order is rounded UP to it, if that stays inside
        # the position cap and risks at most 1.5x a normal trade (else it'd be unsellable dust: skipped below)
        checker = self.desk.live_broker
        if checker is not None and sig.side is Side.BUY:
            least = checker.min_qty(sig.symbol, price) * 1.02
            if 0 < qty < least and least * price <= min(equity * limits.max_position_pct, self.desk.free_cash() * 0.995) \
                    and least * (stop_dist + cost) <= 1.5 * equity * limits.risk_per_trade:
                qty = least
        # never buy a coin that can't be sold again (venue lists it for selling, big enough, liquid enough)
        if checker is not None and s.is_crypto(sig.symbol) and sig.side is Side.BUY:
            why = checker.sell_check(sig.symbol, qty, price, strict=self.desk.is_live)
            if why:
                self.bump("rejected_unsellable")
                return self.reject(sig, f"won't buy what it can't sell: {why}")
        risk_amt = qty * (stop_dist + cost)
        sig.reason += f" | stop {stop:,.6g} ({how}), target {tp:,.6g}, risk {risk_amt:,.2f}"
        self.strength_by_signal[sig.id] = sig.strength
        if len(self.strength_by_signal) > 5000:
            self.strength_by_signal.clear()
        self.approve(OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, False, stop, tp, price))

    def open_risk(self) -> float:
        """Money that would be lost if every open position hit its stop (locked profits count 0)."""
        total = 0.0
        for p in self.portfolio.open_positions:
            if p.stop_price is None:
                total += abs(p.market_value)  # unprotected position: whole value at risk
                continue
            total += max(0.0, (p.avg_price - p.stop_price) * p.qty)
        return total

    # ------------------------------------------------------------------
    async def on_cycle(self) -> None:
        p = self.portfolio
        limits = self.settings.limits(self.desk.is_live)
        self._track_day_peak()
        self.metrics.update(
            {
                "drawdown": round(p.drawdown, 4),
                "daily_pnl_pct": round(p.daily_pnl_pct, 4),
                "gross_exposure": round(p.gross_exposure / p.equity if p.equity > 0 else 0, 4),
                "portfolio_heat": round(self.open_risk() / p.equity if p.equity > 0 else 0, 4),
            }
        )
        if self.desk.halted:
            return
        s = self.settings
        daily_lim, dd_lim = limits.max_daily_loss_pct, limits.max_drawdown_pct
        if self.desk.is_live:  # real money: the stricter of the two
            live_daily, live_dd, _ = s.live_limits()
            daily_lim, dd_lim = min(daily_lim, live_daily), min(dd_lim, live_dd)
        reason, kind = None, "manual"
        principal = p.starting_cash + p.net_flows  # what you put in
        base = min(principal, self.desk.floor_ack.get(self.desk.mode, principal))
        if self.desk.is_live and base > 0 and p.equity < base * (1 - s.live_limits()[2]):
            reason, kind = (f"your real money fell {1 - p.equity / base:.1%} below what you put in - "
                            "everything stopped to protect what's left"), "floor"
        elif p.daily_pnl_pct <= -daily_lim:
            reason, kind = f"daily loss {p.daily_pnl_pct:.2%} breached limit", "daily"
        elif p.drawdown >= dd_lim:
            reason, kind = f"drawdown {p.drawdown:.2%} breached limit", "drawdown"
        if reason:
            self.act(f"KILL SWITCH: {reason}", "critical")
            self.publish("alert", Alert("critical", f"Kill switch tripped: {reason}", self.name))
            self.desk.halt(reason, flatten=True, kind=kind)
