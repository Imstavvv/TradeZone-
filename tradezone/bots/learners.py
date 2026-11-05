"""Learning bots: they learn from the team's own finished trades and adjust within safe limits.

* **Target Learner** - learns how far each coin's trades really run before turning back, and sets that coin's
  take-profit target to match (instead of one fixed target for every coin).
* **Trade Reviewer** - keeps a record for each way the team finds trades (strategy signals, breakout snipes,
  volume snipes, pullbacks, flips, dip buys, skims) and sizes the next trades from each one up when its record is
  good and down when it's poor.

Both remember what they learned across restarts. Neither can touch the hard safety limits.
"""

from __future__ import annotations

from collections import deque

from ..core.bus import Event
from ..core.models import now
from .base import Bot


def source_of(reason: str) -> str:
    """Which trade finder opened a trade, from its reason."""
    r = (reason or "").lower()
    for key, name in (("volume snipe", "volume_snipe"), ("snipe:", "snipe"), ("flip:", "flipper"),
                      ("dip buy", "dip_buy"), ("pullback", "pullback"), ("skim", "skim")):
        if key in r:
            return name
    return "strategy"


NICE = {"strategy": "Strategy signals", "snipe": "Breakout snipes", "volume_snipe": "Volume snipes",
        "pullback": "Pullbacks", "flipper": "Flips", "dip_buy": "Dip buys", "skim": "Skims"}


class TargetLearnerBot(Bot):
    name = "targets"
    title = "Target Learner"
    role = "Learning Each Coin's Targets"
    mission = "🎯 Learn where each coin's moves end - and aim the take-profit there"
    description = ("Records how far each coin's trades ran before turning back; after 8 trades in a coin it sets "
                   "that coin's take-profit at a typical run (within 0.6x-1.6x of the usual target, and always "
                   "well above the fees).")
    MIN_TRADES = 8
    KEEP = 40

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.runs: dict[str, deque[float]] = {}  # symbol -> best gain each finished trade reached (fraction)
        self.open: dict[str, list[float]] = {}  # symbol -> [entry, best price]

    def subscriptions(self):
        return {"market.tick": self.on_tick, "trade.log": self.on_trade}

    def on_tick(self, ev: Event) -> None:
        t = ev.payload
        o = self.open.get(t.symbol)
        if o is not None:
            o[1] = max(o[1], t.price)

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        sym = t["symbol"]
        if t.get("action") == "open" and t.get("side") == "buy":
            self.open[sym] = [t["price"], t["price"]]
        elif abs(t.get("position_after") or 0.0) < 1e-12 and sym in self.open:
            entry, best = self.open.pop(sym)
            if entry > 0 and (t.get("pnl") or 0) > 0:  # winners only: how far a good trade in this coin runs
                self.runs.setdefault(sym, deque(maxlen=self.KEEP)).append(best / entry - 1)
                self.bump("trades_learned")
                self.save()

    def target(self, symbol: str, usual: float) -> float:
        """The take-profit (fraction above the buy price) for ``symbol``: learned, or ``usual`` until enough trades."""
        h = self.runs.get(symbol)
        if not self.enabled or not self.settings.learn_targets or not h or len(h) < self.MIN_TRADES:
            return usual
        ranked = sorted(h)
        typical = ranked[int(0.6 * (len(ranked) - 1))]  # a run 60% of its trades reached
        cost = self.settings.round_trip_cost(symbol, 1.0) * self.settings.flip_min_edge
        if self.settings.learn_targets_raise_only:  # only ever lets winners run further, never sells them sooner
            return max(usual, min(1.6 * usual, typical))
        return max(cost, 0.6 * usual, min(1.6 * usual, typical))

    def summary(self) -> list[dict]:
        out = []
        for sym, h in sorted(self.runs.items()):
            usual = self.settings.plan_pcts(sym, self.quick)[1]
            out.append({"symbol": sym, "trades": len(h), "target": self.target(sym, usual), "usual": usual,
                        "learned": len(h) >= self.MIN_TRADES})
        return out

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("targets", {"runs": {k: list(v) for k, v in self.runs.items()}})

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("targets") if store is not None else None
        if not data:
            return False
        for k, v in (data.get("runs") or {}).items():
            self.runs[k] = deque((float(x) for x in v), maxlen=self.KEEP)
        return True


class TradeReviewerBot(Bot):
    name = "reviewer"
    title = "Trade Reviewer"
    role = "Learning What Works"
    mission = "📋 Review every finished trade - and size up the trade finders that keep winning"
    description = ("Keeps a record for each way the team finds trades (strategy signals, breakout and volume "
                   "snipes, pullbacks, flips, dip buys, skims). After 6 finished trades, a finder that keeps "
                   "winning trades a little bigger (up to 1.25x) and one that keeps losing smaller (down to 0.5x).")
    MIN_TRADES = 6

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.stats: dict[str, dict] = {}  # source -> {n, wins, pnl}
        self.opened_by: dict[str, str] = {}  # symbol -> source of the open trade
        self.pnl: dict[str, float] = {}  # symbol -> P&L so far of the open trade

    def subscriptions(self):
        return {"trade.log": self.on_trade}

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        sym = t["symbol"]
        if t.get("action") == "open":
            self.opened_by[sym] = source_of(t.get("reason", ""))
            self.pnl[sym] = 0.0
            return
        if t.get("pnl") is not None and sym in self.opened_by:
            self.pnl[sym] = self.pnl.get(sym, 0.0) + t["pnl"]
        if abs(t.get("position_after") or 0.0) < 1e-12 and sym in self.opened_by:
            src, pnl = self.opened_by.pop(sym), self.pnl.pop(sym, 0.0)
            st = self.stats.setdefault(src, {"n": 0, "wins": 0, "pnl": 0.0})
            st["n"] += 1
            st["wins"] += pnl > 0
            st["pnl"] += pnl
            self.bump("trades_reviewed")
            self.save()

    def size_mult(self, reason: str) -> float:
        """How big the next trade from this finder should be, from its record (1.0 until enough trades)."""
        return self._mult(source_of(reason))

    def _mult(self, src: str) -> float:
        st = self.stats.get(src)
        if not self.enabled or not self.settings.learn_sources or not st or st["n"] < self.MIN_TRADES:
            return 1.0
        win = (st["wins"] + 1) / (st["n"] + 2)  # shrunk toward 50%: a few lucky trades don't count for much
        mult = 1.0 + 1.5 * (win - 0.5)
        if st["pnl"] < 0:
            mult = min(mult, 0.85)  # losing money overall: never sized up, whatever the win rate
        return max(0.5, min(1.25, mult))

    def summary(self) -> list[dict]:
        out = []
        for src, st in sorted(self.stats.items(), key=lambda x: -x[1]["pnl"]):
            out.append({"source": src, "name": NICE.get(src, src), **st, "size": self._mult(src)})
        return out

    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("reviewer", {"stats": self.stats, "ts": now()})

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("reviewer") if store is not None else None
        if not data:
            return False
        self.stats = {k: v for k, v in (data.get("stats") or {}).items() if isinstance(v, dict)}
        return True
