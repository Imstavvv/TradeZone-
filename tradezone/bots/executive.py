"""Executive - the head trader. Makes the final, real-time call on every trade.

Every order the Risk Manager approves comes here first. Nothing reaches the broker unless the
Executive authorises it. The decision is made the instant the approval arrives (event-driven),
and the latency from signal to authorisation is measured.

For new positions it checks, at that moment:

1. **Team health:** price data for the symbol is live, the safety-critical bots are on duty, and
   the books balance.
2. **Fresh price:** the market hasn't run away since the signal (no chasing). The price must not
   have used up more than ``max_chase_r`` of the planned risk, and must still be on the right side
   of the stop.
3. **Concentration:** no more than ``max_correlated`` crypto positions in the same direction, since
   crypto prices move together.
4. **Performance mode:** after a losing streak or a deep drawdown the desk goes *defensive*: half
   size and stronger conviction required. It returns to *normal* when results recover.

Exits (stops, targets, flatten, kill switch) are authorised instantly and are never held back.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import replace

from ..core.bus import Event
from ..core.models import Alert, OrderRequest, now
from ..core.util import round_qty, us_market_open
from .base import Bot

CRITICAL_TEAM = ("scout", "risk", "execution", "portfolio", "sentinel")


class ExecutiveBot(Bot):
    name = "executive"
    title = "Executive"
    role = "Head Trader - Final Decisions"
    mission = "💰 Make money - the final yes only for the best trades"
    description = (
        "The head trader. Takes the final real-time decision on every trade: checks team health, "
        "re-checks the live price so it never chases, limits correlated bets, switches to a "
        "defensive stance after losses, and authorises the order for execution."
    )
    critical = True

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.mode = "normal"  # normal | defensive
        self.mode_reason = ""
        self.latencies: deque[float] = deque(maxlen=200)  # signal -> authorisation, ms
        self.decisions: deque[dict] = deque(maxlen=100)
        self.metrics.update({"authorized": 0, "held": 0, "exits": 0})

    def subscriptions(self):
        return {"order.approved": self.on_approved, "trade.closed": self.on_trade_closed}

    # ------------------------------------------------------------------ stance
    def on_trade_closed(self, ev: Event) -> None:
        self.review_stance()

    def review_stance(self) -> None:
        s = self.settings
        trades = self.portfolio.closed_trades[-s.exec_lookback_trades:]
        losses_in_row = 0
        for t in reversed(trades):
            if t.pnl > 0:
                break
            losses_in_row += 1
        # cool-off: an old streak expires, so a quiet defensive desk doesn't stay defensive forever
        if losses_in_row and now() - trades[-1].ts > s.exec_defensive_cooloff_min * 60:
            losses_in_row = 0
        recent = trades[-10:]
        wr = sum(t.pnl > 0 for t in recent) / len(recent) if len(recent) >= 6 else None
        dd = self.portfolio.drawdown
        reasons = []
        if losses_in_row >= s.exec_defensive_after_losses:
            reasons.append(f"{losses_in_row} losses in a row")
        if dd >= s.risk.max_drawdown_pct * 0.5:
            reasons.append(f"drawdown {dd:.1%}")
        if wr is not None and wr < 0.3 and recent and now() - recent[-1].ts <= s.exec_defensive_cooloff_min * 60:
            reasons.append(f"win rate {wr:.0%} over last {len(recent)}")
        new = "defensive" if reasons else "normal"
        if new != self.mode:
            self.mode = new
            self.mode_reason = ", ".join(reasons) if reasons else "results recovered"
            level = "warning" if new == "defensive" else "info"
            self.act(f"stance -> {new.upper()} ({self.mode_reason})", level)
            self.publish("alert", Alert(level, f"Executive switched to {new} stance: {self.mode_reason}", self.name))
        self.metrics["stance"] = self.mode

    # ---------------------------------------------------------------- decisions
    def _record(self, req: OrderRequest, verdict: str, why: str, latency_ms: float | None) -> None:
        self.decisions.append({"ts": now(), "symbol": req.symbol, "side": req.side.value, "qty": req.qty,
                               "verdict": verdict, "why": why, "latency_ms": latency_ms,
                               "exit": req.reduce_only})

    def hold(self, req: OrderRequest, why: str, latency_ms: float) -> None:
        self.bump("held")
        self._record(req, "held", why, latency_ms)
        self.publish("order.held", {"request": req.to_dict(), "reason": why})
        self.act(f"HELD {req.side.value} {req.symbol}: {why}", "warning")

    def authorize(self, req: OrderRequest, why: str, latency_ms: float) -> None:
        self.bump("exits" if req.reduce_only else "authorized")
        self.latencies.append(latency_ms)
        self._record(req, "authorized", why, latency_ms)
        self.publish("order.authorized", req)
        tag = "EXIT" if req.reduce_only else "TRADE"
        self.act(f"{tag} {req.side.value.upper()} {req.qty:g} {req.symbol} authorised in {latency_ms:.0f} ms - {why}")

    def team_problem(self, symbol: str) -> str | None:
        for name in CRITICAL_TEAM:
            bot = self.desk.bots.get(name)
            if bot is not None and not bot.enabled:
                return f"{bot.title} is offline"
        sentinel = self.desk.bots.get("sentinel")
        if sentinel is not None and not getattr(sentinel, "books_balanced", True):
            return "books out of balance"
        if self.desk.feed_down:
            return "no market data"
        scout = self.desk.bots.get("scout")
        book = scout.books.get(symbol) if scout else None
        if book is not None and now() - book.last_ts > self.settings.exec_stale_after_s:
            return f"{symbol} price is stale ({now() - book.last_ts:.0f}s old)"
        return None

    def correlated_count(self, req: OrderRequest) -> int:
        crypto = self.settings.is_crypto
        if not crypto(req.symbol):
            return 0
        return sum(1 for p in self.portfolio.open_positions
                   if crypto(p.symbol) and (p.qty > 0) == (req.side.sign > 0))

    def on_approved(self, ev: Event) -> None:
        req: OrderRequest = ev.payload
        started = req.ts or now()
        latency = lambda: max(0.0, (now() - started) * 1000)

        # exits lower risk: always authorised immediately
        if req.reduce_only:
            return self.authorize(req, "exit - always immediate", latency())

        s = self.settings
        if (problem := self.team_problem(req.symbol)) is not None:
            return self.hold(req, problem, latency())

        price = self.desk.prices.get(req.symbol)
        if not price or not math.isfinite(price):
            return self.hold(req, "no live price", latency())
        if req.stop_price is not None:
            entry_ref = req.ref_price or price
            risk_dist = abs(entry_ref - req.stop_price)
            moved = (price - entry_ref) * req.side.sign
            if (price - req.stop_price) * req.side.sign <= 0:
                return self.hold(req, f"price {price:,.6g} already through the stop", latency())
            if risk_dist > 0 and moved > s.exec_max_chase_r * risk_dist:
                return self.hold(req, f"price ran {moved / risk_dist:.2f}R since the signal - not chasing", latency())

        if s.exec_market_hours_only and not s.is_crypto(req.symbol) and self.desk.feed.name != "sim" \
                and not us_market_open():
            return self.hold(req, "US stock market is closed - prices are stale until the open", latency())
        if (n := self.correlated_count(req)) >= s.exec_max_correlated:
            return self.hold(req, f"already {n} crypto positions the same way (correlated)", latency())

        qty, why = req.qty, f"stance {self.mode}"
        if self.mode == "defensive":
            strength = self._strength(req)
            if strength is not None and strength < s.exec_defensive_min_strength:
                return self.hold(req, f"defensive stance needs conviction {s.exec_defensive_min_strength:.2f}+ "
                                      f"(this {strength:.2f})", latency())
            qty = round_qty(req.qty * 0.5, price, s.risk.fractional)
            if qty <= 0:
                return self.hold(req, "defensive half-size rounds to zero", latency())
            why = f"defensive stance: half size ({self.mode_reason})"
        self.authorize(replace(req, qty=qty) if qty != req.qty else req, why, latency())

    def _strength(self, req: OrderRequest) -> float | None:
        risk = self.desk.bots.get("risk")
        return getattr(risk, "strength_by_signal", {}).get(req.signal_id) if risk else None

    # ------------------------------------------------------------------ cycle
    async def on_cycle(self) -> None:
        self.review_stance()
        lat = sorted(self.latencies)
        if lat:
            self.metrics["decision_ms_median"] = round(lat[len(lat) // 2], 1)
            self.metrics["decision_ms_max"] = round(lat[-1], 1)
        self.metrics["stance"] = self.mode

    def summary(self) -> dict:
        lat = sorted(self.latencies)
        return {"stance": self.mode, "reason": self.mode_reason,
                "median_ms": lat[len(lat) // 2] if lat else None, "decisions": list(self.decisions)[-30:][::-1]}
