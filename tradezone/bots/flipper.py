"""Flipper - flip trades, and the best times to flip.

Two jobs:

1. **Learn the best times to flip.** Every closed trade is filed under the hour (UTC) it was opened in:
   how many, how many won, and the total profit after fees. Once an hour has enough trades
   (``flip_min_samples``), it's rated:

   * **good** (made money, won at least half): trades opened in it are a bit bigger and the
     conviction needed is a little lower;
   * **bad** (lost money): trades are smaller, the conviction needed is higher, and the Flipper
     doesn't open its own flips then.

   What it learns is saved, so it keeps learning across restarts.

2. **Find flips itself.** A quick move up on **heavy volume** (at least 2x the usual) with momentum
   behind it and the RSI not yet overbought is a flip: in quickly, out with a small profit
   after fees (the usual real-money plan, profit slices and the moving take-profit apply). It goes
   through Risk and the Executive like any trade, and never in an hour rated bad.
"""

from __future__ import annotations

import time

from ..core.bus import Event
from ..core.models import Regime, Side, Signal, Snapshot, now
from .base import Bot
from .quant import momentum_score


def _hour(ts: float) -> int:
    return time.gmtime(ts).tm_hour


class FlipperBot(Bot):
    name = "flipper"
    title = "Flipper"
    role = "Flip Trades & Timing"
    mission = "⏱️ Flip at the right time - quick in-and-out trades when the hour, volume and momentum line up"
    description = (
        "Learns which hours of the day the team's trades make money in, sizes trades up in the good "
        "hours and down in the bad ones, and flips quick moves that come on heavy volume."
    )

    WAIT_BARS = 5  # don't re-offer the same coin every bar
    COOLDOWN_S = 600  # after a flip closes, wait before flipping that coin again

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.hours: dict[str, dict] = {}  # "13" -> {"n", "wins", "pnl"} (trades opened in that UTC hour)
        self.open_hour: dict[str, int] = {}  # symbol -> hour the open trade started in
        self.wait: dict[str, int] = {}
        self.cooldown: dict[str, float] = {}
        self.signals: set[str] = set()
        self.owned: set[str] = set()
        self._requests: set[str] = set()  # order requests that came from the Flipper's own ideas
        self._dirty = 0
        self.metrics.update({"flips": 0, "flip_ideas": 0, "learned": 0})

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "trade.log": self.on_trade, "order.approved": self.on_approved}

    # --------------------------------------------------------------- timing
    def rating(self, hour: int | None = None) -> str:
        """good | bad | learning for an hour (UTC), from the team's own results."""
        st = self.hours.get(str(_hour(now()) if hour is None else hour))
        if not st or st["n"] < self.settings.flip_min_samples:
            return "learning"
        wr = st["wins"] / st["n"]
        if st["pnl"] > 0 and wr >= 0.5:
            return "good"
        if st["pnl"] < 0 and wr < 0.5:  # losing money AND losing most trades: not just one big loss
            return "bad"
        return "learning"

    def size_mult(self) -> float:
        if not self.enabled:
            return 1.0
        return {"good": self.settings.flip_good_hour_size, "bad": self.settings.flip_bad_hour_size}.get(self.rating(), 1.0)

    def bar_adj(self) -> float:
        if not self.enabled:
            return 0.0
        return {"good": -0.03, "bad": 0.05}.get(self.rating(), 0.0)

    def best_hours(self, n: int = 3, worst: bool = False) -> list[dict]:
        rated = [(int(h), st) for h, st in self.hours.items() if st["n"] >= self.settings.flip_min_samples]
        rated.sort(key=lambda x: x[1]["pnl"] / x[1]["n"], reverse=not worst)
        out = [{"hour": h, "trades": st["n"], "win_rate": st["wins"] / st["n"], "avg": st["pnl"] / st["n"]}
               for h, st in rated[:n]]
        return [x for x in out if (x["avg"] < 0) == worst]

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        sym = t["symbol"]
        if t["action"] == "open":
            self.open_hour[sym] = _hour(t.get("ts") or now())
            exe = self.desk.bots.get("execution")
            order = exe.orders.get(t.get("order_id")) if exe else None
            if order is not None and order.request_id in self._requests:
                self.owned.add(sym)
                self.bump("flips")
            return
        if t.get("pnl") is not None:
            h = str(self.open_hour.get(sym, _hour(t.get("ts") or now())))
            st = self.hours.setdefault(h, {"n": 0, "wins": 0, "pnl": 0.0})
            st["n"] += 1
            st["wins"] += t["pnl"] > 0
            st["pnl"] = round(st["pnl"] + t["pnl"], 6)
            self.bump("learned")
            self._dirty += 1
            if self._dirty >= 5:
                self.save()
        if abs(t.get("position_after") or 0.0) < 1e-12:
            self.open_hour.pop(sym, None)
            if sym in self.owned:
                self.owned.discard(sym)
                self.cooldown[sym] = now() + self.COOLDOWN_S

    # ---------------------------------------------------------------- flips
    def on_approved(self, ev: Event) -> None:
        req = ev.payload
        if not req.reduce_only and req.signal_id in self.signals:
            self._requests.add(req.id)

    def on_snapshot(self, ev: Event) -> None:
        snap: Snapshot = ev.payload
        s, sym = self.settings, snap.symbol
        if not s.flipper or snap.historical or self.desk.halted or self.desk.paused:
            return
        if self.wait.get(sym, 0) > 0:
            self.wait[sym] -= 1
            return
        if not self.portfolio.position(sym).is_flat or now() < self.cooldown.get(sym, 0) or self.desk.in_focus(sym):
            return
        if self.rating() == "bad":
            return  # this hour has been losing money: no flips now
        ind = snap.indicators
        vr = ind.get("volume_ratio")
        if vr is None or vr < s.flip_volume_ratio:
            return  # a flip needs heavy volume behind the move
        mom = momentum_score(snap)
        if mom < 0.4 or snap.price <= ind["ema_fast"] or not 50 <= ind["rsi"] <= 70:
            return  # momentum up, price above its fast average, not yet overbought
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regime(sym) if strategist and strategist.enabled else Regime.UNKNOWN
        if regime is Regime.VOLATILE and not s.is_meme(sym):
            return
        good = self.rating() == "good"
        sig = Signal(sym, Side.BUY, round(min(0.9, 0.5 + 0.3 * mom + (0.1 if good else 0.0)), 3),
                     f"flip: {sym} moving up on {vr:.1f}x the usual volume (momentum {mom:+.2f})"
                     + (", a good hour for flips" if good else ""), self.name,
                     votes={"momentum": "yes", "volume": "yes",
                            "strategist": "yes" if regime is Regime.TRENDING else "abstain"})
        self.signals.add(sig.id)
        if len(self.signals) > 2000:
            self.signals.clear()
            self._requests.clear()
        self.wait[sym] = self.WAIT_BARS
        self.bump("flip_ideas")
        self.publish("signal.proposed", sig)
        self.act(f"FLIP idea {sym} @ {snap.price:,.6g}: {vr:.1f}x volume, momentum {mom:+.2f}")

    # ----------------------------------------------------------- reporting
    async def on_cycle(self) -> None:
        self.metrics["hour_now"] = _hour(now())
        self.metrics["hour_rating"] = self.rating()

    def summary(self) -> dict:
        return {"hour": _hour(now()), "rating": self.rating(), "best": self.best_hours(),
                "worst": self.best_hours(worst=True), "learned": sum(st["n"] for st in self.hours.values()),
                "size_mult": self.size_mult()}

    # --------------------------------------------------------- persistence
    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("flipper", {"hours": self.hours})
            self._dirty = 0

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("flipper") if store is not None else None
        if not data:
            return False
        self.hours = {k: v for k, v in data.get("hours", {}).items() if isinstance(v, dict) and v.get("n", 0) > 0}
        return True
