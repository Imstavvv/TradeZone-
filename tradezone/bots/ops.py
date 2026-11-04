"""Operations: four bots that keep the desk honest about costs, fills, the exchange and the results.

* **Fee Auditor** - measures the fee actually paid on real-money fills and corrects the fee the bots plan with.
* **Slippage Tracker** - measures how much worse than expected each coin's fills are; trims coins that keep costing extra.
* **Exchange Health** - when the exchange keeps rejecting orders, new trades wait a few minutes (exits never do).
* **Accountant** - a daily report: profit, fees, trades, wins, the best and worst trade.
"""

from __future__ import annotations

import statistics
import time
from collections import deque

from ..core.bus import Event
from ..core.models import Alert, Fill, OrderStatus, now
from .advisors import NEUTRAL, Advisor


class FeeAuditorBot(Advisor):
    advisor = False  # reports / adjusts plans; not consulted on each trade
    name = "fees"
    title = "Fee Auditor"
    role = "Real Fee Tracking"
    mission = "🧾 Know the true cost - plan with the fees really paid, not a guess"
    description = ("Records the fee on every real-money crypto fill; after 10 fills it sets the planning fee to what "
                   "was really paid (maker fills and fee tiers make it lower; it never goes below 5 or above 150 bps).")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.paid: deque[float] = deque(maxlen=40)

    def subscriptions(self):
        return {"order.filled": self.on_fill}

    def on_fill(self, ev: Event) -> None:
        f: Fill = ev.payload
        if not self.desk.is_live or not self.settings.is_crypto(f.symbol) or f.order_id == "external" or f.price <= 0:
            return
        self.paid.append(f.commission / (f.qty * f.price) * 10_000)
        if len(self.paid) >= 10:
            seen = statistics.quantiles(self.paid, n=4)[2]  # the 75th percentile: a careful estimate
            seen = max(5.0, min(150.0, seen))
            self.metrics["paid_bps"] = round(seen, 1)
            if abs(seen - self.settings.crypto_fee_bps) >= 5:
                old, self.settings.crypto_fee_bps = self.settings.crypto_fee_bps, round(seen, 1)
                self.act(f"fees really paid: {seen:.1f} bps per side (was planning {old:.1f}) - plans updated")
                self.save()

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None and self.paid:
            store.save("fees", {"paid": list(self.paid), "venue": self.settings.venue()})

    def load(self) -> bool:
        """Remember the fees really paid (same venue only), so a restart doesn't go back to the guess."""
        store = getattr(self.desk, "store", None)
        data = store.load("fees") if store is not None else None
        if not data or data.get("venue") != self.settings.venue():
            return False
        self.paid.extend(float(x) for x in data.get("paid", []))
        if len(self.paid) >= 10:
            seen = max(5.0, min(150.0, statistics.quantiles(self.paid, n=4)[2]))
            self.metrics["paid_bps"] = round(seen, 1)
            self.settings.crypto_fee_bps = round(seen, 1)
        return True


class SlippageBot(Advisor):
    name = "slippage"
    title = "Slippage Tracker"
    role = "Fill Quality"
    mission = "🎯 Fill well - trim coins whose fills keep costing extra"
    description = "Tracks each coin's fills against the expected price; a coin averaging 0.3%+ worse over 5 fills trades at 0.6x."

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.slip: dict[str, deque[float]] = {}

    def subscriptions(self):
        return {"order.filled": self.on_fill}

    def on_fill(self, ev: Event) -> None:
        f: Fill = ev.payload
        exe = self.desk.bots.get("execution")
        ref = exe.ref_price.get(f.order_id) if exe else None
        if not ref:
            return
        bps = (f.price - ref) / ref * 10_000 * f.side.sign
        self.slip.setdefault(f.symbol, deque(maxlen=10)).append(bps)

    def advise(self, sig, snap):
        h = self.slip.get(sig.symbol)
        if h and len(h) >= 5 and statistics.mean(h) >= 30:
            return None, 0.0, 0.6
        return NEUTRAL


