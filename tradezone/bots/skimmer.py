"""Skimmer - takes profits of every size: quick day-trade skims, partial banks and spike sells.

Two jobs:

1. **Skims (small day trades).** On liquid, non-meme coins it buys short pullbacks inside an
   uptrend (fast trend up, price dipped below its fast average, RSI cooled to 30-45), only when the market is calm enough for a tight stop, for a small
   target (+2.5% by default) with a tight stop (-1.2%), and gives each skim a time limit (2 hours).
   When time is up and the skim is in profit after fees it is sold; a skim in a loss keeps its
   stop (it is never sold at a loss by the clock). Skims still need a team vote - trend and dip
   must both agree and nobody may object - and go through Risk and the Executive like any trade.

2. **Profit-taking on every open trade (small and large).**
   * *Small:* once a trade is halfway to its target, a slice (25%) is banked.
   * *Large:* if the price spikes hard in a few minutes (+2.5% on majors, +6% on meme coins),
     half the position is sold into the jump (measured over the last 5 bars) - spikes often fall back.
   Both only ever sell at a profit after fees, once per trade (a spike sell at most every 10 min),
   and use reduce-only exits, so they can never add risk.

The Portfolio Manager still runs stops, the break-even lock, targets and runners; the Skimmer only
adds extra, earlier profit-taking on top.
"""

from __future__ import annotations

import math
from collections import deque

from ..core.bus import Event
from ..core.models import Regime, Side, Signal, SignalKind, Snapshot, Tick, now
from .base import Bot


