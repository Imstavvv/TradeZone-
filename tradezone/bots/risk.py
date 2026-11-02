"""Risk Manager - the gatekeeper between ideas and orders."""

from __future__ import annotations

import math
from collections import deque

from ..core.bus import Event
from ..core.models import Alert, OrderRequest, Regime, Side, Signal, SignalKind, Snapshot, now
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
        if self.settings.risk.fractional:
            decimals = 6 if price > 1000 else 4
            f = 10**decimals
            return math.floor(qty * f) / f
        return float(math.floor(qty))

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

        closing = (pos.qty > 0 and sig.side is Side.SELL) or (pos.qty < 0 and sig.side is Side.BUY)
        if sig.kind is SignalKind.EXIT or closing:
            if pos.is_flat:
                return self.reject(sig, "nothing to close")
            if not closing:
                return self.reject(sig, "exit signal on wrong side")
            if sig.kind is SignalKind.ENTRY and self.settings.exit_only_in_profit:
                # discretionary (strategy) exit: only to bank a net profit; losers are left to the stop
                pm = self.desk.bots.get("portfolio")
                cost = pm.round_trip_cost(pos.avg_price) * abs(pos.qty) if pm else 0.0
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
        atr = max(snap.indicators["atr"], price * 0.001)
        stop_dist = self.settings.stop_atr_mult * atr

        equity = self.portfolio.equity
        risk_budget = equity * limits.risk_per_trade * (0.5 + 0.5 * sig.strength)
        strategist = self.desk.bots.get("strategist")
        if strategist and strategist.regime(sig.symbol) is Regime.VOLATILE:
            risk_budget *= 0.5
        qty = risk_budget / stop_dist
        qty = min(qty, equity * limits.max_position_pct / price)
        room = equity * limits.max_gross_exposure - self.portfolio.gross_exposure
        qty = min(qty, max(room, 0) / price)
        if sig.side is Side.BUY:
            qty = min(qty, max(self.portfolio.cash, 0) * 0.995 / price)
        qty = self._round_qty(qty, price)
        if qty <= 0:
            return self.reject(sig, "insufficient capital / exposure room")

        stop = price - stop_dist * sig.side.sign
        tp = price + self.settings.take_profit_atr_mult * atr * sig.side.sign
        self.approve(OrderRequest(sig.symbol, sig.side, qty, sig.reason, sig.id, False, stop, tp))

    # ------------------------------------------------------------------
    async def on_cycle(self) -> None:
        p = self.portfolio
        limits = self.settings.risk
        self.metrics.update(
            {
                "drawdown": round(p.drawdown, 4),
                "daily_pnl_pct": round(p.daily_pnl_pct, 4),
                "gross_exposure": round(p.gross_exposure / p.equity if p.equity > 0 else 0, 4),
            }
        )
        if self.desk.halted:
            return
        reason = None
        if p.daily_pnl_pct <= -limits.max_daily_loss_pct:
            reason = f"daily loss {p.daily_pnl_pct:.2%} breached limit"
        elif p.drawdown >= limits.max_drawdown_pct:
            reason = f"drawdown {p.drawdown:.2%} breached limit"
        if reason:
            self.act(f"KILL SWITCH: {reason}", "critical")
            self.publish("alert", Alert("critical", f"Kill switch tripped: {reason}", self.name))
            self.desk.halt(reason, flatten=True)
