"""Pump Sniper: buys a pump coin the moment it starts to run, and is out quickly.

Watches meme coins, DEX-born tokens, new listings and smaller coins. A pump starting:

* up 3%+ over the last 3 bars (``pump_min_move``) on 3x its usual volume (``pump_volume_ratio``, last 5 bars),
* at a new high (through the 20-bar high), with RSI under 85 (not already blown off),
* the whales not selling it (Smart Money's read of the tape), and not a coin the Rug Guard sold or Kraken restricts.

It buys at half a normal trade's size (``pump_size_mult``), aims for +4% after every fee (``pump_target_pct``), and
sells a pump that stalls after 45 minutes if it's in profit after fees. A pump that turns is left to the same
protection as every trade (the Rug Guard, the -7% floor). One snipe per coin per hour.
"""

from __future__ import annotations

from collections import deque

from ..core.models import Side, Signal, SignalKind, now
from .base import Bot


class PumpSniperBot(Bot):
    name = "pumpsniper"
    title = "Pump Sniper"
    role = "Pump Coin Entries"
    mission = "🎯 Catch the pump as it starts - in early, out fast with the profit"
    description = ("Watches meme coins, DEX tokens, new listings and smaller coins for a pump starting: 3%+ in three "
                   "bars on 3x the volume, at a new high, whales not selling. Buys at half size, sells at +4% after "
                   "fees or after 45 minutes if in profit; a pump that turns is covered by the Rug Guard and the stop.")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.prices: dict[str, deque[float]] = {}
        self.wait: dict[str, int] = {}
        self.opened: dict[str, float] = {}  # symbol -> when its snipe was bought (market time)
        self.clock = 0.0

    @property
    def on(self) -> bool:
        return bool(getattr(self.settings, "pump_sniper", False))

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "order.filled": self.on_fill}

    def pump_coin(self, symbol: str) -> bool:
        s = self.settings
        if not s.is_crypto(symbol) or s.is_stack_only(symbol) or s.is_forex(symbol):
            return False
        pair = (getattr(self.desk.feed, "pairs", {}) or {}).get(symbol) or {}
        if pair.get("aclass", "currency") != "currency":
            return False  # (tokenized stocks don't pump like coins)
        return s.is_meme(symbol) or s.is_dex(symbol) or s.coin_size(symbol) == "small"

    def on_fill(self, ev) -> None:
        f = ev.payload
        if f.side is Side.SELL and f.symbol in self.opened and self.desk.portfolio.position(f.symbol).qty <= 1e-12:
            self.opened.pop(f.symbol, None)

    def on_snapshot(self, ev) -> None:
        snap = ev.payload
        sym = snap.symbol
        if not self.pump_coin(sym):
            return
        q = self.prices.get(sym)
        if q is None:
            q = self.prices[sym] = deque(maxlen=4)
        q.append(snap.price)
        self.clock = max(self.clock, snap.ts or 0.0)  # (market time: replays run faster than the clock on the wall)
        if self.wait.get(sym, 0) > 0:
            self.wait[sym] -= 1
            return
        if not self.on or snap.historical or len(q) < 4 or q[0] <= 0 or self.desk.halted or self.desk.paused:
            return
        s, ind = self.settings, snap.indicators
        move = snap.price / q[0] - 1
        vr5 = ind.get("volume_ratio_5")
        if move < s.pump_min_move or vr5 is None or vr5 < s.pump_volume_ratio:
            return
        if snap.price < ind.get("donchian_high", float("inf")) or ind.get("rsi", 50) > s.pump_max_rsi:
            return
        if not self.desk.portfolio.position(sym).is_flat:
            return
        rg = self.desk.bots.get("rugguard")
        if rg is not None and rg.enabled and rg.banned(sym):
            return
        sm = self.desk.bots.get("smartmoney")
        lean = sm.tape_lean(sym) if sm is not None and sm.enabled else None
        if lean is not None and lean <= -0.6:
            self.bump("skipped_whales_selling")
            return
        self.wait[sym] = s.pump_cooldown_bars
        self.bump("snipes")
        self.publish("signal.proposed", Signal(
            sym, Side.BUY, round(min(1.0, 0.7 + 0.03 * min(vr5, 10.0)), 3),
            f"pump snipe: {sym} +{move:.1%} in 3 bars on {vr5:.1f}x volume, at a new high", self.name,
            votes={"volume": "yes", "breakout": "yes", "momentum": "yes"}))
        self.opened[sym] = self.clock or now()
        self.act(f"PUMP SNIPE {sym} @ {snap.price:,.6g}: +{move:.1%} on {vr5:.1f}x volume")

    async def on_cycle(self) -> None:
        if not self.opened:
            return
        s, t = self.settings, self.clock or now()
        for sym, at in list(self.opened.items()):
            pos = self.desk.portfolio.position(sym)
            if pos.qty <= 0:
                if t - at > 600:  # (the buy never filled)
                    self.opened.pop(sym, None)
                continue
            px = self.desk.prices.get(sym) or pos.last_price
            if t - at >= s.pump_max_minutes * 60 and px and s.beats_fees(pos, px):
                self.opened.pop(sym, None)
                self.bump("stalled_sold")
                self.publish("signal.proposed", Signal(sym, Side.SELL, 1.0, f"pump snipe stalled after "
                                                       f"{s.pump_max_minutes:.0f} min - banked in profit", self.name,
                                                       SignalKind.EXIT, calm=True))
