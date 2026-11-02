"""Risk Manager - the gatekeeper between ideas and orders."""

from __future__ import annotations

from collections import deque

from ..core.bus import Event
from ..core.models import Alert, OrderRequest, Regime, Side, Signal, SignalKind, Snapshot, now
from ..core.stops import StopRejected, plan_stop
from ..core.util import is_closing, round_qty
from .base import Bot


class RiskBot(Bot):
    name = "risk"
    title = "Risk Manager"
    role = "Risk Control & Position Sizing"
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
        limits = self.settings.risk
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
            if sig.kind is SignalKind.ENTRY and self.settings.exit_only_in_profit:
                # discretionary (strategy) exit: only to bank a net profit; losers are left to the stop
                cost = self.settings.round_trip_cost(sig.symbol, pos.avg_price) * abs(pos.qty)
                net = (price - pos.avg_price) * pos.qty - cost
                if net <= 0:
                    return self.reject(sig, f"strategy exit skipped: not in profit ({net:+,.2f}); stop-loss still protects")
            qty = abs(pos.qty)
            if sig.fraction < 1.0:
                part = self._round_qty(qty * sig.fraction, price)
                if part > 0 and (qty - part) * price >= 1.0:  # keep a meaningful remainder
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
        if sig.side is Side.SELL and not limits.allow_short:
            return self.reject(sig, "short selling disabled")
        if sig.strength < limits.min_signal_strength:
            return self.reject(sig, f"conviction {sig.strength:.2f} below threshold")
        if len(self.portfolio.open_positions) >= limits.max_open_positions:
            return self.reject(sig, "max open positions reached")
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
        if s.stop_method == "structure" and "swing_low" in ind:
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
        cost = s.round_trip_cost(sig.symbol, price)
        reward = abs(tp - price)
        if reward < s.min_edge_cost_multiple * cost:
            self.bump("rejected_fees")
            return self.reject(sig, f"target +{reward / price:.2%} doesn't clear fees "
                                    f"({cost / price:.2%} round trip x{s.min_edge_cost_multiple:g})")

        equity = self.portfolio.equity
        risk_budget = equity * limits.risk_per_trade * (0.5 + 0.5 * sig.strength)
        strategist = self.desk.bots.get("strategist")
        if strategist and strategist.regime(sig.symbol) is Regime.VOLATILE:
            risk_budget *= 0.5
        qty = risk_budget / (stop_dist + cost)  # a stopped-out trade also pays fees
        qty = min(qty, equity * limits.max_position_pct / price)
        room = equity * limits.max_gross_exposure - self.portfolio.gross_exposure
        qty = min(qty, max(room, 0) / price)
        if sig.side is Side.BUY:
            qty = min(qty, max(self.portfolio.cash, 0) * 0.995 / price)
        # portfolio heat: total money at risk to all stops stays under the cap
        heat_room = equity * limits.max_portfolio_heat - self.open_risk()
        if heat_room <= 0:
            return self.reject(sig, f"portfolio heat at limit ({limits.max_portfolio_heat:.0%} of equity at risk)")
        qty = min(qty, heat_room / (stop_dist + cost))
        qty = self._round_qty(qty, price)
        if qty <= 0:
            return self.reject(sig, "insufficient capital / exposure room")

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
        limits = self.settings.risk
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
        reason, kind = None, "manual"
        if p.daily_pnl_pct <= -limits.max_daily_loss_pct:
            reason, kind = f"daily loss {p.daily_pnl_pct:.2%} breached limit", "daily"
        elif p.drawdown >= limits.max_drawdown_pct:
            reason, kind = f"drawdown {p.drawdown:.2%} breached limit", "drawdown"
        if reason:
            self.act(f"KILL SWITCH: {reason}", "critical")
            self.publish("alert", Alert("critical", f"Kill switch tripped: {reason}", self.name))
            self.desk.halt(reason, flatten=True, kind=kind)