class UptimeBot(Advisor):
    name = "uptime"
    title = "Exchange Health"
    role = "Exchange Connectivity"
    mission = "📡 Don't fight a broken connection - wait while the exchange keeps rejecting orders"
    description = "If 5+ orders are rejected by the exchange within 10 minutes, new trades wait 10 minutes; exits never wait."
    WINDOW_S, PAUSE_S, MAX_REJECTS = 600, 600, 5

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.rejects: deque[float] = deque()
        self.until = 0.0

    def subscriptions(self):
        return {"order.update": self.on_update}

    def on_update(self, ev: Event) -> None:
        o = ev.payload
        reason = o.reason or ""
        if not self.desk.is_live or o.status is not OrderStatus.REJECTED or "minimum" in reason:
            return  # (only the exchange rejecting real orders counts; an order too small isn't its fault)
        if not reason.startswith(("kraken", "alpaca", "uphold")):
            return
        t = now()
        self.rejects.append(t)
        while self.rejects and t - self.rejects[0] > self.WINDOW_S:
            self.rejects.popleft()
        if len(self.rejects) >= self.MAX_REJECTS and t >= self.until:
            self.until = t + self.PAUSE_S
            self.bump("holds")
            self.publish("alert", Alert("warning", f"The exchange rejected {len(self.rejects)} orders in 10 min - "
                                                   "new trades wait 10 min (exits continue)", self.name))

    def advise(self, sig, snap):
        if now() < self.until:
            return f"the exchange keeps rejecting orders - new trades wait {(self.until - now()) / 60:.0f} more min", 0, 1
        if self.desk.feed_down:
            return "no fresh prices from the market feed", 0, 1
        return NEUTRAL


class AccountantBot(Advisor):
    advisor = False  # reports / adjusts plans; not consulted on each trade
    name = "accountant"
    title = "Accountant"
    role = "Daily Results"
    mission = "📒 Keep score - a plain daily report of what the team made and spent"
    description = "Totals each UTC day: profit after fees, fees paid, trades closed, wins, the best and worst trade; reports it when the day ends."

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.days: dict[str, dict] = {}

    def subscriptions(self):
        return {"trade.log": self.on_trade}

    @staticmethod
    def _day(ts: float) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts))

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        day = self._day(t.get("ts") or now())
        d = self.days.setdefault(day, {"day": day, "pnl": 0.0, "fees": 0.0, "closed": 0, "wins": 0,
                                       "best": None, "worst": None})
        d["fees"] += t.get("fee") or 0.0
        if t.get("pnl") is not None:
            d["pnl"] += t["pnl"]
            d["closed"] += 1
            d["wins"] += t["pnl"] > 0
            row = (round(t["pnl"], 2), t["symbol"])
            d["best"] = max(d["best"] or row, row)
            d["worst"] = min(d["worst"] or row, row)
        for old in sorted(self.days)[:-7]:  # a week of reports
            del self.days[old]

    async def on_cycle(self) -> None:
        today = self._day(now())
        for day, d in list(self.days.items()):
            if day < today and not d.get("reported"):
                d["reported"] = True
                self.save()
                wr = f"{d['wins']}/{d['closed']} won" if d["closed"] else "no trades closed"
                best = f", best {d['best'][1]} {d['best'][0]:+,.2f}" if d["best"] else ""
                worst = f", worst {d['worst'][1]} {d['worst'][0]:+,.2f}" if d["worst"] else ""
                self.publish("alert", Alert("info", f"Daily report {day}: {d['pnl']:+,.2f} after fees, fees "
                                                    f"{d['fees']:,.2f}, {wr}{best}{worst}", self.name))

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("accountant", {"days": self.days})

    def load(self) -> bool:
        """A restart in the middle of a day keeps the day's totals (and the last week's reports)."""
        store = getattr(self.desk, "store", None)
        data = store.load("accountant") if store is not None else None
        if not data:
            return False
        for k, d in (data.get("days") or {}).items():
            for key in ("best", "worst"):
                if isinstance(d.get(key), list):
                    d[key] = tuple(d[key])
            self.days[k] = d
        return True

    def summary(self) -> list[dict]:
        return [self.days[k] for k in sorted(self.days, reverse=True)]


OPS = [FeeAuditorBot, SlippageBot, UptimeBot, AccountantBot]
