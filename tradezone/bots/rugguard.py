"""Rug Guard: sells a held coin at the first signs of a rug pull - before it's gone.

Watches every held coin that could be pulled (meme coins, DEX-born tokens and smaller coins - never Bitcoin, Ether,
stocks or the hold stack) for the warning signs:

* 🚨 **Kraken stops trading it normally** (reduce-only, cancel-only, delisting) - sold at once;
* 🚨 **a crash on heavy volume**: down 6%+ in 5 bars (or 4x its normal 5-bar move) on 2.5x the usual volume - sold
  at once;
* ⚠️ **the whales are dumping it**: 80%+ of the big trades on the tape are sells (at least $25,000 of them);
* ⚠️ **the market under it is draining**: the bid/ask gap past 3%, or trading all but stopped while it falls;
* ⚠️ **a steady slide**: down 3%+ in 5 bars.

Any 🚨, or two ⚠️ together, sells the whole position straight away at market - even below break-even (the one
exception to "never sell at a loss", asked for: a rug can take everything; a quick small loss can't). The coin then
can't be bought again for 24 hours. Everything is said in plain words on the alerts.
"""

from __future__ import annotations

import time
from collections import deque

from ..core.models import Alert, Side, Signal, SignalKind
from .base import Bot

CRASH, CRASH_VOL, SLIDE, SPREAD, BAN_S = -0.06, 2.5, -0.03, 0.03, 86_400.0
WHALE_LEAN, WHALE_USD = -0.8, 25_000.0


def risky_coin(desk, symbol: str) -> bool:
    """A coin that can pump - and be rug-pulled: a meme coin, a DEX-born token or a smaller coin. Never Bitcoin,
    Ether, the mid-size list, the hold stack, currency pairs or Kraken's tokenized stocks (AAPLx, ...: shares)."""
    s = desk.settings
    if not s.is_crypto(symbol) or s.is_stack_only(symbol) or s.is_forex(symbol):
        return False
    pair = (getattr(desk.feed, "pairs", {}) or {}).get(symbol) or {}
    if pair.get("aclass", "currency") != "currency":
        return False
    return s.is_meme(symbol) or s.is_dex(symbol) or s.coin_size(symbol) == "small"


