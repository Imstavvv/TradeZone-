"""Sentinel - compliance, audit journal, performance analytics and team watchdog."""

from __future__ import annotations

import math
from collections import deque

from ..core.bus import Event
from ..core.models import Alert, now
from .base import Bot

JOURNALED = (
    "signal.proposed", "signal.rejected", "order.approved", "order.update", "order.filled",
    "trade.closed", "regime.update", "alert", "desk.control",
)


class SentinelBot(Bot):
    name = "sentinel"
    title = "Sentinel"
    role = "Audit, Compliance & Performance"
    description = (
        "Journals every decision to an audit trail, computes live performance stats "
        "(Sharpe, drawdown, win rate, profit factor), and watches the team's health."
    )

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.events: deque[dict] = deque(maxlen=300)
        self.alerts: deque[dict] = deque(maxlen=100)
        self.equity_curve: deque[tuple[float, float]] = deque(maxlen=5000)
        self._last_errors: dict[str, int] = {}
        self._cycles = 0

    def subscriptions(self):
        return {"*": self.on_event}

    def on_event(self, ev: Event) -> None:
        if ev.topic in ("market.tick", "market.snapshot", "portfolio.update"):
            return
        d = ev.to_dict()
        self.events.append(d)
        if ev.topic in JOURNALED:
            self.desk.journal.record(ev.ts, ev.topic, ev.source, d["payload"])
            self.bump("journaled")
        if ev.topic == "alert":
            self.alerts.append(d)
            self.act(f"logged {ev.payload.level} alert from {ev.source}")
        elif ev.topic == "trade.closed":
            t = ev.payload
            eq = self.portfolio.equity
            if eq > 0 and t.pnl < -0.02 * eq:
                self.publish("alert", Alert("warning", f"large loss on {t.symbol}: {t.pnl:,.2f}", self.name))

    def performance(self) -> dict:
        trades = self.portfolio.closed_trades
        wins = [t.pnl for t in trades if t.pnl > 0]
        losses = [t.pnl for t in trades if t.pnl <= 0]
        eq = [e for _, e in self.equity_curve]
        rets = [b / a - 1 for a, b in zip(eq, eq[1:]) if a > 0]
        sharpe = 0.0
        if len(rets) > 2:
            m = sum(rets) / len(rets)
            sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
            # Sharpe of the observed window (per-cycle mean/stdev scaled by sqrt(n));
            # annualising second-level returns produces meaningless numbers.
            sharpe = m / sd * math.sqrt(len(rets)) if sd > 0 else 0.0
        max_dd, peak = 0.0, -math.inf
        for e in eq:
            peak = max(peak, e)
            if peak > 0:
                max_dd = max(max_dd, 1 - e / peak)
        gross_loss = -sum(losses)
        return {
            "trades": len(trades),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "profit_factor": (sum(wins) / gross_loss) if gross_loss > 0 else None,
            "sharpe": max(-99.0, min(99.0, sharpe)),
            "max_drawdown": max_dd,
        }

    async def on_cycle(self) -> None:
        t = now()
        self.equity_curve.append((t, self.portfolio.equity))
        self._cycles += 1
        if self._cycles % 5 == 0:
            self.desk.journal.record_equity(t, self.portfolio.equity)
            self.desk.journal.commit()
        for name, bot in self.desk.bots.items():
            prev = self._last_errors.get(name, 0)
            if bot.errors > prev:
                self.publish(
                    "alert", Alert("critical", f"{bot.title} raised {bot.errors - prev} error(s)", self.name)
                )
            self._last_errors[name] = bot.errors
            if not bot.enabled and bot.critical:
                self.metrics["degraded"] = True
        perf = self.performance()
        self.metrics.update({k: round(v, 4) if isinstance(v, float) and math.isfinite(v) else v for k, v in perf.items()})