class SkimmerBot(Bot):
    name = "skimmer"
    title = "Skimmer"
    role = "Profit Taking & Day-Trade Skims"
    mission = "💰 Make money - bank profits big and small, every chance it gets"
    description = (
        "Skims the market with quick day trades on small dips in an uptrend, and takes profits on "
        "every open trade: a slice halfway to the target and a big chunk when the price spikes."
    )

    SPIKE_REPEAT_S = 600

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.signals: dict[str, str] = {}  # skim signal id -> symbol
        self.requests: dict[str, str] = {}  # order request id -> skim signal id
        self.owned: dict[str, float] = {}  # symbol -> when the skim opened
        self.cooldown: dict[str, float] = {}  # symbol -> no new skim before (after a skim closes)
        self.wait_bars: dict[str, int] = {}  # symbol -> bars to wait before re-offering an idea
        self.banked: dict[str, float] = {}  # symbol -> opened_ts of the trade already banked
        self.spiked: dict[str, tuple[float, float]] = {}  # symbol -> (opened_ts, last spike sell)
        self.bars: dict[str, deque] = {}  # symbol -> the last few bar closes (spike detection)
        self.metrics.update({"skims": 0, "skim_wins": 0, "banked_small": 0, "banked_spikes": 0,
                             "time_exits": 0, "profit_banked": 0.0})

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "market.tick": self.on_tick,
                "order.approved": self.on_approved, "trade.log": self.on_trade}

    def owns(self, symbol: str) -> bool:
        return symbol in self.owned

    # ------------------------------------------------------------- skims (entries)
    def on_snapshot(self, ev: Event) -> None:
        s = self.settings
        snap: Snapshot = ev.payload
        sym = snap.symbol
        if not snap.historical:
            self.bars.setdefault(sym, deque(maxlen=max(2, int(s.spike_bars)))).append(snap.price)
        if not s.skimmer or snap.historical or s.is_meme(sym) or self.desk.halted or self.desk.paused \
                or self.desk.in_focus(sym):
            return
        if self.wait_bars.get(sym, 0) > 0:
            self.wait_bars[sym] -= 1
            return
        if not self.portfolio.position(sym).is_flat or sym in self.owned or now() < self.cooldown.get(sym, 0):
            return
        quick = self.quick
        if len(self.owned) >= (s.flip_max_skims if quick else s.max_skims):
            return
        ind = snap.indicators
        price = snap.price
        uptrend = ind["ema_fast"] > ind["ema_slow"] and price > ind["sma50"] * 0.995
        dip = price < ind["ema_fast"] and 30 <= ind["rsi"] <= (s.flip_skim_rsi_max if quick else 45)
        if not (uptrend and dip):
            return
        # the target must be reachable within the time limit at this coin's current volatility
        scout = self.desk.bots.get("scout")
        bar_s = getattr(scout, "bar_seconds", 0) or 60
        bars = self.max_minutes() * 60 / bar_s
        gross = self.target() + s.round_trip_cost(sym, price) / price  # the target is after fees
        if ind["volatility"] * math.sqrt(max(bars, 1)) < gross * 0.5:
            return
        # ...but calm enough that ordinary wiggles over ~10 bars stay well inside the tight stop
        if ind["volatility"] * math.sqrt(10) > s.skim_stop_pct * (s.flip_skim_calm if quick else 0.5):
            return
        strategist = self.desk.bots.get("strategist")
        regime = strategist.regime(sym) if strategist and strategist.enabled else Regime.UNKNOWN
        votes = {"momentum": "yes", "mean_reversion": "yes",
                 "strategist": "no" if regime is Regime.VOLATILE else "yes" if regime is Regime.TRENDING else "abstain"}
        if "no" in votes.values():
            return
        sig = Signal(sym, Side.BUY, 0.6, f"skim: {sym} dipped in an uptrend (RSI {ind['rsi']:.0f}) - "
                     f"quick +{self.target():.1%} target after fees", self.name, votes=votes)
        self.signals[sig.id] = sym
        self.wait_bars[sym] = 3  # don't re-offer it every bar if the team passes
        self.publish("signal.proposed", sig)
        self.act(f"SKIM idea {sym} @ {price:,.6g}: dip in an uptrend, aiming +{self.target():.1%} after fees")

    def target(self) -> float:
        return self.settings.flip_skim_target_pct if self.quick else self.settings.skim_target_pct

    def max_minutes(self) -> float:
        return self.settings.flip_skim_max_minutes if self.quick else self.settings.skim_max_minutes

    def on_approved(self, ev: Event) -> None:
        req = ev.payload
        if not req.reduce_only and req.signal_id in self.signals:
            self.requests[req.id] = req.signal_id

    def on_trade(self, ev: Event) -> None:
        t = ev.payload
        sym = t["symbol"]
        if t["action"] == "open":
            exe = self.desk.bots.get("execution")
            order = exe.orders.get(t.get("order_id")) if exe else None
            if order is not None and order.request_id in self.requests:
                self.owned[sym] = now()
                self.bump("skims")
                self.act(f"SKIM BUY {sym} @ {t['price']:,.6g} - in and out within {self.max_minutes():g} min")
            return
        if t.get("pnl") is not None and t["pnl"] > 0:
            self.metrics["profit_banked"] = round(self.metrics["profit_banked"] + t["pnl"], 2)
        if abs(t.get("position_after") or 0.0) < 1e-12:
            if self.owned.pop(sym, None) is not None:
                wait = self.settings.flip_skim_cooldown_min if self.quick else self.settings.skim_cooldown_min
                self.cooldown[sym] = now() + wait * 60
                if (t.get("pnl") or 0) > 0:
                    self.bump("skim_wins")
            self.banked.pop(sym, None)
            self.spiked.pop(sym, None)
        if len(self.signals) > 2000:
            self.signals.clear()
            self.requests.clear()

    # ---------------------------------------------------- profit-taking (exits)
    def _sell(self, sym: str, fraction: float, reason: str) -> None:
        """Sell part of a position - always reduce-only."""
        side = Side.SELL if self.portfolio.position(sym).qty > 0 else Side.BUY
        self.publish("signal.proposed", Signal(sym, side, 1.0, reason, self.name, SignalKind.EXIT, fraction=fraction,
                                               calm=True))  # banking profit: may wait at the ask

    def on_tick(self, ev: Event) -> None:
        s = self.settings
        t: Tick = ev.payload
        ts = now()
        if not s.skimmer:
            return
        pos = self.portfolio.position(t.symbol)
        pm = self.desk.bots.get("portfolio")
        if pos.is_flat or not pos.opened_ts or (pm and t.symbol in pm.exiting):
            return
        price, entry = t.price, pos.avg_price
        d = 1 if pos.qty > 0 else -1  # shorts profit when the price falls
        gain = (price / entry - 1) * d
        net = s.net_if_sold(pos, price)  # what selling now really makes, after both fees
        net_pct = net / (entry * abs(pos.qty)) if entry > 0 else 0.0
        if net_pct <= s.round_trip_cost(t.symbol, price) / price:
            return  # only ever bank real profit: after fees, and at least as much again as the fees
        least = self.desk.min_take()  # ...and worth its share of the transfer fee to move it out
        sym = t.symbol
        # large: a sharp spike over the last few bars (5 minutes on 1-minute bars) -> sell half into it
        recent = self.bars.get(sym)
        ref = (min(recent) if d > 0 else max(recent)) if recent else price
        spike = (price / ref - 1) * d if ref > 0 else 0.0  # a sharp move our way
        need = s.meme_spike_pct if s.is_meme(sym) else s.spike_pct
        last = self.spiked.get(sym)
        if spike >= need and net * s.spike_fraction >= least and (last is None or last[0] != pos.opened_ts or ts - last[1] > self.SPIKE_REPEAT_S):
            self.spiked[sym] = (pos.opened_ts, ts)
            self.bump("banked_spikes")
            self.act(f"SPIKE {sym} +{spike:.1%} in {len(recent or ())} bars - banking "
                     f"{s.spike_fraction:.0%} at +{gain:.1%}")
            return self._sell(sym, s.spike_fraction, f"skimmer: banked {s.spike_fraction:.0%} into a "
                              f"+{spike:.1%} spike (trade +{gain:.1%})")
        # small: halfway to the target -> bank a slice (skims run to their own small target instead)
        if sym in self.owned or self.banked.get(sym) == pos.opened_ts:
            return
        if pos.take_profit is not None:
            target = abs(pos.take_profit / entry - 1)
        else:
            target = s.plan_pcts(sym)[1]
        if self.quick and s.flip_bank_fraction > 0 and net_pct >= s.flip_net_pct and net * s.flip_bank_fraction >= least:  # quick flip: bank a slice
            self.banked[sym] = pos.opened_ts
            self.bump("banked_small")
            self.bump("quick_flips")
            self.act(f"QUICK FLIP {sym}: banking {s.flip_bank_fraction:.0%} at +{net_pct:.1%} after fees - the rest keeps running")
            return self._sell(sym, s.flip_bank_fraction, f"skimmer: quick flip, banked {s.flip_bank_fraction:.0%} at "
                                                         f"+{net_pct:.1%} after fees (+{net:,.2f})")
        if gain >= s.bank_at_share * target and net * s.bank_fraction >= least:
            self.banked[sym] = pos.opened_ts
            self.bump("banked_small")
            self.act(f"BANKED {s.bank_fraction:.0%} of {sym} at +{gain:.1%} (halfway to target)")
            self._sell(sym, s.bank_fraction, f"skimmer: banked {s.bank_fraction:.0%} halfway to the target (+{gain:.1%})")

    # ------------------------------------------------------------- skim clock
    async def on_cycle(self) -> None:
        s = self.settings
        pm = self.desk.bots.get("portfolio")
        for sym, opened in list(self.owned.items()):
            pos = self.portfolio.position(sym)
            if pos.is_flat:
                self.owned.pop(sym, None)
                continue
            if now() - opened < self.max_minutes() * 60 or (pm and sym in pm.exiting) or pos.last_price is None:
                continue
            net = s.net_if_sold(pos, pos.last_price)  # after both fees
            if s.beats_fees(pos, pos.last_price) and net >= self.desk.min_take():  # more profit than its fees
                self.bump("time_exits")
                self.act(f"SKIM time's up on {sym}: banking +{net:,.2f}")
                self._sell(sym, 1.0, f"skimmer: day-trade time limit, banking +{net:,.2f}")
                self.owned[sym] = now()  # don't re-send every cycle while the exit works
        self.metrics["open_skims"] = len(self.owned)