class RugGuardBot(Bot):
    name = "rugguard"
    title = "Rug Guard"
    role = "Rug-Pull Protection"
    mission = "🛡️ Get out before the rug - a held coin showing the signs of a rug pull is sold at once"
    description = ("Watches every held meme coin, DEX token and smaller coin for rug-pull signs: Kraken restricting it, "
                   "a crash on heavy volume, whales dumping it, the market under it draining, a steady slide. A strong "
                   "sign - or two smaller ones together - sells the whole position at once, even below break-even, "
                   "and the coin isn't bought again for 24 hours.")

    def __init__(self, desk) -> None:
        super().__init__(desk)
        self.prices: dict[str, deque[float]] = {}
        self.snaps: dict[str, object] = {}
        self.sold: dict[str, tuple[float, str]] = {}  # symbol -> (when, why): no buys for a day
        self.pending: dict[str, float] = {}  # symbol -> when the sale was sent (not sent twice)
        self.events: deque[dict] = deque(maxlen=30)
        self.tries: dict[str, int] = {}  # symbol -> rug sales sent while it's still held

    @property
    def on(self) -> bool:
        return bool(getattr(self.settings, "rug_guard", True))

    def subscriptions(self):
        return {"market.snapshot": self.on_snapshot, "order.filled": self.on_fill}

    def watched(self, symbol: str) -> bool:
        return risky_coin(self.desk, symbol)

    def on_snapshot(self, ev) -> None:
        snap = ev.payload
        if snap.historical or not self.watched(snap.symbol):
            return
        q = self.prices.get(snap.symbol)
        if q is None:
            q = self.prices[snap.symbol] = deque(maxlen=6)
        q.append(snap.price)
        self.snaps[snap.symbol] = snap

    def on_fill(self, ev) -> None:
        f = ev.payload
        if f.side is Side.SELL and f.symbol in self.pending and self.desk.portfolio.position(f.symbol).qty <= 1e-12:
            self.pending.pop(f.symbol, None)
            self.tries.pop(f.symbol, None)

    def banned(self, symbol: str) -> str | None:
        if symbol in getattr(self.desk.feed, "restricted", {}) and self.watched(symbol):
            return f"Kraken has stopped trading {symbol} normally (restricted or being delisted) - not bought"
        hit = self.sold.get(symbol)
        if not hit:
            return None
        if time.time() - hit[0] > BAN_S:
            self.sold.pop(symbol, None)
            return None
        return f"the Rug Guard sold {symbol} {((time.time() - hit[0]) / 3600):.0f}h ago ({hit[1]}) - not bought again for a day"

    def warnings(self, symbol: str) -> tuple[list[str], list[str]]:
        """(strong signs, smaller signs) for one held coin."""
        strong, weak = [], []
        feed = self.desk.feed
        if symbol in getattr(feed, "restricted", {}):
            strong.append("Kraken stopped trading it normally (restricted or being delisted)")
        q, snap = self.prices.get(symbol), self.snaps.get(symbol)
        ind = getattr(snap, "indicators", {}) or {}
        chg = q[-1] / q[0] - 1 if q and len(q) >= 6 and q[0] > 0 else None
        if chg is not None:
            vol = ind.get("volatility") or 0.0
            crash_at = min(CRASH, -4 * vol * 5 ** 0.5)
            if chg <= crash_at and (ind.get("volume_ratio_5") or 0) >= CRASH_VOL:
                strong.append(f"crashing on heavy volume ({chg:+.1%} in 5 bars, {ind.get('volume_ratio_5', 0):.1f}x volume)")
            elif chg <= SLIDE:
                weak.append(f"sliding ({chg:+.1%} in 5 bars)")
        sm = self.desk.bots.get("smartmoney")
        if sm is not None and sm.enabled:
            sm.want_tape(symbol)  # (keeps the held coin's tape read)
            f = sm.tape.get(symbol)
            if f and time.time() - f["at"] < 600 and f["lean"] <= WHALE_LEAN and f["sell_usd"] >= WHALE_USD:
                weak.append(f"whales dumping it (${f['sell_usd']:,.0f} sold in big trades vs ${f['buy_usd']:,.0f} bought)")
        quote = (getattr(self.desk.broker, "quotes", {}) or {}).get(symbol) or getattr(feed, "quotes", {}).get(symbol) or {}
        bid, ask = quote.get("bid") or 0, quote.get("ask") or 0
        if ask > bid > 0 and (ask - bid) / ask > SPREAD:
            weak.append(f"the market under it is draining (bid/ask gap {(ask - bid) / ask:.1%})")
        elif (v5 := ind.get("volume_ratio_5")) is not None and v5 < 0.1 and chg is not None and chg < 0:
            weak.append("trading has all but stopped while it falls")
        return strong, weak

    async def on_cycle(self) -> None:
        if not self.on or self.desk.halted:
            return
        t = time.time()
        for pos in self.desk.portfolio.open_positions:
            sym = pos.symbol
            if pos.qty <= 0 or not self.watched(sym) or t - self.pending.get(sym, 0.0) < 120:
                continue
            strong, weak = self.warnings(sym)
            if not strong and len(weak) < 2:
                continue
            why = "; ".join(strong or weak)
            tries = self.tries.get(sym, 0)
            if tries >= 3:  # (tried three times and it's still held - e.g. under the exchange's minimum: said once)
                if tries == 3:
                    self.tries[sym] = 4
                    self.publish("alert", Alert("warning", f"🛡️ Rug Guard couldn't sell {sym} after 3 tries ({why}) - "
                                                           "check it on the exchange", self.name))
                continue
            self.tries[sym] = tries + 1
            self.pending[sym] = t
            self.sold[sym] = (t, why)
            self.bump("rug_sales")
            self.events.append({"symbol": sym, "ts": t, "why": why})
            self.publish("signal.proposed", Signal(sym, Side.SELL, 1.0, f"rug guard: {why}", self.name, SignalKind.EXIT,
                                                   fraction=1.0, emergency=True))
            if tries == 0:
                self.publish("alert", Alert("warning", f"🛡️ Rug Guard sold all of {sym}: {why}. Not bought again for "
                                                       "24 hours.", self.name))
            self.act(f"sold all of {sym} before a possible rug: {why}", "warning")
