"""Rerouter: a buy that didn't go through for a passing reason gets another try - so good trades still get filled.

What it re-routes (buys only):

* **refused by the exchange for a passing reason** - too many requests at once, an out-of-step request number
  (nonce), the exchange busy or in post-only mode for a moment, or "insufficient funds" while cash was still
  settling: the buy is tried again 15 seconds later, then once more after 45;
* **held back while the cash was tied up** in other buys still working: tried again as soon as those finish
  (within 3 minutes);
* **under the exchange's minimum** with the cash then free: tried again once cash frees (within 3 minutes).

Every retry is a fresh signal: it goes through every check again - the risk limits, the stops, the cash and the
loss rules - and is sized against the cash there is *then*. At most two tries per coin in 30 minutes. Refusals that
won't pass on their own (the key can't trade, the pair isn't listed, margin) are never retried: they're explained.
Sells, withdrawals and margin are never touched.
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace

from ..core.models import OrderStatus, Side, Signal, SignalKind, now
from .base import Bot

PASSING = ("rate limit", "too many requests", "nonce", "service", "unavailable", "busy", "timeout", "timed out",
           "insufficient funds", "post_only", "internal error", "temporarily", "throttled")
NEVER = ("permission", "unknown asset", "margin", "leverage", "invalid key", "invalid signature", "withdraw")
DELAYS = (15.0, 45.0)
MAX_TRIES, TRIES_WINDOW_S, WAIT_CASH_S = 2, 1800.0, 180.0


def passing(reason: str) -> bool:
    low = (reason or "").lower()
    return not any(k in low for k in NEVER) and any(k in low for k in PASSING)


class RerouterBot(Bot):
    name = "rerouter"
    title = "Rerouter"
    role = "Getting Trades Filled"
    mission = "🔀 Find a way to fill the trade - a buy refused for a passing reason tries again, safely"
    description = ("Watches for buys that didn't go through: refused by the exchange for a passing reason (busy, too "
                   "many requests, cash still settling) or held back while cash was tied up in other buys. It tries "
                   "them again a little later as fresh signals - every risk check and the cash are checked again - at "
                   "most twice per coin in 30 minutes. Never retries sells, never uses margin.")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.signals: dict[str, Signal] = {}  # recent buy signals, by id (to rebuild one)
        self._order: deque[str] = deque()
        self.requests: dict[str, str] = {}  # request id -> signal id
        self.queue: list[dict] = []  # {"sig", "at", "why", "wait_cash"}
        self.tries: dict[str, deque[float]] = {}
        self.missed: deque[dict] = deque(maxlen=30)

    @property
    def on(self) -> bool:
        return bool(getattr(self.settings, "rerouter", True))

    def subscriptions(self):
        return {"signal.proposed": self.on_signal, "order.approved": self.on_approved,
                "order.update": self.on_update, "order.skipped": self.on_skipped}

    def on_signal(self, ev) -> None:
        sig = ev.payload
        if not isinstance(sig, Signal) or sig.side is not Side.BUY or sig.kind is not SignalKind.ENTRY:
            return
        self.signals[sig.id] = replace(sig)  # (a copy, as proposed)
        self._order.append(sig.id)
        while len(self._order) > 500:
            self.signals.pop(self._order.popleft(), None)

    def on_approved(self, ev) -> None:
        req = ev.payload
        if getattr(req, "side", None) is Side.BUY and not req.reduce_only and req.signal_id in self.signals:
            self.requests[req.id] = req.signal_id
            if len(self.requests) > 500:
                for k in list(self.requests)[:100]:
                    self.requests.pop(k, None)

    def _signal_for(self, request_id: str) -> Signal | None:
        sid = self.requests.get(request_id)
        return self.signals.get(sid) if sid else None

    def on_update(self, ev) -> None:
        o = ev.payload
        if getattr(o, "status", None) is not OrderStatus.REJECTED or o.side is not Side.BUY or o.reduce_only:
            return
        sig = self._signal_for(o.request_id)
        if sig is None:
            return
        if not passing(o.reason):
            self.missed.append({"symbol": o.symbol, "ts": now(), "why": o.reason, "retried": False})
            self.act(f"{o.symbol}: the exchange refused the buy ({o.reason}) - not a passing reason, not retried")
            return
        self._plan(sig, f"the exchange refused it ({o.reason})", wait_cash=False)

    def on_skipped(self, ev) -> None:
        d = ev.payload or {}
        req = d.get("request")
        sig = self._signal_for(req.id) if req is not None else None
        if sig is not None:
            self._plan(sig, "the cash was tied up in other buys" if d.get("why") == "cash_committed"
                       else "under the exchange's minimum with the cash free then", wait_cash=True)

    def _plan(self, sig: Signal, why: str, wait_cash: bool) -> None:
        if not self.on or sig.reason.startswith("rerouted"):
            return
        t = now()
        tries = self.tries.setdefault(sig.symbol, deque())
        while tries and t - tries[0] > TRIES_WINDOW_S:
            tries.popleft()
        if len(tries) >= MAX_TRIES or any(q["sig"].symbol == sig.symbol for q in self.queue):
            self.missed.append({"symbol": sig.symbol, "ts": t, "why": why, "retried": False})
            return
        delay = DELAYS[min(len(tries), len(DELAYS) - 1)]
        tries.append(t)
        self.queue.append({"sig": sig, "at": t + delay, "why": why, "wait_cash": wait_cash, "until": t + WAIT_CASH_S})
        self.bump("planned")
        self.act(f"{sig.symbol}: buy didn't go through - {why}; trying again in {delay:.0f}s")

    def _cash_ready(self, symbol: str) -> bool:
        exe = self.desk.bots.get("execution")
        px = self.desk.prices.get(symbol) or 0.0
        if exe is None or px <= 0:
            return False
        room = exe.cash_room(symbol)
        return room > 0 and room >= self.desk.broker.min_qty(symbol, px)

    async def on_cycle(self) -> None:
        if not self.queue:
            return
        t = now()
        keep = []
        for q in self.queue:
            sig = q["sig"]
            if t < q["at"]:
                keep.append(q)
                continue
            if q["wait_cash"] and not self._cash_ready(sig.symbol):
                if t < q["until"]:
                    keep.append(q)
                else:
                    self.missed.append({"symbol": sig.symbol, "ts": t, "why": q["why"], "retried": False})
                continue
            if self.desk.portfolio.position(sig.symbol).qty > 0 and not sig.qty:
                continue  # (bought meanwhile)
            fresh = Signal(sig.symbol, Side.BUY, sig.strength, f"rerouted: {sig.reason}", sig.source, SignalKind.ENTRY,
                           votes=dict(sig.votes), qty=sig.qty)
            self.bump("retried")
            self.act(f"{sig.symbol}: trying the buy again ({q['why']})")
            self.publish("signal.proposed", fresh)
        self.queue = keep
        self.metrics["waiting"] = len(self.queue)
