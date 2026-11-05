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


class SpeedBot(Advisor):
    advisor = False  # watches the team; not consulted on trades
    name = "speed"
    title = "Speed Monitor"
    role = "Processing Speed"
    mission = "⏱️ Keep the team quick - time every cycle and every bot, and warn if the bots fall behind"
    description = ("Times every trading cycle and how long each bot spends working (including waiting on the "
                   "exchange); warns if cycles keep taking more than half the time between them.")
    WINDOW = 60
    NOTE_EVERY_S = 1800

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.cycles: deque[float] = deque(maxlen=self.WINDOW)
        self._busy_before: dict[str, float] = {}
        self.busiest: list[tuple[str, float]] = []
        self._noted = 0.0
        self.overruns = 0

    async def on_cycle(self) -> None:
        last = getattr(self.desk, "last_cycle_s", None)
        if last is None:
            return
        budget = self.settings.cycle_seconds
        self.cycles.append(last)
        if last > budget:
            self.overruns += 1
        avg = sum(self.cycles) / len(self.cycles)
        self.metrics.update({"cycle_ms": round(last * 1000, 1), "avg_cycle_ms": round(avg * 1000, 1),
                             "slowest_ms": round(max(self.cycles) * 1000, 1), "overruns": self.overruns,
                             "load_pct": round(100 * avg / budget, 1) if budget > 0 else None})
        if len(self.cycles) == self.WINDOW:  # once a minute: who's been busiest
            busy = {n: b.busy_s for n, b in self.desk.bots.items()}
            spent = {n: v - self._busy_before.get(n, 0.0) for n, v in busy.items()}
            self._busy_before = busy
            self.busiest = sorted(((n, round(1000 * v / self.WINDOW, 2)) for n, v in spent.items() if v > 0),
                                  key=lambda x: -x[1])[:3]
            self.metrics["busiest"] = ", ".join(f"{self.desk.bots[n].title} {ms} ms" for n, ms in self.busiest)
            if budget > 0 and avg > 0.5 * budget and now() - self._noted > self.NOTE_EVERY_S:
                self._noted = now()
                who = self.metrics["busiest"] or "-"
                self.publish("alert", Alert("warning", f"The bots are falling behind: a cycle takes {avg * 1000:.0f} ms "
                                                       f"of the {budget * 1000:.0f} ms between cycles (busiest: {who})",
                                            self.name))
            self.cycles.clear()


class HousekeeperBot(Advisor):
    advisor = False
    name = "housekeeper"
    title = "Housekeeper"
    role = "Database Upkeep"
    mission = "🧹 Keep the records lean - so the server's disk never fills and lookups stay fast"
    description = ("Every hour clears old working notes from the records (trade ideas and turn-downs after 2 days; "
                   "order updates and alerts after 30) and thins old chart points; trades, deposits, withdrawals, "
                   "the hold stack and your own actions are kept forever. Compacts the database once a day.")
    EVERY_S = 3600
    COMPACT_EVERY_S = 86_400
    KEEP_FOREVER = ("trade.log", "stack.buy", "funds.deposit", "funds.withdrawal", "wallet.linked", "wallet.removed",
                    "desk.control", "position.adjusted", "cash.adjusted")
    KEEP_30_DAYS = ("order.filled", "order.update", "alert")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.last_run = now()  # (not straight after a restart)
        self.last_compact = now()

    async def on_cycle(self) -> None:
        if now() - self.last_run >= self.EVERY_S:
            self.tidy()

    def tidy(self, at: float | None = None) -> dict:
        t = at or now()
        self.last_run = t
        j = self.desk.journal
        keep = self.settings.journal_keep_days * 86_400
        forever = ",".join("?" * len(self.KEEP_FOREVER))
        month = ",".join("?" * len(self.KEEP_30_DAYS))
        removed = j.run(f"DELETE FROM events WHERE ts < ? AND topic NOT IN ({forever}) AND topic NOT IN ({month})",
                        (t - keep, *self.KEEP_FOREVER, *self.KEEP_30_DAYS))
        removed += j.run(f"DELETE FROM events WHERE ts < ? AND topic IN ({month})", (t - 30 * 86_400, *self.KEEP_30_DAYS))
        thinned = j.run("DELETE FROM equity WHERE ts < ? AND rowid NOT IN (SELECT MIN(rowid) FROM equity WHERE ts < ? "
                        "GROUP BY mode, CAST(ts / 600 AS INTEGER))", (t - 2 * 86_400, t - 2 * 86_400))
        self.bump("records_cleared", removed)
        self.bump("chart_points_thinned", thinned)
        if removed or thinned:
            self.act(f"tidied the records: {removed} old working notes cleared, {thinned} old chart points thinned")
        if t - self.last_compact >= self.COMPACT_EVERY_S and j.path != ":memory:":
            self.last_compact = t
            with j._lock:
                j.conn.execute("VACUUM")  # give the freed space back to the disk
            self.bump("compactions")
        return {"removed": removed, "thinned": thinned}

