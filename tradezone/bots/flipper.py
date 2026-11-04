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

3. **Accumulate coins.** On a coin the bots hold, it sells a slice (20%) into a rise - only when the price
   is 3% above what the bots paid, so the sale itself is a profit after fees - and buys it back once the
   price dips 2.5% below the sale, **only if that gets more coins than were sold** (after fees). Each
   completed round leaves the bots holding more of the coin. If the dip never comes (48 h), the slice
   simply stays sold, as profit in dollars - it never buys back higher (that would mean fewer coins).
"""

from __future__ import annotations

import time

from ..core.bus import Event
from ..core.models import Regime, Side, Signal, SignalKind, Snapshot, Tick, now
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
        # accumulating: symbol -> {"qty": sold, "cash": proceeds after fees, "px": avg sale price, "ts": first sale}
        self.cycles: dict[str, dict] = {}
        self.coins_gained: dict[str, float] = {}
        self._acc_sell_sigs: set[str] = set()
        self._acc_buy_sigs: set[str] = set()
        self._acc_reqs: dict[str, str] = {}  # order request id -> "sell" | "buy"
        self._acc_next: dict[str, float] = {}  # symbol -> no new accumulate action before
        self._clock = 0.0  # latest tick time
        self.metrics.update({"flips": 0, "flip_ideas": 0, "learned": 0, "acc_rounds": 0, "acc_kept_cash": 0})

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "trade.log": self.on_trade, "order.approved": self.on_approved,
                "market.tick": self.on_tick}

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
        self._accumulate_fill(t)
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
        if req.signal_id in self._acc_sell_sigs:
            self._acc_reqs[req.id] = "sell"
            self._acc_sell_sigs.discard(req.signal_id)
        elif req.signal_id in self._acc_buy_sigs:
            self._acc_reqs[req.id] = "buy"
            self._acc_buy_sigs.discard(req.signal_id)
        if len(self._acc_reqs) > 2000:  # keep memory flat
            for k in list(self._acc_reqs)[:1000]:
                self._acc_reqs.pop(k, None)
        for pending in (self._acc_sell_sigs, self._acc_buy_sigs):
            if len(pending) > 2000:  # ideas Risk turned down never get a request
                pending.clear()

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

    # ---------------------------------------------------------- accumulating
    def _accumulate_fill(self, t: dict) -> None:
        exe = self.desk.bots.get("execution")
        order = exe.orders.get(t.get("order_id")) if exe else None
        what = self._acc_reqs.get(order.request_id) if order is not None else None
        sym = t["symbol"]
        if what == "sell":
            c = self.cycles.setdefault(sym, {"qty": 0.0, "cash": 0.0, "px": 0.0, "ts": self._clock or now()})
            c["qty"] += t["qty"]
            c["cash"] += t["qty"] * t["price"] - (t.get("fee") or 0.0)
            c["px"] = (c["cash"] + (t.get("fee") or 0.0)) / c["qty"] if c["qty"] else t["price"]
            self.act(f"ACCUMULATE {sym}: sold {t['qty']:.8g} at {t['price']:,.6g} - waiting to buy it back cheaper")
        elif what == "buy":
            c = self.cycles.pop(sym, None)
            if c:
                gained = t["qty"] - c["qty"]
                self.coins_gained[sym] = self.coins_gained.get(sym, 0.0) + gained
                self.bump("acc_rounds")
                self.act(f"ACCUMULATE {sym}: bought back {t['qty']:.8g} at {t['price']:,.6g} - "
                         f"{gained:+.8g} {sym.split('-')[0]} more than before")
        if abs(t.get("position_after") or 0.0) < 1e-12 and sym in self.cycles:  # the trade closed meanwhile
            self.cycles.pop(sym)
            self.bump("acc_kept_cash")

    def on_tick(self, ev: Event) -> None:
        s = self.settings
        t: Tick = ev.payload
        sym = t.symbol
        clock = self._clock = t.ts or now()  # the market's own clock (same as the wall clock live)
        if not s.accumulate or self.desk.halted or self.desk.paused or clock < self._acc_next.get(sym, 0):
            return
        pos = self.portfolio.position(sym)
        pm, skim = self.desk.bots.get("portfolio"), self.desk.bots.get("skimmer")
        if pos.qty <= 0 or not s.is_crypto(sym) or (pm and sym in pm.exiting) or (skim and skim.owns(sym)):
            return
        price, c = t.price, self.cycles.get(sym)
        if c is None:  # no slice out: sell one into a rise (a profit after fees)
            if price < pos.avg_price * (1 + s.acc_rise_pct) or s.net_if_sold(pos, price) <= 0:
                return
            self._acc_next[sym] = clock + 600
            sig = Signal(sym, Side.SELL, 1.0, f"flipper: accumulate - sold {s.acc_slice:.0%} into a "
                         f"+{price / pos.avg_price - 1:.1%} rise, to buy it back cheaper", self.name,
                         SignalKind.EXIT, fraction=s.acc_slice)
            self._acc_sell_sigs.add(sig.id)
            self.publish("signal.proposed", sig)
            return
        if clock - c["ts"] > s.acc_timeout_h * 3600:  # no dip came: the slice stays sold, as dollar profit
            self.cycles.pop(sym)
            self.bump("acc_kept_cash")
            self.act(f"ACCUMULATE {sym}: no dip in {s.acc_timeout_h:g} h - keeping the slice as profit")
            return
        if price > c["px"] * (1 - s.acc_dip_pct):
            return
        cost_rate = (s.fee_bps(sym) + s.slippage_bps) / 10_000
        qty = c["cash"] / (price * (1 + cost_rate))
        if qty < c["qty"] * (1 + s.acc_min_gain):
            return  # it wouldn't end with more coins: wait for a deeper dip
        self._acc_next[sym] = clock + 60
        sig = Signal(sym, Side.BUY, 1.0, f"flipper: buy back {sym} {1 - price / c['px']:.1%} below the sale - "
                     f"{qty - c['qty']:+.8g} more coins", self.name, qty=qty)
        self._acc_buy_sigs.add(sig.id)
        self.publish("signal.proposed", sig)

    # ----------------------------------------------------------- reporting
    async def on_cycle(self) -> None:
        self.metrics["hour_now"] = _hour(now())
        self.metrics["hour_rating"] = self.rating()

    def summary(self) -> dict:
        return {"coins_gained": {k: round(v, 10) for k, v in self.coins_gained.items() if v > 0},
                "slices_out": {k: {"qty": c["qty"], "sold_at": c["px"]} for k, c in self.cycles.items()},
                "hour": _hour(now()), "rating": self.rating(), "best": self.best_hours(),
                "worst": self.best_hours(worst=True), "learned": sum(st["n"] for st in self.hours.values()),
                "size_mult": self.size_mult()}

    # --------------------------------------------------------- persistence
    def save(self) -> None:
        store = getattr(self.desk, "store", None)
        if store is not None:
            store.save("flipper", {"hours": self.hours, "cycles": self.cycles, "coins_gained": self.coins_gained})
            self._dirty = 0

    def load(self) -> bool:
        store = getattr(self.desk, "store", None)
        data = store.load("flipper") if store is not None else None
        if not data:
            return False
        self.hours = {k: v for k, v in data.get("hours", {}).items() if isinstance(v, dict) and v.get("n", 0) > 0}
        self.cycles = {k: v for k, v in data.get("cycles", {}).items() if isinstance(v, dict) and v.get("qty", 0) > 0}
        self.coins_gained = data.get("coins_gained", {})
        return True
